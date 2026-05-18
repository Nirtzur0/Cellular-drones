# Framework design

End-to-end design for a drone-mounted passive cellular sensor built around
existing open-source stacks. Aimed at a single-day prototype on HackRF +
MacBook, with a clean upgrade path to USRP B210 for RNTI-grade work.

## 1. Goals

1. **Discover** LTE cells visible to a moving SDR: PCI, CGI/eNB-ID, EARFCN,
   bandwidth, RSRP, RSRQ, SNR, TX antenna count.
2. **Extract identities** from downlink traffic. On HackRF this is limited to
   broadcast-plane identifiers (PCI, CGI, TAC, PLMN from MIB/SIB1). On USRP
   B210 this extends to PDCCH RNTIs (FALCON / LTESniffer).
3. **Geo-tag** every measurement with platform position, attitude, and a
   monotonic timestamp suitable for post-flight TDOA-style analysis.
4. **Localize** discovered emitters in 3D using the drone trajectory as a
   synthetic aperture (RSSI methods today, multi-receiver TDOA later).
5. **Stay swappable**: a single measurement schema and pipeline shared
   across radio backends, so a HackRF demo and a B210 mission produce the
   same downstream artifacts.

## 2. Existing tools surveyed

Decisions are anchored to maintained projects rather than rewrites.

| Tool | Role | HackRF? | USRP? | Notes |
| --- | --- | --- | --- | --- |
| [LTE-Cell-Scanner](https://github.com/JiaoXianjun/LTE-Cell-Scanner) | Cell search + MIB/SIB decode (`CellSearch`, `LTE-Tracker`) | yes (`-DUSE_HACKRF=1`) | yes | Has a [Homebrew formula](https://github.com/rxseger/homebrew-hackrf/blob/master/lte-cell-scanner.rb) — the only first-class macOS option. |
| [LTE-Cell-Scanner-CSI](https://github.com/Peco602/LTE-Cell-Scanner-CSI) | Same, plus per-subcarrier CSI export | yes | yes | Useful for richer RSSI/CSI fingerprinting. |
| [srsRAN 4G](https://github.com/srsran/srsRAN_4G) | Full LTE stack incl. `lte_cell_search`, eNB, UE; library used by FALCON/LTESniffer | partial (works for narrow BW cell search but half-duplex) | yes | Best on Linux. Drive a private eNB for lab validation. |
| [FALCON](https://github.com/falkenber9/falcon) | PDCCH decode → live RNTIs and per-RNTI resource allocations | **no** (needs full BW + good timing) | yes | The upstream RNTI extractor. |
| [LTESniffer](https://github.com/SysSec-KAIST/LTESniffer) | DL + UL eavesdropper built on FALCON + srsRAN | **no** | yes, GPSDO mandatory for 2-radio UL mode | Closest match to the reference paper's intent. |
| [5GSniffer](https://github.com/spritelab/5GSniffer) | NR DCI sniffer | **no** | yes | If/when the mission extends to 5G NR. |
| [Wi_UAV_tx_localization](https://github.com/fquitin/Wi_UAV_tx_localization) | DJI M100 + B205-mini gradient-ascent toward an RF carrier | n/a | B205-mini | Useful prior art for the drone-side localization loop. |
| [gr-gsm](https://github.com/ptrkrysik/gr-gsm) | GSM control-channel decode + IMSI catching | yes | yes | Out of scope for LTE work; mentioned only because HackRF is great at it. |
| [Kalibrate](https://github.com/steve-m/kalibrate-rtl) | GSM-FCCH-based TCXO ppm calibration | yes | yes | Calibrate HackRF's free-running TCXO before any LTE work. |

**Conclusion**: there is no maintained HackRF-compatible RNTI sniffer. The
honest 1-day target on HackRF is cell discovery + RSRP mapping using
LTE-Cell-Scanner. RNTI extraction is a Phase-2 upgrade gated on USRP B210.

## 3. Architecture

```
┌──────────────── airborne segment ─────────────────┐  ┌── ground segment ──┐
│                                                   │  │                    │
│  GPS/RTK  ──┐                                     │  │                    │
│             ├─► geotag agent ──► JSONL log ───────┼─►│  post-flight       │
│  IMU/AHRS ──┘            ▲                        │  │  pipeline:         │
│                          │ ts                     │  │   • merge          │
│  Antenna ─► HackRF ─► capture agent ──► IQ ring   │  │   • localize       │
│             (or B210)    │                        │  │   • visualize      │
│                          ▼                        │  │                    │
│                    cell-scan worker               │  │                    │
│                  (LTE-Cell-Scanner /              │  │                    │
│                   FALCON / LTESniffer)            │  │                    │
│                          │                        │  │                    │
│                          ▼                        │  │                    │
│                   measurement records ────────────┼─►│  health telemetry  │
│                                                   │  │  (1–10 Hz, no PII) │
└───────────────────────────────────────────────────┘  └────────────────────┘
```

The airborne host runs three loosely-coupled agents, all writing to a
single timestamped JSONL on local NVMe:

- **capture agent** — owns the SDR. Either short IQ bursts to disk (for
  offline replay) or piped straight into the cell-scan worker.
- **cell-scan worker** — runs `CellSearch` then `LTE-Tracker` (HackRF), or
  `falcon` / `LTESniffer` (USRP). Emits one record per cell sighting.
- **geotag agent** — subscribes to GPS NMEA + IMU, emits one position
  record per ~50 ms, both standalone and as a sidecar timestamp annotation
  on every measurement record.

Ground segment is post-flight only by default. Live telemetry is health +
counters, never raw IQ or subscriber identifiers — the bandwidth math from
the reference paper (≈30,000× over DJI's 4 KB/s payload link for one 20 MHz
RX) makes any live-IQ ambition a non-starter.

## 4. Day-1 hardware reality (HackRF + MacBook)

HackRF One:

- 1 RX, 1 TX, half-duplex, 8-bit ADC at up to 20 MS/s.
- TCXO is ±20 ppm out of the box; ±0.5 ppm after Kalibrate-style
  GSM-FCCH calibration. No internal GPSDO. External 10 MHz ref input
  via CLKIN if you ever bench-sync two HackRFs.
- USB 2.0. Already saturates around 20 MS/s with 8-bit samples.

What this means concretely:

- **OK**: PSS/SSS detection, MIB decode, SIB1 decode, RSRP/RSRQ/SNR per
  cell, time-tagged scans, RSSI heatmaps.
- **Not OK on HackRF**: PDCCH blind decoding (LTESniffer/FALCON refuse
  the device), uplink + downlink concurrent capture, sub-sample TDOA.

For the drone integration choice on a 1-day budget, the practical option is
**drone-as-rover-on-the-ground**: walk the MacBook+HackRF along a planned
trajectory to validate the geotag + localize pipeline end-to-end, then drop
the same software onto a small SBC (Pi 5, LattePanda, ROCKPro64) for a
proper airborne v0.1. The reference paper's Phase 4 (dummy payload flight)
remains the right gate before any RF mission.

## 5. Measurement schema

One JSONL record per cell sighting. Position is the **last known**
geotag-agent fix at the moment the sample window opened.

```json
{
  "schema_version": 1,
  "kind": "cell_sighting",
  "mission_id": "lab-2026-05-18-001",
  "capture_id": "hackrf-0",
  "ts_mono_ns": 174832012345678,
  "ts_utc": "2026-05-18T07:55:01.234567Z",
  "gps": {
    "lat": 32.0853,
    "lon": 34.7818,
    "alt_m": 42.7,
    "fix": "rtk_fix",
    "hdop": 0.6,
    "age_ms": 18
  },
  "attitude": {"yaw_deg": 271.4, "pitch_deg": 0.3, "roll_deg": -1.1},
  "radio": {
    "backend": "lte-cell-scanner",
    "device": "hackrf-0000000000000000",
    "earfcn": 1850,
    "center_hz": 1842500000,
    "bandwidth_hz": 20000000,
    "sample_rate_sps": 19200000,
    "rx_gain_db": 40,
    "tcxo_ppm": -0.42
  },
  "cell": {
    "pci": 271,
    "n_id_1": 90, "n_id_2": 1,
    "mode": "fdd",
    "cp": "normal",
    "n_ports": 2,
    "rsrp_dbm": -84.2,
    "rsrq_db": -10.1,
    "snr_db": 12.3,
    "frame_offset_samples": 30412,
    "mib": {"dl_bandwidth_rb": 100, "phich_dur": "normal", "sfn": 412},
    "sib1": {"plmn": "42501", "tac": 18452, "cell_id": 67305473}
  },
  "rnti": null,
  "notes": ""
}
```

`rnti` stays `null` on HackRF. On B210/FALCON it becomes a list of recently
seen `{rnti, dci_format, n_prb, ts_mono_ns}` entries.

## 6. Timing and synchronization

Three timescales, kept explicit so post-flight analysis can reason about
sub-millisecond effects:

1. **`ts_mono_ns`** — `CLOCK_MONOTONIC_RAW` from the host. Single domain
   for every record. Drift vs. UTC is logged but not corrected in place.
2. **`ts_utc`** — derived from GPS PPS (when present) or NTP (fallback).
   GPS time is stamped on the 1 PPS edge and the host monotonic clock is
   marked at the same instant; the offset is logged in a `time_anchor`
   record once per second.
3. **`frame_offset_samples`** — `LTE-Cell-Scanner` reports each cell's
   frame boundary in samples relative to the capture stream. Combined with
   the per-stream monotonic start time, this gives a sample-accurate
   estimate of when the LTE radio frame boundary passed the antenna —
   the only piece useful for any future TDOA experiment.

On HackRF, the achievable timing accuracy is bounded by:

- 32.55 ns / sample at 30.72 MS/s (≈9.76 m light-travel) — same as the
  reference paper.
- TCXO drift across a 30 min flight: even at 0.5 ppm after calibration,
  900 µs accumulated phase. Re-anchor to GPS PPS continuously.
- USB 2.0 buffering jitter on macOS: hundreds of microseconds, host-load
  dependent. This is the dominant single-receiver timing error and is
  why HackRF cannot do real TDOA.

On USRP B210 with GPSDO, the sample clock is GPS-disciplined to <1 ppb;
two GPSDO-equipped B-series radios can be sample-aligned well enough for
LTESniffer's 2-radio UL/DL mode, and well enough to attempt TDOA between
two airborne or ground+air receivers.

## 7. Localization (summary; see `docs/localization.md`)

Three methods are wired into the framework; only the first is realistic on
day 1.

1. **RSSI-weighted centroid + gradient ascent** along the drone trajectory.
   Single-receiver friendly. Yields tens-of-meters accuracy in open terrain.
   This is the only method available with one HackRF.
2. **Synthetic-aperture AoA**: treat the drone's flight path as a long
   sparse array; combine per-waypoint RSRP/CSI with platform attitude to
   estimate bearing. Needs CSI (LTE-Cell-Scanner-CSI fork) and good
   attitude logs.
3. **TDOA / FDOA**: requires ≥2 GPSDO-locked receivers (or one moving
   receiver with a stable atomic reference). Out of scope for HackRF.
   Documented here so the schema is ready when a B210 pair shows up.

## 8. Phasing

The reference paper's phase ladder is correct; we adapt it to HackRF reality.

| Phase | Hardware | Goal | Exit criteria |
| --- | --- | --- | --- |
| 0. Authorization + bench | MacBook + HackRF + private LTE (srsRAN eNB on Linux box) | Toolchain alive | `CellSearch` finds the lab cell, JSONL records produced, geotag joiner works against a synthetic NMEA log. |
| 1. Static walking survey | MacBook + HackRF + handheld GPS | Validate geotag + localize | Walk a 5-point pattern around the lab cell, weighted-centroid is within 25 m of the known eNB position. |
| 2. Rover survey | Same kit on a wheeled cart or backpack | Real-world RSSI maps | RSRP-vs-position plots match a free-space + log-distance model within reason. |
| 3. Dummy drone flight | DJI / equivalent + ballast | Mechanical | Aircraft stable with payload mass and CG. |
| 4. Powered no-RF flight | Drone + powered payload, RX disabled | Power/thermal/USB | No SDR drops, no GNSS interference, payload <55 W. |
| 5. Authorized airborne RF | Drone + HackRF capturing private LTE | First airborne dataset | Cell discovered and localized in <50 m. |
| 6. USRP upgrade | B210 + GPSDO + SBC + Linux | RNTI extraction | FALCON/LTESniffer reports active RNTIs against the lab UE. |

Phases 0–2 are the realistic day-1 deliverable. Phases 3–6 are the
roadmap.

## 9. Open questions to resolve before drone integration

- **Onboard compute**: MacBook is fine for ground rover. Drone needs a
  small Linux SBC (Pi 5 + active cooling, or a LattePanda 3 Delta for x86
  compatibility with future USRP/UHD work). Pick one and freeze the
  software environment.
- **Antenna**: HackRF's stock dipole is bad. A small log-periodic or a
  pair of band-tuned monopoles will materially improve RSRP and SNR.
- **GPS**: relying on the drone's GPS via Mavlink works for ground tests,
  but a dedicated u-blox ZED-F9P with PPS into the host gives you proper
  time anchoring. Cheap and worth doing before any TDOA ambitions.
- **Ground-truth eNB position** for accuracy reporting. For private LTE,
  this is trivial; for any field test, this is the limiting factor on
  reported localization accuracy.

## 10. Skeptical conclusions

- HackRF buys you a credible cell-discovery + RSSI-mapping platform in
  a day. It does **not** buy you a RNTI sniffer at any sample rate.
- The reference paper's USRP B210 + GPSDO baseline remains the right
  target for the "extract RNTIs" half of the original task. Treat this
  framework as the scaffolding that makes that upgrade a parts swap, not
  a rewrite.
- "3D positioning from timing" with a single, free-running, half-duplex
  receiver is not achievable in any honest sense. Today's framework
  gets 3D positioning from **RSSI + a moving platform**, with a documented
  path to true timing-based methods once the receiver count and clock
  discipline allow it.
