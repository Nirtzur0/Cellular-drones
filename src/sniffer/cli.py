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

    # DroneID is an orthogonal GPS source: works in both --simulate and
    # real-radio modes, and supports multiple producers (one per radio).
    for droneid_cmd in (args.droneid_cmd or []):
        argv += ["--droneid-cmd", droneid_cmd]
    if args.droneid_serial:
        argv += ["--droneid-serial", args.droneid_serial]

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

    from sniffer.lte_bands import earfcn_to_hz_dl
    try:
        f_hz = earfcn_to_hz_dl(int(args.earfcn))
    except ValueError as exc:
        print(f"sniffer live: {exc}", file=sys.stderr)
        return 2

    if args.decoder == "falcon":
        # FalconEye (falkenber9/falcon). Per the README:
        #   FalconEye -f <hz>          # tunes & decodes; auto cell-search
        #   FalconEye -f <hz> -D path  # also write per-DCI CSV (what we tail)
        # No PCI flag — FALCON locks to whichever cell it finds at -f
        # freq. We pass --falcon-pci to live so records get stamped.
        binname = os.environ.get("FALCON_BIN", "FalconEye")
        if shutil.which(binname) is None:
            print(f"{binname} not on PATH. Run `sniffer install` to "
                  f"build FALCON, or set FALCON_BIN.", file=sys.stderr)
            return 3
        falcon_cmd = [binname, "-f", str(int(f_hz))]
        argv += ["--falcon-cmd", " ".join(falcon_cmd),
                 "--falcon-pci", str(args.pci),
                 "--center-hz", str(f_hz)]
        return _delegate("sniffer.live", argv)

    # Default: LTESniffer. CLI per SysSec-KAIST/LTESniffer README:
    #   -A <antennas>   mandatory (typical: 2 for B210)
    #   -W <threads>    mandatory (typical: 4 on a quad-core SBC)
    #   -f <Hz>         DL frequency in Hz (NOT EARFCN — convert first)
    #   -I <PCI>        target PCI; bypasses internal cell search
    #   -m <0|1>        mode: 0 = downlink-only, 1 = uplink+downlink
    #   -a "<usrp_args>"  USRP runtime args (B210 wants
    #                     num_recv_frames=512 for clean sync)
    #
    # No --rx-gain flag exists; UHD AGC is the default. USRP access
    # typically requires sudo or the udev rules shipped with libuhd.
    binname = os.environ.get("LTESNIFFER_BIN", "LTESniffer")
    if shutil.which(binname) is None:
        print(f"{binname} not on PATH. Run `sniffer install` on a "
              f"Linux + USRP B210 host, or set LTESNIFFER_BIN to your build.",
              file=sys.stderr)
        return 3

    ltecmd = [binname,
              "-A", str(args.antennas),
              "-W", str(args.threads),
              "-f", str(int(f_hz)),
              "-I", str(args.pci),
              "-m", "0",   # DL-only — UL needs 2× USRP + GPSDO, out of scope
              "-a", "num_recv_frames=512"]

    argv += ["--ltesniffer-cmd", " ".join(ltecmd),
             "--center-hz", str(f_hz),
             "--normalize-ltesniffer"]
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
                    help="target LTE downlink EARFCN (e.g. 1850 = "
                         "1870.0 MHz on band 3). Internally converted "
                         "to Hz via sniffer.lte_bands.earfcn_to_hz_dl.")
    pl.add_argument("--pci", type=int, default=None,
                    help="target physical cell ID (passed to LTESniffer "
                         "as -I, bypassing internal cell search)")
    pl.add_argument("--antennas", type=int, default=2,
                    help="LTESniffer -A: number of antennas "
                         "(default 2, fits USRP B210)")
    pl.add_argument("--threads", type=int, default=4,
                    help="LTESniffer -W: worker threads (default 4)")
    pl.add_argument("--decoder", choices=("ltesniffer", "falcon"),
                    default="ltesniffer",
                    help="which LTE PDCCH decoder to spawn. 'ltesniffer' "
                         "(default) writes PCAP — its stdout is not "
                         "parsed yet (separate task). 'falcon' uses "
                         "falkenber9/falcon's FalconEye, which writes "
                         "per-DCI CSV that we tail in real time.")
    pl.add_argument("--host", default="127.0.0.1")
    pl.add_argument("--port", type=int, default=8000)
    pl.add_argument("--out-dir", default="data")
    pl.add_argument("--mission-id", default=default_mid)
    pl.add_argument("--droneid-cmd", action="append", default=None,
                    metavar="CMD",
                    help="argv (space-split) for a DJI DroneID decoder "
                         "that prints one JSON object per frame. Repeat "
                         "for multiple radios (one HackRF per band, etc.). "
                         "Wired as an alternative GPS source — coexists "
                         "with gpsd if both are available.")
    pl.add_argument("--droneid-serial", default=None, metavar="SUBSTR",
                    help="restrict DroneID frames to those whose serial "
                         "number contains SUBSTR (case-sensitive). Useful "
                         "when multiple drones are airborne.")
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
