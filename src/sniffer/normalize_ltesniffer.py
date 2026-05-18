"""Translate LTESniffer text output into the canonical `DECODED key=value` form.

LTESniffer's stdout format drifts across versions. Two common shapes:

1. Human-readable per-DCI lines, e.g.::

       [SFN=512 SF=3] PCI=271 RNTI=0x4ad2 DCI=1A MCS=15 RBs=4 dir=DL RSRP=-85.2

2. CSV-style rows with a header, e.g.::

       sfn,sf,pci,rnti,format,mcs,nof_prb,direction,rsrp_dbm,ul_rssi_dbm
       512,3,271,0x4ad2,1A,15,4,DL,-85.2,

This module sniffs each input line, extracts what it can with permissive
regexes, and re-emits a canonical line consumed by `parse_ltesniffer.py`:

       DECODED pci=271 c_rnti=0x4ad2 format=1A direction=DL mcs=15 prb=4 \
               dl_rsrp_dbm=-85.2

Run as a pipe between LTESniffer and the JSONL parser:

       LTESniffer ... | python3 -m sniffer.normalize_ltesniffer \
           | python3 -m sniffer.parse_ltesniffer --mission-id $MID > out.jsonl
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Iterable, Iterator, Optional


# Field synonyms we recognise on either side of an `=` or in CSV columns.
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
}

_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([^\s,]+)")


def _normalise_value(key: str, value: str) -> Optional[str]:
    # LTESniffer headers like "[SFN=512 SF=3]" leave stray brackets/parens
    # on the trailing value — strip the lot.
    value = value.strip().strip(",;[](){}")
    if not value:
        return None
    if key in ("pci", "mcs", "prb", "tbs", "harq", "frame", "subframe"):
        # Strip stray punctuation, keep integers (may be hex).
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
        if v in ("UL", "UPLINK"): return "UL"
        return v
    if key in ("dl_rsrp_dbm", "ul_rssi_dbm"):
        return value
    return value


def normalize_line(line: str) -> Optional[str]:
    """Translate one LTESniffer text line. Returns None if nothing usable."""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    # Drop progress / banner output: anything that doesn't have an RNTI is
    # useless to the downstream parser.
    if "rnti" not in raw.lower() and "c-rnti" not in raw.lower():
        return None
    fields: dict[str, str] = {}
    for k, v in _KV_RE.findall(raw):
        canon = _FIELD_MAP.get(k.lower())
        if canon is None:
            continue
        n = _normalise_value(canon, v)
        if n is not None:
            fields[canon] = n
    if "pci" not in fields or "c_rnti" not in fields:
        return None
    # Best-effort direction inference for binaries that omit it.
    if "direction" not in fields:
        if "ul_rssi_dbm" in fields:
            fields["direction"] = "UL"
        else:
            fields["direction"] = "DL"
    parts = ["DECODED"]
    # Stable field order so test goldens stay deterministic.
    for k in ("frame", "subframe", "pci", "c_rnti", "format", "direction",
             "mcs", "prb", "tbs", "harq", "dl_rsrp_dbm", "ul_rssi_dbm"):
        if k in fields:
            parts.append(f"{k}={fields[k]}")
    return " ".join(parts)


def normalize_stream(lines: Iterable[str]) -> Iterator[str]:
    for line in lines:
        out = normalize_line(line)
        if out is not None:
            yield out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--echo-input-to-stderr", action="store_true",
                   help="copy raw LTESniffer lines to stderr (debug)")
    args = p.parse_args()
    for line in sys.stdin:
        if args.echo_input_to_stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
        out = normalize_line(line)
        if out is None:
            continue
        print(out, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
