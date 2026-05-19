# Cellular-drones

A drone-mounted passive LTE UE sniffer: capture C-RNTIs off the PDCCH
with LTESniffer, and position UEs from a moving SDR using UL grants.

**Status**: working end-to-end against the simulator. Real-radio path
targets LTESniffer on a USRP B210 (Linux).

## What this framework does

| Capability                                    | Status                                |
| --------------------------------------------- | ------------------------------------- |
| PDCCH decode + **C-RNTI list per cell**       | yes (USRP B210 + LTESniffer)          |
| Per-UE positioning, stationary UEs            | yes (UL grants, RSSI weighted centroid)|
| Per-UE positioning, mobile UEs                | not supported — single-RX math biases |
| Subscriber identity (IMSI / SUPI / phone #)   | no — passive LTE never exposes it     |
| 5G NR UE sniffing                             | no — LTESniffer is LTE-only           |

## Read this first

- [`docs/ue-sniffing.md`](docs/ue-sniffing.md) — the pipeline, what you
  get, what you don't.
- [`docs/design.md`](docs/design.md) — architecture and rationale.
- [`docs/localization.md`](docs/localization.md) — how the RSSI
  weighted-centroid estimator works and what it can / cannot do.

## Quick start

```bash
pip install -r requirements.txt
pip install -e .
sniffer --help
```

There is **one** run path: `sniffer live`. It ingests PDCCH events
(real radio or simulated) and serves the dashboard — C-RNTI extraction
and per-UE positioning happen on the same page, on the same map.

### Hardware-free (validates the pipeline)

```bash
sniffer live --simulate          # realtime UI at http://127.0.0.1:8000/
```

The simulator stages three synthetic UEs around an eNB (two stationary,
one walking across the cell). Stationary UEs recover to ~30 m of
ground truth; the mobile UE shows the expected ~200 m bias — staged on
purpose to expose the limit of single-RX positioning.

### Real radio (Linux + USRP B210)

```bash
sniffer install                                       # one-time: srsRAN + LTESniffer + venv
sniffer live --earfcn 1850 --pci 271 --rx-gain 50     # spawns LTESniffer + dashboard
# open http://127.0.0.1:8000/
```

Pick the target EARFCN + PCI for the operator you care about
(CellMapper / OpenCellID are good starting points; `srsRAN_cell_search`
works on the USRP itself if you don't want to rely on external
databases). GPS is auto-picked-up from `gpspipe` if `gpsd` is running
on the host — see *Where GPS comes from* below.

### Where GPS comes from

The dashboard expects a position stream paired with every UE sighting.
Three realistic sources, in order of recommendation:

1. **Dedicated USB/UART GPS → gpsd → gpspipe** (default). Plug a
   u-blox / BU-353 / similar into the SBC, `apt install gpsd
   gpsd-clients`, and `sniffer live` picks it up automatically.
2. **MAVLink from the flight controller.** Pixhawk / PX4 / ArduPilot
   already owns the GPS lock for navigation; a companion computer
   reads `GLOBAL_POSITION_INT` over UART or UDP. Adapter not in the
   tree yet — see `docs/design.md` §7.2 for the ~10-line `pymavlink`
   sketch.
3. **Direct u-blox UBX** (skip both gpsd and the FC). Only worth it if
   you want raw carrier-phase for PPK postprocessing.

The full discussion lives in `docs/design.md` §7.

### Captures

`sniffer live` writes `data/ue-<mission>.jsonl` (one decoded DCI per
line, schema in `src/sniffer/schema.py`). For ad-hoc analysis after a
flight, `jq` over that file is enough — there's no separate offline
report tool.

## Repo layout

```
docs/             design docs (start with ue-sniffing.md)
scripts/          install-linux.sh (apt + srsRAN + LTESniffer build)
src/sniffer/
  cli.py                 CLI entrypoint (sniffer live | sniffer install)
  schema.py              UeSighting record types
  simulate.py            synthetic LTESniffer + gpsd streams (text)
  parse_ltesniffer.py    LTESniffer text → DECODED → ue_sighting JSONL
  parse_gpsd.py          gpspipe JSON → geotag records
  localize.py            RSSI weighted centroid, per-(PCI, C-RNTI)
  live.py                realtime browser dashboard (the run path)
  ta_multilateration.py  alt localizer for rogue-eNB scenarios
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
