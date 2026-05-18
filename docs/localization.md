# Localization

How the framework turns a stream of geo-tagged UE sightings into a 3D
estimate of where each UE sits.

## Methods, ranked by what your hardware can actually do

| Method | Hardware needed | In repo? | Expected accuracy |
| --- | --- | --- | --- |
| RSSI weighted centroid | 1 RX + good geotag | **yes** | 30–80 m, stationary UE |
| RSSI path-loss inversion + WLS | 1 RX + ≥4 waypoints + path-loss model | **yes** | 20–50 m, stationary UE |
| RSRP gradient ascent | 1 RX + closed-loop drone control | no | converges to within ~10 m |
| Synthetic-aperture AoA | 1 RX + CSI + attitude | no (stub) | 10–30 m bearing-limited |
| TDOA | ≥2 GPSDO-locked RX | no | 5–20 m, works on mobile UEs |
| FDOA + TDOA fusion | ≥2 GPSDO RX, well-separated | no | <10 m |
| Multi-drone TDOA (the dream) | 3+ time-synced airborne RX | no | <5 m |

Only **UL grants** contribute to a UE position estimate — DL grants share
the eNB's transmission across every UE on the cell, so they trilaterate
the cell, not the UE. The localizer drops them on input.

## 1. RSSI weighted centroid (baseline)

Given samples `(p_i, r_i)` of position and linear-domain UL RSSI:

```
p_hat = sum(w_i * p_i) / sum(w_i)
w_i   = r_i^k                    # k ≈ 2–4, tune empirically
```

Quick, robust, works with a single moving USRP. Underestimates range to
the UE when the trajectory does not encircle it — biased toward the
densest cluster of samples. We compensate by:

- Down-weighting samples taken within ~10 m of each other (the drone
  hovered there too long).
- Using `k=2` for log-distance environments, `k=4` for line-of-sight.

`sniffer.localize.weighted_centroid_ue` implements this.

## 2. RSSI path-loss inversion (stretch)

Assume log-distance path loss:

```
UL_RSSI_i = P_tx_ue + G - 10 * n * log10(d_i / d0) - L_shadow_i
```

With ≥4 geometrically-diverse waypoints and a fixed `n` (2.5–3.5
depending on environment — UE links tend to be steeper than the
downlink because UEs sit in ground clutter), invert for the unknown UE
position `p_e` via weighted least squares. The 3D version uses altitude
differences between drone waypoints — this is why a moving drone helps
even with a single antenna.

We do **not** estimate `n` jointly with `p_e` from a single flight; the
solution is unstable. Calibrate `n` once against a known UE, then re-use.

`sniffer.localize.path_loss_wls_ue` implements this.

## 3. The mobile-UE problem

Sections 1 and 2 both assume the UE stays put while the drone integrates
RSSI samples. If the UE moves during the integration window, the estimate
biases toward the centroid of the UE's own motion. The simulator's
mobile UE (`c_rnti=0x91ff`) demonstrates the bias directly: ~200 m for a
UE walking 200 m across the cell.

There is no software fix to a single-radio passive sniffer; the math
just doesn't have enough constraints. Real positioning of mobile UEs
needs:

- **TDOA across multiple synchronized receivers** (sec. 5 below). Each
  UL burst arrives at each receiver at a different time-of-flight; the
  hyperbolic intersection localizes the UE *instantaneously*, no
  trajectory integration required.
- **AoA from a phased array** at the drone. Single-bearing per scan, no
  trajectory dependence. Adds an antenna + RF front-end the framework
  doesn't currently spec.

## 4. Synthetic-aperture AoA (deferred)

A 30 m drone pass at 5 m/s with 100 ms scan cadence gives ~60 samples
spaced 0.5 m apart. At 1.8 GHz (λ ≈ 16.6 cm), that is a sparse 30 m
"array" with severe spatial aliasing — but combined with absolute
position and per-DCI CSI, you can solve the bearing to the UE from a
single pass. LTESniffer does not export per-RE CSI today; this is the
deferred item.

Same limit applies as in section 3: this only works for a UE that's
stationary during the pass.

## 5. TDOA (multi-receiver, the real positioning path)

With two GPSDO-locked B210s at known separation, the time-difference of
the same UE UL transmission yields a hyperboloid of possible UE
locations. With three receivers, hyperboloids intersect at a 3D point.

Practical numbers:
- 30.72 MS/s → 32.55 ns/sample → 9.76 m range resolution per sample.
- Sub-sample interpolation (cross-correlation peak fit) gets you a
  factor of 5–20 better in good SNR.
- Real-world accuracy floor is dominated by cable/antenna/filter delay
  calibration, not by the sample clock.

The framework's measurement schema already includes a per-record
`ts_mono_ns` anchored to GPS PPS, which is what a post-flight TDOA
solver needs. The drone becomes one of those receivers; the others are
ground sensors at known positions. None of this is in the codebase yet.

## 6. 3D positioning specifically

"3D" only makes sense if the drone trajectory has meaningful **altitude
diversity**. A flight at constant 30 m AGL gives you 2D positioning with
a ~30 m altitude prior, full stop. Mission plans for localization should
include at least three altitude bands (e.g., 15, 30, 60 m AGL) over the
same horizontal pattern, otherwise the altitude estimate is dominated by
the prior, not the measurements.

`sniffer.localize` returns a covariance proxy and refuses to report
altitude with confidence if the trajectory's altitude variance is below
a configurable threshold (default 5 m). This is intentional and should
not be relaxed.

## 7. Validation against ground truth

In simulator runs the UE positions are known, so we log:

- `error_xy_m` — horizontal localization error vs ground truth.
- `cep95_m`    — covariance-derived 95% CEP.

The acceptance gate for the stationary UEs in `tests/test_e2e.py` is
`error_xy_m < 100 m`; in practice the simulator hits ~30 m. Real-world
accuracy depends on UE EIRP, multipath, and how well the box trajectory
surrounds the target.
