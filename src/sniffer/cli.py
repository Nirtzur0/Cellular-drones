"""Unified CLI for the cellular-drones UE sniffer.

`sniffer live` ingests PDCCH events (real radio via FalconEye *or*
simulated) and serves the live dashboard. The dashboard performs
C-RNTI extraction and per-UE positioning together.

    sniffer live --simulate               # no hardware, synthetic UEs + GPS
    sniffer live --earfcn N --pci P       # real radio: FalconEye on one cell

Auxiliary:

    sniffer scan --band 3                 # discover cells via srsran_cell_search
    sniffer scan --band 3 --decode-sib1   # + PLMN/TAC/CGI via pdsch_ue
    sniffer survey --band 3               # scan + sweep + dwell on each cell
    sniffer install                       # apt + srsRAN + FALCON (Linux)
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

    from sniffer.lte_bands import earfcn_to_hz_dl
    try:
        f_hz = earfcn_to_hz_dl(int(args.earfcn))
    except ValueError as exc:
        print(f"sniffer live: {exc}", file=sys.stderr)
        return 2

    # FalconEye (falkenber9/falcon) — the single real-radio decoder.
    #   FalconEye -f <hz>           # tunes & decodes; auto cell-search
    #   FalconEye -f <hz> -D path   # also write per-DCI CSV (what we tail)
    # No PCI flag — FALCON locks to whichever cell it finds at -f freq.
    # We pass --falcon-pci so every record gets stamped with the cell we
    # intended to target.
    binname = os.environ.get("FALCON_BIN", "FalconEye")
    if shutil.which(binname) is None:
        print(f"{binname} not on PATH. Run `sniffer install` to build "
              f"FALCON, or set FALCON_BIN.", file=sys.stderr)
        return 3
    falcon_cmd = [binname,
                  "-f", str(int(f_hz)),
                  "-A", str(args.antennas),
                  "-g", str(args.gain_db)]
    argv += ["--falcon-cmd", " ".join(falcon_cmd),
             "--falcon-pci", str(args.pci),
             "--center-hz", str(f_hz)]
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
        rf_args=args.rf_args,
        gain_db=args.gain_db,
    )


def _cmd_survey(args: argparse.Namespace) -> int:
    """Sweep across all cells in a band, dwelling on each for N seconds.

    Pipeline: `sniffer scan --band B` → cell list → for each cell,
    spawn a decoder for `--dwell-seconds`, then move on. Repeat until
    `--total-minutes` elapses. All C-RNTIs accumulate in the same
    dashboard.
    """
    import json
    from sniffer.scan import run_scan, Cell

    if args.band is None and args.earfcn_range is None and not args.cells:
        print("sniffer survey: provide --band, --earfcn-range, or --cells",
              file=sys.stderr)
        return 2

    cells_payload: list[dict] = []
    if args.cells:
        # Manual override: user knows the cells already (skips scan).
        try:
            for spec in args.cells.split(","):
                earfcn_s, pci_s = spec.split(":")
                from sniffer.lte_bands import earfcn_to_hz_dl
                earfcn = int(earfcn_s)
                cells_payload.append({
                    "earfcn": earfcn,
                    "pci": int(pci_s),
                    "center_hz": earfcn_to_hz_dl(earfcn),
                })
        except (ValueError, KeyError) as exc:
            print(f"sniffer survey: bad --cells spec ({exc}). "
                  f"Format: EARFCN:PCI,EARFCN:PCI,...",
                  file=sys.stderr)
            return 2
    else:
        # Drive scan, capture its cells, build the payload.
        import io
        earfcn_range = None
        if args.earfcn_range is not None:
            try:
                lo, hi = args.earfcn_range.split(",", 1)
                earfcn_range = (int(lo), int(hi))
            except ValueError:
                print("--earfcn-range must be `start,end`", file=sys.stderr)
                return 2
        scan_buf = io.StringIO()
        scan_rc = run_scan(band=args.band, earfcn_range=earfcn_range,
                           json_out=True, fh=scan_buf)
        if scan_rc != 0:
            print(f"sniffer survey: scan failed with rc={scan_rc}",
                  file=sys.stderr)
            return scan_rc
        from sniffer.lte_bands import earfcn_to_hz_dl
        for line in scan_buf.getvalue().splitlines():
            try:
                cell = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cell.get("earfcn") is None or cell.get("pci") is None:
                continue
            try:
                center_hz = earfcn_to_hz_dl(int(cell["earfcn"]))
            except ValueError:
                continue
            cells_payload.append({
                "earfcn": int(cell["earfcn"]),
                "pci": int(cell["pci"]),
                "center_hz": center_hz,
            })

    if not cells_payload:
        print("sniffer survey: no cells found. Try a different band or "
              "pass --cells manually.", file=sys.stderr)
        return 4

    print(f"sniffer survey: {len(cells_payload)} cells, "
          f"dwell={args.dwell_seconds}s, total={args.total_minutes}min, "
          f"decoder=falcon", file=sys.stderr)
    for c in cells_payload:
        print(f"  EARFCN {c['earfcn']} · PCI {c['pci']} "
              f"@ {c['center_hz']/1e6:.2f} MHz", file=sys.stderr)

    argv = ["--host", args.host,
            "--port", str(args.port),
            "--out-dir", args.out_dir,
            "--mission-id", args.mission_id,
            "--survey-cells", json.dumps(cells_payload),
            "--survey-dwell-seconds", str(args.dwell_seconds),
            "--survey-total-seconds", str(args.total_minutes * 60.0)]
    return _delegate("sniffer.live", argv)


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
                    help="target physical cell ID (stamped on every "
                         "decoded record — FalconEye locks to a cell "
                         "by frequency, not PCI, so the caller asserts "
                         "which cell the chosen -f freq belongs to)")
    pl.add_argument("--gain-db", type=int, default=70,
                    help="FalconEye -g: fixed RX gain in dB (default 70). "
                         "Omitting -g triggers AGC, which empirically "
                         "fails to lock on this fork of srsLTE — keep this "
                         "set unless you're sure AGC works on your build.")
    pl.add_argument("--antennas", type=int, default=1,
                    help="FalconEye -A: number of RX antennas to use "
                         "(default 1; B210 supports up to 2)")
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
    ps.add_argument("--rf-args", default="",
                    help="srsran -a string. Default empty = auto-pick "
                         "(UHD if built-in, then SoapySDR). For HackRF: "
                         "'driver=hackrf'. For USRP with antenna on TX/RX: "
                         "'type=b200,rx_antenna=TX/RX'.")
    ps.add_argument("--gain-db", type=int, default=75,
                    help="RX gain in dB (default 75). HackRF wants 80+; "
                         "USRP B-series max effective is ~76.")
    ps.set_defaults(func=_cmd_scan)

    psv = sub.add_parser("survey",
                         help="sweep across all cells in a band, "
                              "collecting C-RNTIs from each",
                         description="Cell-sweep + dwell orchestrator. "
                                     "Runs `sniffer scan` to find cells "
                                     "in the band, then visits each cell "
                                     "for --dwell-seconds with a decoder "
                                     "(FalconEye default), cycling until "
                                     "--total-minutes elapses. C-RNTIs "
                                     "from every cell accumulate in one "
                                     "dashboard.")
    psv.add_argument("--band", type=int, default=None,
                     help="LTE band to sweep (e.g. 3 for 1800 MHz FDD)")
    psv.add_argument("--earfcn-range", default=None,
                     help="alternative: 'start,end' EARFCN sweep")
    psv.add_argument("--cells", default=None,
                     help="skip scan: comma-separated EARFCN:PCI pairs, "
                          "e.g. 1850:271,1850:88")
    psv.add_argument("--dwell-seconds", type=float, default=15.0,
                     help="seconds per cell per cycle (default 15)")
    psv.add_argument("--total-minutes", type=float, default=30.0,
                     help="how long to run the survey (default 30 min)")
    psv.add_argument("--host", default="127.0.0.1")
    psv.add_argument("--port", type=int, default=8000)
    psv.add_argument("--out-dir", default="data")
    psv.add_argument("--mission-id", default=default_mid)
    psv.set_defaults(func=_cmd_survey)

    pi = sub.add_parser("install", help="install dependencies (Linux only)")
    pi.set_defaults(func=_cmd_install)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
