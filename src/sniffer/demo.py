"""End-to-end demo runner.

Drives the synthetic LTESniffer + gpsd streams through every stage of the
production pipeline:

    simulator → parse_ltesniffer → ue-*.jsonl
    simulator → parse_gpsd       → gps-*.jsonl
    geotag joiner                → geotagged-*.jsonl
    localizer (per-RNTI)         → estimated UE locations
    report + plot                → text summary + PNG

Run:
    python -m sniffer.demo --out-dir data/demo --plot data/demo/ues.png
"""

from __future__ import annotations

import argparse
import os
import re
from argparse import Namespace

from sniffer import simulate
from sniffer.geotag import join
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.parse_ltesniffer import parse_stream as parse_ltesniffer_stream
from sniffer.report import make_plot, text_summary


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


def _drive_gpsd(cfg: simulate.SimulationConfig, out_path: str) -> int:
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


_TICK_RE = re.compile(r"^#\s*TICK\s+t=([-\d.]+)")


def _drive_ltesniffer(cfg: simulate.SimulationConfig, out_path: str) -> int:
    """Stream simulated LTESniffer DECODED lines through the parser.

    The simulator emits `# TICK t=...` comment markers on every tick; we
    intercept them to advance the simulated mono_ns clock so each emitted
    UE record carries the right timestamp for the GPS join.
    """
    sim_clock_ns = {"value": 0}

    def clock_ns() -> int:
        return sim_clock_ns["value"]

    args = Namespace(
        mission_id=cfg.mission_id,
        backend="ltesniffer",
        device="usrp-b210-sim-0",
        center_hz=cfg.emitter.center_hz,
        sample_rate_sps=23.04e6,
        rx_gain_db=50.0,
    )

    def clocked_stream():
        for line in simulate.ltesniffer_lines(cfg):
            m = _TICK_RE.match(line)
            if m:
                sim_clock_ns["value"] = int(float(m.group(1)) * 1e9)
                continue
            yield line

    with open(out_path, "w", encoding="utf-8") as fh:
        return parse_ltesniffer_stream(clocked_stream(), args, fh,
                                       clock_ns=clock_ns)


def run(out_dir: str, mission_id: str = "demo-mission",
        scenario: str = "box", plot: str | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cfg = _build_config(mission_id, scenario)

    gps_path = os.path.join(out_dir, f"gps-{mission_id}.jsonl")
    ue_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")

    n_gps = _drive_gpsd(cfg, gps_path)
    n_ues = _drive_ltesniffer(cfg, ue_path)
    counts = join([ue_path], [gps_path], out_dir, max_age_ms=500)
    geotagged_path = os.path.join(out_dir, f"geotagged-{mission_id}.jsonl")

    ues = simulate._default_ues(cfg.emitter)
    print(text_summary(geotagged_path, default_ues=ues))

    if plot:
        make_plot(geotagged_path, plot, truth_ues=ues,
                  title=f"Demo mission ({scenario}): UEs · {mission_id}")
        print(f"plot: {plot}")

    return {
        "ue_records": n_ues,
        "gpsd_records": n_gps,
        "geotagged_records": counts.get(mission_id, 0),
        "ue_path": ue_path,
        "gps_path": gps_path,
        "geotagged_path": geotagged_path,
        "emitter": cfg.emitter,
        "ues": ues,
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
