"""RSSI-based UE localization from a single moving receiver.

Implements the weighted-centroid method described in docs/localization.md.
Coordinate math is done in a local ENU frame anchored at the trajectory
centroid; results are converted back to WGS84 at the end.

Only UL grants contribute to a UE's position estimate — DL grants come
from the eNB so they trilaterate the cell, not the UE. The PDCCH-decode
pipeline still records DL grants (for an activity timeline / DCI stats),
but they never enter this module.

CLI:
  python -m sniffer.localize <geotagged-*.jsonl>
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


def locate_file(path: str) -> list[LocalizationResult]:
    records = list(read_jsonl(path))
    results: list[LocalizationResult] = []
    for _key, recs in sorted(_group_by_ue(records).items()):
        r = weighted_centroid_ue(recs)
        if r is not None:
            results.append(r)
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_glob", help="glob for geotagged-*.jsonl files")
    args = p.parse_args()
    for path in sorted(glob.glob(args.input_glob)):
        for r in locate_file(path):
            print(json.dumps(r.__dict__, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
