# cellular-drones — orientation for Claude

Most of this lives in code/docs/scripts. This file is the “how do I actually
run the thing on this Pi today” cheat-sheet so we don’t lose track between
sessions.

## Hardware / endpoints

- **Pi (run target):** Debian Trixie, USB-Ethernet `pi@169.254.1.2` (link-local),
  Wi-Fi `pi@10.0.0.1`. Passwordless SSH + sudo.
- **Pi Wi-Fi (AP mode):** Built-in `wlan0` (Broadcom BCM4345). NetworkManager
  profile `drone-ap` (SSID `drone`, WPA-PSK) puts the Pi at `10.0.0.1/24` in
  `shared` mode — NM runs DHCP/NAT for clients automatically. Autoconnect:
  on (priority 10), so it comes back up at boot. `nmcli connection up
  drone-ap ifname wlan0` to bring it up manually. The dashboard's
  `--host 0.0.0.0` binding means it serves on both eth0 and wlan0.
- **Browser binary on Pi:** `chromium` (not `chromium-browser`). Launch on
  the Pi’s X session, not the Mac:
  ```
  ssh pi@169.254.1.2 'DISPLAY=:0 setsid -f chromium --kiosk \
    --noerrdialogs --disable-infobars --no-first-run \
    http://127.0.0.1:8000/ </dev/null >/tmp/chromium.log 2>&1'
  ```
- **SDRs in active use:** Ettus B210 (`3367EB5`), Ettus B200mini, Ettus
  B205mini, HackRF (`f77c60dc298051c3`). Only **one** SDR can be open via
  UHD at a time per USB endpoint — concurrent processes will collide.

## Repos / paths

| What                            | Where on Pi                              |
|---------------------------------|------------------------------------------|
| Our Python project              | `~/cellular-drones`                      |
| Venv with `sniffer` installed   | `~/cellular-drones/.venv/bin/sniffer`    |
| FALCON source (forked at build) | `~/src/falcon`                           |
| FalconEye binary                | `~/src/falcon/build/src/FalconEye`       |
|                                 | symlinked at `/usr/local/bin/FalconEye`  |
| srsRAN_4G source                | `~/src/srsRAN_4G`                        |
| `srsran_cell_search`, `pdsch_ue`| `/usr/local/bin/`                        |
| rx_samples_to_file (UHD example)| `/usr/libexec/uhd/examples/rx_samples_to_file` |

The FALCON tree on the Pi has the **cellular-drones SpectrumTap patch**
applied (`patches/falcon-spectrum-tap.patch` in our repo,
auto-applied by `scripts/install-linux.sh`). The patch adds `-X <csv>`
and `-x <subframes>` to `FalconEye`; without those flags FalconEye
behaves exactly like upstream.

## Running the dashboard

All three modes share `--host 127.0.0.1 --port 8000` and the dashboard
URL `http://127.0.0.1:8000/`.

### Simulate (no radio needed)
```
ssh pi@169.254.1.2 'cd ~/cellular-drones && setsid -f .venv/bin/python \
  -m sniffer.live --simulate --spectrum \
  --host 127.0.0.1 --port 8000 \
  --out-dir /tmp/sniffer-runs \
  --mission-id ui-$(date +%s) >/tmp/sniffer-live.log 2>&1 </dev/null'
```
`--spectrum` in simulate mode → wide USRP sweep via `sniffer.uhd_sweep`
(700–2700 MHz, ~5–20 s/sweep). Needs an SDR plugged in but no cell lock.

### Real-radio FALCON (single cell — the main path)
Either via the wrapper (recommended):
```
ssh pi@169.254.1.2 'cd ~/cellular-drones && setsid -f .venv/bin/sniffer \
  live --earfcn 3050 --pci 275 --spectrum \
  --host 0.0.0.0 --port 18901 \
  --out-dir /tmp/sniffer-live >/tmp/sniffer-live.log 2>&1 </dev/null'
```
…or directly:
```
ssh pi@169.254.1.2 'cd ~/cellular-drones && setsid -f .venv/bin/python \
  -m sniffer.live --spectrum \
  --falcon-cmd "FalconEye -f 2650000000 -A 1 -g 70" --falcon-pci 275 \
  --host 0.0.0.0 --port 18901 --out-dir /tmp/sniffer-live \
  --mission-id ui-$(date +%s) >/tmp/sniffer-live.log 2>&1 </dev/null'
```
`--spectrum` + `--falcon-cmd` (or `--earfcn/--pci`) → `sniffer.live` appends
`-X /tmp/sniffer-live/spectrum-<mission>.csv` to FalconEye and tails it. The
dashboard waterfall then shows the **actual ~20 MHz around the locked cell**
(at 100 kHz resolution), not a wideband sweep. No second SDR needed.

### Control mode (scan + pick cells from the dashboard)
Start `sniffer.live` with **no producer flag** (no `--simulate`,
`--falcon-cmd`, or `--survey-cells`) and it boots *idle* with a live
**RADIO CONTROL** panel — the radio is retargetable from the browser
instead of frozen at launch:
```
ssh pi@169.254.1.2 'cd ~/cellular-drones && setsid -f .venv/bin/python \
  -m sniffer.live --spectrum --host 0.0.0.0 --port 18901 \
  --out-dir /tmp/sniffer-live >/tmp/sniffer-live.log 2>&1 </dev/null'
```
The panel lists tunable cells from `data/known_cells.jsonl`; click **Tune**
to retarget, **Scan** (band number) to discover more (releases the radio,
runs `sniffer scan`, repopulates the list), **Release radio** to idle.
`--falcon-cmd`/`--earfcn` still work and are *also* retargetable now — the
initial cell is just pre-loaded. Control endpoints (all POST except
`/cells`): `GET /cells`, `POST /retarget {earfcn,pci[,gain_db,antennas]}`,
`POST /scan {band}` (or `{earfcn_range:[lo,hi]}`), `POST /pause`. Under
`--simulate`/`--survey-cells` the radio isn't retargetable, so these
return HTTP 409.

Lock-state surfaced per cell (status `lock_state`, shown on the panel
badge): `acquiring` → `producing` → `locked-quiet` (cell idle, **not**
restarted anymore) / `lock-lost` (FALCON died after a lock → respawn) /
`no-lock` (wrong freq/no signal → give up, idle for a retarget). A merely
quiet cell no longer triggers the kill/respawn thrash the old watchdog had.

### Cell discovery (no dashboard)
```
ssh pi@169.254.1.2 'cd ~/cellular-drones && .venv/bin/sniffer scan --band 7'
```
Every cell `sniffer scan` finds is auto-logged to
`data/known_cells.jsonl` (see below).

## Known-cell store

`sniffer cells` maintains `data/known_cells.jsonl` — an append-only JSONL
of LTE cells we know about. Two sources merge into one file:

| source       | comes from                  | brings                          |
|--------------|-----------------------------|---------------------------------|
| `opencellid` | imported OpenCellID dump    | MCC/MNC/TAC/eci/operator/lat/lon |
| `scan`       | every `sniffer scan` run    | EARFCN/PCI/RSRP                  |
| `live-lock`  | first DCI FALCON decodes    | confirms a (EARFCN, PCI) tunes  |

OpenCellID gives geographic prior (where cells are, who operates them);
`sniffer scan` gives radio-layer specifics (which EARFCN/PCI to tune to).
Together they let `sniffer survey` start with a curated nearby list instead
of crawling a whole band.

### Import OpenCellID
1. Get a free API key at <https://opencellid.org/>.
2. Download the IL dump (MCC=425). It's a CSV (or .csv.gz):
   `https://opencellid.org/ocid/downloads?token=KEY&type=mcc&file=425.csv.gz`
3. Import:
   ```
   sniffer cells import-opencellid 425.csv.gz
   ```
   Defaults to filtering MCC=425 LTE; pass `--mcc 0` for all countries
   or `--radio ""` for all radios.

### Day-to-day
```
sniffer cells stats                       # source/operator breakdown
sniffer cells list --source opencellid    # everything imported
sniffer cells list --operator Cellcom
sniffer cells nearby --lat 32.0861 --lon 34.7815 --radius-km 2
sniffer cells nearby --lat ... --lon ... --json   # JSON for survey --cells
```

### Survey from known cells
Once both sources have meat in them, point survey at the union via
the `--json` output:
```
CELLS=$(sniffer cells nearby --lat 32.0861 --lon 34.7815 --radius-km 5 --json)
sniffer survey --cells "$CELLS"
```
The `--json` filter only emits rows that have *both* EARFCN and PCI
(what tuning needs), so OpenCellID-only rows fall out automatically.

### Override store path
Env var `SNIFFER_KNOWN_CELLS=/some/other.jsonl` (tests rely on this).

## Process / radio hygiene

- One process at a time per USRP. If you see `No UHD Devices Found`,
  some other process (FalconEye, rx_samples_to_file, srsran_cell_search,
  `uhd_find_devices` itself) is holding the device — `pgrep -af FalconEye`
  and friends, then `kill` cleanly.
- Don’t `pkill -9 -f chromium` over SSH from the Mac — it can match the
  SSH session and drop the connection. Use plain `pkill -f` first.
- The live process runs spectrum sweeps as a *subprocess* (`uhd_sweep`)
  which keeps spawning `rx_samples_to_file` per tile. Killing only the
  outer `sniffer.live` is enough — children get SIGHUP via setsid group.

## Rebuilding FALCON (after our patch changes)

```
ssh pi@169.254.1.2 'cd ~/src/falcon/build && make -j2 FalconEye'
```
**Use the `FalconEye` target explicitly.** Bare `make` tries to build the
Qt GUI parts (qcustomplot, rangewidget) and will fail without Qt headers.

## Verifying state

- HTTP: `curl -s http://127.0.0.1:8000/spectrum | python3 -m json.tool | head`
- Control surface: `curl -s http://127.0.0.1:8000/cells | python3 -m json.tool`
  lists tunable cells + `active` target; `curl -XPOST .../retarget -d
  '{"earfcn":3050,"pci":275}'` retunes; `-XPOST .../scan -d '{"band":7}'`
  scans; `-XPOST .../pause` idles. (409 under `--simulate`/`--survey-cells`.)
- Dashboard HTML element IDs added by this project: `radio`, `spec-pill`,
  `cell-panel`, `coach-banner`, `cp-pci`, `cp-fc`, `cp-gain`, `cp-rate-dci`,
  `ctl-panel`, `ctl-lock`, `ctl-cells`, `ctl-scan`, `ctl-pause`.
- FalconEye sanity: `FalconEye -h | grep -E "^\s+-[Xx]"` (must show
  `-X spectrum-tap CSV output file ...`).
