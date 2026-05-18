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
    # Start from the centroid in ENU, not the origin (which IS the centroid
    # in our local frame but the iteration is more stable with a small kick
    # away from a singular point in symmetric geometries).
    p = np.array([1.0, 1.0, 0.0 if estimate_z else float(np.mean(zs))])
    lam = 1e-3  # Levenberg-Marquardt damping
    last_norm = math.inf
    for _ in range(30):
        dx = xs - p[0]; dy = ys - p[1]; dz = zs - p[2]
        d = np.sqrt(dx * dx + dy * dy + dz * dz) + 1e-3
        d_anchor = math.sqrt(dx[idx0] ** 2 + dy[idx0] ** 2 + dz[idx0] ** 2) + 1e-3
        predicted = 10 * n_path_loss * np.log10(d / d_anchor)
        residual = dl - predicted
        # d(predicted_i)/dp_x = c * (-dx_i/d_i^2 + dx_anchor/d_anchor^2)
        c = 10.0 * n_path_loss / math.log(10.0)
        Jx = c * (dx[idx0] / (d_anchor ** 2) - dx / (d * d))
        Jy = c * (dy[idx0] / (d_anchor ** 2) - dy / (d * d))
        Jz = c * (dz[idx0] / (d_anchor ** 2) - dz / (d * d))
        J = np.column_stack([Jx, Jy, Jz]) if estimate_z else np.column_stack([Jx, Jy])
        # Levenberg-Marquardt normal equations: (J^T J + λI) Δ = J^T r
        JTJ = J.T @ J
        damping = lam * np.diag(np.diag(JTJ) + 1.0)
        try:
            step = np.linalg.solve(JTJ + damping, J.T @ residual)
        except np.linalg.LinAlgError:
            break
        if not np.all(np.isfinite(step)):
            break
        if estimate_z:
            p[:3] += step
        else:
            p[:2] += step
        rnorm = float(np.linalg.norm(residual))
        if rnorm < last_norm:
            lam *= 0.5
        else:
            lam *= 2.0
        last_norm = rnorm
        if np.linalg.norm(step) < 1e-3:
            break
    if not np.all(np.isfinite(p)):
        return init  # fall back to centroid result
    lat_e, lon_e, alt_e = _enu_to_ll(p[0], p[1], p[2], init.lat, init.lon)
    # Residual-based covariance proxy
    cov_xy = float(np.var(residual)) / max(1, len(records) - 2)
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
