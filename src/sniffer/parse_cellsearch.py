"""Turn LTE-Cell-Scanner CellSearch stdout into schema-conformant JSONL.

CellSearch (JiaoXianjun fork) prints two kinds of identity-bearing output.

1. Realtime per-cell detection lines emitted as the scan runs:

       Detected a FDD cell! At freqeuncy 1842.5MHz, try 0
         cell ID: 271
          PSS ID: 1
         RX power level: -84.2 dB
         residual frequency offset: 234.1 Hz
                          k_factor: 0.99999987

2. End-of-scan summary table:

       Detected the following cells:
       DPX:TDD/FDD; A: #antenna ports ...
       DPX CID A      fc   freq-offset RXPWR C nRB P  PR CrystalCorrectionFactor
       FDD 271  2  1842.5M   234.1Hz   -84.2 N  50 N 1/6 0.99999987

The realtime block fires *as soon as* a cell is identified; the summary table
contains richer fields (antenna ports, RB count, CP type, PHICH config). We
emit a provisional `cell_sighting` JSONL record from the realtime block and a
second (more complete) record from the summary row when it arrives.
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

_DETECT_LINE = re.compile(
    # The fork's print statement spells it "freqeuncy" but tolerate every
    # plausible variant ("frequency", "freqency", future fixes).
    r"Detected a (FDD|TDD) cell!\s*At freq\w*ncy\s+([\d.]+)\s*MHz",
    re.IGNORECASE,
)
_CELL_ID = re.compile(r"cell\s*ID\s*[:=]\s*(\d+)", re.IGNORECASE)
_PSS_ID = re.compile(r"PSS\s*ID\s*[:=]\s*(\d+)", re.IGNORECASE)
_RX_PWR = re.compile(r"RX\s*power\s*level\s*[:=]\s*(-?[\d.]+)\s*dB", re.IGNORECASE)
_FREQ_OFF = re.compile(r"residual\s*frequency\s*offset\s*[:=]\s*(-?[\d.]+)\s*Hz", re.IGNORECASE)
_KFACTOR = re.compile(r"k_factor\s*[:=]\s*([-\d.eE+]+)", re.IGNORECASE)

# Summary table header line; once seen, every subsequent non-blank line that
# starts with FDD/TDD is a cell row until another blank line or block break.
_SUMMARY_HEADER = re.compile(r"^DPX\s+CID\s+A\s+fc", re.IGNORECASE)
_SUMMARY_ROW = re.compile(
    r"^(FDD|TDD)\s+(\d+)\s+(\d+)\s+([\d.]+)M\s+(-?[\d.]+)\s*Hz\s+(-?[\d.]+)\s+(\w)\s+(\d+)\s+(\w)\s+(\S+)\s+([\d.]+)",
    re.IGNORECASE,
)


def _make_record(
    args, clock_ns: Callable[[], int],
    *, mode: str, pci: int, center_hz: Optional[float],
    rxpwr_db: Optional[float] = None,
    freq_offset_hz: Optional[float] = None,
    n_ports: Optional[int] = None,
    cp: Optional[str] = None,
    n_rb_dl: Optional[int] = None,
    n_id_2: Optional[int] = None,
    k_factor: Optional[float] = None,
    crystal_correction: Optional[float] = None,
    source: str = "realtime",
) -> CellSighting:
    radio = RadioConfig(
        backend=args.backend,
        device=args.device,
        center_hz=center_hz,
        sample_rate_sps=1.92e6,  # LTE-Cell-Scanner caps at 1.92 Msps regardless
        rx_gain_db=args.rx_gain_db,
    )
    cell = CellInfo(
        pci=pci,
        n_id_2=n_id_2,
        mode=mode.lower(),
        cp=cp or "normal",
        n_ports=n_ports,
        rsrp_dbm=rxpwr_db,  # PSS RX power; not RSRP proper, but the only power metric this fork emits
    )
    mib = {}
    if n_rb_dl is not None:
        mib["n_rb_dl"] = n_rb_dl
    if freq_offset_hz is not None:
        mib["residual_freq_offset_hz"] = freq_offset_hz
    if k_factor is not None:
        mib["k_factor"] = k_factor
    if crystal_correction is not None:
        mib["crystal_correction"] = crystal_correction
    cell.mib = mib
    return CellSighting(
        mission_id=args.mission_id,
        capture_id=args.device,
        ts_mono_ns=clock_ns(),
        ts_utc=utc_iso(),
        radio=radio,
        cell=cell,
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
    """Read CellSearch output line by line; emit JSONL on every detection.

    `human_stream`, if provided, receives a one-line human-readable summary
    for every detection (useful for an attached terminal alongside the JSON).
    """
    buf: dict = {}
    in_realtime_block = False
    in_summary = False
    n = 0

    def flush_realtime():
        nonlocal n, buf
        if "pci" in buf and "mode" in buf:
            rec = _make_record(
                args, clock_ns,
                mode=buf["mode"],
                pci=buf["pci"],
                center_hz=buf.get("center_hz"),
                rxpwr_db=buf.get("rxpwr_db"),
                freq_offset_hz=buf.get("freq_offset_hz"),
                n_id_2=buf.get("pss_id"),
                k_factor=buf.get("k_factor"),
                source="realtime",
            )
            output_stream.write(rec.to_jsonl() + "\n")
            output_stream.flush()
            if human_stream is not None:
                human_stream.write(
                    f"[realtime] {buf['mode']} cell PCI={buf['pci']} "
                    f"@ {buf.get('center_hz', 0)/1e6:.3f} MHz "
                    f"RXPWR={buf.get('rxpwr_db', 'NA')} dB "
                    f"PSS_ID={buf.get('pss_id', 'NA')}\n"
                )
                human_stream.flush()
            n += 1
        buf = {}

    for raw in input_stream:
        line = raw.rstrip("\n")

        # Realtime block detection.
        m = _DETECT_LINE.search(line)
        if m:
            # Flush any prior block before starting a new one.
            if in_realtime_block:
                flush_realtime()
            in_realtime_block = True
            buf["mode"] = m.group(1).upper()
            buf["center_hz"] = float(m.group(2)) * 1e6
            continue

        if in_realtime_block:
            m = _CELL_ID.search(line)
            if m:
                buf["pci"] = int(m.group(1))
                continue
            m = _PSS_ID.search(line)
            if m:
                buf["pss_id"] = int(m.group(1))
                continue
            m = _RX_PWR.search(line)
            if m:
                buf["rxpwr_db"] = float(m.group(1))
                continue
            m = _FREQ_OFF.search(line)
            if m:
                buf["freq_offset_hz"] = float(m.group(1))
                continue
            m = _KFACTOR.search(line)
            if m:
                buf["k_factor"] = float(m.group(1))
                # k_factor is the last field of the block.
                flush_realtime()
                in_realtime_block = False
                continue

        # Summary table.
        if _SUMMARY_HEADER.search(line):
            in_summary = True
            continue
        if in_summary:
            m = _SUMMARY_ROW.match(line.strip())
            if m:
                mode = m.group(1).upper()
                pci = int(m.group(2))
                n_ports = int(m.group(3))
                center_hz = float(m.group(4)) * 1e6
                freq_offset_hz = float(m.group(5))
                rxpwr_db = float(m.group(6))
                cp_letter = m.group(7).upper()
                cp = "normal" if cp_letter == "N" else "extended"
                n_rb_dl = int(m.group(8))
                crystal_correction = float(m.group(11))
                rec = _make_record(
                    args, clock_ns,
                    mode=mode, pci=pci, center_hz=center_hz,
                    rxpwr_db=rxpwr_db, freq_offset_hz=freq_offset_hz,
                    n_ports=n_ports, cp=cp, n_rb_dl=n_rb_dl,
                    crystal_correction=crystal_correction,
                    source="summary",
                )
                output_stream.write(rec.to_jsonl() + "\n")
                output_stream.flush()
                if human_stream is not None:
                    bw_mhz = {6: 1.4, 15: 3, 25: 5, 50: 10, 75: 15, 100: 20}.get(n_rb_dl, n_rb_dl)
                    human_stream.write(
                        f"[summary]  {mode} PCI={pci} ports={n_ports} "
                        f"@ {center_hz/1e6:.3f} MHz BW={bw_mhz} MHz "
                        f"RXPWR={rxpwr_db} dB cp={cp}\n"
                    )
                    human_stream.flush()
                n += 1
                continue
            # blank line or non-row line → exit summary
            if not line.strip() or line.startswith("Examining"):
                in_summary = False

    # End-of-stream: flush any pending block.
    if in_realtime_block:
        flush_realtime()
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mission-id", required=True)
    p.add_argument("--backend", default="lte-cell-scanner")
    p.add_argument("--device", default="hackrf-0")
    p.add_argument("--rx-gain-db", type=float, default=40.0)
    p.add_argument("--human", action="store_true",
                   help="Also print a human-readable summary to stderr.")
    args = p.parse_args()
    human = sys.stderr if args.human else None
    parse_stream(sys.stdin, args, sys.stdout, human_stream=human)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
