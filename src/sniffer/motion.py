"""Mobility classifier — is this UE stationary or moving?

The weighted-centroid localizer assumes a static emitter; mobile UEs
get badly biased estimates. Rather than silently report those bad
estimates, classify each UE up front and suppress positions whose
geometry says the UE was almost certainly moving.

Method: for each UE we have N drone positions where a UL grant landed
and the observed UL RSSI at each. If the UE is stationary, the
inverse-square law predicts a monotonic relationship between drone-
to-UE distance and observed RSSI: closer drone, stronger RSSI.

We measure the Pearson correlation between -log10(distance to the
centroid estimate) and observed RSSI. High correlation = RSSI is well
explained by drone proximity to a fixed point → stationary. Low
correlation = RSSI varies with structure the centroid can't explain
→ the UE was probably moving, the estimate is unreliable.

This is intentionally a cheap, robust heuristic. It doesn't try to
estimate the UE's velocity or trajectory — single-RX geometry can't
do that. It just classifies, and the dashboard hides positions that
the classifier flags `mobile`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
from pyproj import Geod

_GEOD = Geod(ellps="WGS84")

MIN_SAMPLES = 5
# Pearson r thresholds, with -log10(d) vs ul_rssi_dbm.
# Free-space gives r ≈ 1 for a clean stationary case; real-world
# multipath erodes it. Picked conservatively to avoid claiming
# "stationary" when geometry happens to fake it — slow-moving UEs whose
# residual path-loss signal still partially explains RSSI sit in the
# indeterminate band rather than the stationary one. Empirically against
# the simulator: stationary trajectories land r ≈ 0.85, the mobile UE
# lands r ≈ 0.55, parallel-co-move adversarial cases land r ≈ 0.
STATIONARY_R = 0.7
MOBILE_R = 0.3

Label = Literal["stationary", "mobile", "indeterminate"]


@dataclass
class MotionResult:
    label: Label
    corr: Optional[float]   # Pearson r, or None if insufficient data
    n_samples: int
    note: str = ""


def classify_motion(records: list[dict],
                    centroid_lat: float, centroid_lon: float) -> MotionResult:
    """Classify a single (PCI, C-RNTI) using its geo-tagged UL grants.

    `records` must be the per-UE geo_history items the localizer uses:
    `{"gps": {"lat", "lon", ...}, "ue": {"ul_rssi_dbm", ...}, ...}`.
    Only UL grants with a non-null `ul_rssi_dbm` and a GPS fix contribute.
    """
    distances: list[float] = []
    rssis: list[float] = []
    for r in records:
        ue = r.get("ue") or {}
        gps = r.get("gps") or {}
        rssi = ue.get("ul_rssi_dbm")
        if rssi is None:
            continue
        if gps.get("lat") is None or gps.get("lon") is None:
            continue
        _, _, d = _GEOD.inv(centroid_lon, centroid_lat,
                            gps["lon"], gps["lat"])
        # Clamp at 1 m so log10 stays defined when the drone passes
        # directly overhead in the simulator / contrived geometries.
        distances.append(max(1.0, d))
        rssis.append(float(rssi))

    n = len(rssis)
    if n < MIN_SAMPLES:
        return MotionResult(label="indeterminate", corr=None, n_samples=n,
                            note=f"need ≥ {MIN_SAMPLES} UL grants (have {n})")

    log_d = -np.log10(np.asarray(distances))   # closer = larger
    r = np.asarray(rssis)

    if float(np.std(log_d)) < 1e-6 or float(np.std(r)) < 1e-6:
        return MotionResult(label="indeterminate", corr=None, n_samples=n,
                            note="drone trajectory too tight for classification")

    corr = float(np.corrcoef(log_d, r)[0, 1])
    if not math.isfinite(corr):
        return MotionResult(label="indeterminate", corr=None, n_samples=n,
                            note="degenerate correlation")

    if corr >= STATIONARY_R:
        return MotionResult(label="stationary", corr=corr, n_samples=n,
                            note=f"RSSI tracks distance (r={corr:+.2f})")
    if corr < MOBILE_R:
        return MotionResult(label="mobile", corr=corr, n_samples=n,
                            note=f"RSSI uncorrelated with drone position (r={corr:+.2f})")
    return MotionResult(label="indeterminate", corr=corr, n_samples=n,
                        note=f"weak signal–distance correlation (r={corr:+.2f})")
