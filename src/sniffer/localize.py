"""RSSI-based UE localization from a single moving receiver.

Implements the methods described in docs/localization.md. Coordinate
math is done in a local ENU frame anchored at the trajectory centroid;
results are converted back to WGS84 at the end.

Only UL grants contribute to a UE's position estimate — DL grants come
from the eNB so they trilaterate the cell, not the UE. The PDCCH-decode
pipeline still records DL grants (for an activity timeline / DCI stats),
but they never enter this module.

Methods:
  weighted_centroid_ue  — robust baseline.
  path_loss_wls_ue      — weighted least squares against a log-distance model.

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
from typing import Callable, Optional

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
    c_rnti: int
    n_samples: int
    method: str
    lat: float
    lon: float
    alt_m: float | None
    cov_xy_m2: float
    cep95_m: float
    altitude_estimated: bool
    notes: str = ""


def _linear(power_dbm: float) -> float:
    return 10 ** (power_dbm / 10.0)


def _ue_power(r: dict) -> Optional[float]:
    """Per-UE positioning consumes only UL grants — DL grants share the
    eNB's transmission across every UE on the cell."""
    ue = r.get("ue") or {}
    if ue.get("direction", "").lower() != "ul":
        return None
    return ue.get("ul_rssi_dbm")


def _group_by_ue(records: list[dict]) -> dict[tuple[int, int], list[dict]]:
    out: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in records:
        if r.get("kind") != "ue_sighting":
            continue
        if r.get("gps") is None:
            continue
        if _ue_power(r) is None:
            continue
        ue = r["ue"]
        out[(int(ue["pci"]), int(ue["c_rnti"]))].append(r)
    return out


def _weighted_centroid_core(records: list[dict],
                            power_fn: Callable[[dict], float],
                            k: float) -> Optional[dict]:
    if len(records) < 2:
        return None
    lats = np.array([r["gps"]["lat"] for r in records])
    lons = np.array([r["gps"]["lon"] for r in records])
    alts = np.array([r["gps"]["alt_m"] for r in records])
    power = np.array([power_fn(r) for r in records])
    weights = np.array([_linear(v) ** (k / 2.0) for v in power])
    weights /= weights.sum()
    lat_hat = float(np.sum(weights * lats))
    lon_hat = float(np.sum(weights * lons))
    alt_var = float(np.var(alts))
    altitude_estimated = math.sqrt(alt_var) >= MIN_ALT_STDDEV_M
    alt_hat = float(np.sum(weights * alts)) if altitude_estimated else None
    xs, ys, _ = _ll_to_enu(lats, lons, alts, lat_hat, lon_hat)
    var_xy = float(np.var(xs) + np.var(ys))
    cep95 = 2.45 * math.sqrt(var_xy / len(records))
    notes = "" if altitude_estimated else (
        f"altitude not estimated: trajectory alt stddev "
        f"{math.sqrt(alt_var):.1f}m < {MIN_ALT_STDDEV_M}m"
    )
    return dict(
        n_samples=len(records),
        method="weighted_centroid",
        lat=lat_hat, lon=lon_hat, alt_m=alt_hat,
        cov_xy_m2=var_xy, cep95_m=cep95,
        altitude_estimated=altitude_estimated, notes=notes,
    )


def _path_loss_wls_core(records: list[dict],
                        power_fn: Callable[[dict], float],
                        n_path_loss: float) -> Optional[dict]:
    init = _weighted_centroid_core(records, power_fn, k=2.0)
    if init is None:
        return None
    lons = np.array([r["gps"]["lon"] for r in records])
    lats = np.array([r["gps"]["lat"] for r in records])
    alts = np.array([r["gps"]["alt_m"] for r in records])
    xs, ys, zs = _ll_to_enu(lats, lons, alts, init["lat"], init["lon"])
    xs = np.asarray(xs); ys = np.asarray(ys); zs = np.asarray(zs)
    power = np.array([power_fn(r) for r in records])
    idx0 = int(np.argmax(power))
    dl = power[idx0] - power
    estimate_z = init["altitude_estimated"]
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
    lat_e, lon_e, alt_e = _enu_to_ll(p[0], p[1], p[2], init["lat"], init["lon"])
    final_res = residuals(result.x)
    cov_xy = float(np.var(final_res)) / max(1, len(records) - 2)
    cep95 = 2.45 * math.sqrt(cov_xy)
    return dict(
        n_samples=len(records),
        method="path_loss_wls",
        lat=float(lat_e), lon=float(lon_e),
        alt_m=float(alt_e) if estimate_z else None,
        cov_xy_m2=cov_xy, cep95_m=cep95,
        altitude_estimated=estimate_z, notes="",
    )


def weighted_centroid_ue(records: list[dict],
                         k: float = 2.0) -> LocalizationResult | None:
    """Per-UE weighted centroid. Records must all share one (pci, c_rnti)
    and at least two must be UL grants with non-null `ul_rssi_dbm`.
    """
    ul = [r for r in records if _ue_power(r) is not None]
    core = _weighted_centroid_core(ul, _ue_power, k=k)
    if core is None:
        return None
    ue0 = ul[0]["ue"]
    return LocalizationResult(
        pci=int(ue0["pci"]),
        c_rnti=int(ue0["c_rnti"]),
        **core,
    )


def path_loss_wls_ue(records: list[dict],
                     n_path_loss: float = 3.2) -> LocalizationResult | None:
    ul = [r for r in records if _ue_power(r) is not None]
    core = _path_loss_wls_core(ul, _ue_power, n_path_loss=n_path_loss)
    if core is None:
        return None
    ue0 = ul[0]["ue"]
    return LocalizationResult(
        pci=int(ue0["pci"]),
        c_rnti=int(ue0["c_rnti"]),
        **core,
    )


def locate_file(path: str, method: str) -> list[LocalizationResult]:
    records = list(read_jsonl(path))
    results: list[LocalizationResult] = []
    for _key, recs in sorted(_group_by_ue(records).items()):
        if method == "centroid":
            r = weighted_centroid_ue(recs)
        elif method == "wls":
            r = path_loss_wls_ue(recs)
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
        for r in locate_file(path, args.method):
            print(json.dumps(r.__dict__, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
