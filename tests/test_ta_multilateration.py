"""Tests for the TA-multilateration localizer."""
from __future__ import annotations

import math
import random

from pyproj import Geod

from sniffer.ta_multilateration import (
    DEFAULT_SIGMA_M,
    MIN_ANCHORS,
    TA_STEP_METERS,
    ta_multilateration_ue,
)

GEOD = Geod(ellps="WGS84")


def _rec(lat: float, lon: float, alt: float, ta_meters: float) -> dict:
    return {
        "gps": {"lat": lat, "lon": lon, "alt_m": alt, "fix": "3d"},
        "ue": {"pci": 271, "c_rnti": 0x4ad2,
               "direction": "ul", "ul_rssi_dbm": -80.0,
               "ta_meters": ta_meters,
               "ta_n_steps": int(round(ta_meters / TA_STEP_METERS))},
    }


def _quantize_ta(d_one_way_m: float, sigma_jitter: float = 5.0,
                 rng: random.Random | None = None) -> float:
    """Realistic TA observation: distance quantised to one step + a bit of
    jitter from SDR timing imperfection."""
    n_steps = round(d_one_way_m / TA_STEP_METERS)
    base = n_steps * TA_STEP_METERS
    if rng is not None:
        base += rng.gauss(0.0, sigma_jitter)
    return max(0.0, base)


def _records_around(ue_lat: float, ue_lon: float, ue_alt: float,
                    n: int = 6, radius_m: float = 200.0,
                    altitudes=(30.0,), seed: int = 0) -> list[dict]:
    """Drone flies a spiral around the UE at one or more altitudes;
    each waypoint produces one TA-bearing UL grant. Varying radius
    matters: an exact circle gives every anchor the same TA step and
    multilateration can't tell points on the circle apart."""
    rng = random.Random(seed)
    records: list[dict] = []
    for z in altitudes:
        for i in range(n):
            theta = 2 * math.pi * i / n + 0.4   # offset so no symmetric ambiguities
            # Spiral the radius from 0.5× to 1.5× the nominal — gives
            # distance diversity across anchors, which is what TA needs.
            r = radius_m * (0.5 + i / max(1, n - 1))
            dx = r * math.cos(theta)
            dy = r * math.sin(theta)
            lat = ue_lat + (dy / 111_320.0)
            lon = ue_lon + (dx / (111_320.0 * math.cos(math.radians(ue_lat))))
            _, _, d2 = GEOD.inv(ue_lon, ue_lat, lon, lat)
            d_one_way = math.sqrt(d2 * d2 + (z - ue_alt) ** 2)
            records.append(_rec(lat, lon, z, _quantize_ta(d_one_way, rng=rng)))
    return records


def test_recovers_stationary_ue_within_30m():
    ue_lat, ue_lon, ue_alt = 32.0853, 34.7818, 5.0
    records = _records_around(ue_lat, ue_lon, ue_alt,
                              n=8, radius_m=150.0, seed=1)
    r = ta_multilateration_ue(records, sigma_m=DEFAULT_SIGMA_M)
    assert r is not None
    _, _, err = GEOD.inv(ue_lon, ue_lat, r.lon, r.lat)
    # With 8 well-spread anchors and σ_TA ~ TA_STEP/√12 + jitter,
    # the fit should comfortably land inside one TA step.
    assert err < TA_STEP_METERS, f"recovered {err:.1f} m off (one TA step is {TA_STEP_METERS:.1f} m)"
    assert r.cep95_m < 80.0


def test_returns_none_when_too_few_anchors():
    records = _records_around(32.0853, 34.7818, 5.0,
                              n=MIN_ANCHORS - 1)
    assert ta_multilateration_ue(records) is None


def test_returns_none_when_drone_trajectory_is_collinear():
    """Drone flies in a straight line — TA hyperbolas degenerate to
    a pair of mirror solutions; we refuse rather than pick a side."""
    rng = random.Random(2)
    records: list[dict] = []
    ue_lat, ue_lon = 32.0853, 34.7818
    # Drone flies east-west at constant latitude, UE is south of the line.
    for i in range(8):
        lon = ue_lon - 0.0008 + 2e-4 * i
        lat = 32.0858                      # 50 m north of UE
        d_xy = GEOD.inv(ue_lon, ue_lat, lon, lat)[2]
        records.append(_rec(lat, lon, 30.0, _quantize_ta(d_xy, rng=rng)))
    assert ta_multilateration_ue(records) is None


def test_returns_none_when_all_ta_identical():
    """UE on a single circle around the drone trajectory's pivot point →
    no information to pick a point on that circle."""
    rng = random.Random(3)
    records: list[dict] = []
    ue_lat, ue_lon = 32.0853, 34.7818
    # 8 drone positions evenly around the UE at *exactly* the same radius.
    for i in range(8):
        theta = 2 * math.pi * i / 8
        dx = 200.0 * math.cos(theta)
        dy = 200.0 * math.sin(theta)
        lat = ue_lat + dy / 111_320.0
        lon = ue_lon + dx / (111_320.0 * math.cos(math.radians(ue_lat)))
        # Every anchor sees the same nominal range → ambiguous.
        records.append(_rec(lat, lon, 30.0, _quantize_ta(200.0, rng=rng)))
    assert ta_multilateration_ue(records) is None


def test_rejects_records_without_ta():
    """Records that lack ta_meters (e.g. RSSI-only) are filtered out;
    if the remainder is below MIN_ANCHORS the fit refuses."""
    records = _records_around(32.0853, 34.7818, 5.0,
                              n=MIN_ANCHORS + 2)
    # Strip TA from all but two records.
    for r in records[:-2]:
        r["ue"]["ta_meters"] = None
    assert ta_multilateration_ue(records) is None


def test_outlier_one_anchor_does_not_swing_estimate():
    """One bad anchor with TA off by 10 steps shouldn't ruin the fit
    when the other anchors agree (Huber loss kicks in)."""
    ue_lat, ue_lon, ue_alt = 32.0853, 34.7818, 5.0
    records = _records_around(ue_lat, ue_lon, ue_alt,
                              n=8, radius_m=150.0, seed=4)
    records[0]["ue"]["ta_meters"] += 10 * TA_STEP_METERS  # ~780 m blunder
    r = ta_multilateration_ue(records)
    assert r is not None
    _, _, err = GEOD.inv(ue_lon, ue_lat, r.lon, r.lat)
    assert err < 2.5 * TA_STEP_METERS, f"outlier dragged the fit {err:.1f} m"
