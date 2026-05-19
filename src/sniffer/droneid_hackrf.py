"""HackRF capture loop wrapper for HackRF-friendly DroneID decoders.

`samples2djidroneid` (and its proto17/dji_droneid back-end) cannot
consume a live IQ stream — they only work on recorded files. This
module bridges that gap with the simplest possible loop:

    every N seconds:
      - dump N s of HackRF IQ at 15.36 MSPS to a temp file (hackrf_transfer)
      - convert int8 IQ → complex float32 (samples2djidroneid's format)
      - hand the file to `--decoder-cmd` (with `{iq}` substituted)
      - stream the decoder's stdout (DroneID JSON lines) to *our* stdout
      - delete the temp file
      - go again

The decoder command is user-supplied so this module stays
decoder-agnostic. Typical invocation pointing at the bundled
samples2djidroneid Docker image:

    python -m sniffer.droneid_hackrf \\
        --center-hz 2434500000 \\
        --device-serial 0000000000000000a06463c823e3xxxx \\
        --chunk-seconds 3 \\
        --decoder-cmd "docker run --rm -v {iq_dir}:/data -i samples2djidroneid /data/{iq_name}"

Caveats — please read before deploying:

- **HackRF gives you ~15 MHz of band.** Each instance of this loop
  covers one tune. The 2.4 GHz DroneID channels span 2414–2475 MHz —
  one HackRF tuned mid-band catches a few. Use a second HackRF (and a
  second `--droneid-cmd` on `sniffer live`) for the 5 GHz block.
- **Detection rate << B210/UHD live path.** Capture/decode windows are
  not contiguous; frames during decode are missed.
- **i8 → f32 conversion** is in-process numpy; for very small chunks
  it's free, for large chunks consider a sox / fixed-point pipeline.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

DEFAULT_SAMPLE_RATE = 15_360_000  # samples2djidroneid prefers 15.36 MSPS
DEFAULT_CHUNK_SECONDS = 3.0
DEFAULT_HACKRF_BIN = "hackrf_transfer"


def _capture_chunk(*, binary: str, raw_path: str, center_hz: int,
                   sample_rate: int, num_samples: int,
                   device_serial: Optional[str],
                   rx_gain_db: int, lna_gain_db: int) -> int:
    cmd = [binary,
           "-r", raw_path,
           "-f", str(center_hz),
           "-s", str(sample_rate),
           "-n", str(num_samples),
           "-l", str(lna_gain_db),    # LNA gain (0-40, 8 dB steps)
           "-g", str(rx_gain_db),     # VGA gain (0-62, 2 dB steps)
           "-a", "1"]                 # amp on
    if device_serial:
        cmd += ["-d", device_serial]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode


def _i8_to_cf32(raw_path: str, cf32_path: str) -> None:
    """Convert hackrf_transfer's int8 interleaved IQ to complex float32."""
    # numpy is in requirements.txt — already a project dep.
    import numpy as np
    i8 = np.fromfile(raw_path, dtype=np.int8)
    # Two bytes per IQ pair, alternating I,Q; scale to [-1, 1)
    cf32 = (i8.astype(np.float32) / 128.0)
    cf32.tofile(cf32_path)


def _run_decoder(decoder_cmd_template: str, iq_path: str, *,
                 timeout_s: float) -> Optional[str]:
    """Run the decoder with {iq} / {iq_dir} / {iq_name} substituted."""
    iq_dir = str(Path(iq_path).parent)
    iq_name = Path(iq_path).name
    cmd_str = decoder_cmd_template.format(
        iq=iq_path, iq_dir=iq_dir, iq_name=iq_name)
    try:
        proc = subprocess.run(shlex.split(cmd_str), capture_output=True,
                              text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        print(f"droneid_hackrf: decoder timed out after {timeout_s}s",
              file=sys.stderr)
        return None
    if proc.returncode != 0 and not proc.stdout.strip():
        # Decoder failed AND produced nothing — surface stderr.
        print(f"droneid_hackrf: decoder rc={proc.returncode} "
              f"stderr={proc.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return proc.stdout


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="sniffer.droneid_hackrf",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--center-hz", type=float, required=True,
                   help="HackRF center frequency in Hz (e.g. 2434500000)")
    p.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE,
                   help="sample rate in samples/sec (default 15.36e6)")
    p.add_argument("--chunk-seconds", type=float, default=DEFAULT_CHUNK_SECONDS,
                   help="capture length per iteration in seconds")
    p.add_argument("--device-serial", default=None,
                   help="HackRF device serial (when multiple are attached)")
    p.add_argument("--rx-gain-db", type=int, default=30,
                   help="VGA gain 0-62 dB in 2 dB steps")
    p.add_argument("--lna-gain-db", type=int, default=32,
                   help="LNA gain 0-40 dB in 8 dB steps")
    p.add_argument("--decoder-cmd", required=True,
                   help="shell command run per chunk. Tokens substituted: "
                        "{iq} = full path, {iq_dir} = directory, "
                        "{iq_name} = filename")
    p.add_argument("--decoder-timeout-s", type=float, default=30.0,
                   help="hard kill of decoder after this many seconds")
    p.add_argument("--hackrf-bin", default=DEFAULT_HACKRF_BIN,
                   help="path to hackrf_transfer binary")
    p.add_argument("--once", action="store_true",
                   help="capture and decode a single chunk then exit")
    args = p.parse_args(argv)

    num_samples = int(args.sample_rate * args.chunk_seconds)
    center_hz = int(args.center_hz)
    while True:
        with tempfile.TemporaryDirectory(prefix="droneid_hackrf_") as td:
            raw_path = os.path.join(td, "chunk.i8")
            cf32_path = os.path.join(td, "chunk.cf32")
            rc = _capture_chunk(
                binary=args.hackrf_bin, raw_path=raw_path,
                center_hz=center_hz, sample_rate=args.sample_rate,
                num_samples=num_samples, device_serial=args.device_serial,
                rx_gain_db=args.rx_gain_db, lna_gain_db=args.lna_gain_db,
            )
            if rc != 0:
                print(f"droneid_hackrf: hackrf_transfer rc={rc} — retrying "
                      f"in 2s", file=sys.stderr)
                time.sleep(2.0)
                continue
            if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
                print("droneid_hackrf: empty capture — retrying", file=sys.stderr)
                time.sleep(1.0)
                continue
            try:
                _i8_to_cf32(raw_path, cf32_path)
            except Exception as exc:  # noqa: BLE001
                print(f"droneid_hackrf: i8→cf32 failed: {exc}", file=sys.stderr)
                continue
            stdout = _run_decoder(args.decoder_cmd, cf32_path,
                                  timeout_s=args.decoder_timeout_s)
            if stdout:
                sys.stdout.write(stdout)
                if not stdout.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.flush()
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
