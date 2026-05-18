# Localization

How the framework turns a stream of geo-tagged cell sightings into a
3D estimate of where each emitter (eNB / UE) sits.

## Methods, ranked by what your hardware can actually do

| Method | Hardware needed | Day-1 with HackRF? | Expected accuracy |
| --- | --- | --- | --- |
| Cell-ID + nominal range | any | yes | 200 m – 1 km |
| RSSI weighted centroid | 1 RX + good geotag | **yes** | 30–80 m open terrain |
| RSSI path-loss inversion + WLS | 1 RX + ≥4 waypoints + path-loss model | **yes** | 20–50 m |
| RSRP gradient ascent | 1 RX + closed-loop drone control | yes (rover today) | converges to within ~10 m |
| Synthetic-aperture AoA | 1 RX + CSI + attitude | partial | 10–30 m bearing-limited |
| TDOA | ≥2 GPSDO-locked RX | no (HackRF) | 5–20 m |
| FDOA + TDOA fusion | ≥2 GPSDO RX, well-separated | no | <10 m |
| Multi-drone TDOA (the dream) | 3+ time-synced airborne RX | no | <5 m |

## 1. RSSI weighted centroid (day-1 baseline)

Given samples `(p_i, r_i)` of position and linear-domain RSSI:

```
p_hat = sum(w_i * p_i) / sum(w_i)
w_i   = r_i^k                    # k ≈ 2–4, tune empirically
```

Quick, robust, works with a single moving HackRF. Underestimates range to
the emitter when the trajectory does not encircle it — biased toward the
densest cluster of samples. We compensate by:

- Down-weighting samples taken within ~10 m of each other (the drone
  hovered there too long).
- Using `k=2` for log-distance environments, `k=4` for line-of-sight.

`sniffer.localize.weighted_centroid` implements this.

## 2. RSSI path-loss inversion (day-1 stretch)

Assume log-distance path loss:

```
RSRP_i = P_tx + G - 10 * n * log10(d_i / d0) - L_shadow_i
```

With ≥4 geometrically-diverse waypoints and a fixed `n` (2.0–3.5
depending on environment), invert for the unknown emitter position `p_e`
via weighted least squares. The 3D version uses altitude differences
between drone waypoints — this is why a moving drone is more useful than
a static rover even with a single antenna.

We do **not** estimate `n` jointly with `p_e` from a single flight; the
solution is unstable. Calibrate `n` once against a known emitter, then
re-use.

`sniffer.localize.path_loss_wls` implements this.

## 3. RSRP gradient ascent (the drone-actually-moves-itself loop)

This is the closed-loop variant: the drone steers in the direction of
increasing RSRP. Reference implementation:
[`fquitin/Wi_UAV_tx_localization`](https://github.com/fquitin/Wi_UAV_tx_localization)
on a DJI M100 + USRP B205-mini.

For HackRF the loop is identical; the limitations are:

- Slow update rate (`CellSearch` takes hundreds of ms per look).
- Noisy single-antenna RSRP — needs heavy smoothing (Kalman or a simple
  exponential moving average with α=0.2).
- Multipath dominates indoors; gradient ascent works much better in
  open terrain.

`sniffer.localize.gradient_step` produces the next-waypoint vector given
the current trajectory and RSRP history. Drone-side autonomy code is
out of scope for the framework — that lives in your MAVSDK / PSDK layer.

## 4. Synthetic-aperture AoA (the interesting middle option)

A 30 m drone pass at 5 m/s with 100 ms scan cadence gives ~60 samples
spaced 0.5 m apart. At 1.8 GHz (λ ≈ 16.6 cm), that is a sparse 30 m
"array" with severe spatial aliasing — but combined with absolute
position and CSI from `LTE-Cell-Scanner-CSI`, you can solve the bearing
to the emitter from a single pass.

This is the most promising HackRF-era extension: it gets you angle,
which the RSSI methods cannot. We log CSI in the schema (when the CSI
fork is enabled) so this can be added post-flight without re-collecting
data.

`sniffer.localize.synthetic_aperture_aoa` is a stub; implementation is
deferred until the CSI capture is validated.

## 5. TDOA (USRP-only, documented for completeness)

With two GPSDO-locked B210s at known separation, the time-difference of
the same downlink reference signal (CRS for LTE) yields a hyperboloid of
possible emitter locations. With three receivers, hyperboloids intersect
at a 3D point.

Practical numbers from the reference paper still apply:
- 30.72 MS/s → 32.55 ns/sample → 9.76 m range resolution per sample.
- Sub-sample interpolation (cross-correlation peak fit) gets you a
  factor of 5–20 better in good SNR.
- Real-world accuracy floor is dominated by cable/antenna/filter delay
  calibration, not by the sample clock.

The framework's measurement schema already includes `frame_offset_samples`
and a per-record `ts_mono_ns` anchored to GPS PPS, which is what a
post-flight TDOA solver needs. **Do not** advertise TDOA capability on
HackRF.

## 6. 3D positioning specifically

"3D" only makes sense if the drone trajectory has meaningful **altitude
diversity**. A flight at constant 30 m AGL gives you 2D positioning with
a ~30 m altitude prior, full stop. Mission plans for localization should
include at least three altitude bands (e.g., 15, 30, 60 m AGL) over the
same horizontal pattern, otherwise the altitude estimate is dominated by
the prior, not the measurements.

The `sniffer.localize` solver returns a 3×3 covariance and will refuse
to report altitude with confidence if the trajectory's altitude variance
is below a configurable threshold (default 5 m). This is intentional and
should not be relaxed.

## 7. Validation against ground truth

For private LTE, the eNB position is known. We log:

- `error_xy_m` — horizontal localization error.
- `error_z_m` — altitude error (or `null` if altitude diversity gate
  rejected).
- `cep95_m` — 95th-percentile circular error over a bootstrap of
  trajectory subsets.

Hitting `error_xy_m < 50 m` on a single open-terrain pass with HackRF
+ RSSI weighted centroid is a realistic Phase-1 acceptance gate.
