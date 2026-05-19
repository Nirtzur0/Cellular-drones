"""Unified CLI for the cellular-drones UE sniffer.

`sniffer live` ingests PDCCH events (real radio *or* simulated) and
serves the live dashboard. The dashboard performs C-RNTI extraction
and per-UE positioning together — they are not separate surfaces.

    sniffer live --simulate               # no hardware, synthetic UEs + GPS
    sniffer live --earfcn N --pci P       # real radio, LTESniffer on a cell

Auxiliary:

    sniffer scan --band 3                 # discover cells via srsran_cell_search
    sniffer scan --band 3 --decode-sib1   # + PLMN/TAC/CGI via pdsch_ue
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


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.band is None and args.earfcn_range is None:
        print("sniffer scan needs --band or --earfcn-range", file=sys.stderr)
        return 2
    earfcn_range = None
    if args.earfcn_range is not None:
        try:
            start, end = args.earfcn_range.split(",", 1)
            earfcn_range = (int(start), int(end))
        except ValueError:
            print("--earfcn-range must be `start,end` (e.g. 1800,1900)",
                  file=sys.stderr)
            return 2
    from sniffer.scan import run_scan
    binary = (args.binary or os.environ.get("SRSRAN_CELL_SEARCH_BIN")
              or "srsran_cell_search")
    sib_binary = (args.sib_binary or os.environ.get("PDSCH_UE_BIN")
                  or "pdsch_ue")
    return run_scan(
        band=args.band, earfcn_range=earfcn_range,
        decode_sib1=args.decode_sib1,
        binary=binary, sib_binary=sib_binary,
        json_out=args.jsonl,
    )


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

    ps = sub.add_parser("scan",
                        help="discover LTE cells with srsran_cell_search",
                        description="One-shot wrapper around srsRAN's "
                                    "cell_search binary. Use the printed "
                                    "(EARFCN, PCI) to target `sniffer live`.")
    ps.add_argument("--band", type=int, default=None,
                    help="3GPP LTE band number (e.g. 3, 7, 20)")
    ps.add_argument("--earfcn-range", default=None,
                    help="alternative to --band: `start,end` EARFCN sweep "
                         "(e.g. 1800,1900)")
    ps.add_argument("--decode-sib1", action="store_true",
                    help="after each cell, run pdsch_ue to extract "
                         "PLMN / TAC / CGI from SIB1 (adds 5-10 s/cell)")
    ps.add_argument("--jsonl", action="store_true",
                    help="emit JSONL instead of a table")
    ps.add_argument("--binary", default=None,
                    help="path to srsran_cell_search "
                         "(env: SRSRAN_CELL_SEARCH_BIN)")
    ps.add_argument("--sib-binary", default=None,
                    help="path to pdsch_ue (env: PDSCH_UE_BIN)")
    ps.set_defaults(func=_cmd_scan)

    pi = sub.add_parser("install", help="install dependencies (Linux only)")
    pi.set_defaults(func=_cmd_install)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
