# Hardware trade-offs

Why HackRF caps the day-1 capability at cell discovery, and exactly what
each upgrade unlocks.

## Quick comparison

| Property | HackRF One | USRP B200 | USRP B210 | USRP X310 |
| --- | --- | --- | --- | --- |
| RX channels | 1 | 1 | 2 (shared LO in 2×2) | 2 (independent LO) |
| ADC | 8-bit | 12-bit | 12-bit | 14-bit |
| Max BW per chan | 20 MHz | 56 MHz (1×1) | 56 MHz (1×1) / 30.72 MHz (2×2) | 160 MHz |
| Duplex | half | full | full | full |
| GPSDO option | external CLKIN only | yes | yes | yes |
| Host link | USB 2.0 | USB 3.0 | USB 3.0 | 10 GbE / PCIe |
| Power (typ) | 1.0 W | 4.5 W | 4.5 W | 35 W |
| Price (USD, 2026) | ~330 | ~1,462 | ~2,387 | >11,000 |
| Mass (g) | ~85 | ~350 | ~350 | ~1,700 |
| LTESniffer / FALCON | **no** | yes (UL only role) | yes | yes |
| srsRAN cell search | yes (narrow BW) | yes | yes | yes |
| LTE-Cell-Scanner | **yes** | yes | yes | yes |

## Why HackRF is excluded from FALCON / LTESniffer

PDCCH blind decoding needs:

1. **Full 20 MHz capture with low quantization noise**. 8-bit ADCs hurt
   PDCCH detection at realistic SNRs; FALCON's threshold and CFO trackers
   were tuned for 12-bit USRPs.
2. **Stable sample clock**. Frame-aligned PDCCH search is timing-sensitive;
   a free-running ±20 ppm TCXO drifts off the eNB's slot grid faster than
   the tracker can recover.
3. **For UL+DL mode: two synchronized radios at separated frequencies**.
   HackRF is half-duplex with a single mixer — physically impossible.

LTESniffer's README is explicit: only srsRAN-supported SDRs for the DL
role, GPSDO mandatory for the 2-radio UL role.

## Why HackRF still works for cell discovery

`LTE-Cell-Scanner` (`CellSearch` + `LTE-Tracker`) was engineered for
low-cost, low-precision front ends. It:

- Tolerates ±100 kHz CFO via a Kalibrate-style calibration pass.
- Operates on captured IQ blocks rather than streaming sync — robust to
  USB 2.0 jitter.
- Decodes PSS/SSS → MIB → SIB1 chain, which is all you need for cell ID
  and RSRP — no PDCCH blind decode.
- Has a maintained macOS Homebrew formula.

## Upgrade ladder

1. **Calibrate HackRF**: run Kalibrate against a known GSM cell, write
   `tcxo_ppm` into `config.yaml`. Free.
2. **External 10 MHz + 1 PPS into HackRF CLKIN**: gets you ~1 ppb sample
   clock and PPS-aligned IQ, at the cost of ~$120 for a Leo Bodnar mini
   GPSDO. Still single-RX, still 8-bit, still no FALCON support — but
   timing-anchored RSSI maps become much better.
3. **Add a second HackRF, both on same external 10 MHz/PPS**: gives you
   coarse two-antenna RSSI diversity for AoA experiments. Not enough for
   real TDOA but enough to validate the synthetic-aperture math.
4. **Swap HackRF → USRP B210 + GPSDO**: unlocks FALCON for live RNTI
   extraction. Single radio, downlink only. ~$3,900.
5. **Add USRP B200 + second GPSDO**: unlocks LTESniffer 2-radio UL+DL
   mode. ~$2,964 more. This is the configuration the reference paper
   recommends for sub-$10k airborne.

Steps 1–2 are appropriate for a 1-day prototype. Step 4 is the right
target for "we actually need RNTIs".

## macOS-specific notes

- `brew install hackrf` and `brew install --HEAD rxseger/hackrf/lte-cell-scanner`
  is the supported path. Apple Silicon: build LTE-Cell-Scanner from
  source with `-DUSE_HACKRF=1 -DUSE_OPENCL=0` (OpenCL on Apple Silicon
  is flaky for this use).
- UHD/srsRAN on macOS: technically buildable, but the FALCON/LTESniffer
  chain is much smoother on Ubuntu 22.04. Plan to move to a Linux SBC
  the moment you upgrade to USRP.
- HackRF on macOS draws ~1 W from the USB port — fine for laptop bench
  work, irrelevant for drone integration.

## Drone power budget (for the eventual airborne payload)

Same math as the reference paper, adapted to a HackRF-based prototype:

| Component | Typical W | Notes |
| --- | --- | --- |
| HackRF One | 1.0 | USB-bus powered |
| Raspberry Pi 5 host + active cooler | 7.0 | Capture + cell scan |
| NVMe (M.2 hat) | 2.5 | Sustained write at ~25 MB/s for 8-bit IQ |
| u-blox ZED-F9P GPS module | 0.5 | PPS + RTK option |
| LNA + bias-tee | 0.3 | Optional, band-dependent |
| **Total** | **~11 W** | Far below the M350 96 W ceiling — leaves real margin. |

The B210 upgrade roughly doubles this; the LTESniffer 2-radio + small
x86 host configuration is the 55–70 W payload class.
