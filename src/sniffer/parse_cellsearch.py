"""Turn LTE-Cell-Scanner CellSearch stdout into schema-conformant JSONL.

CellSearch prints human-readable lines like:

    Found LTE cell:
      Carrier frequency: 1842500000
      n_id_1: 90 n_id_2: 1
      PCI: 271
      RSRP: -84.2 dBm
      RSRQ: -10.1 dB
      SNR: 12.3 dB
      Frame offset samples: 30412
      MIB: PHICH duration normal, ...

Lines vary across forks. The parser is forgiving: it collects key/value
pairs inside a `Found LTE cell:` block and emits a record at the next
blank line or block boundary.
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Callable, Optional

from sniffer.schema import (
    CellInfo,
    CellSighting,
    RadioConfig,
    mono_ns,
    utc_iso,
)

# Tolerant regex: "key: value" with optional units after the value.
_KV = re.compile(r"^\s*([A-Za-z][\w \-/]*?)\s*[:=]\s*(.+?)\s*$")
_BLOCK_START = re.compile(r"found.*cell", re.IGNORECASE)


def _as_float(value: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?", value)
    return float(m.group(0)) if m else None


def _as_int(value: str) -> Optional[int]:
    f = _as_float(value)
    return int(f) if f is not None else None


def _flush(buf: dict, args, clock_ns: Callable[[], int]) -> Optional[CellSighting]:
    if "pci" not in buf:
        return None
    radio = RadioConfig(
        backend=args.backend,
        device=args.device,
        center_hz=buf.get("carrier_frequency"),
        bandwidth_hz=buf.get("bandwidth_hz"),
        sample_rate_sps=buf.get("sample_rate_sps"),
        rx_gain_db=buf.get("rx_gain_db"),
        tcxo_ppm=buf.get("tcxo_ppm"),
    )
    cell = CellInfo(
        pci=int(buf["pci"]),
        n_id_1=buf.get("n_id_1"),
        n_id_2=buf.get("n_id_2"),
        n_ports=buf.get("n_ports"),
        rsrp_dbm=buf.get("rsrp"),
        rsrq_db=buf.get("rsrq"),
        snr_db=buf.get("snr"),
        frame_offset_samples=buf.get("frame_offset_samples"),
        cp=buf.get("cp", "normal"),
        mib=buf.get("mib", {}),
    )
    return CellSighting(
        mission_id=args.mission_id,
        capture_id=args.device,
        ts_mono_ns=clock_ns(),
        ts_utc=utc_iso(),
        radio=radio,
        cell=cell,
    )


_FIELD_MAP = {
    "carrier frequency": "carrier_frequency",
    "n_id_1": "n_id_1",
    "n_id_2": "n_id_2",
    "pci": "pci",
    "rsrp": "rsrp",
    "rsrq": "rsrq",
    "snr": "snr",
    "frame offset samples": "frame_offset_samples",
    "n_ports": "n_ports",
    "tx ports": "n_ports",
    "cp": "cp",
}

_INT_FIELDS = {"n_id_1", "n_id_2", "pci", "n_ports", "frame_offset_samples"}


def parse_stream(input_stream, args, output_stream,
                 clock_ns: Callable[[], int] = mono_ns) -> int:
    """Read CellSearch output line by line, write JSONL records.

    `clock_ns` is injectable so deterministic tests can drive a simulated
    timeline; defaults to `time.monotonic_ns`.

    Returns the number of records emitted.
    """
    buf: dict = {}
    in_block = False
    n = 0
    for raw in input_stream:
        line = raw.rstrip("\n")
        if _BLOCK_START.search(line):
            if in_block:
                rec = _flush(buf, args, clock_ns)
                if rec is not None:
                    output_stream.write(rec.to_jsonl() + "\n")
                    output_stream.flush()
                    n += 1
                buf = {}
            in_block = True
            continue
        if not in_block:
            continue
        if not line.strip():
            rec = _flush(buf, args, clock_ns)
            if rec is not None:
                output_stream.write(rec.to_jsonl() + "\n")
                output_stream.flush()
                n += 1
            buf = {}
            in_block = False
            continue
        m = _KV.match(line)
        if not m:
            continue
        key = m.group(1).strip().lower()
        if key not in _FIELD_MAP:
            continue
        slot = _FIELD_MAP[key]
        value = m.group(2)
        if slot in _INT_FIELDS:
            parsed = _as_int(value)
        elif slot == "cp":
            parsed = "extended" if "extend" in value.lower() else "normal"
        else:
            parsed = _as_float(value)
        if parsed is not None:
            buf[slot] = parsed
    rec = _flush(buf, args, clock_ns)
    if rec is not None:
        output_stream.write(rec.to_jsonl() + "\n")
        output_stream.flush()
        n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mission-id", required=True)
    p.add_argument("--backend", default="lte-cell-scanner")
    p.add_argument("--device", default="hackrf-0")
    args = p.parse_args()
    parse_stream(sys.stdin, args, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
