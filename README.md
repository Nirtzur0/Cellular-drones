# Cellular-drones

A drone-mounted passive LTE UE sniffer: capture C-RNTIs off the PDCCH
with LTESniffer, and position UEs from a moving SDR using UL grants.

**Status**: working end-to-end against the simulator. Real-radio path
targets LTESniffer on a USRP B210 (Linux).

## What this framework does

| Capability                                    | Status                                |
| --------------------------------------------- | ------------------------------------- |
| PDCCH decode + **C-RNTI list per cell**       | yes (USRP B210 + LTESniffer)          |
| Per-UE positioning, stationary UEs            | yes (UL grants, weighted centroid/WLS)|
| Per-UE positioning, mobile UEs                | biased — needs multi-static TDOA      |
| Subscriber identity (IMSI / SUPI / phone #)   | no — passive LTE never exposes it     |
| 5G NR UE sniffing                             | no — LTESniffer is LTE-only           |

## Read this first

- [`docs/ue-sniffing.md`](docs/ue-sniffing.md) — the pipeline, what you
  get, what you don't.
- [`docs/design.md`](docs/design.md) — architecture and rationale.
- [`docs/localization.md`](docs/localization.md) — RSSI methods today,
  TDOA later.

## Quick start

### Hardware-free demo (validates the pipeline)

```bash
pip install -r requirements.txt
pip install -e .

python3 -m sniffer.demo --out-dir data/demo --plot data/demo/ues.png
python3 -m sniffer.live --simulate
# open http://127.0.0.1:8000/
```

The demo stages three synthetic UEs around an eNB (two stationary, one
walking across the cell). Stationary UEs recover to ~30 m of ground
truth; the mobile UE shows the expected ~200 m bias — the simulator
stages this on purpose to expose the limit of single-RX positioning.

### Real radio (Linux + USRP B210)

```bash
./scripts/install-linux.sh                          # one-time: srsRAN + LTESniffer + venv
./scripts/gps-logger.sh &                           # GPS stream → data/gps-<mission>.jsonl
./scripts/ue-sniff.sh 1850 271 --rx-gain 50 &       # LTESniffer DL-only on EARFCN 1850 / PCI 271
./scripts/live-dashboard.sh --ue 1850 271           # browser UI
# open http://127.0.0.1:8000/
```

Pick the target EARFCN + PCI for the operator you care about (CellMapper
/ OpenCellID are good starting points; `srsRAN_cell_search` works on the
USRP itself if you don't want to rely on external databases).

## Repo layout

```
docs/             design docs (start with ue-sniffing.md)
scripts/          install + capture wrappers (LTESniffer, gpsd)
src/sniffer/
  schema.py              UeSighting record types
  simulate.py            synthetic LTESniffer + gpsd streams
  parse_ltesniffer.py    DECODED key=value lines → ue_sighting JSONL
  normalize_ltesniffer.py permissive translator from LTESniffer text → DECODED
  parse_gpsd.py          gpspipe JSON → geotag records
  geotag.py              join UE sightings with the nearest GPS fix
  localize.py            weighted centroid + WLS, per-(PCI, C-RNTI)
  live.py                realtime browser dashboard
  report.py              per-mission text summary + matplotlib plot
  demo.py                end-to-end: simulator → pipeline → plot
data/             JSONL captures (gitignored)
tests/            unit + e2e tests
```

## Legal / scope

Passive receive only, in authorized environments: private LTE (srsRAN
labrig, Amarisoft, SDR-based eNB), shielded/cabled benches, or
operator-approved field tests with written authorization.

The framework collects C-RNTI (a temporary, per-connection ID assigned
by the network) and per-UE signal strength. It does **not** attempt to
recover subscriber identity (IMSI/SUPI), which would require either
operating the eNB (lab) or running a rogue cell (out of scope here and
restricted in most jurisdictions).

The dashboard shows a banner reminding operators that C-RNTI is
connection-scoped, not subscriber-scoped.
