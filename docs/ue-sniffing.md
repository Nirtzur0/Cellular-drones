# UE sniffing (C-RNTI + per-UE positioning)

This document describes the rebuilt primary flow of the framework: pull
C-RNTIs off the PDCCH with LTESniffer and localize each UE from a moving
passive receiver.

## What you get

- **Identity**: per-UE C-RNTI captured from PDCCH blind decode, per cell
  (PCI). DCI format, MCS, PRB allocation, TBS — everything LTESniffer
  emits. C-RNTI is a temporary, per-connection ID; see *Caveats* below.
- **Position**: per-(PCI, C-RNTI) location estimate from an RSSI
  weighted-centroid estimator, fed by UL grants only.

## What you do *not* get

- **Subscriber identity** (IMSI / SUPI / phone number). Passive LTE
  reveals C-RNTI/TMSI/GUTI; the real identifiers stay encrypted past
  the RRC layer. To get IMSI you need either to operate the eNB (lab)
  or run a rogue cell — the latter is out of scope for this framework
  and is gated by jurisdictional authorization.
- **5G UE identity**. The standard encrypts IMSI as SUCI before
  transmission. LTESniffer is LTE-only.
- **Mobile-UE positioning from a single radio**. The
  weighted-centroid math only converges on stationary emitters. See
  *Limits* below.

## Pipeline

Everything runs inside `sniffer live` — there is no offline join step.

```
LTESniffer stdout ──► normalize_stream ──► parse_ltesniffer ─┐
                                                              │
gpspipe -w stdout ──► parse_gpsd ─────────────────────────────┤
                                                              ▼
                                                          live.State
                                                  (in-memory join, ≤500 ms;
                                                   weighted_centroid_ue)
                                                              │
                                                              ▼
                                                  SSE → http://127.0.0.1:8000/
```

The simulator follows exactly the same path: `simulate.ltesniffer_lines`
and `simulate.gpsd_lines` emit the same wire text, paced to wall clock
and piped through the same parsers.

## Quick start

There is one run path: `sniffer live`. It does C-RNTI extraction
*and* per-UE positioning on the same dashboard.

### Hardware-free (validates the pipeline)

```bash
pip install -r requirements.txt
pip install -e .

sniffer live --simulate
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
sniffer install                                       # one-time
sniffer live --earfcn 1850 --pci 271 --rx-gain 50     # spawns LTESniffer + UI
# open http://127.0.0.1:8000/
```

GPS is auto-ingested from `gpspipe -w` if `gpsd` is running on the
host. No separate step.

For drone platforms, `--droneid-cmd` lets you skip the USB GPS and
sniff the drone's own RemoteID broadcast directly with an SDR. See
`docs/design.md` § 7.4 for the supported decoders and tradeoffs.

Pick the target EARFCN + PCI by running the bundled cell-discovery
wrapper:

```bash
sniffer scan --band 3                 # sweep band 3 (1800 MHz)
sniffer scan --band 3 --decode-sib1   # + operator (PLMN), TAC, CGI
```

…or from CellMapper / OpenCellID if no USRP is plugged in.

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
    "ta_n_steps": 14,
    "ta_meters": 1093.75,
    "raw": {"frame": "512", "subframe": "3"}
  },
  "gps": {"lat": 32.085, "lon": 34.781, "alt_m": 30.0, "fix": "rtk_fix", "age_ms": 14},
  "schema_version": 1
}
```

DL grants are stored too (for an activity timeline) but never feed the
localizer — DL energy is the eNB's transmission, the same for every UE
on the cell.

`ta_n_steps` and `ta_meters` are populated only when the upstream
LTESniffer build emits Timing Advance per grant (the simulator does;
the published PDCCH-only build does not — see `docs/localization.md`).
When present, they feed a second, independent positioning path —
`ta_multilateration` — that the dashboard shows alongside the
RSSI-centroid estimate as a separately labelled position. Neither
estimator is a fallback for the other.

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

The weighted centroid assumes a static emitter; the math integrates
RSSI over the drone's trajectory. If the UE moves during the
integration window the estimate biases toward the centroid of the UE's
own motion, not its current position. The simulator's mobile UE shows
this directly: ~200 m bias for a UE walking 200 m over the mission.

There is no software fix to a single-RX passive sniffer. Real
positioning of mobile UEs would need multi-static TDOA across
synchronized receivers, which is not in the codebase.

### UL grants are intermittent

The localizer needs ≥ 2 UL grants with GPS attached before it produces
an estimate. A UE that's mostly DL-bound
(streaming, browsing) might not give you enough UL volume in a short
window. Watch the *UL / DL grants* column in the dashboard — if it's
heavily skewed to DL, the UE is "heard but unlocalisable."

## Adapting LTESniffer's actual output

Real LTESniffer text drifts across versions. `sniffer.parse_ltesniffer`
handles both halves of the text-to-record translation in one module:

1. `normalize_stream` uses permissive regexes to recognise `KEY=value`
   and `KEY: value` fields in any order, normalises field names
   (`rnti` → `c_rnti`, `RBs` → `prb`, etc.), and emits canonical
   `DECODED key=value` lines.
2. `parse_stream` reads those canonical lines and emits
   schema-conformant `ue_sighting` records.

If your LTESniffer build emits a format the normaliser doesn't handle,
add the field to `_FIELD_MAP` at the top of
`src/sniffer/parse_ltesniffer.py`. `tests/test_parse_ltesniffer.py`
is the regression net.
