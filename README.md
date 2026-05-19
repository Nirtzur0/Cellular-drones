# Cellular-drones

A drone-mounted passive LTE UE sniffer: capture C-RNTIs off the PDCCH,
and (with the right hardware) position UEs from a moving SDR using UL
grant energy.

**Status**: pipeline works end-to-end against the simulator. Real-radio
LTE path requires LTESniffer (Linux). Positioning specifically requires
**UL sniffing hardware** — see capability matrix below.

## What this framework does

| Capability | DL-only (HackRF / 1× USRP) | UL+DL (2× USRP + GPSDO / X310) | Simulator |
| --- | --- | --- | --- |
| **C-RNTI list per cell** | ✓ | ✓ | ✓ |
| DL/UL grant counts, MCS, PRB, TBS | ✓ | ✓ | ✓ |
| Per-UE positioning, stationary | **✗** (no UL energy observable) | ✓ (RSSI centroid) | ✓ |
| Per-UE positioning, mobile | ✗ (single-RX math biases) | partial bias | partial bias |
| Subscriber identity (IMSI / SUPI) | ✗ — never on passive LTE | ✗ — never on passive LTE | ✗ |
| 5G NR UE sniffing | ✗ — LTESniffer is LTE-only | ✗ | ✗ |

The dashboard auto-detects DL-only mode and surfaces a banner explaining
that positioning is inactive (rather than promising it indefinitely).

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

The primary workflow is **survey** — sweep across all cells in a band,
dwell on each, accumulate every C-RNTI seen. C-RNTI is cell-scoped, so
a single-cell sniffer only enumerates one cell's UEs; `sniffer survey`
orchestrates a sweep so you get a census of all visible UEs.

```bash
# Real radio:
sniffer survey --band 3 --dwell-seconds 15 --total-minutes 30

# Or test the orchestration logic without hardware:
sniffer live --simulate          # realtime UI at http://127.0.0.1:8000/
```

Subcommands:

| Command | What it does |
| --- | --- |
| `sniffer scan` | one-shot: find LTE cells in a band (wraps `srsran_cell_search`) |
| `sniffer survey` | sweep across all cells, dwell on each, harvest C-RNTIs |
| `sniffer live` | single-cell mode (or `--simulate` for hardware-free) |
| `sniffer install` | Linux dependency installer (apt + srsRAN + FALCON + LTESniffer) |

### Hardware-free (validates the pipeline)

```bash
sniffer live --simulate          # realtime UI at http://127.0.0.1:8000/
```

The simulator stages three synthetic UEs around an eNB (two stationary,
one walking across the cell). Stationary UEs recover to ~30 m of
ground truth; the mobile UE shows the expected ~200 m bias — staged on
purpose to expose the limit of single-RX positioning.

### Real radio (Linux + USRP B210 or HackRF)

```bash
sniffer install                          # one-time: srsRAN + FALCON + LTESniffer + venv
sniffer survey --band 3                  # sweep every cell on band 3
# open http://127.0.0.1:8000/
```

Or, if you already know one specific cell:

```bash
sniffer live --decoder falcon --earfcn 1850 --pci 271
```

`sniffer survey` runs `sniffer scan` first to find cells, then cycles
through them. The dashboard shows a `SURVEYING` banner with current
cell + countdown. C-RNTIs from every cell accumulate in one view.

**Decoder note.** Stock LTESniffer writes PCAP files, not text — the
dashboard parser doesn't consume that yet, so `--decoder ltesniffer`
launches the binary but no UEs reach the UI. **Use `--decoder falcon`
(the default for `sniffer survey`)** — FalconEye writes per-DCI CSV
that the dashboard tails in real time. Tradeoffs documented in
`docs/design.md` "Decoder choice".

GPS is auto-picked from `gpspipe` if `gpsd` is running, or sniffed
from the drone's own RemoteID broadcast via `--droneid-cmd` (see
`docs/design.md` §7.4). Neither is required for C-RNTI extraction —
positioning is the only thing that needs GPS.

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
scripts/          install-linux.sh (apt + srsRAN + FALCON + LTESniffer + venv)
src/sniffer/
  cli.py                 CLI entrypoint (scan | survey | live | install)
  schema.py              UeSighting / GeotagRecord dataclasses
  lte_bands.py           EARFCN ↔ Hz per 3GPP TS 36.101
  scan.py                wraps srsran_cell_search (cell discovery)
  sib1.py                wraps pdsch_ue (PLMN/TAC/CGI enrichment)
  survey.py              sweep + dwell orchestrator (the primary flow)
  falcon.py              tails FalconEye CSV → ue_sighting JSONL
  parse_ltesniffer.py    text → ue_sighting JSONL (LTESniffer text mode)
  parse_gpsd.py          gpspipe JSON → geotag records
  parse_droneid.py       DroneID decoder JSON → geotag records (alt GPS)
  droneid_hackrf.py      HackRF capture-loop for file-based DroneID decoders
  spectrum.py            hackrf_sweep wrapper, live RF waterfall
  localize.py            RSSI weighted centroid (needs UL energy)
  ta_multilateration.py  TA-range multilateration (alt positioning)
  live.py                realtime browser dashboard
  simulate.py            synthetic UE + GPS streams (for tests)
data/             JSONL captures (gitignored)
tests/            unit + integration tests
```

## Testing on the Pi

The simulator covers the pipeline; real-radio coverage requires the
target host. `scripts/pi-smoke-test.sh` walks the whole stack:

```bash
# On the Pi, after `sniffer install`:
./scripts/pi-smoke-test.sh                  # full test
./scripts/pi-smoke-test.sh --no-radio       # skip radio-dependent phases
./scripts/pi-smoke-test.sh --band 7         # test on a different LTE band
./scripts/pi-smoke-test.sh --earfcn 1850 --pci 271
                                            # pin a known cell
```

The script reports PASS / FAIL / SKIP per phase and prints expected
output + retry hints on failure. **Phase 8 (single-cell C-RNTI via
FalconEye) is the must-pass for the framework to be useful** — every
other phase is supporting cast. Logs land in `/tmp/{sim,live,survey,pytest}.log`.

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
