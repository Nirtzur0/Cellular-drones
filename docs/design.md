# Framework design

End-to-end design for a drone-mounted passive LTE UE sniffer built around
existing open-source stacks. Aimed at LTESniffer on USRP B210; positions
stationary UEs only (single-RX, single-receiver RSSI weighted centroid).

## 1. Goals

1. **Extract C-RNTIs** off the PDCCH of a target LTE cell using LTESniffer.
2. **Geo-tag** every decoded DCI with platform position, attitude, and a
   monotonic timestamp suitable for downstream TDOA-style analysis.
3. **Position UEs** in 3D using the drone trajectory as a synthetic aperture
   on the UE's UL grants (per-RNTI weighted RSS, stationary UEs only).
4. **Stay swappable**: a single measurement schema across radio backends
   so a simulator run and a B210 mission produce the same downstream
   artifacts.

## 2. Existing tools used

Decisions are anchored to maintained projects rather than rewrites.

| Tool | Role | Notes |
| --- | --- | --- |
| [LTESniffer](https://github.com/SPRITZ-Research-Group/LTESniffer) | PDCCH blind-decode → per-DCI C-RNTI + grant info | Requires srsRAN_4G build deps + USRP. DL-only mode is the production path here. |
| [srsRAN_4G](https://github.com/srsRAN/srsRAN_4G) | LTE PHY library LTESniffer links against | Built once; LTESniffer's CMake picks it up automatically. |
| [gpsd](https://gpsd.gitlab.io) + `gpspipe` | NMEA → JSON TPV stream | We just tail `gpspipe -w` and parse to our geotag schema. |
| UHD | USRP host-driver | Plain `libuhd-dev` from apt; no IO/perf tuning required for B210 over USB3. |

There is **no** in-tree HackRF / cell-discovery surface — the prior
`LTE-Cell-Scanner` path was removed in v0.2 along with the macOS bits.
Pick the target EARFCN + PCI out-of-band (CellMapper, OpenCellID, or
`srsRAN_cell_search`) and feed it to `sniffer live --earfcn N --pci P`.

## 3. Architecture

```
┌─────────────────────────────────────────────┐  ┌──────────────────────┐
│ Airborne payload                            │  │ Post-flight          │
│                                             │  │                      │
│  Antenna ─► USRP B210 ─► LTESniffer ─►      │  │   • report.py        │
│             (UHD)        DECODED text       │  │   • localize.py      │
│                              │              │  │   • make_plot()      │
│                              ▼              │  │                      │
│              normalize_ltesniffer.py        │  └──────────────────────┘
│                              │              │           ▲
│                              ▼              │           │
│                  parse_ltesniffer.py        │           │
│                              │              │           │
│                              ▼              │  geotagged-<mission>.jsonl
│                  ue-<mission>.jsonl  ───────┼──────┐
│                                             │      │
│  GPS module ─► gpsd ─► gpspipe ─► parse_gpsd│      │
│                              │              │      │
│                              ▼              │      ▼
│                  gps-<mission>.jsonl  ──────┼──► geotag.join()
│                                             │
└─────────────────────────────────────────────┘
```

### Components

- **LTESniffer (driven by `sniffer live`)** — `cli.py` builds the
  binary's argv from `--earfcn/--pci/--rx-gain`, spawns it as a
  subprocess inside `live.py`, pipes stdout through `normalize_stream`
  in-process, and feeds the result to the Python parser writing JSONL.
- **normalize_ltesniffer** — permissive field-renaming step. LTESniffer's
  text output drifts between versions; this layer keeps the downstream
  schema stable.
- **parse_ltesniffer** — emits one `ue_sighting` JSONL record per DCI.
  Records carry UL RSSI for UL grants and DL RSRP for DL grants. Both
  are stored, but only UL grants feed the per-UE localizer.
- **gpsd / parse_gpsd** — one `geotag` record per fix at ~10 Hz.
- **geotag.join** — single-pass O(n+m) merge on monotonic time, ±500 ms
  window. UE sightings older than the GPS fix window are dropped.
- **localize** — per-(PCI, C-RNTI) RSSI weighted centroid, computed
  in a local ENU frame. Single estimator, no fallback path.
- **live** — stdlib HTTP server + SSE; pushes per-UE updates to the
  browser dashboard as DCIs come in.
- **report** — text summary + matplotlib PNG of the per-UE positions
  alongside the drone trajectory.

## 4. Record schema

One JSONL row per decoded DCI; see `docs/ue-sniffing.md` for the full
example. Key invariants:

- `mission_id` groups all artifacts of one flight.
- `ts_mono_ns` is the join key — same monotonic clock on UE-sighting
  records and geotag records on a given host.
- `kind == "ue_sighting"` for DCIs; `kind == "geotag"` for GPS fixes.
- `ue.direction` is `"ul"` or `"dl"`. The localizer keys off this.

The schema is dependency-free Python dataclasses (`src/sniffer/schema.py`)
so it runs on a constrained airborne SBC without dragging numpy in.

## 5. Why UL grants only for positioning

DL grants encode the eNB → UE direction. The energy our passive receiver
sees on those subframes is the eNB's transmission — the same signal for
every UE on the cell. Using DL grants would trilaterate the eNB, not the
UE.

UL grants schedule the UE's own PUSCH transmission. When the UE
transmits, our drone receiver measures *that*, and the per-UE energy
becomes the localization observable. The localizer therefore filters
`ue.direction == "ul" AND ue.ul_rssi_dbm IS NOT NULL`.

For a UE that's mostly DL-bound (streaming) you'll see lots of grants but
few UL ones — the dashboard flags this with a "need ≥ 2 UL grants"
message and the position estimate stays null.

## 6. Known limits

- **Mobile UEs bias the estimate.** The weighted-centroid math
  assumes a static emitter; a UE that moves during the integration
  window biases the estimate toward the centroid of its own motion.
  No software fix exists for a single-RX passive sniffer.
- **C-RNTI rotates.** Connection-scoped IDs reset on RRC release.
  Per-RNTI tracks here are connection-scoped, not subscriber-scoped.
- **No 5G NR.** LTESniffer is LTE-only. SUCI on 5G would close the
  identity hole regardless.
- **Single-RX TDOA is impossible.** The synthetic-aperture math works
  for static emitters; UEs that move at all need ≥ 2 GPSDO-disciplined
  receivers.

## 7. Where the GPS comes from

The drone needs a position stream paired with every UE sighting. The
framework reads a single canonical record schema (`geotag` records, see
`sniffer.schema.GpsFix`) so the source is swappable. There are three
realistic places to get the data from, in order of recommendation:

### 7.1 USB or UART GPS → gpsd → gpspipe (default, wired today)

A dedicated NMEA-capable GPS receiver attached to the SBC over
USB/UART. `gpsd` does the parsing; the dashboard tails `gpspipe -w`
and feeds `sniffer.parse_gpsd`. This is what `sniffer live` uses today
when `gpspipe` is on `PATH`.

| Pros | Cons |
| --- | --- |
| Plug-and-play with almost any GPS (u-blox 7/8/9, BU-353, USGlobalSat) | Adds one more peripheral the airframe has to carry |
| RTK-capable (gpsd handles RTCM3 inputs) | gpsd is a daemon — has to be alive on the drone |
| Decoupled from the flight stack; survives FC reboots | No coupling to autopilot attitude/heading |

Wiring:

```bash
# Pi side, one-time:
sudo apt install gpsd gpsd-clients
sudo systemctl enable --now gpsd
# Plug receiver into USB → /dev/ttyACM0 (auto-discovered by gpsd)

# Then on the same host:
sniffer live --earfcn 1850 --pci 271     # automatically picks up gpspipe
```

### 7.2 MAVLink from the flight controller

The Pixhawk / PX4 / ArduPilot already owns the authoritative GPS lock
for navigation. A companion computer reads
`GLOBAL_POSITION_INT` (msgid 33) or `GPS_RAW_INT` (msgid 24) over the
FC's UART or a UDP MAVLink endpoint.

**Why bother**: one fewer antenna on the airframe, and the position
stream is already disciplined against the FC's EKF, which fuses GPS
with IMU/baro — useful when GPS coverage briefly degrades.

**Why not the default**: the framework deliberately doesn't bind to a
particular flight stack. A MAVLink dropout in flight is much worse than
a separate-GPS dropout.

Sketch (`pymavlink`, ~10 lines):

```python
from pymavlink import mavutil
mav = mavutil.mavlink_connection("udpin:0.0.0.0:14550")  # or /dev/ttyAMA0
while True:
    msg = mav.recv_match(type="GLOBAL_POSITION_INT", blocking=True)
    fix = GpsFix(lat=msg.lat / 1e7, lon=msg.lon / 1e7,
                 alt_m=msg.alt / 1e3, fix="3d", hdop=None)
    # emit a geotag JSONL line here
```

Pull this into `sniffer/parse_mavlink.py` when you have a Pixhawk in
hand to test against. Don't ship it without the test rig.

### 7.3 Direct u-blox UBX binary

Skip both gpsd and the flight controller. Open the GPS UART, parse UBX
frames (`NAV-PVT` is the position-velocity-time message), emit
`GpsFix`. Gives you raw observables (carrier phase, doppler) if you
ever want to PPK-post-process for sub-meter accuracy after the
mission. Operationally only worth it if you're chasing TDOA-grade
clock discipline.

### Time discipline

`ts_mono_ns` is the join key between UE sightings and geotag records.
For all three sources, the recommended setup is GPS PPS into chrony or
ptp4l on the SBC, so `time.monotonic_ns()` advances at a clean rate
disciplined against UTC. Without it, host clock drift bounds how
tightly the geotag join window can shrink (`GEOTAG_MAX_AGE_MS = 500`
in `live.py` is generous on purpose for this reason).

### Feed rate

| Source | Typical rate | Geotag window dominated by |
| --- | --- | --- |
| u-blox @ 10 Hz | 100 ms | LTESniffer DCI cadence (~20 Hz) |
| Phone-class GPS @ 1 Hz | 1000 ms | GPS rate (will exceed the 500 ms join window) |
| MAVLink default | 5 Hz | GPS rate |

If you're seeing the dashboard say *"need ≥ 2 geo-tagged grants"* but
UL grants are flowing, the GPS rate is probably too slow to match the
join window — raise the GPS update rate before debugging the parser.

## 8. Drone power budget (real-radio path)

| Component | Typical W | Notes |
| --- | --- | --- |
| USRP B210 | 4.5 | USB-3 host, gain ~50 dB |
| Linux SBC (e.g. Pi 5 / NUC-class) | 7–15 | LTESniffer + Python pipeline |
| NVMe storage | 2.5 | Optional; JSONL is tiny, but raw-IQ recording isn't |
| GPSDO module | 1.0 | Disciplined oscillator for clean clock |
| u-blox GPS | 0.5 | NMEA / PPS |
| LNA + bias-tee | 0.3 | Band-dependent, optional |
| **Total** | **~16–24 W** | Comfortable on the M350-class payload bus |
