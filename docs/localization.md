# Localization

How the framework turns a stream of geo-tagged UE sightings into a
position estimate for each UE.

## Two independent estimators, one per observable

Two methods, each driven by a different physical observable on the same
UL grants. **Neither falls back to the other.** When both are available
they are shown side-by-side in the dashboard and JSONL, labelled with
`method` so the consumer knows which observable produced which estimate.

| Method | Observable | Hardware needed | Accuracy on stationary UE |
| --- | --- | --- | --- |
| `weighted_centroid` | UL RSSI per grant | 1 RX + good geotag | 30–80 m |
| `ta_multilateration` | UL Timing Advance per grant | 1 RX + good geotag + TA-emitting LTESniffer | 5–30 m, ≥ 4 anchors |

The two answer different questions. RSSI weights samples by received
power and biases toward the densest cluster of close passes. TA solves
range circles around each drone position, so geometry — not power —
drives the fit. They will visibly disagree by tens of metres on the
same UE; that is informative, not an error condition.

> **Real-radio TA caveat.** LTESniffer's published PDCCH-only build does
> not emit Timing Advance (TA arrives on PDSCH in RAR / MAC CE). The
> parser is ready for `ta` / `timing_advance` / `ta_n_steps` fields, the
> simulator emits TA today, and the e2e test asserts the full path. On
> real radio the TA estimate stays empty until LTESniffer (or a fork) is
> taught to surface TA. The plumbing is dead-safe in the absence of TA
> — the solver simply returns `None`.

### RSSI weighted centroid

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

### TA multilateration

Given samples `(p_i, ta_i)` of drone position and one-way TA range:

```
minimise_x  sum( huber( ||p_i - x|| - ta_i ; sigma ) )
```

One TA step (16·Ts) is 156.25 m round-trip, so each step encodes 78.125 m
one-way; quantisation noise alone is ~22.5 m. We solve with Huber loss
(outlier-tolerant) via `scipy.optimize.least_squares`. The estimator
refuses to converge when geometry is degenerate (anchors collinear; all
TA values equal; fewer than 4 anchors) — see `sniffer.ta_multilateration`.

The same per-UE history that feeds the centroid feeds the TA solver in
parallel: a separate deque (`ta_geo_history`) buffers records that carry
`ue.ta_meters`. Result lands on the UE entry as `est_position_ta` and
ships in the same `ue_sighting` SSE payload as the centroid estimate.

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

`tests/test_localize.py` exercises the centroid math on synthetic
free-space trajectories. `tests/test_ta_multilateration.py` exercises
the TA solver in isolation, including geometry-refusal cases.
`tests/test_e2e.py` runs the full simulator-driven pipeline through
both estimators and asserts `error_xy_m < 100 m` for the centroid and
`< 50 m` for TA on the stationary UEs; in practice the simulator hits
~30 m for the centroid and ~5–10 m for TA.
