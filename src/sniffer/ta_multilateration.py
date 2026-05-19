"""Multilateration of a UE from per-grant LTE Timing-Advance measurements.

When the drone is the serving eNB (rogue base station, path A in
docs/design.md), each UL grant carries a TA value that encodes the
round-trip UE↔drone propagation delay. One TA step = 16·Ts ≈ 0.521 µs
→ 156.25 m round-trip = **78.125 m one-way per step**. We store
`ta_meters` as the one-way distance.

Given N geo-tagged TA measurements `(drone_i, ta_meters_i)`, we solve

    minimise_x  Σᵢ ρ( ||drone_i − x|| − ta_meters_i ; σ )

where ρ is Huber loss so a single multipath/timing outlier doesn't
swing the fit. Closed-form initial guess from the centroid of the
drone positions, then `scipy.optimize.least_squares`. Covariance from
the Jacobian; CEP95 from the trace of the XY covariance block.

Reject cases the geometry can't solve:
  - fewer than `MIN_ANCHORS` measurements,
  - drone positions almost collinear (GDOP guard),
  - all TA values identical (UE lives on a single circle, no fix).

These refusals are intentional — the framework returns None rather
than a plausible-looking number with no actual information behind it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.optimize import least_squares

from sniffer.localize import (
    MIN_ALT_STDDEV_M,
    LocalizationResult,
    _enu_to_ll,
    _ll_to_enu,
)
from sniffer.schema import TA_STEP_METERS  # noqa: F401  (re-exported for callers)

# Number of TA-bearing geo-tagged grants required before we'll try.
# Three would be the theoretical minimum for a 2D fix from ranges, but
# the fit gets badly ill-conditioned with three; four is the practical
# floor.
MIN_ANCHORS = 4

# Per-measurement uncertainty in metres. Quantisation gives σ_quant
# = TA_STEP / sqrt(12) ≈ 22.5 m on the underlying continuous distance.
# Add timing jitter and call it 30 m — conservative but not absurd.
DEFAULT_SIGMA_M = 30.0

# GDOP-style geometry guard. Pearson r² of the drone XY positions
# above this → the anchors are nearly collinear, the problem is
# ill-posed (TA hyperbolas degenerate to parallels), bail out.
COLLINEARITY_R2_MAX = 0.985


def ta_multilateration_ue(records: list[dict],
                          sigma_m: float = DEFAULT_SIGMA_M,
                          ) -> LocalizationResult | None:
    """Per-UE multilateration. `records` is the same per-UE geo_history
    used by the centroid path, except each record must carry a
    `ue.ta_meters` (one-way drone-to-UE distance from the TA step).

    Returns None if there aren't enough usable anchors, the geometry
    is degenerate, or the solver doesn't converge.
    """
    anchors: list[dict] = []
    for r in records:
        ue = r.get("ue") or {}
        gps = r.get("gps") or {}
        ta = ue.get("ta_meters")
        if ta is None:
            continue
        if gps.get("lat") is None or gps.get("lon") is None:
            continue
        anchors.append({
            "lat": float(gps["lat"]), "lon": float(gps["lon"]),
            "alt_m": float(gps.get("alt_m", 0.0) or 0.0),
            "ta_meters": float(ta),
        })

    n = len(anchors)
    if n < MIN_ANCHORS:
        return None

    lats = np.array([a["lat"] for a in anchors])
    lons = np.array([a["lon"] for a in anchors])
    alts = np.array([a["alt_m"] for a in anchors])
    ta = np.array([a["ta_meters"] for a in anchors])

    # Origin at trajectory centroid keeps ENU coords small for the solver.
    lat0 = float(np.mean(lats))
    lon0 = float(np.mean(lons))
    xs, ys, zs = _ll_to_enu(lats, lons, alts, lat0, lon0)
    xs = np.asarray(xs); ys = np.asarray(ys); zs = np.asarray(zs)

    # Collinearity check on the drone XY footprint. R² > threshold →
    # anchors lie on a line, two solutions mirror each other across it.
    if float(np.var(xs) + np.var(ys)) < 1e-3:
        return None
    cov = np.cov(xs, ys)
    denom = math.sqrt(cov[0, 0] * cov[1, 1])
    r2 = (cov[0, 1] ** 2) / denom ** 2 if denom > 1e-9 else 1.0
    if r2 > COLLINEARITY_R2_MAX:
        return None

    # All TA equal → UE is somewhere on one circle, fit is ambiguous.
    if float(np.std(ta)) < TA_STEP_METERS * 0.25:
        return None

    estimate_z = float(np.std(alts)) >= MIN_ALT_STDDEV_M
    z_fixed = float(np.mean(zs))

    # Initial guess: drone centroid (origin in this ENU frame).
    x0 = [0.0, 0.0] + ([z_fixed] if estimate_z else [])

    def residuals(params):
        if estimate_z:
            px, py, pz = params
        else:
            px, py = params
            pz = z_fixed
        dx = xs - px; dy = ys - py; dz = zs - pz
        d = np.sqrt(dx * dx + dy * dy + dz * dz)
        return (d - ta) / sigma_m

    # Bounds keep the solver from running off to infinity on degenerate
    # samples. 50 km is well past any TA-feasible distance (LTE max TA
    # ≈ 100 km, so even an outlier won't escape).
    if estimate_z:
        bounds = ([-50_000, -50_000, -200.0],
                  [ 50_000,  50_000, 5_000.0])
    else:
        bounds = ([-50_000, -50_000], [50_000, 50_000])

    try:
        result = least_squares(
            residuals, x0=x0, bounds=bounds,
            method="trf", loss="huber", f_scale=1.5,
            max_nfev=200,
        )
    except (ValueError, np.linalg.LinAlgError):
        return None

    if not result.success and result.status <= 0:
        return None
    if not np.all(np.isfinite(result.x)):
        return None

    # Covariance ≈ (Jᵀ J)⁻¹ when residuals are already σ-weighted
    # (which they are: residuals(x) = (d − ta) / σ). That gives a unit-
    # consistent param covariance directly in m². The `scale` term
    # rescues us if σ_m was set too optimistic — if χ²/dof > 1 the
    # measurement noise was larger than we assumed.
    J = result.jac
    try:
        cov = np.linalg.inv(J.T @ J)
    except np.linalg.LinAlgError:
        return None
    final = residuals(result.x)
    rss = float(final @ final)
    dof = max(1, n - len(result.x))
    scale = max(1.0, rss / dof)
    cov_xy_m2 = float(cov[0, 0] + cov[1, 1]) * scale
    if not math.isfinite(cov_xy_m2) or cov_xy_m2 < 0:
        return None
    cep95 = 2.45 * math.sqrt(cov_xy_m2 / 2.0)

    px = float(result.x[0]); py = float(result.x[1])
    pz = float(result.x[2]) if estimate_z else z_fixed
    lat_e, lon_e, alt_e = _enu_to_ll(px, py, pz, lat0, lon0)

    # Pull (PCI, C-RNTI) off the first usable record so the result
    # matches the centroid path's contract.
    pci = c_rnti = 0
    for r in records:
        ue = r.get("ue") or {}
        if (ue.get("pci") is not None and ue.get("c_rnti") is not None
                and ue.get("ta_meters") is not None):
            pci = int(ue["pci"]); c_rnti = int(ue["c_rnti"])
            break

    return LocalizationResult(
        pci=pci, c_rnti=c_rnti,
        n_samples=n,
        method="ta_multilateration",
        lat=float(lat_e), lon=float(lon_e),
        alt_m=float(alt_e) if estimate_z else None,
        cov_xy_m2=cov_xy_m2, cep95_m=cep95,
        altitude_estimated=estimate_z,
        notes=f"{n} TA anchors · residual rms {math.sqrt(scale) * sigma_m:.1f} m",
    )
