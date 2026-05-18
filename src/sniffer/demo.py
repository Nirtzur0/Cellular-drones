"""End-to-end demo runner.

Drives the synthetic simulator through every stage of the production
pipeline: cellsearch + gpsd parsers, the geotag joiner, and the localizer.
Writes JSONL artifacts to a chosen directory and an optional PNG plot.

Run:
    python -m sniffer.demo --out-dir data/ --plot data/demo.png
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from argparse import Namespace

from pyproj import Geod

from sniffer import simulate
from sniffer.geotag import join
from sniffer.parse_cellsearch import parse_stream as parse_cellsearch_stream
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.report import make_plot, text_summary

GEOD = Geod(ellps="WGS84")


def _build_config(mission_id: str, scenario: str) -> simulate.SimulationConfig:
    emitter = simulate.Emitter(lat=32.0853, lon=34.7818, alt_m=25.0)
    if scenario == "box":
        wps = simulate.box_trajectory(emitter)
    elif scenario == "line":
        wps = simulate.line_trajectory(emitter)
    else:
        raise ValueError(f"unknown scenario {scenario}")
    return simulate.SimulationConfig(
        mission_id=mission_id,
        emitter=emitter,
        waypoints=wps,
    )


def _drive_cellsearch(cfg: simulate.SimulationConfig, out_path: str) -> int:
    """Stream simulated CellSearch lines through the production parser
    with an injected clock that walks the simulated mission timeline."""
    sim_clock_ns = {"value": 0}

    def clock_ns() -> int:
        return sim_clock_ns["value"]

    # The simulator yields blocks separated by blank lines. Bump the clock
    # just before the parser would flush each block — at the blank line.
    lines_iter = simulate.cellsearch_lines(cfg)
    args = Namespace(
        mission_id=cfg.mission_id,
        backend="lte-cell-scanner",
        device="hackrf-sim-0",
    )

    # Re-yield lines, mutating sim_clock_ns at sample boundaries so each
    # emitted block carries the correct simulated time.
    sample_period_ns = int(cfg.sample_period_s * 1e9)
    t_offset_start_ns = int(cfg.waypoints[0].t_offset_s * 1e9)

    def clocked_stream():
        sample_idx = 0
        in_block = False
        for line in lines_iter:
            if "Found LTE cell" in line:
                in_block = True
                # the parser flushes either at the next "Found LTE cell"
                # OR at a blank line — so we set the clock at block start.
                sim_clock_ns["value"] = t_offset_start_ns + sample_idx * sample_period_ns
                sample_idx += 1
            elif in_block and not line.strip():
                in_block = False
            yield line

    with open(out_path, "w", encoding="utf-8") as fh:
        return parse_cellsearch_stream(clocked_stream(), args, fh, clock_ns=clock_ns)


def _drive_gpsd(cfg: simulate.SimulationConfig, out_path: str) -> int:
    """Stream simulated gpsd JSON lines through the production parser
    with a clock injected to match each TPV's simulated time."""
    sim_clock_ns = {"value": 0}

    def clock_ns() -> int:
        return sim_clock_ns["value"]

    gps_period_ns = int(cfg.gps_period_s * 1e9)
    t_offset_start_ns = int(cfg.waypoints[0].t_offset_s * 1e9)

    def clocked_stream():
        for i, line in enumerate(simulate.gpsd_lines(cfg)):
            sim_clock_ns["value"] = t_offset_start_ns + i * gps_period_ns
            yield line

    with open(out_path, "w", encoding="utf-8") as fh:
        return parse_gpsd_stream(clocked_stream(), fh, cfg.mission_id,
                                 clock_ns=clock_ns)


def run(out_dir: str, mission_id: str = "demo-mission",
        scenario: str = "box", plot: str | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cfg = _build_config(mission_id, scenario)
    scan_path = os.path.join(out_dir, f"scan-{mission_id}.jsonl")
    gps_path = os.path.join(out_dir, f"gps-{mission_id}.jsonl")
    geotagged_path = os.path.join(out_dir, f"geotagged-{mission_id}.jsonl")

    n_cells = _drive_cellsearch(cfg, scan_path)
    n_gps = _drive_gpsd(cfg, gps_path)
    counts = join([scan_path], [gps_path], out_dir, max_age_ms=500)

    summary = text_summary(geotagged_path)
    print(summary)
    print()
    print(f"emitter ground truth: lat={cfg.emitter.lat} lon={cfg.emitter.lon} "
          f"alt={cfg.emitter.alt_m}")

    if plot:
        make_plot(geotagged_path, plot,
                  ground_truth_lat=cfg.emitter.lat,
                  ground_truth_lon=cfg.emitter.lon,
                  title=f"Demo mission ({scenario}): {mission_id}")
        print(f"plot: {plot}")

    return {
        "cellsearch_records": n_cells,
        "gpsd_records": n_gps,
        "geotagged_records": counts.get(mission_id, 0),
        "scan_path": scan_path,
        "gps_path": gps_path,
        "geotagged_path": geotagged_path,
        "emitter": cfg.emitter,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", default="data")
    p.add_argument("--mission-id", default="demo-mission")
    p.add_argument("--scenario", choices=("box", "line"), default="box")
    p.add_argument("--plot", default=None,
                   help="path to write PNG plot (optional)")
    args = p.parse_args()
    run(args.out_dir, args.mission_id, args.scenario, args.plot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
