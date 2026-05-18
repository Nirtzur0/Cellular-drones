"""Unified CLI for the cellular-drones UE sniffer.

There is one run path. `sniffer live` ingests PDCCH events (real radio
*or* simulated) and serves the live dashboard. The dashboard performs
C-RNTI extraction and per-UE positioning together — they are not
separate surfaces.

    sniffer live --simulate               # no hardware, synthetic UEs + GPS
    sniffer live --earfcn N --pci P       # real radio, LTESniffer on a cell

Auxiliary commands:

    sniffer report <jsonl> [--plot ...]   # offline summary + plot from a capture
    sniffer gps-log                       # standalone gpsd -> JSONL recorder
    sniffer install                       # apt + srsRAN + LTESniffer (Linux)
"""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _delegate(module_name: str, argv: list[str]) -> int:
    mod = importlib.import_module(module_name)
    saved = sys.argv
    sys.argv = [module_name, *argv]
    try:
        return mod.main()
    finally:
        sys.argv = saved


def _cmd_live(args: argparse.Namespace) -> int:
    argv = ["--host", args.host,
            "--port", str(args.port),
            "--out-dir", args.out_dir,
            "--mission-id", args.mission_id]

    if args.simulate:
        if args.earfcn or args.pci:
            print("--simulate is mutually exclusive with --earfcn/--pci",
                  file=sys.stderr)
            return 2
        argv.append("--simulate")
        return _delegate("sniffer.live", argv)

    if args.earfcn is None or args.pci is None:
        print("sniffer live needs either --simulate or both --earfcn and --pci",
              file=sys.stderr)
        return 2

    binname = os.environ.get("LTESNIFFER_BIN", "LTESniffer")
    if shutil.which(binname) is None:
        print(f"{binname} not on PATH. Run `sniffer install` on a "
              f"Linux + USRP B210 host, or set LTESNIFFER_BIN to your build.",
              file=sys.stderr)
        return 3

    ltecmd = [binname, "--earfcn", str(args.earfcn),
              "--target-pci", str(args.pci)]
    if args.rx_gain is not None:
        ltecmd += ["--rx-gain", str(args.rx_gain)]

    argv += ["--ltesniffer-cmd", " ".join(ltecmd),
             "--center-hz", str(args.center_hz),
             "--normalize-ltesniffer"]
    if args.rx_gain is not None:
        argv += ["--rx-gain-db", str(args.rx_gain)]
    return _delegate("sniffer.live", argv)


def _cmd_report(args: argparse.Namespace) -> int:
    argv = [args.input_glob]
    if args.plot:
        argv += ["--plot", args.plot]
    return _delegate("sniffer.report", argv)


def _cmd_gps_log(args: argparse.Namespace) -> int:
    if shutil.which("gpspipe") is None:
        print("gpspipe not found. Install gpsd (apt install gpsd gpsd-clients).",
              file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    out_path = Path(args.out_dir) / f"gps-{args.mission_id}.jsonl"
    print(f"Logging GPS fixes -> {out_path}", file=sys.stderr)
    gpspipe = subprocess.Popen(["gpspipe", "-w"], stdout=subprocess.PIPE)
    try:
        with open(out_path, "ab") as fh:
            parse = subprocess.Popen(
                [sys.executable, "-m", "sniffer.parse_gpsd",
                 "--mission-id", args.mission_id],
                stdin=gpspipe.stdout, stdout=fh,
            )
            assert gpspipe.stdout is not None
            gpspipe.stdout.close()  # let SIGPIPE propagate on shutdown
            return parse.wait()
    finally:
        if gpspipe.poll() is None:
            gpspipe.terminate()


def _cmd_install(_args: argparse.Namespace) -> int:
    script = Path(__file__).resolve().parents[2] / "scripts" / "install-linux.sh"
    if not script.exists():
        print(f"installer not found at {script}\n"
              f"`sniffer install` only works from a git checkout.",
              file=sys.stderr)
        return 1
    return subprocess.call(["bash", str(script)])


def _build_parser() -> argparse.ArgumentParser:
    default_mid = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    p = argparse.ArgumentParser(prog="sniffer", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("live", help="run the realtime dashboard (the run path)",
                        description="The single run path. Ingest PDCCH events "
                                    "(real radio with --earfcn/--pci, or simulated "
                                    "with --simulate) and serve the live dashboard "
                                    "with both C-RNTI extraction and per-UE "
                                    "positioning.")
    pl.add_argument("--simulate", action="store_true",
                    help="synthetic UEs + GPS (no radio needed)")
    pl.add_argument("--earfcn", type=int, default=None,
                    help="target LTE downlink EARFCN (e.g. 1850)")
    pl.add_argument("--pci", type=int, default=None,
                    help="target physical cell ID")
    pl.add_argument("--rx-gain", type=float, default=None,
                    help="LTESniffer RX gain in dB")
    pl.add_argument("--center-hz", type=float, default=1_842_500_000,
                    help="DL carrier in Hz (for the UI caption; "
                         "EARFCN is what actually tunes the SDR)")
    pl.add_argument("--host", default="127.0.0.1")
    pl.add_argument("--port", type=int, default=8000)
    pl.add_argument("--out-dir", default="data")
    pl.add_argument("--mission-id", default=default_mid)
    pl.set_defaults(func=_cmd_live)

    pr = sub.add_parser("report", help="text summary + plot from JSONL",
                        description="Aggregate a saved geotagged capture into "
                                    "a per-UE summary and optional PNG plot.")
    pr.add_argument("input_glob", help="glob for geotagged-*.jsonl files")
    pr.add_argument("--plot", default=None, help="path to write PNG")
    pr.set_defaults(func=_cmd_report)

    pg = sub.add_parser("gps-log", help="standalone gpsd -> JSONL recorder")
    pg.add_argument("--mission-id", default=default_mid)
    pg.add_argument("--out-dir", default="data")
    pg.set_defaults(func=_cmd_gps_log)

    pi = sub.add_parser("install", help="install dependencies (Linux only)")
    pi.set_defaults(func=_cmd_install)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
