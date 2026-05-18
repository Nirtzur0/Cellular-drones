# UE sniffing (C-RNTI + per-UE positioning)

This document describes the rebuilt primary flow of the framework: pull
C-RNTIs off the PDCCH with LTESniffer and localize each UE from a moving
passive receiver.

## What you get

- **Identity**: per-UE C-RNTI captured from PDCCH blind decode, per cell
  (PCI). DCI format, MCS, PRB allocation, TBS — everything LTESniffer
  emits. C-RNTI is a temporary, per-connection ID; see *Caveats* below.
- **Position**: per-(PCI, C-RNTI) location estimate using the same
  weighted-centroid / WLS engine the cell-discovery flow uses, fed by
  UL grants only.

## What you do *not* get

- **Subscriber identity** (IMSI / SUPI / phone number). Passive LTE
  reveals C-RNTI/TMSI/GUTI; the real identifiers stay encrypted past
  the RRC layer. To get IMSI you need either to operate the eNB (lab)
  or run a rogue cell — the latter is out of scope for this framework
  and is gated by jurisdictional authorization.
- **5G UE identity**. The standard encrypts IMSI as SUCI before
  transmission. LTESniffer is LTE-only.
- **Sub-10 m positioning of mobile UEs from a single radio**. The
  synthetic-aperture math only converges on stationary emitters.
  Mobile UEs require multi-static TDOA with multiple synchronized
  receivers — see *Limits* below.

## Pipeline

```
USRP B210 (UHD) ──► LTESniffer ──► ue-sniff.sh ──► JSONL (ue_sighting)
                                       │                  │
GPS module ──► gpsd ──► gpspipe ──► gps-*.jsonl ──┐       │
                                                  │       │
                          sniffer.geotag joiner ◄─┴───────┘
                                  │
                                  ▼
                       geotagged-<mission>.jsonl
                                  │
                ┌─────────────────┴─────────────────┐
                ▼                                   ▼
  sniffer.localize (per-RNTI)        sniffer.live (browser dashboard)
                │                                   │
                ▼                                   ▼
  sniffer.report → PNG plot            http://127.0.0.1:8000/
```

The same pipeline runs end-to-end against the simulator without any
hardware (`python3 -m sniffer.demo`); the real-radio path swaps the
simulator stream for LTESniffer's stdout.

## Quick start

### Hardware-free (validates the pipeline)

```bash
pip install -r requirements.txt
pip install -e .

python3 -m sniffer.demo --out-dir data/demo --plot data/demo/ues.png
python3 -m sniffer.live --simulate
# open http://127.0.0.1:8000/
```

The simulator stages three UEs:

| C-RNTI    | Position                  | Mobile? |
| --------- | ------------------------- | ------- |
| `0x4ad2`  | NE of eNB, street level   | no      |
| `0x73a1`  | SW of eNB, weaker UL      | no      |
| `0x91ff`  | walking W→E across cell   | yes     |

Stationary UEs typically localize to within ~30 m using the weighted
centroid. The mobile UE shows a ~200 m bias — this is the simulator
demonstrating the limit of single-RX positioning for moving targets.

### Real radio (Linux + USRP B210)

```bash
./scripts/install-linux.sh                          # one-time
./scripts/gps-logger.sh &                           # GPS stream
./scripts/ue-sniff.sh 1850 271 --rx-gain 50 &       # LTESniffer on PCI 271
./scripts/live-dashboard.sh --ue 1850 271           # browser UI
```

Pick the target EARFCN + PCI from CellMapper / OpenCellID, or run
`srsRAN_cell_search` on the USRP directly if you don't trust external
databases.

## Record schema (UE sighting)

One JSONL row per decoded DCI:

```json
{
  "kind": "ue_sighting",
  "mission_id": "2026-05-18T12-03-50Z",
  "capture_id": "usrp-b210-0",
  "ts_mono_ns": 12345678901234,
  "ts_utc": "2026-05-18T12:03:50Z",
  "radio": {
    "backend": "ltesniffer",
    "device": "usrp-b210-0",
    "center_hz": 1842500000.0,
    "sample_rate_sps": 23040000.0,
    "rx_gain_db": 50.0
  },
  "ue": {
    "pci": 271,
    "c_rnti": 19154,
    "direction": "ul",
    "dci_format": "0",
    "mcs": 12,
    "n_prb": 4,
    "tbs_bytes": 408,
    "ul_rssi_dbm": -92.1,
    "dl_rsrp_dbm": null,
    "raw": {"frame": "512", "subframe": "3"}
  },
  "gps": {"lat": 32.085, "lon": 34.781, "alt_m": 30.0, "fix": "rtk_fix", "age_ms": 14},
  "schema_version": 1
}
```

DL grants are stored too (for an activity timeline) but never feed the
localizer — DL energy is the eNB's transmission, the same for every UE
on the cell.

## Caveats

### C-RNTI rotates

A C-RNTI is assigned by the eNB when a UE establishes an RRC connection
and is released when the connection ends. The same physical handset
reconnecting later — after a cell reselect, after RRC release, after
airplane mode, after a coverage gap — will be reissued a new C-RNTI.
Per-RNTI "tracks" are connection-scoped, not subscriber-scoped.

For longer-running surveillance you'd need to fingerprint UEs by
behavior (PUCCH/PUSCH timing patterns, harmonic UL bursts, MCS
distributions). That's research territory, not in this codebase.

### Mobile UE positioning is biased

Weighted-centroid and WLS assume a static emitter; the math integrates
RSSI over the drone's trajectory. If the UE moves during the
integration window the estimate biases toward the centroid of the UE's
own motion, not its current position. The simulator's mobile UE shows
this directly: ~200 m bias for a UE walking 200 m over the mission.

The fix is multi-static TDOA: 3+ GPSDO-disciplined receivers, sample-
aligned IQ, ToA differences between the same UL burst arriving at each
receiver. The drone becomes one of those receivers; the others sit on
the ground at known positions. None of this is in the codebase today.

### UL grants are intermittent

The localizer needs ≥ 2 UL grants with GPS attached before it produces
an estimate, and ≥ 6 before WLS kicks in. A UE that's mostly DL-bound
(streaming, browsing) might not give you enough UL volume in a short
window. Watch the *UL / DL grants* column in the dashboard — if it's
heavily skewed to DL, the UE is "heard but unlocalisable."

## Adapting LTESniffer's actual output

Real LTESniffer text drifts across versions. Rather than hard-code one
format, the pipeline uses a two-stage normaliser:

1. `scripts/ue-sniff.sh` pipes LTESniffer's stdout through
   `python3 -m sniffer.normalize_ltesniffer`, which uses permissive
   regexes to recognise `KEY=value` and `KEY: value` fields in any
   order, normalises field names (`rnti` → `c_rnti`, `RBs` → `prb`,
   etc.), and emits canonical `DECODED key=value` lines.
2. `python3 -m sniffer.parse_ltesniffer` reads those canonical lines
   and emits schema-conformant JSONL.

If your LTESniffer build emits a format the normaliser doesn't handle,
add the field to `_FIELD_MAP` in `src/sniffer/normalize_ltesniffer.py`.
The test in `tests/test_parse_ltesniffer.py` is the regression net.
