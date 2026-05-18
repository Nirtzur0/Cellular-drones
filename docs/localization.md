# Localization

How the framework turns a stream of geo-tagged UE sightings into a
position estimate for each UE.

## Method: RSSI weighted centroid

One implemented method. No fallbacks, no second algorithm pretending to
add precision it doesn't have.

| Method | Hardware needed | Expected accuracy |
| --- | --- | --- |
| RSSI weighted centroid | 1 RX + good geotag | 30–80 m, stationary UE |

Given samples `(p_i, r_i)` of position and linear-domain UL RSSI:

```
p_hat = sum(w_i * p_i) / sum(w_i)
w_i   = r_i^k                    # k = 2 by default; raise for LOS environments
```

Quick, robust, works with a single moving USRP. Underestimates range to
the UE when the trajectory does not encircle it — biased toward the
densest cluster of samples.

Only **UL grants** contribute. DL grants share the eNB's transmission
across every UE on the cell, so they trilaterate the cell, not the UE.
The localizer drops DL on input.

`sniffer.localize.weighted_centroid_ue` implements this. The live
dashboard calls it every time a new geo-tagged UL grant lands, and
publishes the estimate to the browser over SSE.

## Uncertainty

Each estimate carries:

- `cep95_m` — 95% circular error probable, derived from the sample
  covariance and corrected for sample count.
- `n_samples` — number of geo-tagged UL grants that fed it.

CEP95 is conservative for stationary UEs (real error tends to land
inside the reported circle) and is **not meaningful for mobile UEs** —
see below.

## Altitude

`weighted_centroid_ue` refuses to estimate altitude when the trajectory's
altitude standard deviation is below 5 m, returning `alt_m = None` and a
note. A flight at constant 30 m AGL gives you 2D positioning with a 30 m
altitude prior, full stop. Mission plans that need 3D should include at
least three altitude bands (e.g. 15, 30, 60 m AGL) over the same
horizontal pattern.

## The mobile-UE problem

The method assumes the UE stays put while the drone integrates RSSI
samples. If the UE moves during the integration window, the estimate
biases toward the centroid of the UE's own motion. The simulator's
mobile UE (`c_rnti=0x91ff`) demonstrates the bias directly: ~200 m for a
UE walking 200 m across the cell.

There is no software fix to a single-radio passive sniffer; the math
just doesn't have enough constraints. Real positioning of mobile UEs
would need multi-static TDOA across synchronized receivers, which is
not in the codebase.

## Validation

`tests/test_localize.py` exercises the math on synthetic free-space
trajectories. `tests/test_e2e.py` runs the full pipeline against the
simulator and asserts `error_xy_m < 100 m` for the stationary UEs; in
practice the simulator hits ~30 m.
