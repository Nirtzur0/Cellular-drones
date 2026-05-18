"""End-to-end UE pipeline test.

Drives the simulator through every production stage:
    simulate → LTESniffer parser → ue-*.jsonl
    simulate → gpsd parser       → gps-*.jsonl
    geotag joiner                → geotagged-*.jsonl
    localizer (per-RNTI)         → estimated UE positions

Asserts that the recovered position of each stationary UE is within
tolerance of the synthetic ground truth.
"""

from __future__ import annotations

import tempfile

from pyproj import Geod

from sniffer import demo
from sniffer.localize import locate_file

GEOD = Geod(ellps="WGS84")


def _haversine_m(lat1, lon1, lat2, lon2):
    _, _, d = GEOD.inv(lon1, lat1, lon2, lat2)
    return d


def test_e2e_ue_pipeline_localizes_stationary_ues():
    """Two of the three synthetic UEs are stationary; the third walks
    across the cell. We only assert tight bounds for the stationary pair —
    the mobile UE is expected to bias by ~200 m, the known limit of
    single-RX passive positioning.
    """
    with tempfile.TemporaryDirectory() as td:
        result = demo.run(out_dir=td, mission_id="t-ues", scenario="box")
        assert result["ue_records"] > 0
        assert result["gpsd_records"] > 0
        assert result["geotagged_records"] > 0

        results = locate_file(result["geotagged_path"], "centroid")
        assert len(results) == 3
        by_rnti = {r.c_rnti: r for r in results}
        truth_by_rnti = {ue.c_rnti: ue for ue in result["ues"]}

        for rnti in (0x4ad2, 0x73a1):  # stationary UEs
            est = by_rnti[rnti]
            truth = truth_by_rnti[rnti]
            err = _haversine_m(truth.lat, truth.lon, est.lat, est.lon)
            assert err < 100.0, (
                f"stationary UE {rnti:#06x} error {err:.1f} m too large"
            )

        # Mobile UE: confirm the pipeline produced a result on the correct
        # side of the world (< 500 m), without asserting accuracy.
        est = by_rnti[0x91ff]
        truth = truth_by_rnti[0x91ff]
        err = _haversine_m(truth.lat, truth.lon, est.lat, est.lon)
        assert err < 500.0
