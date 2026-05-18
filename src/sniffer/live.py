"""Realtime browser dashboard for C-RNTI sniffing + per-UE positioning.

LTESniffer feeds DCI events; gpsd feeds positions. The aggregator joins
them in monotonic time, runs the per-UE localizer, and pushes per-UE
state to connected browsers over Server-Sent Events. Stdlib HTTP only.

Run:
    python -m sniffer.live --simulate                # no radio needed
    python -m sniffer.live --ltesniffer-cmd "..."    # real LTESniffer

Open http://127.0.0.1:8000/ in a browser.
"""

from __future__ import annotations

import argparse
import bisect
import io
import json
import math
import os
import queue
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from sniffer.localize import path_loss_wls_ue, weighted_centroid_ue
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.parse_ltesniffer import parse_stream as parse_ltesniffer_stream
from sniffer.schema import (
    GpsFix,
    RadioConfig,
    UeEvent,
    UeSighting,
    mono_ns,
    utc_iso,
)
from sniffer.simulate import (
    Emitter,
    _default_ues,
    _interp_waypoint,
    _rsrp_dbm,
    _ue_position,
    _ul_rssi_dbm,
    box_trajectory,
)


# Knobs --------------------------------------------------------------------
GPS_BUFFER_MAX = 4000          # ~10 min at 10 Hz
GEOTAG_MAX_AGE_MS = 500        # drop sightings >500 ms from nearest fix
UE_HISTORY_MAX = 400           # per-UE UL-grant history retained for positioning
RSSI_HISTORY_MAX = 120         # rolling sparkline points
WLS_EVERY = 12                 # recompute path_loss_wls every N sightings


# --------------------------------------------------------------------------
# Aggregator + broadcaster
# --------------------------------------------------------------------------


class State:
    """Per-UE rolling state plus a fan-out queue list for SSE clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key: (pci, c_rnti)
        self._ues: dict[tuple[int, int], dict[str, Any]] = {}
        self._clients: list[queue.Queue[str]] = []
        self._started_mono_ns = mono_ns()
        self._total_ue_sightings = 0
        self._scan_status: dict[str, Any] = {"phase": "idle", "ts_utc": utc_iso()}
        # GPS buffer: list of {ts_mono_ns, ts_utc, gps:{lat,lon,alt_m,fix,hdop}}
        self._gps: list[dict[str, Any]] = []
        self._gps_ts_keys: list[int] = []  # parallel array for bisect
        self._latest_gps: Optional[dict[str, Any]] = None

    # --- GPS --------------------------------------------------------------

    def add_gps(self, fix: GpsFix, *, ts_mono_ns_: Optional[int] = None,
                ts_utc_: Optional[str] = None) -> None:
        ts = ts_mono_ns_ if ts_mono_ns_ is not None else mono_ns()
        iso = ts_utc_ if ts_utc_ is not None else utc_iso()
        rec = {"ts_mono_ns": ts, "ts_utc": iso,
               "gps": {"lat": fix.lat, "lon": fix.lon, "alt_m": fix.alt_m,
                       "fix": fix.fix, "hdop": fix.hdop}}
        with self._lock:
            if not self._gps or ts >= self._gps_ts_keys[-1]:
                self._gps.append(rec)
                self._gps_ts_keys.append(ts)
            else:
                idx = bisect.bisect_left(self._gps_ts_keys, ts)
                self._gps.insert(idx, rec)
                self._gps_ts_keys.insert(idx, ts)
            if len(self._gps) > GPS_BUFFER_MAX:
                drop = len(self._gps) - GPS_BUFFER_MAX
                self._gps = self._gps[drop:]
                self._gps_ts_keys = self._gps_ts_keys[drop:]
            self._latest_gps = rec
        self._broadcast({"type": "gps", "fix": rec})

    def _nearest_gps_locked(self, ts_mono_ns_: int,
                            max_age_ms: int = GEOTAG_MAX_AGE_MS):
        if not self._gps:
            return None
        idx = bisect.bisect_left(self._gps_ts_keys, ts_mono_ns_)
        candidates: list[dict[str, Any]] = []
        if idx < len(self._gps):
            candidates.append(self._gps[idx])
        if idx > 0:
            candidates.append(self._gps[idx - 1])
        best = min(candidates, key=lambda r: abs(r["ts_mono_ns"] - ts_mono_ns_))
        age_ms = abs(best["ts_mono_ns"] - ts_mono_ns_) / 1e6
        if age_ms > max_age_ms:
            return None
        return best, int(age_ms)

    # --- ingest -----------------------------------------------------------

    def ingest_ue(self, sighting: UeSighting) -> dict[str, Any]:
        pci = sighting.ue.pci
        c_rnti = sighting.ue.c_rnti
        key = (pci, c_rnti)
        now_iso = sighting.ts_utc or utc_iso()
        ue = sighting.ue
        with self._lock:
            entry = self._ues.get(key)
            if entry is None:
                entry = {
                    "key": f"{pci}-{c_rnti:#06x}",
                    "pci": pci,
                    "c_rnti": c_rnti,
                    "c_rnti_hex": f"{c_rnti:#06x}",
                    "center_hz": sighting.radio.center_hz,
                    "first_seen": now_iso,
                    "count": 0,
                    "ul_count": 0,
                    "dl_count": 0,
                    "dci_formats": [],
                    "ul_rssi_history": deque(maxlen=RSSI_HISTORY_MAX),
                    "geo_history": deque(maxlen=UE_HISTORY_MAX),
                    "est_position": None,
                    "_since_wls": 0,
                }
                self._ues[key] = entry

            entry["last_seen"] = now_iso
            entry["count"] += 1
            if ue.direction == "ul":
                entry["ul_count"] += 1
            elif ue.direction == "dl":
                entry["dl_count"] += 1
            if ue.dci_format and ue.dci_format not in entry["dci_formats"]:
                entry["dci_formats"] = entry["dci_formats"] + [ue.dci_format]
            entry["mcs"] = ue.mcs
            entry["n_prb"] = ue.n_prb
            entry["tbs_bytes"] = ue.tbs_bytes
            if ue.ul_rssi_dbm is not None:
                entry["ul_rssi_dbm"] = ue.ul_rssi_dbm
                entry["ul_rssi_history"].append(
                    [now_iso, round(ue.ul_rssi_dbm, 2)]
                )
            if ue.dl_rsrp_dbm is not None:
                entry["dl_rsrp_dbm"] = ue.dl_rsrp_dbm
            self._total_ue_sightings += 1

            if ue.direction == "ul" and ue.ul_rssi_dbm is not None:
                hit = self._nearest_gps_locked(sighting.ts_mono_ns)
                if hit is not None:
                    gps_rec, age_ms = hit
                    entry["geo_history"].append({
                        "kind": "ue_sighting",
                        "ts_mono_ns": sighting.ts_mono_ns,
                        "gps": {**gps_rec["gps"], "age_ms": age_ms},
                        "ue": {"pci": pci, "c_rnti": c_rnti,
                               "direction": "ul",
                               "ul_rssi_dbm": ue.ul_rssi_dbm},
                    })
                    entry["_since_wls"] += 1
                    _recompute_ue_position(entry)

            payload = _entry_to_dict_ue(entry)
        self._broadcast({"type": "ue_sighting", "ue": payload})
        return payload

    def set_status(self, phase: str, **extra: Any) -> None:
        with self._lock:
            self._scan_status = {"phase": phase, "ts_utc": utc_iso(), **extra}
            status = dict(self._scan_status)
        self._broadcast({"type": "status", "status": status})

    # --- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            ues = [_entry_to_dict_ue(e) for e in self._ues.values()]
            ues.sort(
                key=lambda u: (
                    u.get("est_position") is None,
                    -(u.get("ul_count") or 0),
                    -(u.get("ul_rssi_dbm") or -1e9),
                )
            )
            return {
                "type": "snapshot",
                "ues": ues,
                "status": dict(self._scan_status),
                "latest_gps": self._latest_gps,
                "total_ue_sightings": self._total_ue_sightings,
                "uptime_s": (mono_ns() - self._started_mono_ns) / 1e9,
            }

    # --- fan-out ----------------------------------------------------------

    def register(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=512)
        with self._lock:
            self._clients.append(q)
        q.put(json.dumps(self.snapshot()))
        return q

    def unregister(self, q: queue.Queue[str]) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event: dict[str, Any]) -> None:
        msg = json.dumps(event)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # slow client; drop


def _entry_to_dict_ue(entry: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in entry.items()
           if k not in ("ul_rssi_history", "geo_history", "_since_wls")}
    out["ul_rssi_history"] = list(entry["ul_rssi_history"])
    out["n_geo_samples"] = len(entry["geo_history"])
    trail = list(entry["geo_history"])[-40:]
    out["trail"] = [{"lat": g["gps"]["lat"], "lon": g["gps"]["lon"],
                     "ul_rssi_dbm": g["ue"]["ul_rssi_dbm"]} for g in trail]
    return out


def _recompute_ue_position(entry: dict[str, Any]) -> None:
    records = list(entry["geo_history"])
    if len(records) < 2:
        return
    centroid = weighted_centroid_ue(records)
    if centroid is None:
        return
    est = {
        "lat": centroid.lat, "lon": centroid.lon,
        "alt_m": centroid.alt_m, "cep95_m": centroid.cep95_m,
        "method": "weighted_centroid",
        "n_samples": centroid.n_samples,
        "altitude_estimated": centroid.altitude_estimated,
    }
    if entry["_since_wls"] >= WLS_EVERY and len(records) >= 6:
        try:
            wls = path_loss_wls_ue(records)
        except Exception:
            wls = None
        if wls is not None and math.isfinite(wls.cep95_m):
            est = {
                "lat": wls.lat, "lon": wls.lon,
                "alt_m": wls.alt_m, "cep95_m": wls.cep95_m,
                "method": "path_loss_wls",
                "n_samples": wls.n_samples,
                "altitude_estimated": wls.altitude_estimated,
            }
        entry["_since_wls"] = 0
    entry["est_position"] = est


# --------------------------------------------------------------------------
# Sinks: bridge parse_stream's JSONL output into the State aggregator
# --------------------------------------------------------------------------


class _UeSightingSink(io.TextIOBase):
    """parse_ltesniffer writes JSONL strings here; we decode + push to State."""

    def __init__(self, state: State, jsonl_out: Optional[io.TextIOBase] = None):
        super().__init__()
        self._state = state
        self._jsonl_out = jsonl_out
        self._buf = ""

    def write(self, s: str) -> int:
        if self._jsonl_out is not None:
            self._jsonl_out.write(s)
            self._jsonl_out.flush()
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._ingest_record(rec)
        return len(s)

    def flush(self) -> None:
        if self._jsonl_out is not None:
            self._jsonl_out.flush()

    def _ingest_record(self, rec: dict[str, Any]) -> None:
        radio = rec.get("radio") or {}
        ue = rec.get("ue") or {}
        sighting = UeSighting(
            mission_id=rec.get("mission_id", ""),
            capture_id=rec.get("capture_id", ""),
            ts_mono_ns=rec.get("ts_mono_ns", mono_ns()),
            ts_utc=rec.get("ts_utc", utc_iso()),
            radio=RadioConfig(**{k: radio.get(k)
                                 for k in RadioConfig.__dataclass_fields__
                                 if k in radio}),
            ue=UeEvent(**{k: ue.get(k)
                          for k in UeEvent.__dataclass_fields__
                          if k in ue}),
            notes=rec.get("notes", ""),
        )
        self._state.ingest_ue(sighting)


class _GpsSink(io.TextIOBase):
    """parse_gpsd writes GeotagRecord JSONL here; we decode + push to State."""

    def __init__(self, state: State):
        super().__init__()
        self._state = state
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            gps = rec.get("gps") or {}
            fix = GpsFix(
                lat=float(gps.get("lat", 0.0)),
                lon=float(gps.get("lon", 0.0)),
                alt_m=float(gps.get("alt_m", 0.0)),
                fix=gps.get("fix", "unknown"),
                hdop=gps.get("hdop"),
            )
            self._state.add_gps(fix,
                                ts_mono_ns_=rec.get("ts_mono_ns"),
                                ts_utc_=rec.get("ts_utc"))
        return len(s)


# --------------------------------------------------------------------------
# Producers
# --------------------------------------------------------------------------


class _ParseArgs:
    def __init__(self, mission_id: str, backend: str, device: str,
                 rx_gain_db: float = 50.0,
                 center_hz: Optional[float] = None,
                 sample_rate_sps: Optional[float] = None):
        self.mission_id = mission_id
        self.backend = backend
        self.device = device
        self.rx_gain_db = rx_gain_db
        self.center_hz = center_hz
        self.sample_rate_sps = sample_rate_sps


def run_ltesniffer_loop(state: State, *, ltesniffer_cmd: list[str],
                        mission_id: str, out_dir: str,
                        center_hz: Optional[float], rx_gain_db: float,
                        stop: threading.Event) -> None:
    """Spawn LTESniffer, stream its DECODED stdout through parse_ltesniffer.

    The user provides a fully-formed argv via `--ltesniffer-cmd`: this gives
    them control over which binary, mode, frequency, gain, etc. The process
    is restarted on exit until `stop` is set.

    Binaries that don't natively emit `DECODED key=value` lines should be
    wrapped by `scripts/ue-sniff.sh` (which pipes through the normaliser).
    """
    if not ltesniffer_cmd:
        state.set_status("error", message="no --ltesniffer-cmd provided")
        return
    if (shutil.which(ltesniffer_cmd[0]) is None
            and not os.path.exists(ltesniffer_cmd[0])):
        state.set_status(
            "error",
            message=(f"`{ltesniffer_cmd[0]}` not found. Build LTESniffer "
                     f"(see scripts/install-linux.sh) and pass --ltesniffer-cmd."),
        )
        return

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")
    parse_args = _ParseArgs(mission_id, "ltesniffer", "usrp-b210-0",
                            rx_gain_db=rx_gain_db, center_hz=center_hz,
                            sample_rate_sps=23.04e6)

    while not stop.is_set():
        state.set_status("sniffing", center_hz=center_hz,
                         gain_db=rx_gain_db, out=out_path)
        proc = subprocess.Popen(
            ltesniffer_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        try:
            with open(out_path, "a", encoding="utf-8") as jsonl_fh:
                sink = _UeSightingSink(state, jsonl_out=jsonl_fh)
                assert proc.stdout is not None
                parse_ltesniffer_stream(proc.stdout, parse_args, sink)
        except Exception as exc:  # noqa: BLE001
            state.set_status("error", message=f"ltesniffer parse failed: {exc}")
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            proc.wait()
        if stop.is_set():
            return
        time.sleep(1.0)


def run_gpsd_loop(state: State, *, mission_id: str,
                  stop: threading.Event) -> None:
    """Stream gpsd TPV messages into State via gpspipe -w."""
    if shutil.which("gpspipe") is None:
        # GPS is optional — silently skip if gpsd isn't installed.
        return
    while not stop.is_set():
        proc = subprocess.Popen(
            ["gpspipe", "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        try:
            sink = _GpsSink(state)
            assert proc.stdout is not None
            parse_gpsd_stream(proc.stdout, sink, mission_id)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if stop.is_set():
            return
        time.sleep(1.0)


def run_simulator(state: State, *, mission_id: str,
                  stop: threading.Event) -> None:
    """Drive State directly from a moving-drone simulation.

    Walks a box trajectory around a synthetic eNB in wall-clock time:
    GpsFix at ~10 Hz, per-UE LTESniffer-style DCI events at ~10 Hz. The UE
    list is the same one the demo + tests use so behaviour is consistent
    across surfaces.
    """
    state.set_status("simulating", mission_id=mission_id)
    emitter = Emitter(lat=32.0853, lon=34.7818, alt_m=25.0, pci=271,
                      center_hz=1_842_500_000, n_id_1=90, n_id_2=1,
                      tx_power_dbm=24.0)
    ues = _default_ues(emitter)
    trajectory = box_trajectory(emitter, half_size_m=100.0,
                                altitudes=(15.0, 30.0, 60.0),
                                n_per_side=20, leg_speed_mps=8.0)
    rng = random.Random(1337)

    gps_period = 0.1
    ue_period = 0.1
    detect_threshold = -115.0
    ul_detect_threshold = -120.0

    t_start_wall = time.monotonic()
    last_gps_t = -1.0
    last_ue_t = -1.0
    t_total = trajectory[-1].t_offset_s

    while not stop.is_set():
        t_sim = time.monotonic() - t_start_wall
        if t_sim > t_total:
            t_start_wall = time.monotonic()
            continue
        w = _interp_waypoint(trajectory, t_sim)
        if w is None:
            time.sleep(0.05)
            continue

        if t_sim - last_gps_t >= gps_period:
            last_gps_t = t_sim
            jlat = w.lat + rng.gauss(0.0, 5e-6)
            jlon = w.lon + rng.gauss(0.0, 5e-6)
            jalt = w.alt_m + rng.gauss(0.0, 0.3)
            state.add_gps(GpsFix(lat=jlat, lon=jlon, alt_m=jalt,
                                 fix="rtk_fix", hdop=0.6))

        if t_sim - last_ue_t >= ue_period:
            last_ue_t = t_sim
            dl_rsrp = _rsrp_dbm(emitter, w, rng, 1.5)
            if dl_rsrp >= detect_threshold:
                for ue in ues:
                    p_grant = ue.activity_rate_hz * ue_period
                    if rng.random() >= p_grant:
                        continue
                    ue_pos = _ue_position(ue, t_sim)
                    is_ul = rng.random() < ue.ul_share
                    radio = RadioConfig(
                        backend="sim", device="sim-ltesniffer",
                        center_hz=emitter.center_hz, sample_rate_sps=23.04e6,
                    )
                    if is_ul:
                        rssi = _ul_rssi_dbm(ue, ue_pos, w, emitter.center_hz,
                                            rng, 1.5)
                        if rssi < ul_detect_threshold:
                            continue
                        ue_ev = UeEvent(
                            pci=emitter.pci, c_rnti=ue.c_rnti,
                            direction="ul", dci_format="0",
                            mcs=rng.randint(2, 24),
                            n_prb=rng.choice([1, 2, 4, 8, 16]),
                            ul_rssi_dbm=round(rssi, 2),
                        )
                    else:
                        ue_ev = UeEvent(
                            pci=emitter.pci, c_rnti=ue.c_rnti,
                            direction="dl", dci_format="1A",
                            mcs=rng.randint(4, 27),
                            n_prb=rng.choice([2, 4, 8, 16, 25, 50]),
                            dl_rsrp_dbm=round(dl_rsrp, 2),
                        )
                    state.ingest_ue(UeSighting(
                        mission_id=mission_id, capture_id="sim-ltesniffer",
                        ts_mono_ns=mono_ns(), ts_utc=utc_iso(),
                        radio=radio, ue=ue_ev,
                        notes="source=simulator",
                    ))

        time.sleep(0.03)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Cellular drones · live · UEs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
       background:#0b0d10; color:#e6e8eb; }
header { padding: 14px 20px; border-bottom: 1px solid #1f242a;
         display:flex; align-items:center; justify-content:space-between; gap:18px;
         flex-wrap: wrap; }
header h1 { font-size: 16px; font-weight: 600; margin: 0; }
.meta { font-size: 12px; color:#8a93a0; display:flex; gap:18px; flex-wrap: wrap; }
.meta strong { color:#e6e8eb; font-weight:600; }
.status-pill { padding:2px 10px; border-radius: 999px; font-size: 11px; border:1px solid #2a3038; }
.status-sniffing { color:#7ad9a1; border-color:#1d4032; background:#11211a; }
.status-simulating { color:#f0c270; border-color:#403118; background:#21190f; }
.status-error { color:#f08580; border-color:#4a1f1f; background:#2a1414; }
.status-idle { color:#8a93a0; }
.gps-pill { font-size: 11px; padding: 2px 10px; border:1px solid #2a3038; border-radius:999px;
            color:#8a93a0; font-family: ui-monospace, monospace; }
.gps-pill.fix-3d, .gps-pill.fix-rtk_fix, .gps-pill.fix-rtk_float { color:#7ad9a1; border-color:#1d4032; }
.gps-pill.fix-2d { color:#f0c270; border-color:#403118; }
.gps-pill.fix-none, .gps-pill.fix-unknown { color:#8a93a0; }
main { padding: 16px 20px; }
.banner { background:#1a1610; border:1px solid #403118; color:#f0c270;
          font-size: 12px; padding: 8px 12px; border-radius: 5px;
          margin-bottom: 14px; }
.banner strong { color:#f7d99c; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1a1f25; font-size: 13px; vertical-align: top; }
th { font-weight: 600; color:#8a93a0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
tr.fresh td { background: #1c1d2a; transition: background 1.4s ease; }
.rsrp { font-weight: 600; }
.rsrp-strong { color:#7ad9a1; }
.rsrp-mid { color:#f0c270; }
.rsrp-weak { color:#f08580; }
.spark { width: 120px; height: 28px; vertical-align: middle; }
.minimap { width: 110px; height: 84px; background:#13171c; border-radius:4px; vertical-align: middle; }
.empty { padding: 36px 20px; text-align: center; color:#8a93a0; font-size: 13px;
         border:1px dashed #2a3038; border-radius: 6px; }
.log { margin-top: 24px; font-family: ui-monospace, monospace; font-size:11px;
       color:#8a93a0; max-height: 160px; overflow-y: auto; padding: 8px 0;
       border-top: 1px solid #1a1f25; }
.log div { padding: 2px 0; }
.log .ts { color:#525a66; margin-right: 8px; }
.dim { color:#8a93a0; font-size: 11px; }
.tag { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px;
       background:#1a2230; color:#8aa0c0; margin-right:4px; }
.tag.tag-ul { background:#11212b; color:#7fb9d9; }
.tag.tag-dl { background:#1f1f2c; color:#9aa0d0; }
.tag.tag-dci { background:#2a1f1f; color:#d99080; }
.pos { font-family: ui-monospace, monospace; font-size:12px; }
.pos .cep { color:#8a93a0; }
.pos .method { display:block; font-size:10px; color:#8a93a0; }
.no-pos { color:#525a66; font-size: 11px; font-style: italic; }
.crnti { font-family: ui-monospace, monospace; font-weight:600; color:#d9d3ff; }
</style>
</head><body>
<header>
  <h1>Cellular drones · live · UEs</h1>
  <div class="meta">
    <span>UEs <strong id="n-ues">0</strong></span>
    <span>grants <strong id="n-ue-sightings">0</strong></span>
    <span>uptime <strong id="uptime">0s</strong></span>
    <span class="gps-pill fix-unknown" id="gps">GPS: —</span>
    <span class="status-pill status-idle" id="status">idle</span>
  </div>
</header>
<main>
  <div class="banner">
    <strong>C-RNTI is a temporary, per-connection ID.</strong>
    A handset that re-attaches (cell reselect, RRC release, airplane mode)
    will be reissued a new C-RNTI by the eNB. Per-RNTI tracks here are
    connection-scoped, not subscriber-scoped. Position estimates use UL
    grants only — DL grants come from the eNB.
  </div>
  <div id="empty" class="empty">waiting for first PDCCH decode…</div>
  <table id="tbl" style="display:none">
    <thead><tr>
      <th>C-RNTI</th>
      <th>Attached PCI</th>
      <th>DCI · MCS · PRB</th>
      <th>UL RSSI</th>
      <th>UL / DL grants</th>
      <th>Last seen</th>
      <th>UL trend</th>
      <th>Estimated UE location</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="log" id="log"></div>
</main>
<script>
const rowsEl = document.getElementById('rows');
const tblEl = document.getElementById('tbl');
const emptyEl = document.getElementById('empty');
const logEl = document.getElementById('log');
const nUesEl = document.getElementById('n-ues');
const nUeSightingsEl = document.getElementById('n-ue-sightings');
const uptimeEl = document.getElementById('uptime');
const statusEl = document.getElementById('status');
const gpsEl = document.getElementById('gps');

let totalUeSightings = 0;
let latestGps = null;
const ues = new Map();

function rsrpClass(v) {
  if (v == null) return '';
  if (v >= -75) return 'rsrp-strong';
  if (v >= -95) return 'rsrp-mid';
  return 'rsrp-weak';
}
function fmt(v, suffix='', digits=1) {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(digits) + suffix;
}
function fmtFreq(hz) {
  if (hz == null) return '—';
  return (hz / 1e6).toFixed(2) + ' MHz';
}
function timeAgo(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 2) return 'now';
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s/60) + 'm ago';
  return Math.round(s/3600) + 'h ago';
}
function sparkPath(history) {
  if (!history || history.length < 2) return '';
  const w = 120, h = 28, pad = 2;
  const vals = history.map(p => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = Math.max(1, max - min);
  const dx = (w - 2*pad) / (history.length - 1);
  return history.map((p, i) => {
    const x = pad + i*dx;
    const y = pad + (h - 2*pad) * (1 - (p[1] - min) / span);
    return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
}
function minimapSvg(ue) {
  const trail = ue.trail || [];
  const est = ue.est_position;
  if (trail.length < 2 && !est) return '';
  const all = [];
  trail.forEach(p => all.push([p.lat, p.lon]));
  if (est) all.push([est.lat, est.lon]);
  const lats = all.map(p => p[0]);
  const lons = all.map(p => p[1]);
  const padDeg = 0.0001;
  const minLat = Math.min(...lats) - padDeg, maxLat = Math.max(...lats) + padDeg;
  const minLon = Math.min(...lons) - padDeg, maxLon = Math.max(...lons) + padDeg;
  const W = 110, H = 84;
  const project = (lat, lon) => {
    const x = (lon - minLon) / Math.max(1e-9, maxLon - minLon) * W;
    const y = H - (lat - minLat) / Math.max(1e-9, maxLat - minLat) * H;
    return [x, y];
  };
  let svg = '';
  trail.forEach(p => {
    const [x, y] = project(p.lat, p.lon);
    const c = rsrpClass(p.ul_rssi_dbm);
    const fill = c === 'rsrp-strong' ? '#7ad9a1'
              : c === 'rsrp-mid'    ? '#f0c270'
              : c === 'rsrp-weak'   ? '#f08580' : '#5a6470';
    svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.6" fill="${fill}" fill-opacity="0.7"/>`;
  });
  if (est) {
    const [x, y] = project(est.lat, est.lon);
    svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="none" stroke="#d9d3ff" stroke-width="1.4"/>`;
    svg += `<line x1="${(x-6).toFixed(1)}" y1="${y.toFixed(1)}" x2="${(x+6).toFixed(1)}" y2="${y.toFixed(1)}" stroke="#d9d3ff" stroke-width="0.9"/>`;
    svg += `<line x1="${x.toFixed(1)}" y1="${(y-6).toFixed(1)}" x2="${x.toFixed(1)}" y2="${(y+6).toFixed(1)}" stroke="#d9d3ff" stroke-width="0.9"/>`;
  }
  return `<svg class="minimap" viewBox="0 0 ${W} ${H}">${svg}</svg>`;
}
function positionBlock(ue) {
  const p = ue.est_position;
  if (!p) {
    if (!latestGps) return '<span class="no-pos">no GPS yet</span>';
    if ((ue.ul_count || 0) < 2) return `<span class="no-pos">need ≥ 2 UL grants (have ${ue.ul_count || 0})</span>`;
    if (ue.n_geo_samples < 2) return `<span class="no-pos">need ≥ 2 fixes (have ${ue.n_geo_samples})</span>`;
    return '<span class="no-pos">computing…</span>';
  }
  const lat = p.lat.toFixed(6), lon = p.lon.toFixed(6);
  const cep = p.cep95_m != null ? '±' + p.cep95_m.toFixed(1) + 'm' : '';
  const alt = p.alt_m != null ? `, ${p.alt_m.toFixed(0)}m AGL` : '';
  return `<div class="pos">${lat}, ${lon} <span class="cep">${cep}</span>`
       + `<span class="method">${p.method} · ${p.n_samples} UL samples${alt}</span></div>`
       + minimapSvg(ue);
}
function renderRow(ue) {
  let tr = document.getElementById('row-' + ue.key);
  if (!tr) {
    tr = document.createElement('tr');
    tr.id = 'row-' + ue.key;
    rowsEl.appendChild(tr);
  }
  const dciTags = (ue.dci_formats || []).map(f => `<span class="tag tag-dci">DCI ${f}</span>`).join('');
  const ulTag = (ue.ul_count > 0) ? `<span class="tag tag-ul">UL ${ue.ul_count}</span>` : '';
  const dlTag = (ue.dl_count > 0) ? `<span class="tag tag-dl">DL ${ue.dl_count}</span>` : '';
  const lastMcs = ue.mcs != null ? `MCS ${ue.mcs}` : '—';
  const lastPrb = ue.n_prb != null ? `${ue.n_prb} PRB` : '—';
  const lastTbs = ue.tbs_bytes != null ? `${ue.tbs_bytes} B` : '';
  tr.innerHTML = `
    <td><span class="crnti">${ue.c_rnti_hex}</span><div class="dim">${ue.center_hz != null ? fmtFreq(ue.center_hz) : '—'}</div></td>
    <td><strong>${ue.pci}</strong></td>
    <td>${dciTags}<div class="dim">${lastMcs} · ${lastPrb} ${lastTbs ? '· ' + lastTbs : ''}</div></td>
    <td class="rsrp ${rsrpClass(ue.ul_rssi_dbm)}">${fmt(ue.ul_rssi_dbm, ' dBm')}</td>
    <td>${ulTag}${dlTag}</td>
    <td title="${ue.last_seen}">${timeAgo(ue.last_seen)}</td>
    <td><svg class="spark" viewBox="0 0 120 28"><path d="${sparkPath(ue.ul_rssi_history)}" fill="none" stroke="#7fb9d9" stroke-width="1.5"/></svg></td>
    <td>${positionBlock(ue)}</td>
  `;
  tr.classList.add('fresh');
  setTimeout(() => tr.classList.remove('fresh'), 1500);
}
function sortRows() {
  const sorted = [...ues.values()].sort((a, b) => {
    const ap = a.est_position ? 0 : 1;
    const bp = b.est_position ? 0 : 1;
    if (ap !== bp) return ap - bp;
    return (b.ul_count || 0) - (a.ul_count || 0);
  });
  sorted.forEach((u, i) => {
    const tr = document.getElementById('row-' + u.key);
    if (tr && rowsEl.children[i] !== tr) rowsEl.appendChild(tr);
  });
}
function setStatus(s) {
  let text = s.phase;
  if (s.phase === 'sniffing') {
    const mhz = s.center_hz != null ? (s.center_hz/1e6).toFixed(2) + ' MHz' : 'target cell';
    text = `sniffing PDCCH @ ${mhz} · gain ${s.gain_db ?? '?'} dB`;
  } else if (s.message) {
    text += ' — ' + s.message;
  }
  statusEl.textContent = text;
  statusEl.className = 'status-pill status-' + s.phase;
}
function setGps(rec) {
  latestGps = rec;
  if (!rec) { gpsEl.textContent = 'GPS: —'; return; }
  const g = rec.gps;
  gpsEl.className = 'gps-pill fix-' + (g.fix || 'unknown');
  gpsEl.textContent = `GPS: ${g.lat.toFixed(5)}, ${g.lon.toFixed(5)} (${g.fix})`;
}
function logLine(text) {
  const d = document.createElement('div');
  const ts = new Date().toTimeString().slice(0, 8);
  d.innerHTML = '<span class="ts">' + ts + '</span>' + text;
  logEl.prepend(d);
  while (logEl.children.length > 50) logEl.removeChild(logEl.lastChild);
}
function applyEvent(ev) {
  if (ev.type === 'snapshot') {
    ues.clear();
    rowsEl.innerHTML = '';
    (ev.ues || []).forEach(u => { ues.set(u.key, u); renderRow(u); });
    setStatus(ev.status || {phase: 'idle'});
    setGps(ev.latest_gps);
    totalUeSightings = ev.total_ue_sightings || 0;
  } else if (ev.type === 'ue_sighting') {
    const u = ev.ue;
    ues.set(u.key, u);
    renderRow(u);
    totalUeSightings += 1;
    const pos = u.est_position
      ? ` · est ${u.est_position.lat.toFixed(5)},${u.est_position.lon.toFixed(5)} ±${u.est_position.cep95_m.toFixed(0)}m`
      : '';
    logLine(`UE ${u.c_rnti_hex} on PCI ${u.pci} · UL ${fmt(u.ul_rssi_dbm, ' dBm')}${pos}`);
  } else if (ev.type === 'gps') {
    setGps(ev.fix);
  } else if (ev.type === 'status') {
    setStatus(ev.status);
    logLine('status: ' + ev.status.phase + (ev.status.message ? ' — ' + ev.status.message : ''));
  }
  nUesEl.textContent = ues.size;
  nUeSightingsEl.textContent = totalUeSightings;
  if (ues.size > 0) { tblEl.style.display = ''; emptyEl.style.display = 'none'; }
  sortRows();
}
let t0 = Date.now();
setInterval(() => {
  uptimeEl.textContent = Math.round((Date.now() - t0)/1000) + 's';
  ues.forEach(u => {
    const tr = document.getElementById('row-' + u.key);
    if (tr) tr.children[5].textContent = timeAgo(u.last_seen);
  });
}, 1000);
const es = new EventSource('/events');
es.onmessage = (e) => { try { applyEvent(JSON.parse(e.data)); } catch (_) {} };
es.onerror = () => logLine('stream disconnected, browser will retry');
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    state: State  # set on class before serving

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/state":
            body = json.dumps(self.state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/events":
            self._sse()
            return
        self.send_response(404)
        self.end_headers()

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.state.register()
        try:
            while True:
                try:
                    msg = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + msg.encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unregister(q)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ltesniffer-cmd", default=None,
                   help="argv (space-split) for LTESniffer / wrapper script.")
    p.add_argument("--center-hz", type=float, default=1_842_500_000,
                   help="target cell DL carrier frequency for LTESniffer")
    p.add_argument("--rx-gain-db", type=float, default=50.0)
    p.add_argument("--mission-id",
                   default=time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()))
    p.add_argument("--out-dir", default="data")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--simulate", action="store_true",
                   help="generate fake UEs + GPS (no hardware needed)")
    args = p.parse_args()

    state = State()
    stop = threading.Event()
    threads: list[threading.Thread] = []

    if args.simulate:
        threads.append(threading.Thread(
            target=run_simulator,
            kwargs=dict(state=state, mission_id=args.mission_id, stop=stop),
            daemon=True,
        ))
    else:
        if args.ltesniffer_cmd:
            cmd = args.ltesniffer_cmd.split()
            threads.append(threading.Thread(
                target=run_ltesniffer_loop,
                kwargs=dict(
                    state=state, ltesniffer_cmd=cmd,
                    mission_id=args.mission_id, out_dir=args.out_dir,
                    center_hz=args.center_hz, rx_gain_db=args.rx_gain_db,
                    stop=stop,
                ),
                daemon=True,
            ))
        threads.append(threading.Thread(
            target=run_gpsd_loop,
            kwargs=dict(state=state, mission_id=args.mission_id, stop=stop),
            daemon=True,
        ))
    for t in threads:
        t.start()

    handler = type("H", (_Handler,), {"state": state})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)

    def _shutdown(*_: Any) -> None:
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    url = f"http://{args.host}:{args.port}/"
    print(f"live dashboard: {url}   (mission {args.mission_id})", flush=True)
    if args.simulate:
        print("mode: simulate (synthetic UEs + GPS)", flush=True)
    elif args.ltesniffer_cmd:
        print(f"mode: LTESniffer · cmd={args.ltesniffer_cmd!r} "
              f"@ {args.center_hz/1e6:.2f} MHz gain {args.rx_gain_db} dB",
              flush=True)
        print("GPS: gpspipe -w (only ingested if gpsd is running)", flush=True)
    else:
        print("mode: GPS only (pass --ltesniffer-cmd to feed the dashboard)",
              flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
