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
import json
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


# LTE PRB → FALCON sample rate (which doubles as the B210 master clock).
# FALCON uses 23.04 MHz for 100 PRB (not the standard 30.72 MHz srsLTE rate)
# so the table below intentionally mirrors srslte_sampling_freq_hz.
_PRB_TO_MASTER_CLOCK_HZ = {
    6:   1_920_000,
    15:  3_840_000,
    25:  5_760_000,
    50:  11_520_000,
    75:  15_360_000,
    100: 23_040_000,
}


def _lookup_master_clock_for_cell(earfcn: int, pci: int) -> int:
    """Look up the master clock rate for a known (EARFCN, PCI) cell.

    Reads data/known_cells.jsonl for any prior live-lock that recorded
    nof_prb for this cell. Falls back to 23.04 MHz (100-PRB default),
    which is the right value for the IL macro deployments we've tested.
    """
    try:
        from sniffer.cells import load_all
        for c in load_all():
            if (c.earfcn == earfcn and c.pci == pci
                    and getattr(c, "nof_prb", None)):
                clk = _PRB_TO_MASTER_CLOCK_HZ.get(int(c.nof_prb))
                if clk:
                    return clk
    except Exception:  # noqa: BLE001
        pass
    return 23_040_000


def _cmd_live(args: argparse.Namespace) -> int:
    argv = ["--host", args.host,
            "--port", str(args.port),
            "--out-dir", args.out_dir,
            "--mission-id", args.mission_id]
    if args.spectrum:
        argv += ["--spectrum",
                 "--spectrum-freq-mhz", args.spectrum_freq_mhz,
                 "--spectrum-gain-db", str(args.spectrum_gain_db)]

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
                  "-g", str(args.gain_db),
                  # Skip FALCON's N_id_2 brute force — we already know the
                  # PCI. N_id_2 = pci % 3 (LTE spec). Saves ~30–60 s per
                  # FALCON spawn, which matters when the watchdog has to
                  # respawn on stall.
                  "-l", str(args.pci % 3),
                  # FALCON default is 20 worker threads — wildly oversubscribes
                  # a 4-core Pi and leads to "No worker available, skipping
                  # subframe" floods. -W 4 matches Pi cores and lets workers
                  # actually finish per-subframe DCI searches.
                  "-W", "4"]
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
        decode_neighbors=args.decode_neighbors,
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
    if args.spectrum:
        argv += ["--spectrum",
                 "--spectrum-freq-mhz", args.spectrum_freq_mhz,
                 "--spectrum-gain-db", str(args.spectrum_gain_db)]
    return _delegate("sniffer.live", argv)


def _cmd_cells(args: argparse.Namespace) -> int:
    from sniffer import cells as _cells
    sub = args.cells_cmd

    if sub == "decode-neighbors":
        from sniffer.sib1 import decode_neighbors as _decode_neighbors
        from sniffer.scan import _log_neighbors
        seen: set[tuple[int, int]] = set()
        for c in _cells.load_all():
            if c.earfcn is not None and c.pci is not None:
                seen.add((c.earfcn, c.pci))
        if not seen:
            print("sniffer cells decode-neighbors: no cells with EARFCN+PCI in store")
            return 0
        sib_binary = os.environ.get("PDSCH_UE_BIN", "pdsch_ue")
        total = len(seen)
        found = 0
        for i, (earfcn, pci) in enumerate(sorted(seen), 1):
            print(f"[{i}/{total}] EARFCN={earfcn} PCI={pci} (30 s) ...",
                  end=" ", flush=True)
            neighbors = _decode_neighbors(earfcn, pci, binary=sib_binary)
            if neighbors:
                _log_neighbors(neighbors)
                found += len(neighbors)
                print(f"{len(neighbors)} neighbor(s)")
            else:
                print("none")
        print(f"done: {found} neighbor cell(s) discovered and logged.")
        return 0

    if sub == "import-opencellid":
        if not os.path.exists(args.csv_path):
            print(f"sniffer cells: file not found: {args.csv_path}",
                  file=sys.stderr)
            return 2
        read, written = _cells.import_opencellid(
            args.csv_path,
            mcc_filter=args.mcc if args.mcc is not None else None,
            radio_filter=args.radio,
        )
        print(f"opencellid: {read:,} rows read · {written:,} cells written "
              f"to {_cells.DEFAULT_STORE}")
        return 0

    if sub == "list":
        rows = list(_cells.load_all())
        if args.source:
            rows = [c for c in rows if c.source == args.source]
        if args.operator:
            rows = [c for c in rows
                    if (c.operator or "").lower() == args.operator.lower()]
        if args.limit:
            rows = rows[: args.limit]
        if args.jsonl:
            for c in rows:
                print(c.to_jsonl())
        else:
            _print_cells_table(rows)
        return 0

    if sub == "nearby":
        results = _cells.nearby(args.lat, args.lon,
                                radius_km=args.radius_km,
                                source=args.source)
        if args.json:
            payload = []
            for _d_km, c in results:
                sc = _cells.to_survey_cell_dict(c)
                if sc is not None:
                    payload.append(sc)
            print(json.dumps(payload, separators=(",", ":")))
        else:
            if not results:
                print(f"no cells within {args.radius_km} km of "
                      f"({args.lat:.4f}, {args.lon:.4f})")
                return 0
            print(f"# {len(results)} cell(s) within {args.radius_km} km:")
            for d_km, c in results:
                pci = "—" if c.pci is None else c.pci
                earfcn = "—" if c.earfcn is None else c.earfcn
                op = c.operator or "?"
                print(f"  {d_km*1000:6.0f} m  src={c.source:11s}  op={op:18s}"
                      f"  earfcn={earfcn}  pci={pci}")
        return 0

    if sub == "stats":
        s = _cells.stats()
        print(json.dumps(s, indent=2, sort_keys=True))
        return 0

    print(f"sniffer cells: unknown sub-command {sub!r}", file=sys.stderr)
    return 2


def _print_cells_table(rows: list) -> None:
    if not rows:
        print("# no cells in store")
        return
    print(f"# {len(rows)} cell(s)")
    print(f"{'SRC':12s} {'OP':18s} {'MCC/MNC':9s} {'EARFCN':>7s} "
          f"{'PCI':>4s} {'RSRP':>7s} {'LAT':>10s} {'LON':>10s}")
    for c in rows:
        op = (c.operator or "?")[:18]
        mm = (f"{c.mcc}/{c.mnc}" if c.mcc is not None and c.mnc is not None
              else "—")
        ef = "—" if c.earfcn is None else str(c.earfcn)
        pci = "—" if c.pci is None else str(c.pci)
        rsrp = "—" if c.rsrp_dbm is None else f"{c.rsrp_dbm:6.1f}"
        lat = "—" if c.lat is None else f"{c.lat:9.5f}"
        lon = "—" if c.lon is None else f"{c.lon:9.5f}"
        print(f"{c.source:12s} {op:18s} {mm:9s} {ef:>7s} {pci:>4s} "
              f"{rsrp:>7s} {lat:>10s} {lon:>10s}")


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
    pl.add_argument("--spectrum", action="store_true",
                    help="show a spectrum waterfall on the dashboard. With "
                         "--earfcn/--pci this tails FALCON's per-cell FFT "
                         "(no extra radio needed); with --simulate (or a "
                         "spare SDR plugged in) it runs a wideband sweep "
                         "via sniffer.uhd_sweep.")
    pl.add_argument("--spectrum-freq-mhz", default="700:2700",
                    help="sweep range as start:end MHz for the wideband "
                         "spectrum mode (default 700:2700; ignored when "
                         "--earfcn/--pci tails the FALCON SpectrumTap)")
    pl.add_argument("--spectrum-gain-db", type=float, default=60.0,
                    help="USRP RX gain for the wideband spectrum sweep "
                         "(default 60 dB)")
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
    ps.add_argument("--decode-neighbors", action="store_true",
                    help="after each cell, decode SIB3/SIB5 to discover "
                         "intrafreq (SIB3) and interfreq (SIB5) neighbor "
                         "cells; results are added to known_cells.jsonl "
                         "as source=sib-neighbor (adds ~30 s/cell)")
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
    psv.add_argument("--spectrum", action="store_true",
                     help="run a wideband UHD spectrum sweep alongside "
                          "the survey (needs a second idle SDR)")
    psv.add_argument("--spectrum-freq-mhz", default="700:2700",
                     help="sweep range as start:end MHz (default 700:2700)")
    psv.add_argument("--spectrum-gain-db", type=float, default=60.0,
                     help="USRP RX gain for spectrum sweep (default 60 dB)")
    psv.add_argument("--host", default="127.0.0.1")
    psv.add_argument("--port", type=int, default=8000)
    psv.add_argument("--out-dir", default="data")
    psv.add_argument("--mission-id", default=default_mid)
    psv.set_defaults(func=_cmd_survey)

    pi = sub.add_parser("install", help="install dependencies (Linux only)")
    pi.set_defaults(func=_cmd_install)

    pc = sub.add_parser("cells",
                        help="known-cell store (OpenCellID prior + log of "
                             "what we've actually found)",
                        description="Maintain data/known_cells.jsonl — the "
                                    "unified store of LTE cells we know "
                                    "about. Two upstream sources: an "
                                    "imported OpenCellID country dump (gives "
                                    "geographic prior, MCC/MNC/TAC/lat/lon) "
                                    "and any `sniffer scan` we've ever run "
                                    "(gives EARFCN/PCI/RSRP, no lat/lon "
                                    "unless GPS was running). Downstream: "
                                    "feed `sniffer survey --cells` and "
                                    "`sniffer live --earfcn/--pci`.")
    pc_sub = pc.add_subparsers(dest="cells_cmd", required=True)

    pci_ = pc_sub.add_parser("import-opencellid",
                             help="import an OpenCellID CSV dump")
    pci_.add_argument("csv_path",
                      help="path to the OpenCellID CSV (or .csv.gz). Get "
                           "one with an API key from "
                           "opencellid.org — country dumps are free.")
    pci_.add_argument("--mcc", type=int, default=425,
                      help="filter by MCC (default 425 = Israel; "
                           "pass 0 to import every country)")
    pci_.add_argument("--radio", default="LTE",
                      help="filter by radio type (default LTE; pass empty "
                           "string to import all radios)")
    pci_.set_defaults(func=_cmd_cells)

    pcl = pc_sub.add_parser("list", help="print all known cells")
    pcl.add_argument("--source", default=None,
                     choices=["opencellid", "scan", "live-lock", "sib-neighbor"],
                     help="filter by source")
    pcl.add_argument("--operator", default=None,
                     help="filter by operator name (case-insensitive)")
    pcl.add_argument("--limit", type=int, default=0,
                     help="cap output rows (default 0 = no cap)")
    pcl.add_argument("--jsonl", action="store_true",
                     help="emit JSONL instead of a table")
    pcl.set_defaults(func=_cmd_cells)

    pcn = pc_sub.add_parser("nearby",
                            help="cells within --radius-km of (--lat, --lon)")
    pcn.add_argument("--lat", type=float, required=True)
    pcn.add_argument("--lon", type=float, required=True)
    pcn.add_argument("--radius-km", type=float, default=5.0)
    pcn.add_argument("--source", default=None,
                     choices=["opencellid", "scan", "live-lock", "sib-neighbor"])
    pcn.add_argument("--json", action="store_true",
                     help="emit JSON in `sniffer survey --cells` shape "
                          "(only includes rows with both earfcn AND pci, "
                          "which is what tuning needs)")
    pcn.set_defaults(func=_cmd_cells)

    pcs = pc_sub.add_parser("stats", help="summary counts by source/operator")
    pcs.set_defaults(func=_cmd_cells)

    pcdn = pc_sub.add_parser(
        "decode-neighbors",
        help="run SIB3/SIB5 neighbor discovery on every cell in the store "
             "that has both EARFCN and PCI; results are logged as "
             "source=sib-neighbor (~30 s/cell)")
    pcdn.set_defaults(func=_cmd_cells)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
