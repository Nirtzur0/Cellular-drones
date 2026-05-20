"""UHD-based spectrum sweeper for the USRP — drop-in replacement for hackrf_sweep.

Captures IQ samples at a sequence of center frequencies via UHD's
`rx_samples_to_file` example tool, FFTs them with numpy, and prints
power-per-bin in the same CSV format hackrf_sweep emits so the existing
`SpectrumScanner` parser can ingest it without changes:

    YYYY-MM-DD, HH:MM:SS.sssss, hz_low, hz_high, hz_bin_width, num_samples, db, db, ...

Tradeoffs:
  * Each tune costs ~1.5–2 s of `rx_samples_to_file` setup (open USRP,
    set rate/freq/gain, capture, close). With wide tiles (20 MHz) and a
    moderate scan range, one full sweep takes 5–20 s — slower than
    hackrf_sweep's near-realtime output but adequate for a dashboard.
  * IQ is captured as `short` (int16) for half the bandwidth of float32 —
    fits Pi 4 USB 3 budget comfortably even at 20 MHz.

Run:
    python -m sniffer.uhd_sweep              # default 700-2700 MHz @ 20 MHz tiles
    python -m sniffer.uhd_sweep -f 1805:1880 # focus on LTE Band 3 DL
"""

from __future__ import annotations

import argparse
import datetime
import os
import struct
import subprocess
import sys
import tempfile
import time
from typing import Iterator

import numpy as np


# rx_samples_to_file shipped with libuhd's `uhd-host` package
_RX_SAMPLES = "/usr/libexec/uhd/examples/rx_samples_to_file"


def _capture(center_hz: float, sample_rate: float, n_samples: int,
             gain_db: float, iq_path: str) -> bool:
    """Capture n_samples complex int16 samples to iq_path. Return True on success."""
    cmd = [
        _RX_SAMPLES,
        "--freq", str(center_hz),
        "--rate", str(sample_rate),
        "--gain", str(gain_db),
        "--nsamps", str(n_samples),
        "--file", iq_path,
        "--type", "short",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        print(f"# UHD capture timeout @ {center_hz/1e6:.1f} MHz", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"# UHD capture rc={proc.returncode} @ {center_hz/1e6:.1f} MHz: "
              f"{proc.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return os.path.exists(iq_path) and os.path.getsize(iq_path) >= n_samples * 4


def _spectrum_db(iq_path: str, n_samples: int) -> np.ndarray:
    """FFT the IQ file and return per-bin dBFS (length n_samples)."""
    raw = np.fromfile(iq_path, dtype=np.int16, count=n_samples * 2)
    if raw.size < n_samples * 2:
        return np.full(n_samples, -120.0)
    # Interleaved I,Q int16 → complex64 in [-1, 1]
    iq = (raw[::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 32768.0
    # Hann window suppresses sidelobes
    window = np.hanning(len(iq)).astype(np.float32)
    spec = np.fft.fftshift(np.fft.fft(iq * window))
    mag2 = (spec.real * spec.real + spec.imag * spec.imag) / (len(iq) ** 2)
    return 10.0 * np.log10(mag2 + 1e-12)


def _csv_row(ts: datetime.datetime, hz_low: int, hz_high: int, bin_hz: int,
             dbs: np.ndarray) -> str:
    """Format one CSV row in the hackrf_sweep schema."""
    head = (f"{ts.strftime('%Y-%m-%d')}, {ts.strftime('%H:%M:%S.%f')}, "
            f"{hz_low}, {hz_high}, {float(bin_hz):.2f}, {len(dbs)}")
    return head + ", " + ", ".join(f"{d:.2f}" for d in dbs)


def _tiles(freq_start_hz: int, freq_end_hz: int,
           tile_bw_hz: int) -> Iterator[tuple[int, int]]:
    """Yield (center_hz, low_hz) per tile that fully covers the range."""
    center = freq_start_hz + tile_bw_hz // 2
    while center - tile_bw_hz // 2 < freq_end_hz:
        yield center, center - tile_bw_hz // 2
        center += tile_bw_hz


def _bin_step(spec_db: np.ndarray, bins_per_mhz: int,
              tile_bw_hz: int) -> tuple[np.ndarray, int]:
    """Downsample spec_db to per-MHz bins (peak hold within each bin).

    Returns (bin_db, bin_width_hz).
    """
    n = spec_db.size
    bin_hz = 1_000_000
    bins_in_tile = max(1, tile_bw_hz // bin_hz)
    # Split the FFT output into equal-width slabs, take peak per slab.
    slab = n // bins_in_tile
    if slab < 1:
        return spec_db, bin_hz
    trimmed = spec_db[: slab * bins_in_tile]
    grouped = trimmed.reshape(bins_in_tile, slab)
    return grouped.max(axis=1), bin_hz


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-f", "--freq-range-mhz", default="700:2700",
                   help="start:end in MHz (default 700:2700)")
    p.add_argument("--tile-bw-mhz", type=int, default=20,
                   help="bandwidth per UHD tuning (default 20 MHz — B210 USB3 budget)")
    p.add_argument("--gain", type=float, default=60.0,
                   help="RX gain in dB (default 60)")
    p.add_argument("--nsamps", type=int, default=8192,
                   help="samples per capture (default 8192 → ~410 us at 20 MHz)")
    p.add_argument("--continuous", action="store_true", default=True,
                   help="loop forever (default; pass --once for one sweep)")
    p.add_argument("--once", dest="continuous", action="store_false")
    args = p.parse_args()

    try:
        f_start_mhz, f_end_mhz = (int(x) for x in args.freq_range_mhz.split(":"))
    except ValueError:
        print(f"bad --freq-range-mhz '{args.freq_range_mhz}', want start:end",
              file=sys.stderr)
        return 2

    tile_bw_hz = args.tile_bw_mhz * 1_000_000
    sample_rate = float(tile_bw_hz)  # nyquist rate matches tile width

    if not os.path.exists(_RX_SAMPLES):
        print(f"# {_RX_SAMPLES} not found; install uhd-host", file=sys.stderr)
        return 1

    iq_dir = tempfile.mkdtemp(prefix="uhd_sweep_")
    iq_path = os.path.join(iq_dir, "iq.bin")

    while True:
        for center_hz, low_hz in _tiles(f_start_mhz * 1_000_000,
                                        f_end_mhz * 1_000_000, tile_bw_hz):
            ok = _capture(center_hz, sample_rate, args.nsamps, args.gain, iq_path)
            if not ok:
                continue
            spec_db = _spectrum_db(iq_path, args.nsamps)
            bin_db, bin_hz = _bin_step(spec_db, 1, tile_bw_hz)
            hz_high = low_hz + tile_bw_hz
            print(_csv_row(datetime.datetime.now(), low_hz, hz_high,
                           bin_hz, bin_db), flush=True)
        if not args.continuous:
            break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
