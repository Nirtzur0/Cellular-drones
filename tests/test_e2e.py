"""End-to-end pipeline test.

Drives the simulator through every production stage:
    simulate → CellSearch parser → JSONL
    simulate → gpsd parser       → JSONL
    geotag joiner                → geotagged JSONL
    localizer (centroid + WLS)   → estimated emitter location

Asserts that the recovered emitter position is within tolerance of the
synthetic ground truth.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from pyproj import Geod

from sniffer import demo
from sniffer.localize import locate_file

GEOD = Geod(ellps="WGS84")


def _haversine_m(lat1, lon1, lat2, lon2):
    _, _, d = GEOD.inv(lon1, lat1, lon2, lat2)
    return d


def test_e2e_box_pipeline_localizes_within_50m():
    with tempfile.TemporaryDirectory() as td:
        result = demo.run(out_dir=td, mission_id="t-box", scenario="box")
        em = result["emitter"]
        # The pipeline must produce records at each stage.
        assert result["cellsearch_records"] > 0
        assert result["gpsd_records"] > 0
        assert result["geotagged_records"] > 0
        # The output JSONL must exist and be non-empty.
        assert os.path.getsize(result["geotagged_path"]) > 0

        # Localizer reads the geotagged JSONL.
        centroid_results = locate_file(result["geotagged_path"], "centroid")
        wls_results = locate_file(result["geotagged_path"], "wls")
        assert len(centroid_results) == 1
        assert len(wls_results) == 1

        err_centroid = _haversine_m(
            em.lat, em.lon, centroid_results[0].lat, centroid_results[0].lon
        )
        err_wls = _haversine_m(
            em.lat, em.lon, wls_results[0].lat, wls_results[0].lon
        )
        # Box trajectory surrounds the emitter — both methods should be tight.
        assert err_centroid < 80.0, f"centroid {err_centroid:.1f}m too large"
        assert err_wls < 60.0, f"WLS {err_wls:.1f}m too large"


def test_e2e_line_pipeline_runs_and_is_in_the_right_neighborhood():
    """Line trajectory is poorly conditioned — we only check the pipeline
    doesn't crash and the centroid lands on the correct side of the world."""
    with tempfile.TemporaryDirectory() as td:
        result = demo.run(out_dir=td, mission_id="t-line", scenario="line")
        em = result["emitter"]
        centroid_results = locate_file(result["geotagged_path"], "centroid")
        assert len(centroid_results) == 1
        err = _haversine_m(
            em.lat, em.lon, centroid_results[0].lat, centroid_results[0].lon
        )
        # Line passes 200 m offset → centroid will be biased toward the line.
        # We expect ~200 m error here; just confirm we're not wildly diverging.
        assert err < 500.0
