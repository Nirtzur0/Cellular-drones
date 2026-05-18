"""Tests for the mobility classifier."""
from __future__ import annotations

import math
import random

from pyproj import Geod

from sniffer.motion import (
    MIN_SAMPLES,
    classify_motion,
    MOBILE_R,
    STATIONARY_R,
)


GEOD = Geod(ellps="WGS84")


def _rec(lat: float, lon: float, rssi: float) -> dict:
    return {
        "gps": {"lat": lat, "lon": lon, "alt_m": 30.0, "fix": "3d"},
        "ue": {"direction": "ul", "ul_rssi_dbm": rssi},
    }


def _rssi_at(ue_lat: float, ue_lon: float,
             drone_lat: float, drone_lon: float,
             p_tx: float = -20.0, n: float = 3.0) -> float:
    _, _, d = GEOD.inv(ue_lon, ue_lat, drone_lon, drone_lat)
    d = max(1.0, d)
    return p_tx - 10 * n * math.log10(d)


def test_classify_stationary_ue_from_circular_pass():
    """Stationary UE + drone flying a circle around it → high correlation."""
    rng = random.Random(0)
    ue_lat, ue_lon = 32.0853, 34.7818
    records = []
    radius_m = 80.0
    n_points = 24
    for i in range(n_points):
        theta = 2 * math.pi * i / n_points
        dx = radius_m * math.cos(theta)
        dy = radius_m * math.sin(theta) * 0.6   # ellipse, not a circle, so
                                                # distances vary
        lat = ue_lat + (dy / 111_320.0)
        lon = ue_lon + (dx / (111_320.0 * math.cos(math.radians(ue_lat))))
        rssi = _rssi_at(ue_lat, ue_lon, lat, lon) + rng.gauss(0, 1.0)
        records.append(_rec(lat, lon, rssi))

    # Centroid is close to truth for a well-spread trajectory.
    r = classify_motion(records, ue_lat, ue_lon)
    assert r.label == "stationary"
    assert r.corr is not None and r.corr > STATIONARY_R
    assert r.n_samples == n_points


def test_classify_mobile_ue_walking_with_drone():
    """UE walks ~parallel to the drone → distance and RSSI are decoupled."""
    rng = random.Random(1)
    # Drone flies west→east along a constant latitude
    drone_lat = 32.0853
    drone_lon_start = 34.7818
    # UE walks west→east starting just south of the drone, same speed.
    # Drone-to-UE distance stays ~constant, RSSI stays ~constant → no
    # signal that the UE is anywhere in particular near the drone path.
    ue_lat = 32.0853 - 0.0004        # ~45 m south of drone track
    ue_lon = drone_lon_start
    records = []
    for i in range(24):
        drone_lon = drone_lon_start + i * 5e-5
        ue_lon += 5e-5                # UE matches drone velocity
        rssi = _rssi_at(ue_lat, ue_lon, drone_lat, drone_lon)
        rssi += rng.gauss(0, 0.5)
        records.append(_rec(drone_lat, drone_lon, rssi))

    # If we naïvely centroid this, we get something on the drone track;
    # use that as the reference, exactly like live.py does.
    ref_lat = sum(r["gps"]["lat"] for r in records) / len(records)
    ref_lon = sum(r["gps"]["lon"] for r in records) / len(records)
    r = classify_motion(records, ref_lat, ref_lon)
    assert r.label == "mobile", f"got {r}"
    assert r.corr is not None and r.corr < MOBILE_R


def test_classify_too_few_samples_is_indeterminate():
    records = [_rec(32.085, 34.781, -80.0) for _ in range(MIN_SAMPLES - 1)]
    r = classify_motion(records, 32.085, 34.781)
    assert r.label == "indeterminate"
    assert r.corr is None
    assert "≥ 5" in r.note or "have" in r.note


def test_classify_drops_records_without_ul_rssi():
    """Records missing ul_rssi_dbm (e.g. DL grants) are skipped."""
    records = []
    for i in range(6):
        rec = _rec(32.085 + 1e-4 * i, 34.781, -80.0)
        records.append(rec)
    records[0]["ue"]["ul_rssi_dbm"] = None
    records[1]["ue"]["ul_rssi_dbm"] = None
    r = classify_motion(records, 32.0853, 34.7818)
    # Four remaining is < MIN_SAMPLES → indeterminate.
    assert r.label == "indeterminate"
    assert r.n_samples == 4


def test_classify_flat_trajectory_is_indeterminate():
    """All drone fixes at the same point → log_d variance ≈ 0."""
    records = [_rec(32.085, 34.781, -80.0 + 0.01 * i)
               for i in range(MIN_SAMPLES + 2)]
    r = classify_motion(records, 32.0853, 34.7818)
    assert r.label == "indeterminate"
    assert "trajectory too tight" in r.note
