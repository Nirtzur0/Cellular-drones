# Cellular-drones

A drone-mounted passive cellular reconnaissance framework: discover LTE cells,
geo-tag radio measurements, and localize emitters from a moving SDR.

**Status**: design + scaffolding. Day-1 capability targets HackRF One on macOS.
A documented upgrade path swaps in a USRP B210 to unlock PDCCH/RNTI decoding.

## What this framework does

| Capability | Day-1 (HackRF + MacBook) | Upgraded (USRP B210 + Linux) |
| --- | --- | --- |
| LTE cell discovery (PCI, CGI, RSRP, SNR) | yes, via `LTE-Cell-Scanner` | yes, via srsRAN `lte_cell_search` |
| MIB / SIB decode | yes, via `LTE-Tracker` | yes |
| PDCCH decode + active **RNTI** list | no (HackRF unsupported by FALCON/LTESniffer) | yes, via FALCON or LTESniffer |
| Uplink (UE) capture | no | yes (LTESniffer multi-SDR mode) |
| RSRP heatmap + cell trilateration | yes | yes |
| TDOA-style timing localization | no (single RX, free-running TCXO) | partial (requires GPSDO + ≥2 receivers) |

## Read this first

- [`docs/design.md`](docs/design.md) — full framework design and rationale.
- [`docs/hardware-tradeoffs.md`](docs/hardware-tradeoffs.md) — why HackRF caps at cell discovery, and what changes with USRP.
- [`docs/localization.md`](docs/localization.md) — 3D positioning from a moving drone (RSSI methods today, TDOA later).

## Quick start

### Hardware-free demo (use this first to validate the pipeline)

```bash
pip install -r requirements.txt
pip install -e .
python -m sniffer.demo --out-dir data/demo --scenario box --plot data/demo/box.png
```

This runs a synthetic emitter + 1535-sample box drone trajectory through every
pipeline stage (CellSearch parser → gpsd parser → geotag joiner → localizer
→ PNG report). On a clean checkout, centroid localization recovers the
emitter to within ~1 m horizontal and WLS to within ~50 m.

`--scenario line` exercises a single straight pass; the plot makes the
geometric limitation (no cross-track resolution from one pass) obvious.

### Real radio on macOS + HackRF

```bash
./scripts/install-macos.sh                              # Homebrew install + LTE-Cell-Scanner from source
./scripts/gps-logger.sh &                               # tail gpsd → data/gps-<mission>.jsonl
./scripts/cell-scan.sh 1840e6 1845e6                    # scan LTE band 3 DL block → data/scan-<mission>.jsonl
python -m sniffer.geotag 'data/scan-*.jsonl'            # merge with GPS, → data/geotagged-<mission>.jsonl
python -m sniffer.report 'data/geotagged-*.jsonl' --plot data/report.png
```

## Repo layout

```
docs/         design docs (start here)
scripts/      install + capture wrappers around LTE-Cell-Scanner / hackrf_transfer
src/sniffer/  python pipeline: schema, geotag, localize, replay
data/         JSONL captures, NMEA logs (gitignored)
tests/        unit tests for the geotag + localize math
```

## Legal / scope

Passive receive only, in authorized environments: private LTE (srsRAN
labrig, Amarisoft, SDR-based eNB), shielded/cabled benches, or operator-approved
field tests. Do not collect third-party subscriber identifiers. The framework
treats decoded artifacts as controlled data; RNTI capture (when on USRP) is
gated by an explicit config flag.
