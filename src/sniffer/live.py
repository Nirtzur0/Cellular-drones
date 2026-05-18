"""Realtime browser dashboard for HackRF + LTE-Cell-Scanner.

Drives identity extraction (PCI, PSS/SSS, FDD/TDD, antenna ports, BW,
MIB extras) and positioning (per-cell geotagged history → weighted
centroid + WLS) in a single process, and pushes per-cell aggregates
to connected browsers over Server-Sent Events. Stdlib only.

Run:
    python -m sniffer.live --start-hz 1840e6 --end-hz 1845e6
    python -m sniffer.live --simulate           # no HackRF / no GPS needed

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

from sniffer.localize import path_loss_wls, weighted_centroid
from sniffer.parse_cellsearch import parse_stream
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.schema import CellInfo, CellSighting, GpsFix, RadioConfig, mono_ns, utc_iso
from sniffer.simulate import (
    Emitter,
    _interp_waypoint,
    _rsrp_dbm,
    _rsrq_db,
    _snr_db,
    box_trajectory,
)


# Knobs --------------------------------------------------------------------
GPS_BUFFER_MAX = 4000          # ~10 min at 10 Hz
GEOTAG_MAX_AGE_MS = 500        # drop sightings >500 ms from nearest fix
CELL_HISTORY_MAX = 200         # geotagged sightings retained per cell
RSRP_HISTORY_MAX = 120         # rolling sparkline points
WLS_EVERY = 10                 # recompute path_loss_wls every N sightings


# --------------------------------------------------------------------------
# Aggregator + broadcaster
# --------------------------------------------------------------------------


def _bandwidth_mhz(n_rb_dl: Optional[int]) -> Optional[float]:
    return {6: 1.4, 15: 3.0, 25: 5.0, 50: 10.0, 75: 15.0, 100: 20.0}.get(
        n_rb_dl  # type: ignore[arg-type]
    )


class State:
    """Per-cell rolling state plus a fan-out queue list for SSE clients."""

    def __init__(self):
        self._lock = threading.Lock()
        # key: (round(center_hz / 1e5), pci)
        self._cells: dict[tuple[int, int], dict[str, Any]] = {}
        self._clients: list[queue.Queue[str]] = []
        self._started_mono_ns = mono_ns()
        self._total_sightings = 0
        self._scan_status: dict[str, Any] = {"phase": "idle", "ts_utc": utc_iso()}
        # GPS buffer: list of dicts {ts_mono_ns, ts_utc, gps:{lat,lon,alt_m,fix,hdop}}
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
            # Keep buffer time-ordered: append if newer than last, else insort.
            if not self._gps or ts >= self._gps_ts_keys[-1]:
                self._gps.append(rec)
                self._gps_ts_keys.append(ts)
            else:
                idx = bisect.bisect_left(self._gps_ts_keys, ts)
                self._gps.insert(idx, rec)
                self._gps_ts_keys.insert(idx, ts)
            # Trim oldest if oversized.
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

    def ingest(self, sighting: CellSighting) -> dict[str, Any]:
        center = sighting.radio.center_hz or 0.0
        pci = sighting.cell.pci
        key = (int(round(center / 1e5)), pci)
        now_iso = sighting.ts_utc or utc_iso()
        with self._lock:
            entry = self._cells.get(key)
            if entry is None:
                entry = {
                    "key": f"{key[0]}-{key[1]}",
                    "pci": pci,
                    "center_hz": center,
                    "n_id_1": None,
                    "n_id_2": None,
                    "mode": None,
                    "n_ports": None,
                    "n_rb_dl": None,
                    "bandwidth_mhz": None,
                    "cp": None,
                    "mib": {},
                    "sources_seen": [],
                    "first_seen": now_iso,
                    "count": 0,
                    "rsrp_history": deque(maxlen=RSRP_HISTORY_MAX),
                    "geo_history": deque(maxlen=CELL_HISTORY_MAX),
                    "est_position": None,
                    "_since_wls": 0,
                }
                self._cells[key] = entry

            # Identity (merge: only overwrite when sighting carries a value)
            _merge(entry, "n_id_1", sighting.cell.n_id_1)
            _merge(entry, "n_id_2", sighting.cell.n_id_2)
            _merge(entry, "mode", sighting.cell.mode)
            _merge(entry, "n_ports", sighting.cell.n_ports)
            _merge(entry, "cp", sighting.cell.cp)
            mib = sighting.cell.mib or {}
            if mib:
                entry["mib"] = {**entry["mib"], **mib}
                if "n_rb_dl" in mib:
                    entry["n_rb_dl"] = mib["n_rb_dl"]
                    entry["bandwidth_mhz"] = _bandwidth_mhz(mib["n_rb_dl"])
            src = _source_of(sighting)
            if src and src not in entry["sources_seen"]:
                entry["sources_seen"] = entry["sources_seen"] + [src]

            # Measurement
            entry["last_seen"] = now_iso
            entry["count"] += 1
            entry["rsrp_dbm"] = sighting.cell.rsrp_dbm
            entry["rsrq_db"] = sighting.cell.rsrq_db
            entry["snr_db"] = sighting.cell.snr_db
            entry["frame_offset_samples"] = sighting.cell.frame_offset_samples
            if sighting.cell.rsrp_dbm is not None:
                entry["rsrp_history"].append(
                    [now_iso, round(sighting.cell.rsrp_dbm, 2)]
                )
            self._total_sightings += 1

            # Positioning: attach nearest GPS, recompute estimate if attached
            hit = self._nearest_gps_locked(sighting.ts_mono_ns)
            if hit is not None and sighting.cell.rsrp_dbm is not None:
                gps_rec, age_ms = hit
                entry["geo_history"].append({
                    "kind": "cell_sighting",
                    "ts_mono_ns": sighting.ts_mono_ns,
                    "gps": {**gps_rec["gps"], "age_ms": age_ms},
                    "cell": {"pci": pci, "rsrp_dbm": sighting.cell.rsrp_dbm},
                })
                entry["_since_wls"] += 1
                _recompute_position(entry)

            payload = _entry_to_dict(entry)
        self._broadcast({"type": "sighting", "cell": payload})
        return payload

    def set_status(self, phase: str, **extra: Any) -> None:
        with self._lock:
            self._scan_status = {"phase": phase, "ts_utc": utc_iso(), **extra}
            status = dict(self._scan_status)
        self._broadcast({"type": "status", "status": status})

    # --- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cells = [_entry_to_dict(e) for e in self._cells.values()]
            cells.sort(
                key=lambda c: (c.get("rsrp_dbm") is None, -(c.get("rsrp_dbm") or 0))
            )
            return {
                "type": "snapshot",
                "cells": cells,
                "status": dict(self._scan_status),
                "latest_gps": self._latest_gps,
                "total_sightings": self._total_sightings,
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


def _merge(entry: dict[str, Any], field: str, value: Any) -> None:
    if value is not None and value != "" and value != {}:
        if entry.get(field) is None or entry.get(field) == "" or entry.get(field) == {}:
            entry[field] = value
        else:
            entry[field] = value  # later value wins; identity rarely changes


def _source_of(sighting: CellSighting) -> Optional[str]:
    note = sighting.notes or ""
    if "source=realtime" in note:
        return "realtime"
    if "source=summary" in note:
        return "summary"
    return None


def _entry_to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in entry.items()
           if k not in ("rsrp_history", "geo_history", "_since_wls")}
    out["rsrp_history"] = list(entry["rsrp_history"])
    out["n_geo_samples"] = len(entry["geo_history"])
    # Trail of recent positions for the mini-map (last 40 points).
    trail = list(entry["geo_history"])[-40:]
    out["trail"] = [{"lat": g["gps"]["lat"], "lon": g["gps"]["lon"],
                     "rsrp_dbm": g["cell"]["rsrp_dbm"]} for g in trail]
    return out


def _recompute_position(entry: dict[str, Any]) -> None:
    """Compute weighted centroid every sighting, WLS every WLS_EVERY."""
    records = list(entry["geo_history"])
    if len(records) < 2:
        return
    centroid = weighted_centroid(records)
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
            wls = path_loss_wls(records)
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
# Sink: bridges parse_stream's text-output into the State aggregator
# --------------------------------------------------------------------------


class _SightingSink(io.TextIOBase):
    """parse_stream writes JSONL strings here; we decode + push to State."""

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
        cell = rec.get("cell") or {}
        sighting = CellSighting(
            mission_id=rec.get("mission_id", ""),
            capture_id=rec.get("capture_id", ""),
            ts_mono_ns=rec.get("ts_mono_ns", mono_ns()),
            ts_utc=rec.get("ts_utc", utc_iso()),
            radio=RadioConfig(**{k: radio.get(k)
                                 for k in RadioConfig.__dataclass_fields__
                                 if k in radio}),
            cell=CellInfo(**{k: cell.get(k)
                             for k in CellInfo.__dataclass_fields__
                             if k in cell}),
            notes=rec.get("notes", ""),
        )
        self._state.ingest(sighting)


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
                 rx_gain_db: float = 40.0):
        self.mission_id = mission_id
        self.backend = backend
        self.device = device
        self.rx_gain_db = rx_gain_db


def run_cellsearch_loop(state: State, *, start_hz: float, end_hz: float,
                        step_hz: float, mission_id: str, out_dir: str,
                        rx_gain_db: float, stop: threading.Event) -> None:
    """Run CellSearch repeatedly, parse stdout, ingest into State."""
    if shutil.which("CellSearch") is None:
        state.set_status(
            "error",
            message=("CellSearch not on PATH — run ./scripts/install-macos.sh and "
                     'export PATH="$HOME/src/LTE-Cell-Scanner/build/src:$PATH"'),
        )
        return

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"scan-{mission_id}.jsonl")
    parse_args = _ParseArgs(mission_id, "lte-cell-scanner", "hackrf-0",
                            rx_gain_db=rx_gain_db)

    while not stop.is_set():
        state.set_status("scanning",
                         start_hz=start_hz, end_hz=end_hz, step_hz=step_hz,
                         gain_db=rx_gain_db, out=out_path)
        proc = subprocess.Popen(
            ["CellSearch",
             "-s", f"{start_hz}", "-e", f"{end_hz}",
             "-n", "1", "-g", f"{rx_gain_db}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        try:
            with open(out_path, "a", encoding="utf-8") as jsonl_fh:
                sink = _SightingSink(state, jsonl_out=jsonl_fh)
                assert proc.stdout is not None
                parse_stream(proc.stdout, parse_args, sink)
        except Exception as exc:  # noqa: BLE001
            state.set_status("error", message=f"parse failed: {exc}")
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if stop.is_set():
            return
        for _ in range(5):
            if stop.is_set():
                return
            time.sleep(0.2)


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

    Walks a box trajectory around two synthetic emitters in wall-clock time,
    emitting GpsFix events (~10 Hz) and CellSighting events (~2 Hz) so that
    positioning has both measurements and motion. Bypasses the text parser
    — that path is exercised by the unit tests and the real-radio mode.
    """
    state.set_status("simulating", mission_id=mission_id)
    emitters = [
        Emitter(lat=32.0853, lon=34.7818, alt_m=25.0, pci=271,
                center_hz=1_842_500_000, n_id_1=90, n_id_2=1,
                tx_power_dbm=24.0),
        Emitter(lat=32.0855, lon=34.7825, alt_m=18.0, pci=148,
                center_hz=1_840_000_000, n_id_1=49, n_id_2=1,
                tx_power_dbm=20.0),
    ]
    # Use the first emitter as the box center; both emitters fall inside.
    trajectory = box_trajectory(emitters[0], half_size_m=100.0,
                                altitudes=(15.0, 30.0, 60.0),
                                n_per_side=20, leg_speed_mps=8.0)
    rng = random.Random(1337)

    gps_period = 0.1
    sighting_period = 0.5
    detect_threshold = -115.0

    per_emitter_count: dict[int, int] = {em.pci: 0 for em in emitters}
    t_start_wall = time.monotonic()
    last_gps_t = -1.0
    last_sight_t = -1.0
    t_total = trajectory[-1].t_offset_s

    while not stop.is_set():
        # Wall-clock seconds since this lap began.
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
            # 0.5 m horizontal jitter to look like a real fix.
            jlat = w.lat + rng.gauss(0.0, 5e-6)
            jlon = w.lon + rng.gauss(0.0, 5e-6)
            jalt = w.alt_m + rng.gauss(0.0, 0.3)
            state.add_gps(GpsFix(lat=jlat, lon=jlon, alt_m=jalt,
                                 fix="rtk_fix", hdop=0.6))

        if t_sim - last_sight_t >= sighting_period:
            last_sight_t = t_sim
            for em in emitters:
                rsrp = _rsrp_dbm(em, w, rng, 1.5)
                if rsrp < detect_threshold:
                    continue
                # Alternate between realtime and summary per-emitter so every
                # cell eventually shows both source tags (matches what one
                # CellSearch invocation produces: realtime per detection +
                # one summary row at the end).
                per_emitter_count[em.pci] += 1
                source = ("summary" if per_emitter_count[em.pci] % 5 == 0
                          else "realtime")
                radio = RadioConfig(
                    backend="sim", device="sim-0",
                    center_hz=em.center_hz, sample_rate_sps=1.92e6,
                )
                if source == "realtime":
                    cell = CellInfo(
                        pci=em.pci, n_id_2=em.n_id_2, mode="fdd", cp="normal",
                        rsrp_dbm=round(rsrp, 2),
                        rsrq_db=round(_rsrq_db(rsrp), 2),
                        snr_db=round(_snr_db(rsrp, rng), 2),
                        frame_offset_samples=int(rng.uniform(0, 30720)),
                        mib={
                            "residual_freq_offset_hz": round(rng.gauss(0, 250), 1),
                            "k_factor": 1.0 + rng.gauss(0, 1.5e-7),
                        },
                    )
                else:
                    cell = CellInfo(
                        pci=em.pci, mode="fdd", cp="normal",
                        n_ports=2,
                        rsrp_dbm=round(rsrp, 2),
                        mib={"n_rb_dl": 50,
                             "crystal_correction": 0.99999987},
                    )
                state.ingest(CellSighting(
                    mission_id=mission_id, capture_id="sim-0",
                    ts_mono_ns=mono_ns(), ts_utc=utc_iso(),
                    radio=radio, cell=cell,
                    notes=f"source={source}",
                ))

        time.sleep(0.03)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


_INDEX_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>Cellular drones · live</title>
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
.status-scanning { color:#7ad9a1; border-color:#1d4032; background:#11211a; }
.status-simulating { color:#f0c270; border-color:#403118; background:#21190f; }
.status-error { color:#f08580; border-color:#4a1f1f; background:#2a1414; }
.status-idle { color:#8a93a0; }
.gps-pill { font-size: 11px; padding: 2px 10px; border:1px solid #2a3038; border-radius:999px;
            color:#8a93a0; font-family: ui-monospace, monospace; }
.gps-pill.fix-3d, .gps-pill.fix-rtk_fix, .gps-pill.fix-rtk_float { color:#7ad9a1; border-color:#1d4032; }
.gps-pill.fix-2d { color:#f0c270; border-color:#403118; }
.gps-pill.fix-none, .gps-pill.fix-unknown { color:#8a93a0; }
main { padding: 16px 20px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1a1f25; font-size: 13px; vertical-align: top; }
th { font-weight: 600; color:#8a93a0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
tr.fresh td { background: #16241c; transition: background 1.4s ease; }
.rsrp { font-weight: 600; }
.rsrp-strong { color:#7ad9a1; }
.rsrp-mid { color:#f0c270; }
.rsrp-weak { color:#f08580; }
.spark { width: 120px; height: 28px; vertical-align: middle; }
.minimap { width: 90px; height: 70px; background:#13171c; border-radius:4px; vertical-align: middle; }
.empty { padding: 60px 20px; text-align: center; color:#8a93a0; font-size: 14px; }
.log { margin-top: 24px; font-family: ui-monospace, monospace; font-size:11px;
       color:#8a93a0; max-height: 160px; overflow-y: auto; padding: 8px 0;
       border-top: 1px solid #1a1f25; }
.log div { padding: 2px 0; }
.log .ts { color:#525a66; margin-right: 8px; }
.dim { color:#8a93a0; font-size: 11px; }
.tag { display:inline-block; padding:1px 6px; border-radius:3px; font-size:10px;
       background:#1a2230; color:#8aa0c0; margin-right:4px; }
.tag.tag-realtime { background:#13261b; color:#7ad9a1; }
.tag.tag-summary { background:#26211c; color:#f0c270; }
.id-grid { display:grid; grid-template-columns: auto 1fr; gap:2px 10px;
           font-size:11px; color:#c4cad3; }
.id-grid .k { color:#8a93a0; }
.pos { font-family: ui-monospace, monospace; font-size:12px; }
.pos .cep { color:#8a93a0; }
.pos .method { display:block; font-size:10px; color:#8a93a0; }
.no-pos { color:#525a66; font-size: 11px; font-style: italic; }
</style>
</head><body>
<header>
  <h1>Cellular drones · live</h1>
  <div class="meta">
    <span>cells <strong id="n-cells">0</strong></span>
    <span>sightings <strong id="n-sightings">0</strong></span>
    <span>uptime <strong id="uptime">0s</strong></span>
    <span class="gps-pill fix-unknown" id="gps">GPS: —</span>
    <span class="status-pill status-idle" id="status">idle</span>
  </div>
</header>
<main>
  <div id="empty" class="empty">waiting for first detection…</div>
  <table id="tbl" style="display:none">
    <thead><tr>
      <th>PCI</th>
      <th>Carrier</th>
      <th>Identity</th>
      <th>RSRP</th>
      <th>SNR</th>
      <th>Count</th>
      <th>Last seen</th>
      <th>RSRP trend</th>
      <th>Estimated location</th>
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
const nCellsEl = document.getElementById('n-cells');
const nSightingsEl = document.getElementById('n-sightings');
const uptimeEl = document.getElementById('uptime');
const statusEl = document.getElementById('status');
const gpsEl = document.getElementById('gps');

let totalSightings = 0;
let latestGps = null;
const cells = new Map();

function rsrpClass(v) {
  if (v == null) return '';
  if (v >= -85) return 'rsrp-strong';
  if (v >= -100) return 'rsrp-mid';
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
function minimapSvg(cell) {
  const trail = cell.trail || [];
  const est = cell.est_position;
  if (trail.length < 2 && !est) return '';
  const all = [];
  trail.forEach(p => all.push([p.lat, p.lon]));
  if (est) all.push([est.lat, est.lon]);
  const lats = all.map(p => p[0]);
  const lons = all.map(p => p[1]);
  const padDeg = 0.0001;
  const minLat = Math.min(...lats) - padDeg, maxLat = Math.max(...lats) + padDeg;
  const minLon = Math.min(...lons) - padDeg, maxLon = Math.max(...lons) + padDeg;
  const W = 90, H = 70;
  const project = (lat, lon) => {
    const x = (lon - minLon) / Math.max(1e-9, maxLon - minLon) * W;
    const y = H - (lat - minLat) / Math.max(1e-9, maxLat - minLat) * H;
    return [x, y];
  };
  let svg = '';
  trail.forEach(p => {
    const [x, y] = project(p.lat, p.lon);
    const c = rsrpClass(p.rsrp_dbm);
    const fill = c === 'rsrp-strong' ? '#7ad9a1'
              : c === 'rsrp-mid'    ? '#f0c270'
              : c === 'rsrp-weak'   ? '#f08580' : '#5a6470';
    svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.5" fill="${fill}" fill-opacity="0.7"/>`;
  });
  if (est) {
    const [x, y] = project(est.lat, est.lon);
    svg += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5" fill="none" stroke="#e6e8eb" stroke-width="1.2"/>`;
    svg += `<line x1="${(x-5).toFixed(1)}" y1="${y.toFixed(1)}" x2="${(x+5).toFixed(1)}" y2="${y.toFixed(1)}" stroke="#e6e8eb" stroke-width="0.8"/>`;
    svg += `<line x1="${x.toFixed(1)}" y1="${(y-5).toFixed(1)}" x2="${x.toFixed(1)}" y2="${(y+5).toFixed(1)}" stroke="#e6e8eb" stroke-width="0.8"/>`;
  }
  return `<svg class="minimap" viewBox="0 0 ${W} ${H}">${svg}</svg>`;
}
function identityBlock(cell) {
  const tags = (cell.sources_seen || []).map(s => `<span class="tag tag-${s}">${s}</span>`).join('');
  const rows = [];
  if (cell.mode) rows.push(['mode', cell.mode.toUpperCase()]);
  rows.push(['PSS/SSS', `${cell.n_id_2 ?? '—'} / ${cell.n_id_1 ?? '—'}`]);
  if (cell.n_ports != null) rows.push(['Tx ports', cell.n_ports]);
  if (cell.bandwidth_mhz != null) rows.push(['BW', cell.bandwidth_mhz + ' MHz (' + cell.n_rb_dl + ' RB)']);
  if (cell.cp) rows.push(['CP', cell.cp]);
  if (cell.mib && cell.mib.residual_freq_offset_hz != null) {
    rows.push(['Δf', cell.mib.residual_freq_offset_hz.toFixed(1) + ' Hz']);
  }
  if (cell.mib && cell.mib.crystal_correction != null) {
    rows.push(['XO', cell.mib.crystal_correction.toFixed(8)]);
  }
  const grid = rows.map(([k, v]) => `<span class="k">${k}</span><span>${v}</span>`).join('');
  return `${tags}<div class="id-grid">${grid}</div>`;
}
function positionBlock(cell) {
  const p = cell.est_position;
  if (!p) {
    if (!latestGps) return '<span class="no-pos">no GPS yet</span>';
    if (cell.n_geo_samples < 2) return `<span class="no-pos">need ≥ 2 fixes (have ${cell.n_geo_samples})</span>`;
    return '<span class="no-pos">computing…</span>';
  }
  const lat = p.lat.toFixed(6), lon = p.lon.toFixed(6);
  const cep = p.cep95_m != null ? '±' + p.cep95_m.toFixed(1) + 'm' : '';
  const alt = p.alt_m != null ? `, ${p.alt_m.toFixed(0)}m AGL` : '';
  return `<div class="pos">${lat}, ${lon} <span class="cep">${cep}</span>`
       + `<span class="method">${p.method} · ${p.n_samples} samples${alt}</span></div>`
       + minimapSvg(cell);
}
function renderRow(cell) {
  let tr = document.getElementById('row-' + cell.key);
  if (!tr) {
    tr = document.createElement('tr');
    tr.id = 'row-' + cell.key;
    rowsEl.appendChild(tr);
  }
  tr.innerHTML = `
    <td><strong>${cell.pci}</strong></td>
    <td>${fmtFreq(cell.center_hz)}</td>
    <td>${identityBlock(cell)}</td>
    <td class="rsrp ${rsrpClass(cell.rsrp_dbm)}">${fmt(cell.rsrp_dbm, ' dBm')}<div class="dim">SNR ${fmt(cell.snr_db, ' dB')} · RSRQ ${fmt(cell.rsrq_db, ' dB')}</div></td>
    <td>${fmt(cell.snr_db, ' dB')}</td>
    <td>${cell.count}</td>
    <td title="${cell.last_seen}">${timeAgo(cell.last_seen)}</td>
    <td><svg class="spark" viewBox="0 0 120 28"><path d="${sparkPath(cell.rsrp_history)}" fill="none" stroke="#7ad9a1" stroke-width="1.5"/></svg></td>
    <td>${positionBlock(cell)}</td>
  `;
  tr.classList.add('fresh');
  setTimeout(() => tr.classList.remove('fresh'), 1500);
}
function sortRows() {
  const sorted = [...cells.values()].sort((a, b) =>
    (b.rsrp_dbm ?? -1e9) - (a.rsrp_dbm ?? -1e9));
  sorted.forEach((c, i) => {
    const tr = document.getElementById('row-' + c.key);
    if (tr && rowsEl.children[i] !== tr) rowsEl.appendChild(tr);
  });
}
function setStatus(s) {
  statusEl.textContent = s.phase + (s.message ? ' — ' + s.message : '');
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
    cells.clear();
    rowsEl.innerHTML = '';
    (ev.cells || []).forEach(c => { cells.set(c.key, c); renderRow(c); });
    setStatus(ev.status || {phase: 'idle'});
    setGps(ev.latest_gps);
    totalSightings = ev.total_sightings || 0;
  } else if (ev.type === 'sighting') {
    const c = ev.cell;
    cells.set(c.key, c);
    renderRow(c);
    totalSightings += 1;
    const pos = c.est_position
      ? ` · est ${c.est_position.lat.toFixed(5)},${c.est_position.lon.toFixed(5)} ±${c.est_position.cep95_m.toFixed(0)}m`
      : '';
    logLine(`PCI ${c.pci} @ ${fmtFreq(c.center_hz)} · RSRP ${fmt(c.rsrp_dbm, ' dBm')}${pos}`);
  } else if (ev.type === 'gps') {
    setGps(ev.fix);
  } else if (ev.type === 'status') {
    setStatus(ev.status);
    logLine('status: ' + ev.status.phase + (ev.status.message ? ' — ' + ev.status.message : ''));
  }
  nCellsEl.textContent = cells.size;
  nSightingsEl.textContent = totalSightings;
  if (cells.size > 0) { tblEl.style.display = ''; emptyEl.style.display = 'none'; }
  sortRows();
}
let t0 = Date.now();
setInterval(() => {
  uptimeEl.textContent = Math.round((Date.now() - t0)/1000) + 's';
  cells.forEach(c => {
    const tr = document.getElementById('row-' + c.key);
    if (tr) tr.children[6].textContent = timeAgo(c.last_seen);
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
    p.add_argument("--start-hz", type=float, default=1_840_000_000)
    p.add_argument("--end-hz", type=float, default=1_845_000_000)
    p.add_argument("--step-hz", type=float, default=100_000)
    p.add_argument("--rx-gain-db", type=float, default=40.0)
    p.add_argument("--mission-id",
                   default=time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()))
    p.add_argument("--out-dir", default="data")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--simulate", action="store_true",
                   help="generate fake sightings + GPS (no hardware needed)")
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
        threads.append(threading.Thread(
            target=run_cellsearch_loop,
            kwargs=dict(
                state=state, start_hz=args.start_hz, end_hz=args.end_hz,
                step_hz=args.step_hz, mission_id=args.mission_id,
                out_dir=args.out_dir, rx_gain_db=args.rx_gain_db, stop=stop,
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
        print("mode: simulate (synthetic emitters + synthetic GPS)", flush=True)
    else:
        print(f"mode: HackRF · scan {args.start_hz:.0f}–{args.end_hz:.0f} Hz "
              f"step {args.step_hz:.0f} gain {args.rx_gain_db} dB", flush=True)
        print("GPS: gpspipe -w (only ingested if gpsd is running)", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
