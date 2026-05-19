"""FalconEye DCI CSV → `ue_sighting` JSONL.

`FalconEye -f <hz> -D /tmp/dci.csv` writes one tab-separated row per
decoded DCI. Unlike LTESniffer (which only writes PCAP), FALCON
emits text designed for tailing — exactly the input shape our pipeline
already consumes from `parse_ltesniffer`. This module is the tail +
parser; `live.py:run_falcon_loop` is the subprocess + sink wiring.

CSV format (verified against
`src/eye/phy/SubframeInfoConsumer.cc` in falkenber9/falcon@master):

  - No header row.
  - Tab-separated.
  - 20 columns:
      0  timestamp     (float, e.g. "1700000123.456789")
      1  sfn           (zero-padded 4-digit int)
      2  subframe      (int 0..9)
      3  rnti          (decimal int)
      4  direction     (int: 1 = DL, 0 = UL)
      5  mcs_idx       (int)
      6  nof_prb       (int)
      7  tbs_sum       (int bytes; for DL = TB size, for UL = grant size)
      8  tbs_0         (int or -1 when N/A)
      9  tbs_1         (int or -1 when N/A)
     10  format        (int DCI format code)
     11  ndi           (int)
     12  ndi_1         (int or -1)
     13  harq_process  (int)
     14  ncce          (int)
     15  L             (int)
     16  cfi           (int)
     17  histval       (int — RNTI histogram bucket count)
     18  nof_bits      (int)
     19  hex           (hex string, the raw DCI payload)

FalconEye locks to one cell at a time — there's no PCI column. The
caller passes the PCI it asked the decoder to target; we stamp every
record with it.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Iterator, Optional

from sniffer.schema import (
    RadioConfig,
    UeEvent,
    UeSighting,
    mono_ns,
    utc_iso,
)

# A handful of DCI format codes mapped to human strings. FALCON emits
# an internal integer; we expose a stable text in the UeEvent so the
# dashboard chips stay readable. Unknown codes pass through as str(int).
_DCI_FORMAT_NAMES = {
    0:  "0",     # UL grant
    1:  "1",     # DL, single-codeword
    2:  "1A",    # DL compact, fallback
    3:  "1B",
    4:  "1C",
    5:  "1D",
    6:  "2",     # DL, 2 codewords (TM3/4)
    7:  "2A",
    8:  "2B",
    9:  "2C",
    10: "2D",
    11: "3",     # TPC commands
    12: "3A",
    13: "4",     # UL MIMO (rare)
}


def _to_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _to_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def parse_csv_row(cols: list[str]) -> Optional[dict]:
    """Map a 20-column FalconEye row to our UeEvent fields.

    Returns None on malformed rows so the caller can skip them.
    """
    if len(cols) < 19:
        # Need at least everything up to nof_bits; hex column 19 is optional.
        return None
    rnti = _to_int(cols[3])
    if rnti is None:
        return None
    direction_int = _to_int(cols[4])
    if direction_int is None:
        return None
    direction = "dl" if direction_int == 1 else "ul"
    mcs = _to_int(cols[5])
    n_prb = _to_int(cols[6])
    tbs_sum = _to_int(cols[7])
    # FALCON writes -1 for N/A fields; normalise to None in the record.
    if tbs_sum is not None and tbs_sum < 0:
        tbs_sum = None
    fmt_int = _to_int(cols[10])
    dci_format = (_DCI_FORMAT_NAMES.get(fmt_int)
                  if fmt_int is not None else "")
    if dci_format is None and fmt_int is not None:
        dci_format = str(fmt_int)
    harq_id = _to_int(cols[13])
    out: dict = {
        "c_rnti": rnti,
        "direction": direction,
        "dci_format": dci_format or "",
        "mcs": mcs,
        "n_prb": n_prb,
        "tbs_bytes": tbs_sum,
        "harq_id": harq_id,
        "raw": {
            "sfn":      cols[1],
            "subframe": cols[2],
            "ncce":     cols[14],
            "L":        cols[15],
            "cfi":      cols[16],
            "histval":  cols[17],
        },
    }
    # FALCON's `histval` is the RNTI histogram bucket count — when it's
    # very small the decode is more likely a false positive (per the
    # paper). Stash for downstream filtering; not exposed in the schema.
    return out


def _make_record(args, clock_ns: Callable[[], int],
                 pci: int, fields: dict) -> Optional[UeSighting]:
    c_rnti = fields.get("c_rnti")
    if c_rnti is None:
        return None
    radio = RadioConfig(
        backend=args.backend,
        device=args.device,
        center_hz=getattr(args, "center_hz", None),
        sample_rate_sps=getattr(args, "sample_rate_sps", None),
        rx_gain_db=getattr(args, "rx_gain_db", None),
    )
    ue = UeEvent(
        pci=int(pci),
        c_rnti=int(c_rnti),
        direction=fields.get("direction", "dl"),
        dci_format=fields.get("dci_format", ""),
        mcs=fields.get("mcs"),
        n_prb=fields.get("n_prb"),
        harq_id=fields.get("harq_id"),
        tbs_bytes=fields.get("tbs_bytes"),
        raw=fields.get("raw", {}),
    )
    return UeSighting(
        mission_id=args.mission_id,
        capture_id=args.device,
        ts_mono_ns=clock_ns(),
        ts_utc=utc_iso(),
        radio=radio,
        ue=ue,
        notes="source=falcon",
    )


def parse_stream(
    input_stream: Iterable[str],
    args,
    output_stream,
    *,
    pci: int,
    clock_ns: Callable[[], int] = mono_ns,
) -> int:
    """Consume FalconEye CSV lines; write UeSighting JSONL.

    Mirrors `parse_ltesniffer.parse_stream` contract. `pci` is mandatory
    because FALCON locks to one cell and never writes the PCI itself —
    the caller knows it from the `-f <hz>` it passed.

    Returns the number of records emitted.
    """
    n = 0
    for raw in input_stream:
        line = raw.rstrip("\n").rstrip("\r")
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        fields = parse_csv_row(cols)
        if fields is None:
            continue
        rec = _make_record(args, clock_ns, pci, fields)
        if rec is None:
            continue
        output_stream.write(rec.to_jsonl() + "\n")
        output_stream.flush()
        n += 1
    return n


def tail_csv(path: str, *, stop: threading.Event,
             poll_interval_s: float = 0.1,
             startup_wait_s: float = 30.0) -> Iterator[str]:
    """Tail a CSV file as it grows. Robust to:

      - File not existing yet (FalconEye creates it on first DCI).
      - Slow growth (no DCIs for a while — most idle cells).
      - Truncation / rotation (decoder restart shrinks or recreates
        the file → we reopen and replay from the beginning).
      - Clean shutdown when `stop` is set.

    Yields complete lines (with trailing `\\n` stripped). Partial
    lines are buffered until a newline arrives.
    """
    # Wait for the file to appear. FalconEye on a quiet cell may not
    # create it for a while; bail after `startup_wait_s` to surface a
    # clear failure rather than blocking the dashboard forever.
    waited = 0.0
    while not os.path.exists(path):
        if stop.is_set():
            return
        time.sleep(poll_interval_s)
        waited += poll_interval_s
        if waited > startup_wait_s:
            return

    last_inode: Optional[int] = None
    last_size: int = 0
    fh = None
    partial = ""
    try:
        while not stop.is_set():
            try:
                st = os.stat(path)
            except FileNotFoundError:
                # File was deleted (e.g. decoder restart).
                if fh is not None:
                    fh.close(); fh = None
                last_inode = None
                last_size = 0
                partial = ""
                time.sleep(poll_interval_s)
                continue

            reopen = (
                fh is None
                or st.st_ino != last_inode
                or st.st_size < last_size  # truncated
            )
            if reopen:
                if fh is not None:
                    fh.close()
                fh = open(path, "r", encoding="utf-8", errors="replace")
                last_inode = st.st_ino
                last_size = 0
                partial = ""

            chunk = fh.read()
            if not chunk:
                time.sleep(poll_interval_s)
                continue
            last_size = fh.tell()
            partial += chunk
            while "\n" in partial:
                line, partial = partial.split("\n", 1)
                yield line
    finally:
        if fh is not None:
            fh.close()
