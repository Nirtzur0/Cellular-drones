"""LTESniffer text → `ue_sighting` JSONL.

LTESniffer (oai-research-cci/LTESniffer) decodes the PDCCH of a target
LTE cell and prints one line per recovered DCI. Real builds emit one of
several shapes — `[SFN=… SF=…] PCI=… RNTI=… DCI=… …`, CSV rows,
upstream-version text drift, etc. This module does both halves of the
text-to-record pipeline:

1. **normalize** — `normalize_line` / `normalize_stream` translate any
   recognisable LTESniffer line into a canonical `DECODED key=value`
   form using permissive regexes and a field-name synonym table.

2. **parse** — `parse_stream` reads canonical `DECODED …` lines and
   emits one `UeSighting` JSONL record per DCI.

Canonical form (one DCI per line):

    DECODED ts=1700000000123 pci=271 c_rnti=0x4ad2 format=1A direction=DL \
            mcs=15 prb=8 tbs=752 dl_rsrp_dbm=-85.3 frame=42 subframe=3

Notes:
- `c_rnti` may be decimal or `0x`-prefixed hex.
- Anything not promoted to a typed field is preserved under `ue.raw`.

The live dashboard pipes LTESniffer stdout through `normalize_stream`
in-process and then into `parse_stream`. There is no separate
canonicaliser process.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Iterator, Optional

from sniffer.schema import (
    RadioConfig,
    UeEvent,
    UeSighting,
    mono_ns,
    utc_iso,
)

# --- normalize -----------------------------------------------------------

_FIELD_MAP = {
    "pci": "pci",
    "rnti": "c_rnti",
    "c_rnti": "c_rnti",
    "crnti": "c_rnti",
    "format": "format",
    "dci": "format",
    "mcs": "mcs",
    "rbs": "prb",
    "nof_prb": "prb",
    "n_prb": "prb",
    "prb": "prb",
    "direction": "direction",
    "dir": "direction",
    "tbs": "tbs",
    "tb_size": "tbs",
    "harq": "harq",
    "rsrp": "dl_rsrp_dbm",
    "rsrp_dbm": "dl_rsrp_dbm",
    "dl_rsrp": "dl_rsrp_dbm",
    "ul_rssi": "ul_rssi_dbm",
    "ul_rssi_dbm": "ul_rssi_dbm",
    "rssi": "ul_rssi_dbm",
    "sfn": "frame",
    "frame": "frame",
    "sf": "subframe",
    "subframe": "subframe",
    "ta": "ta_n_steps",
    "ta_n_steps": "ta_n_steps",
    "timing_advance": "ta_n_steps",
}

_KV_NORMALIZE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^\s,]+)")


def _normalise_value(key: str, value: str) -> Optional[str]:
    value = value.strip().strip(",;[](){}")
    if not value:
        return None
    if key in ("pci", "mcs", "prb", "tbs", "harq", "frame", "subframe",
               "ta_n_steps"):
        return value.rstrip(".")
    if key == "c_rnti":
        if value.lower().startswith("0x"):
            return value.lower()
        try:
            return f"{int(value):#06x}"
        except ValueError:
            return value
    if key == "direction":
        v = value.upper()
        if v in ("DL", "DOWNLINK"): return "DL"
        if v in ("UL", "UPLINK"):   return "UL"
        return v
    return value


def normalize_line(line: str) -> Optional[str]:
    """Translate one LTESniffer text line into canonical DECODED form.

    Returns None if the line has nothing usable (banner/log noise, no
    RNTI, etc.).
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "rnti" not in raw.lower() and "c-rnti" not in raw.lower():
        return None
    fields: dict[str, str] = {}
    for k, v in _KV_NORMALIZE_RE.findall(raw):
        canon = _FIELD_MAP.get(k.lower())
        if canon is None:
            continue
        n = _normalise_value(canon, v)
        if n is not None:
            fields[canon] = n
    if "pci" not in fields or "c_rnti" not in fields:
        return None
    if "direction" not in fields:
        fields["direction"] = "UL" if "ul_rssi_dbm" in fields else "DL"
    parts = ["DECODED"]
    for k in ("frame", "subframe", "pci", "c_rnti", "format", "direction",
             "mcs", "prb", "tbs", "harq", "dl_rsrp_dbm", "ul_rssi_dbm",
             "ta_n_steps"):
        if k in fields:
            parts.append(f"{k}={fields[k]}")
    return " ".join(parts)


def normalize_stream(lines: Iterable[str]) -> Iterator[str]:
    """Pipe an LTESniffer text stream through `normalize_line`."""
    for line in lines:
        out = normalize_line(line)
        if out is not None:
            yield out


# --- parse ---------------------------------------------------------------

_DECODED = re.compile(r"^\s*DECODED\b", re.IGNORECASE)
_KV = re.compile(r"(\w+)=([\S]+)")


def _to_int(s: str) -> Optional[int]:
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except ValueError:
        return None


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except ValueError:
        return None


def _parse_decoded(line: str) -> Optional[dict]:
    if not _DECODED.search(line):
        return None
    fields = dict(_KV.findall(line))
    if not fields:
        return None
    out: dict = {}
    if "pci" in fields:
        out["pci"] = _to_int(fields["pci"])
    if "c_rnti" in fields:
        out["c_rnti"] = _to_int(fields["c_rnti"])
    elif "rnti" in fields:
        out["c_rnti"] = _to_int(fields["rnti"])
    if "format" in fields:
        out["dci_format"] = fields["format"]
    if "direction" in fields:
        out["direction"] = fields["direction"].lower()
    if "mcs" in fields:
        out["mcs"] = _to_int(fields["mcs"])
    if "prb" in fields:
        out["n_prb"] = _to_int(fields["prb"])
    if "tbs" in fields:
        out["tbs_bytes"] = _to_int(fields["tbs"])
    if "harq" in fields:
        out["harq_id"] = _to_int(fields["harq"])
    if "dl_rsrp_dbm" in fields:
        out["dl_rsrp_dbm"] = _to_float(fields["dl_rsrp_dbm"])
    if "ul_rssi_dbm" in fields:
        out["ul_rssi_dbm"] = _to_float(fields["ul_rssi_dbm"])
    if "ta_n_steps" in fields:
        out["ta_n_steps"] = _to_int(fields["ta_n_steps"])
    raw = {k: v for k, v in fields.items() if k not in (
        "pci", "c_rnti", "rnti", "format", "direction", "mcs", "prb",
        "tbs", "harq", "dl_rsrp_dbm", "ul_rssi_dbm", "ta_n_steps",
    )}
    if raw:
        out["raw"] = raw
    return out


def _make_record(args, clock_ns: Callable[[], int],
                 fields: dict, source: str) -> Optional[UeSighting]:
    pci = fields.get("pci")
    c_rnti = fields.get("c_rnti")
    if pci is None or c_rnti is None:
        return None
    radio = RadioConfig(
        backend=args.backend,
        device=args.device,
        center_hz=getattr(args, "center_hz", None),
        sample_rate_sps=getattr(args, "sample_rate_sps", None),
        rx_gain_db=getattr(args, "rx_gain_db", None),
    )
    raw = fields.get("raw", {})
    ue = UeEvent(
        pci=int(pci),
        c_rnti=int(c_rnti),
        direction=fields.get("direction", "dl"),
        dci_format=fields.get("dci_format", ""),
        mcs=fields.get("mcs"),
        n_prb=fields.get("n_prb"),
        harq_id=fields.get("harq_id"),
        tbs_bytes=fields.get("tbs_bytes"),
        ul_rssi_dbm=fields.get("ul_rssi_dbm"),
        dl_rsrp_dbm=fields.get("dl_rsrp_dbm"),
        ta_n_steps=fields.get("ta_n_steps"),
        raw=raw,
    )
    return UeSighting(
        mission_id=args.mission_id,
        capture_id=args.device,
        ts_mono_ns=clock_ns(),
        ts_utc=utc_iso(),
        radio=radio,
        ue=ue,
        notes=f"source={source}",
    )


def parse_stream(
    input_stream,
    args,
    output_stream,
    clock_ns: Callable[[], int] = mono_ns,
) -> int:
    """Consume canonical DECODED lines; write UeSighting JSONL.

    Returns the number of records emitted. `args` must have
    `mission_id`, `backend`, `device`, `center_hz`, `sample_rate_sps`,
    `rx_gain_db` attributes (a Namespace or `_ParseArgs` works).
    """
    n = 0
    for raw in input_stream:
        line = raw.rstrip("\n")
        fields = _parse_decoded(line)
        if fields is None:
            continue
        rec = _make_record(args, clock_ns, fields, source="ltesniffer")
        if rec is None:
            continue
        output_stream.write(rec.to_jsonl() + "\n")
        output_stream.flush()
        n += 1
    return n
