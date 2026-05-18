"""Turn LTESniffer DL-mode stdout into schema-conformant `ue_sighting` JSONL.

LTESniffer (oai-research-cci/LTESniffer) decodes the PDCCH of a target LTE
cell and prints one line per recovered DCI. We canonicalise its output —
either the upstream CSV/text form or a wrapper-script-flattened form — to
this `key=value` syntax (one DCI per line):

    DECODED ts=1700000000123 pci=271 c_rnti=0x4ad2 format=1A direction=DL \
            mcs=15 prb=8 tbs=752 dl_rsrp_dbm=-85.3 frame=42 subframe=3

    DECODED ts=1700000000125 pci=271 c_rnti=0x4ad2 format=0  direction=UL \
            mcs=12 prb=4 tbs=224 ul_rssi_dbm=-92.1 frame=42 subframe=4

Notes:
- `c_rnti` may be decimal or `0x`-prefixed hex.
- `ts` is optional; absent → use the injected `clock_ns` callback.
- Anything else is preserved verbatim under `ue.raw` so downstream code
  can do its own dissection without re-parsing stdout.

This module is deliberately format-tolerant: real LTESniffer text output
drifts across versions, so `sniffer.normalize_ltesniffer.normalize_stream`
canonicalises it before we parse here. The `sniffer live --earfcn ...`
CLI wires that pipe up in-process.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, Optional

from sniffer.schema import (
    RadioConfig,
    UeEvent,
    UeSighting,
    mono_ns,
    utc_iso,
)

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
    elif "rnti" in fields:  # tolerate either spelling
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
    # Stash anything we did not promote — frame/subframe/cqi/etc.
    raw = {k: v for k, v in fields.items() if k not in (
        "pci", "c_rnti", "rnti", "format", "direction", "mcs", "prb",
        "tbs", "harq", "dl_rsrp_dbm", "ul_rssi_dbm",
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
    # If the wrapper emitted a `ts` (ms epoch), prefer it; we keep mono_ns
    # for the join since GPS uses the same clock.
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
    *,
    human_stream=None,
) -> int:
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
        if human_stream is not None:
            direction = rec.ue.direction.upper()
            power = (rec.ue.ul_rssi_dbm if rec.ue.direction == "ul"
                     else rec.ue.dl_rsrp_dbm)
            power_str = f"{power:.1f} dBm" if power is not None else "—"
            human_stream.write(
                f"[ltesniffer] {direction} PCI={rec.ue.pci} "
                f"C-RNTI={rec.ue.c_rnti:#06x} "
                f"DCI={rec.ue.dci_format} MCS={rec.ue.mcs} "
                f"PRB={rec.ue.n_prb} pwr={power_str}\n"
            )
            human_stream.flush()
        n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mission-id", required=True)
    p.add_argument("--backend", default="ltesniffer")
    p.add_argument("--device", default="usrp-b210-0")
    p.add_argument("--center-hz", type=float, default=None)
    p.add_argument("--sample-rate-sps", type=float, default=None)
    p.add_argument("--rx-gain-db", type=float, default=None)
    p.add_argument("--human", action="store_true",
                   help="Also print a human-readable summary to stderr.")
    args = p.parse_args()
    human = sys.stderr if args.human else None
    parse_stream(sys.stdin, args, sys.stdout, human_stream=human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
