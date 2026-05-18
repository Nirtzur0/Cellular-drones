import math
import random

from pyproj import Geod

from sniffer.localize import path_loss_wls_ue, weighted_centroid_ue

GEOD = Geod(ellps="WGS84")


def _sample(lat, lon, alt, ul_rssi, pci=271, c_rnti=0x4ad2):
    """Build a UE-sighting record with a UL grant — the only kind the
    localizer consumes."""
    return {
        "kind": "ue_sighting",
        "gps": {"lat": lat, "lon": lon, "alt_m": alt, "fix": "3d"},
        "ue": {
            "pci": pci,
            "c_rnti": c_rnti,
            "direction": "ul",
            "ul_rssi_dbm": ul_rssi,
        },
    }


def _free_space_rsrp(true_lat, true_lon, true_alt, lat, lon, alt,
                     p_tx=-20.0, n=2.0):
    _, _, d = GEOD.inv(true_lon, true_lat, lon, lat)
    d = math.sqrt(d * d + (alt - true_alt) ** 2)
    if d < 1.0:
        d = 1.0
    return p_tx - 10 * n * math.log10(d)


def _trajectory_around(true_lat, true_lon, true_alt, radius_m=80.0,
                       altitudes=(15.0, 30.0, 60.0), per_alt=12, n=3.0,
                       sigma_db=1.5, seed=0):
    rng = random.Random(seed)
    records = []
    for z in altitudes:
        for i in range(per_alt):
            theta = 2 * math.pi * i / per_alt
            dx = radius_m * math.cos(theta)
            dy = radius_m * math.sin(theta)
            lat = true_lat + (dy / 111_320.0)
            lon = true_lon + (dx / (111_320.0 * math.cos(math.radians(true_lat))))
            rsrp = _free_space_rsrp(true_lat, true_lon, true_alt,
                                    lat, lon, z, n=n)
            rsrp += rng.gauss(0.0, sigma_db)
            records.append(_sample(lat, lon, z, rsrp))
    return records


def test_weighted_centroid_ue_recovers_within_50m():
    true_lat, true_lon, true_alt = 32.0853, 34.7818, 25.0
    records = _trajectory_around(true_lat, true_lon, true_alt)
    r = weighted_centroid_ue(records, k=4.0)
    assert r is not None
    _, _, err = GEOD.inv(true_lon, true_lat, r.lon, r.lat)
    assert err < 50.0, f"centroid error {err:.1f}m too large"


def _box_trajectory(true_lat, true_lon, true_alt, half_size_m=150.0,
                    n_per_side=15, altitudes=(15.0, 30.0, 60.0),
                    n=3.0, sigma_db=0.5, seed=1):
    """Box pattern around the UE at multiple altitudes — surrounds the
    target so the WLS Jacobian is well-conditioned in all 3 axes."""
    rng = random.Random(seed)
    records = []
    for z in altitudes:
        for side_idx in range(4):
            for i in range(n_per_side):
                t = -half_size_m + 2 * half_size_m * i / max(1, n_per_side - 1)
                if side_idx == 0:    # north edge
                    dx, dy = t,  half_size_m
                elif side_idx == 1:  # east edge
                    dx, dy =  half_size_m, -t
                elif side_idx == 2:  # south edge
                    dx, dy = -t, -half_size_m
                else:                # west edge
                    dx, dy = -half_size_m,  t
                lat = true_lat + (dy / 111_320.0)
                lon = true_lon + (dx / (111_320.0 * math.cos(math.radians(true_lat))))
                rsrp = _free_space_rsrp(true_lat, true_lon, true_alt,
                                        lat, lon, z, n=n)
                rsrp += rng.gauss(0.0, sigma_db)
                records.append(_sample(lat, lon, z, rsrp))
    return records


def test_path_loss_wls_ue_recovers_under_good_geometry():
    true_lat, true_lon, true_alt = 32.0853, 34.7818, 25.0
    records = _box_trajectory(true_lat, true_lon, true_alt)
    w = path_loss_wls_ue(records, n_path_loss=3.0)
    assert w is not None
    assert math.isfinite(w.lat) and math.isfinite(w.lon)
    _, _, err_w = GEOD.inv(true_lon, true_lat, w.lon, w.lat)
    assert err_w < 50.0, f"WLS error {err_w:.1f}m too large"


def test_altitude_refused_when_trajectory_is_flat():
    true_lat, true_lon = 32.0853, 34.7818
    records = _trajectory_around(true_lat, true_lon, 30.0,
                                 altitudes=(30.0,), per_alt=20)
    r = weighted_centroid_ue(records)
    assert r is not None
    assert r.altitude_estimated is False
    assert r.alt_m is None
    assert "altitude not estimated" in r.notes


def test_dl_only_records_yield_no_estimate():
    """A UE we only hear on DL grants is unlocalizable — every grant
    carries the eNB's energy, not the UE's. The localizer must refuse."""
    records = _trajectory_around(32.0853, 34.7818, 25.0)
    for r in records:
        r["ue"]["direction"] = "dl"
        r["ue"]["dl_rsrp_dbm"] = r["ue"].pop("ul_rssi_dbm")
    assert weighted_centroid_ue(records) is None
    assert path_loss_wls_ue(records) is None
