"""End-to-end pipeline test.

Drives the simulator through the same modules the live dashboard uses
(`simulate.ltesniffer_lines` → `parse_ltesniffer.parse_stream` →
JSONL → `weighted_centroid_ue`) and asserts that stationary UEs
localize to within tolerance of the synthetic ground truth.

This is the live path, not a separate batch path — there is no
geotag / report / demo module in between.
"""

from __future__ import annotations

import io
import json
import re
from argparse import Namespace
from collections import defaultdict

from pyproj import Geod

from sniffer import simulate
from sniffer.localize import weighted_centroid_ue
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.parse_ltesniffer import parse_stream as parse_ltesniffer_stream
from sniffer.ta_multilateration import ta_multilateration_ue

GEOD = Geod(ellps="WGS84")
_TICK_RE = re.compile(r"^#\s*TICK\s+t=([-\d.]+)")


class _JsonlBuf(io.TextIOBase):
    """Sink that decodes JSONL lines and appends them to a list."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line:
                self.records.append(json.loads(line))
        return len(s)


def _run_pipeline(cfg: simulate.SimulationConfig) -> list[dict]:
    """Drive the simulator through the live parsers; return joined records.

    Uses a shared simulated clock so UE-sighting and GPS timestamps
    line up, then performs the same nearest-neighbour join (within
    500 ms) that `live.State` performs in-process.
    """
    sim_clock_ns = {"value": 0}

    def clock_ns() -> int:
        return sim_clock_ns["value"]

    # GPS: the generator advances simulated time by gps_period_s per yield.
    gps_buf = _JsonlBuf()
    gps_period_ns = int(cfg.gps_period_s * 1e9)
    t_start_ns = int(cfg.waypoints[0].t_offset_s * 1e9)

    def gps_clocked():
        for i, line in enumerate(simulate.gpsd_lines(cfg)):
            sim_clock_ns["value"] = t_start_ns + i * gps_period_ns
            yield line

    parse_gpsd_stream(gps_clocked(), gps_buf, cfg.mission_id,
                      clock_ns=clock_ns)

    # UE: `# TICK t=X` markers carry simulated time; we drain them onto
    # sim_clock so each DECODED line is timestamped at its emission time.
    ue_buf = _JsonlBuf()

    def ue_clocked():
        for line in simulate.ltesniffer_lines(cfg):
            m = _TICK_RE.match(line)
            if m:
                sim_clock_ns["value"] = int(float(m.group(1)) * 1e9)
                continue
            yield line

    args = Namespace(mission_id=cfg.mission_id, backend="sim",
                     device="sim", center_hz=cfg.emitter.center_hz,
                     sample_rate_sps=23.04e6, rx_gain_db=0.0)
    parse_ltesniffer_stream(ue_clocked(), args, ue_buf, clock_ns=clock_ns)

    # Join: for each UE sighting, attach the nearest GPS fix within 500 ms.
    # This is the same rule `live.State._nearest_gps_locked` enforces.
    gps_by_ts = sorted(
        (g["ts_mono_ns"], g["gps"]) for g in gps_buf.records
    )
    if not gps_by_ts:
        return []
    out: list[dict] = []
    for r in ue_buf.records:
        ts = r["ts_mono_ns"]
        nearest = min(gps_by_ts, key=lambda g: abs(g[0] - ts))
        if abs(nearest[0] - ts) > 500_000_000:
            continue
        out.append({"kind": "ue_sighting", "gps": nearest[1], "ue": r["ue"]})
    return out


def test_e2e_pipeline_localizes_stationary_ues() -> None:
    """Two of the three synthetic UEs are stationary; the third walks
    across the cell. Stationary pair must localize tightly. The mobile
    UE is allowed up to 500 m bias — the known limit of single-RX
    passive positioning, staged on purpose in the simulator.
    """
    emitter = simulate.Emitter(lat=32.0853, lon=34.7818, alt_m=25.0)
    cfg = simulate.SimulationConfig(
        mission_id="t-ues",
        emitter=emitter,
        waypoints=simulate.box_trajectory(emitter),
    )
    cfg.ues = simulate._default_ues(emitter)

    joined = _run_pipeline(cfg)
    assert joined, "pipeline produced no joined records"

    by_rnti: dict[int, list[dict]] = defaultdict(list)
    for r in joined:
        if r["ue"]["direction"] == "ul":
            by_rnti[int(r["ue"]["c_rnti"])].append(r)

    truth_by_rnti = {ue.c_rnti: ue for ue in cfg.ues}

    for rnti in (0x4ad2, 0x73a1):  # stationary
        recs = by_rnti.get(rnti, [])
        assert len(recs) >= 2, (
            f"need ≥2 UL grants for stationary UE {rnti:#06x}, got {len(recs)}"
        )
        cent = weighted_centroid_ue(recs)
        assert cent is not None
        truth = truth_by_rnti[rnti]
        _, _, err = GEOD.inv(truth.lon, truth.lat, cent.lon, cent.lat)
        assert err < 100.0, (
            f"stationary UE {rnti:#06x} centroid error {err:.1f} m too large"
        )

        # TA multilateration runs alongside the centroid (independent estimator,
        # not a fallback). For stationary UEs with the simulator's range-banded
        # TA, it should beat the centroid comfortably.
        ta = ta_multilateration_ue(recs)
        assert ta is not None, (
            f"TA multilateration refused on stationary UE {rnti:#06x} "
            f"(got {len(recs)} UL records, all with ta_meters)"
        )
        _, _, ta_err = GEOD.inv(truth.lon, truth.lat, ta.lon, ta.lat)
        assert ta_err < 50.0, (
            f"stationary UE {rnti:#06x} TA error {ta_err:.1f} m too large"
        )

    # Mobile UE: produces a result, but bias is allowed.
    recs = by_rnti.get(0x91ff, [])
    if len(recs) >= 2:
        cent = weighted_centroid_ue(recs)
        if cent is not None:
            truth = truth_by_rnti[0x91ff]
            _, _, err = GEOD.inv(truth.lon, truth.lat, cent.lon, cent.lat)
            assert err < 500.0
