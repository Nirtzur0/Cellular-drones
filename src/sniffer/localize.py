"""RSSI-based emitter localization from a single moving receiver.

Implements the methods described in docs/localization.md. Coordinate
math is done in a local ENU frame anchored at the trajectory centroid;
results are converted back to WGS84 at the end.

Methods:
  weighted_centroid  — robust day-1 baseline.
  path_loss_wls      — weighted least squares against a log-distance model.
  synthetic_aperture_aoa — stub; needs CSI.

CLI:
  python -m sniffer.localize <geotagged-*.jsonl> [--method centroid|wls]
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from pyproj import Geod
from scipy.optimize import least_squares

from sniffer.schema import read_jsonl

GEOD = Geod(ellps="WGS84")

# WGS84 semi-major axis. Good enough for the local-tangent-plane math we
# do here at <1 km scales (sub-meter error vs full ellipsoidal transform).
_R_EARTH_M = 6_378_137.0

# Altitude-diversity threshold below which we refuse to estimate z.
MIN_ALT_STDDEV_M = 5.0


def _ll_to_enu(lats, lons, alts, lat0, lon0, alt0=0.0):
    """Local-tangent-plane ENU conversion. Accurate to ~0.1 m at 1 km."""
    lat0r = math.radians(lat0)
    dlat = np.radians(np.asarray(lats) - lat0)
    dlon = np.radians(np.asarray(lons) - lon0)
    x = dlon * _R_EARTH_M * math.cos(lat0r)
    y = dlat * _R_EARTH_M
    z = np.asarray(alts) - alt0
    return x, y, z


def _enu_to_ll(x, y, z, lat0, lon0, alt0=0.0):
    lat0r = math.radians(lat0)
    lat = lat0 + math.degrees(y / _R_EARTH_M)
    lon = lon0 + math.degrees(x / (_R_EARTH_M * math.cos(lat0r)))
    return lat, lon, z + alt0


@dataclass
class LocalizationResult:
    pci: int
    n_samples: int
    method: str
    lat: float
    lon: float
    alt_m: float | None
    cov_xy_m2: float
    cep95_m: float
    altitude_estimated: bool
    notes: str = ""


def _rsrp_to_linear(rsrp_dbm: float) -> float:
    return 10 ** (rsrp_dbm / 10.0)


def _group_by_pci(records: list[dict]) -> dict[int, list[dict]]:
    by_pci: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("kind") != "cell_sighting":
            continue
        if r.get("gps") is None:
            continue
        cell = r["cell"]
        if cell.get("rsrp_dbm") is None:
            continue
        by_pci[int(cell["pci"])].append(r)
    return by_pci


def weighted_centroid(records: list[dict], k: float = 2.0) -> LocalizationResult | None:
    if len(records) < 2:
        return None
    lats = np.array([r["gps"]["lat"] for r in records])
    lons = np.array([r["gps"]["lon"] for r in records])
    alts = np.array([r["gps"]["alt_m"] for r in records])
    rsrp = np.array([r["cell"]["rsrp_dbm"] for r in records])
    weights = np.array([_rsrp_to_linear(v) ** (k / 2.0) for v in rsrp])
    weights /= weights.sum()
    lat_hat = float(np.sum(weights * lats))
    lon_hat = float(np.sum(weights * lons))
    alt_var = float(np.var(alts))
    altitude_estimated = math.sqrt(alt_var) >= MIN_ALT_STDDEV_M
    alt_hat = float(np.sum(weights * alts)) if altitude_estimated else None
    # crude covariance from sample dispersion (m^2) in local ENU
    xs, ys, _ = _ll_to_enu(lats, lons, alts, lat_hat, lon_hat)
    var_xy = float(np.var(xs) + np.var(ys))
    cep95 = 2.45 * math.sqrt(var_xy / len(records))  # ~95% CEP for 2D normal
    return LocalizationResult(
        pci=int(records[0]["cell"]["pci"]),
        n_samples=len(records),
        method="weighted_centroid",
        lat=lat_hat,
        lon=lon_hat,
        alt_m=alt_hat,
        cov_xy_m2=var_xy,
        cep95_m=cep95,
        altitude_estimated=altitude_estimated,
        notes="" if altitude_estimated else (
            f"altitude not estimated: trajectory alt stddev "
            f"{math.sqrt(alt_var):.1f}m < {MIN_ALT_STDDEV_M}m"
        ),
    )


def path_loss_wls(records: list[dict], n_path_loss: float = 3.0,
                  d0_m: float = 1.0) -> LocalizationResult | None:
    """Weighted least squares against log-distance path loss.

    Solves for emitter (x_e, y_e, z_e) given measurements (x_i, y_i, z_i, RSRP_i)
    by linearizing the path-loss equation around an initial guess from the
    weighted centroid and iterating Gauss-Newton a few times.
    """
    init = weighted_centroid(records)
    if init is None:
        return None
    lons = np.array([r["gps"]["lon"] for r in records])
    lats = np.array([r["gps"]["lat"] for r in records])
    alts = np.array([r["gps"]["alt_m"] for r in records])
    xs, ys, zs = _ll_to_enu(lats, lons, alts, init.lat, init.lon)
    xs = np.asarray(xs); ys = np.asarray(ys); zs = np.asarray(zs)
    rsrp = np.array([r["cell"]["rsrp_dbm"] for r in records])
    # Normalize: define path loss relative to first sample to drop P_tx.
    idx0 = int(np.argmax(rsrp))  # anchor on strongest sample
    dl = rsrp[idx0] - rsrp  # positive for samples weaker than anchor
    estimate_z = init.altitude_estimated
    z_fixed = float(np.mean(zs))

    def residuals(params):
        if estimate_z:
            px, py, pz = params
        else:
            px, py = params
            pz = z_fixed
        dx_ = xs - px; dy_ = ys - py; dz_ = zs - pz
        d_ = np.sqrt(dx_ * dx_ + dy_ * dy_ + dz_ * dz_) + 1e-3
        d_a = math.sqrt(dx_[idx0] ** 2 + dy_[idx0] ** 2 + dz_[idx0] ** 2) + 1e-3
        predicted = 10.0 * n_path_loss * np.log10(d_ / d_a)
        return dl - predicted

    # Trust-region bounds: keep the search within ~5 km of the trajectory
    # centroid (the local ENU origin), and altitude within reasonable AGL.
    x0 = [0.0, 0.0] + ([z_fixed] if estimate_z else [])
    if estimate_z:
        bounds = ([-5000.0, -5000.0, -200.0], [5000.0, 5000.0, 1000.0])
    else:
        bounds = ([-5000.0, -5000.0], [5000.0, 5000.0])

    try:
        result = least_squares(
            residuals, x0=x0, bounds=bounds,
            method="trf", loss="huber", f_scale=3.0, max_nfev=200,
        )
    except Exception:
        return init

    if not np.all(np.isfinite(result.x)):
        return init
    p = np.array([result.x[0], result.x[1],
                  result.x[2] if estimate_z else z_fixed])
    lat_e, lon_e, alt_e = _enu_to_ll(p[0], p[1], p[2], init.lat, init.lon)
    # Residual-based covariance proxy
    final_res = residuals(result.x)
    cov_xy = float(np.var(final_res)) / max(1, len(records) - 2)
    cep95 = 2.45 * math.sqrt(cov_xy)
    return LocalizationResult(
        pci=int(records[0]["cell"]["pci"]),
        n_samples=len(records),
        method="path_loss_wls",
        lat=float(lat_e),
        lon=float(lon_e),
        alt_m=float(alt_e) if estimate_z else None,
        cov_xy_m2=cov_xy,
        cep95_m=cep95,
        altitude_estimated=estimate_z,
    )


def synthetic_aperture_aoa(records: list[dict]):
    """Placeholder. Requires CSI from LTE-Cell-Scanner-CSI fork.

    See docs/localization.md §4.
    """
    raise NotImplementedError(
        "synthetic_aperture_aoa needs CSI samples; enable the CSI capture "
        "fork and revisit when records carry per-subcarrier phase data."
    )


def locate_file(path: str, method: str) -> list[LocalizationResult]:
    records = list(read_jsonl(path))
    by_pci = _group_by_pci(records)
    results: list[LocalizationResult] = []
    for pci, recs in sorted(by_pci.items()):
        if method == "centroid":
            r = weighted_centroid(recs)
        elif method == "wls":
            r = path_loss_wls(recs)
        else:
            raise ValueError(f"unknown method {method}")
        if r is not None:
            results.append(r)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_glob", help="glob for geotagged-*.jsonl files")
    p.add_argument("--method", choices=("centroid", "wls"), default="centroid")
    args = p.parse_args()
    for path in sorted(glob.glob(args.input_glob)):
        results = locate_file(path, args.method)
        for r in results:
            print(json.dumps(r.__dict__, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
