"""Live spectrum scanner.

Maintains a rolling (frequency → power_dBFS) snapshot the dashboard renders
as a waterfall. Two ingest modes:

  * source="sweep"  — spawn `python -m sniffer.uhd_sweep` and parse its
                      hackrf_sweep-schema CSV from stdout. One USRP, wide
                      sweep, slow refresh. Used when the radio is otherwise
                      idle (e.g. simulate mode).
  * source="file"   — tail a CSV file in the same schema. Used when FALCON
                      owns the radio and is publishing per-cell FFT rows via
                      its SpectrumTap (`-X <path>`). The dashboard then
                      shows the actual cell FALCON is decoding, not a
                      separate sweep.
"""

from __future__ import annotations

import collections
import csv
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Optional


class SpectrumScanner:
    """Background thread that ingests per-bin power CSVs and reports snapshots."""

    def __init__(
        self,
        on_snapshot: Optional[Callable[[dict], None]] = None,
        *,
        source: str = "sweep",
        file_path: Optional[str] = None,
        freq_start_mhz: int = 700,
        freq_end_mhz: int = 2700,
        bin_width_hz: int = 1_000_000,
        history_seconds: int = 60,
        gain_db: float = 60.0,
    ) -> None:
        if source not in ("sweep", "file"):
            raise ValueError(f"unknown source {source!r}, want sweep|file")
        if source == "file" and not file_path:
            raise ValueError("source='file' requires file_path")

        self._on_snapshot = on_snapshot
        self._source = source
        self._file_path = file_path
        self._freq_start_mhz = freq_start_mhz
        self._freq_end_mhz = freq_end_mhz
        self._bin_width_hz = bin_width_hz
        self._gain_db = gain_db
        self._history: collections.deque[dict] = collections.deque(
            maxlen=history_seconds
        )
        self._latest: dict[int, float] = {}
        self._lock = threading.Lock()
        self._running = False
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None
        self._last_row_t: Optional[float] = None

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if self._source == "sweep" and shutil.which(sys.executable) is None:
            self._error = "no python interpreter on PATH"
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def source(self) -> str:
        return self._source

    @property
    def last_row_age_seconds(self) -> Optional[float]:
        if self._last_row_t is None:
            return None
        return time.time() - self._last_row_t

    # --- snapshot for SSE / /spectrum endpoint ------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "source": self._source,
                "freq_start_mhz": self._freq_start_mhz,
                "freq_end_mhz": self._freq_end_mhz,
                "bin_width_hz": self._bin_width_hz,
                "latest": [
                    {"mhz": m, "dbfs": round(d, 1)}
                    for m, d in sorted(self._latest.items())
                ],
                "n_history": len(self._history),
                "last_row_age_s": (
                    None if self._last_row_t is None
                    else round(time.time() - self._last_row_t, 2)
                ),
                "error": self._error,
            }

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    # --- main loop ----------------------------------------------------

    def _run(self) -> None:
        if self._source == "file":
            self._run_file()
        else:
            self._run_sweep()

    def _run_sweep(self) -> None:
        """Spawn `python -m sniffer.uhd_sweep` and parse its stdout."""
        cmd = [
            sys.executable, "-m", "sniffer.uhd_sweep",
            "-f", f"{self._freq_start_mhz}:{self._freq_end_mhz}",
            "--gain", str(self._gain_db),
        ]
        while self._running:
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception as exc:  # noqa: BLE001
                self._error = f"failed to spawn uhd_sweep: {exc}"
                return
            reader = csv.reader(
                io.TextIOWrapper(
                    self._proc.stdout.buffer, encoding="utf-8", errors="replace"
                ),
                skipinitialspace=True,
            )
            self._consume_rows(reader)
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if not self._running:
                return
            time.sleep(2.0)

    def _run_file(self) -> None:
        """Tail the CSV file written by FALCON's SpectrumTap. Handles the
        case where the file doesn't exist yet, gets rotated, or shrinks."""
        assert self._file_path is not None
        while self._running:
            try:
                f = open(self._file_path, "r")
            except FileNotFoundError:
                time.sleep(0.5)
                continue
            with f:
                # Start at end — we only care about new rows.
                f.seek(0, os.SEEK_END)
                self._consume_rows(self._tail_rows(f))
            if not self._running:
                return
            time.sleep(0.5)

    def _tail_rows(self, f) -> Iterator[list[str]]:
        """Yield CSV rows as they appear in f. Returns on truncation."""
        last_size = -1
        while self._running:
            line = f.readline()
            if not line:
                # No new data — check for truncation, then sleep briefly.
                try:
                    cur_pos = f.tell()
                    end_pos = os.path.getsize(self._file_path)  # type: ignore[arg-type]
                    if end_pos < cur_pos or end_pos < last_size:
                        return  # truncated; caller reopens
                    last_size = end_pos
                except OSError:
                    return
                time.sleep(0.1)
                continue
            for row in csv.reader([line], skipinitialspace=True):
                yield row

    def _consume_rows(self, rows: Iterable[list[str]]) -> None:
        """Per-row peak-hold per MHz, publish once per second, fade 6 dB per
        publish so a one-time spike doesn't pin the waterfall."""
        cur: dict[int, float] = {}
        last_publish_t = time.time()
        publish_interval_s = 1.0
        for row in rows:
            if not self._running:
                return
            if len(row) < 7:
                continue
            try:
                lo = int(row[2]); hi = int(row[3]); bw = float(row[4])
                vals = [float(v) for v in row[6:]]
            except (ValueError, IndexError):
                continue
            self._last_row_t = time.time()
            for i, v in enumerate(vals):
                mhz = int((lo + i*bw + bw/2) / 1e6)
                if mhz not in cur or v > cur[mhz]:
                    cur[mhz] = v
            # In file mode the cell's RF window is whatever FALCON tuned to;
            # widen the dashboard's frequency axis to fit the incoming rows.
            if self._source == "file":
                lo_mhz = int(lo / 1e6)
                hi_mhz = int(hi / 1e6)
                if (lo_mhz != self._freq_start_mhz
                        or hi_mhz != self._freq_end_mhz):
                    with self._lock:
                        self._freq_start_mhz = lo_mhz
                        self._freq_end_mhz = hi_mhz
            now = time.time()
            if now - last_publish_t >= publish_interval_s and cur:
                self._publish(cur)
                cur = {k: v - 6.0 for k, v in cur.items()}
                last_publish_t = now

    def _publish(self, snapshot_data: dict[int, float]) -> None:
        if not snapshot_data:
            return
        with self._lock:
            self._latest = snapshot_data
            self._history.append({
                "ts": time.time(),
                "data": dict(snapshot_data),
            })
        if self._on_snapshot is not None:
            payload = {
                "source": self._source,
                "freq_start_mhz": self._freq_start_mhz,
                "freq_end_mhz": self._freq_end_mhz,
                "latest": [
                    {"mhz": m, "dbfs": round(d, 1)}
                    for m, d in sorted(snapshot_data.items())
                ],
            }
            try:
                self._on_snapshot(payload)
            except Exception:  # noqa: BLE001 — never let the callback kill the thread
                pass
