"""USRP live spectrum scanner.

Drives `sniffer.uhd_sweep` as a long-running subprocess, parses its CSV
output (same schema hackrf_sweep emits), and maintains a rolling
(frequency → power_dBFS) snapshot. The dashboard renders the snapshots
as a waterfall.

Only one process can hold a USRP at a time, so this scanner is mutually
exclusive with the FALCON path. The dashboard auto-disables it when
real-radio LTE sniffing is active (FalconEye holds the radio).
"""

from __future__ import annotations

import collections
import csv
import io
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional


class SpectrumScanner:
    """Background thread that runs the UHD sweeper and reports per-bin power."""

    def __init__(
        self,
        on_snapshot: Optional[Callable[[dict], None]] = None,
        *,
        freq_start_mhz: int = 700,
        freq_end_mhz: int = 2700,
        bin_width_hz: int = 1_000_000,
        history_seconds: int = 60,
        gain_db: float = 60.0,
    ) -> None:
        self._on_snapshot = on_snapshot
        self._freq_start_mhz = freq_start_mhz
        self._freq_end_mhz = freq_end_mhz
        self._bin_width_hz = bin_width_hz
        self._gain_db = gain_db
        # One snapshot per ~1 s of accumulated sweep rows.
        self._history: collections.deque[dict] = collections.deque(
            maxlen=history_seconds
        )
        self._latest: dict[int, float] = {}  # mhz_int -> dBFS
        self._lock = threading.Lock()
        self._running = False
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[str] = None

    # --- lifecycle ----------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if shutil.which(sys.executable) is None:
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

    # --- snapshot for SSE / /spectrum endpoint ------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "freq_start_mhz": self._freq_start_mhz,
                "freq_end_mhz": self._freq_end_mhz,
                "bin_width_hz": self._bin_width_hz,
                "latest": [
                    {"mhz": m, "dbfs": round(d, 1)}
                    for m, d in sorted(self._latest.items())
                ],
                "n_history": len(self._history),
                "error": self._error,
            }

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    # --- main loop ----------------------------------------------------

    def _run(self) -> None:
        """Run `python -m sniffer.uhd_sweep` until stop() is called.
        uhd_sweep emits CSV rows in the same schema hackrf_sweep emits;
        we aggregate across rows at a fixed publish cadence."""
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
            self._consume_csv(self._proc.stdout)
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if not self._running:
                return
            time.sleep(2.0)

    def _consume_csv(self, stream) -> None:
        """Parse CSV stream; publish a snapshot every ~1 s. Decay older
        peaks 6 dB per refresh so the waterfall doesn't get pinned by a
        one-time spike."""
        if stream is None:
            return
        cur: dict[int, float] = {}
        last_publish_t = time.time()
        publish_interval_s = 1.0
        reader = csv.reader(io.TextIOWrapper(stream.buffer, encoding="utf-8",
                                             errors="replace"),
                            skipinitialspace=True)
        for row in reader:
            if not self._running:
                return
            if len(row) < 7:
                continue
            try:
                lo = int(row[2]); bw = float(row[4])
                vals = [float(v) for v in row[6:]]
            except (ValueError, IndexError):
                continue
            for i, v in enumerate(vals):
                mhz = int((lo + i*bw + bw/2) / 1e6)
                if mhz not in cur or v > cur[mhz]:
                    cur[mhz] = v
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
