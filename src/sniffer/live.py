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

from sniffer.localize import weighted_centroid_ue
from sniffer.motion import classify_motion
from sniffer.normalize_ltesniffer import normalize_stream
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
GPS_TRAIL_MAX = 600            # drone trail points sent in /state snapshot


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
                    "motion": None,
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
            trail_slice = self._gps[-GPS_TRAIL_MAX:]
            gps_trail = [{"lat": r["gps"]["lat"], "lon": r["gps"]["lon"]}
                         for r in trail_slice]
            t_min = self._gps_ts_keys[0] if self._gps_ts_keys else None
            t_max = self._gps_ts_keys[-1] if self._gps_ts_keys else None
            return {
                "type": "snapshot",
                "ues": ues,
                "status": dict(self._scan_status),
                "latest_gps": self._latest_gps,
                "gps_trail": gps_trail,
                "total_ue_sightings": self._total_ue_sightings,
                "uptime_s": (mono_ns() - self._started_mono_ns) / 1e9,
                "time_bounds": {"t_min_ns": t_min, "t_max_ns": t_max,
                                "started_ns": self._started_mono_ns},
            }

    def time_bounds(self) -> dict[str, Any]:
        """Earliest/latest ts_mono_ns we can replay to."""
        with self._lock:
            t_min = self._gps_ts_keys[0] if self._gps_ts_keys else None
            t_max = self._gps_ts_keys[-1] if self._gps_ts_keys else None
            return {"t_min_ns": t_min, "t_max_ns": t_max,
                    "started_ns": self._started_mono_ns,
                    "now_ns": mono_ns()}

    def snapshot_at(self, at_ts_mono_ns: int) -> dict[str, Any]:
        """Reconstruct a snapshot as it would have looked at `at_ts_mono_ns`.

        Drone trail and per-UE est_position are recomputed from the
        stored history. UE counts/timestamps reflect what was visible up
        to `at_ts_mono_ns`; live cumulative fields the history doesn't
        carry (DL grants, MCS, PRB) are zeroed/None.
        """
        with self._lock:
            idx = bisect.bisect_right(self._gps_ts_keys, at_ts_mono_ns)
            gps_slice = self._gps[:idx]
            latest_gps = gps_slice[-1] if gps_slice else None
            trail = gps_slice[-GPS_TRAIL_MAX:]
            gps_trail = [{"lat": r["gps"]["lat"], "lon": r["gps"]["lon"]}
                         for r in trail]
            ues_out: list[dict[str, Any]] = []
            for entry in self._ues.values():
                past = [g for g in entry["geo_history"]
                        if g["ts_mono_ns"] <= at_ts_mono_ns]
                if not past:
                    continue
                last_g = past[-1]
                cent = weighted_centroid_ue(past)
                est_pos = None
                motion = None
                if cent is not None:
                    est_pos = {
                        "lat": cent.lat, "lon": cent.lon,
                        "alt_m": cent.alt_m, "cep95_m": cent.cep95_m,
                        "method": "weighted_centroid",
                        "n_samples": cent.n_samples,
                        "altitude_estimated": cent.altitude_estimated,
                    }
                    m = classify_motion(past, cent.lat, cent.lon)
                    motion = {"label": m.label, "corr": m.corr,
                              "n_samples": m.n_samples, "note": m.note}
                ues_out.append({
                    "key": entry["key"],
                    "pci": entry["pci"],
                    "c_rnti": entry["c_rnti"],
                    "c_rnti_hex": entry["c_rnti_hex"],
                    "center_hz": entry["center_hz"],
                    "first_seen": entry["first_seen"],
                    "last_seen": last_g.get("ts_utc", entry["first_seen"]),
                    "count": len(past),
                    "ul_count": len(past),
                    "dl_count": 0,
                    "dci_formats": entry.get("dci_formats", []),
                    "mcs": None, "n_prb": None, "tbs_bytes": None,
                    "ul_rssi_dbm": last_g["ue"].get("ul_rssi_dbm"),
                    "dl_rsrp_dbm": None,
                    "ul_rssi_history": [],
                    "n_geo_samples": len(past),
                    "trail": [{"lat": g["gps"]["lat"],
                               "lon": g["gps"]["lon"],
                               "ul_rssi_dbm": g["ue"]["ul_rssi_dbm"]}
                              for g in past[-40:]],
                    "est_position": est_pos,
                    "motion": motion,
                })
            ues_out.sort(
                key=lambda u: (u.get("est_position") is None,
                               -(u.get("ul_count") or 0)),
            )
            return {
                "type": "snapshot",
                "replay": True,
                "at_ts_mono_ns": at_ts_mono_ns,
                "ues": ues_out,
                "status": {**self._scan_status,
                           "replay": True,
                           "at_ts_mono_ns": at_ts_mono_ns},
                "latest_gps": latest_gps,
                "gps_trail": gps_trail,
                "total_ue_sightings": sum(u["ul_count"] for u in ues_out),
                "uptime_s": max(0.0, (at_ts_mono_ns - self._started_mono_ns) / 1e9),
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
           if k not in ("ul_rssi_history", "geo_history")}
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
    entry["est_position"] = {
        "lat": centroid.lat, "lon": centroid.lon,
        "alt_m": centroid.alt_m, "cep95_m": centroid.cep95_m,
        "method": "weighted_centroid",
        "n_samples": centroid.n_samples,
        "altitude_estimated": centroid.altitude_estimated,
    }
    motion = classify_motion(records, centroid.lat, centroid.lon)
    entry["motion"] = {
        "label": motion.label,
        "corr": motion.corr,
        "n_samples": motion.n_samples,
        "note": motion.note,
    }


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
                        stop: threading.Event,
                        normalize: bool = False) -> None:
    """Spawn LTESniffer, stream its DECODED stdout through parse_ltesniffer.

    The user provides a fully-formed argv via `--ltesniffer-cmd`: this gives
    them control over which binary, mode, frequency, gain, etc. The process
    is restarted on exit until `stop` is set.

    Set `normalize=True` when the binary emits human-readable text rather
    than canonical `DECODED key=value` lines: raw stdout is piped through
    `normalize_stream` in-process before parsing.
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
                stream = (normalize_stream(proc.stdout) if normalize
                          else proc.stdout)
                parse_ltesniffer_stream(stream, parse_args, sink)
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
<meta charset="utf-8"><title>UE tracker · cellular drones</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      crossorigin=""/>
<style>
:root {
  color-scheme: dark;
  --bg: #07090c;
  --panel: #0f131a;
  --panel-2: #141923;
  --line: #1d232d;
  --line-2: #262d3a;
  --dim: #8a93a0;
  --dim-2: #5a6470;
  --txt: #e6e8eb;
  --accent: #d9d3ff;
  --green: #7ad9a1;
  --yellow: #f0c270;
  --red: #f08580;
  --blue: #7fb9d9;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, "Inter", system-ui, sans-serif;
  background: radial-gradient(1100px 700px at 80% -10%, #11161f 0%, #07090c 60%) var(--bg);
  color: var(--txt); -webkit-font-smoothing: antialiased; font-size: 13px;
}
body { display: flex; flex-direction: column; min-height: 100vh; }

/* ---------------------------------------------------------- header */
header {
  flex: 0 0 auto;
  padding: 11px 20px;
  border-bottom: 1px solid var(--line);
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  background: linear-gradient(180deg, rgba(20,25,35,0.55), rgba(15,19,26,0.3));
}
.brand { display: flex; align-items: center; gap: 10px; }
.brand .led {
  width: 9px; height: 9px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 0 4px rgba(122,217,161,0.14);
}
header h1 { font-size: 14px; font-weight: 600; margin: 0; letter-spacing: 0.01em; }
header h1 span { color: var(--dim); font-weight: 400; margin-left: 6px; }
.meta { font-size: 11px; color: var(--dim); display: flex; gap: 14px;
        flex-wrap: wrap; align-items: center; }
.stat { display: flex; gap: 6px; align-items: baseline; text-transform: uppercase;
        letter-spacing: 0.08em; }
.stat strong { color: var(--txt); font-weight: 600; font-size: 14px;
               font-variant-numeric: tabular-nums; letter-spacing: 0; text-transform: none; }
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px; border-radius: 999px;
  font-size: 11px; border: 1px solid var(--line-2);
  background: rgba(20, 25, 35, 0.5);
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.pill .led { width: 6px; height: 6px; border-radius: 50%; background: var(--dim-2); }
.pill.status-sniffing { color: var(--green); border-color:#1d4032; background:#0f1d18; }
.pill.status-sniffing .led { background: var(--green); box-shadow: 0 0 0 3px rgba(122,217,161,0.18);
                              animation: pulse 1.6s ease-in-out infinite; }
.pill.status-simulating { color: var(--yellow); border-color:#403118; background:#1f1810; }
.pill.status-simulating .led { background: var(--yellow); }
.pill.status-error { color: var(--red); border-color:#4a1f1f; background:#241313; }
.pill.status-error .led { background: var(--red); }
.pill.status-idle { color: var(--dim); }
.pill.gps-3d, .pill.gps-rtk_fix, .pill.gps-rtk_float { color: var(--green); border-color:#1d4032; }
.pill.gps-3d .led, .pill.gps-rtk_fix .led, .pill.gps-rtk_float .led { background: var(--green); }
.pill.gps-2d { color: var(--yellow); border-color:#403118; }
.pill.gps-2d .led { background: var(--yellow); }
.pill.gps-none, .pill.gps-unknown { color: var(--dim); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }

/* ---------------------------------------------------------- banner */
.banner {
  margin: 10px 20px 0;
  border: 1px solid var(--line-2);
  background: linear-gradient(180deg, rgba(240,194,112,0.04), rgba(240,194,112,0.01));
  color: var(--yellow);
  font-size: 11.5px; padding: 7px 14px; border-radius: 7px;
  display: flex; align-items: center; gap: 10px;
}
.banner strong { color: #f7d99c; }
.banner .dim { color: var(--dim); }

/* ---------------------------------------------------------- main grid */
.workspace {
  flex: 1 1 auto; min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(380px, 0.95fr);
  gap: 14px;
  padding: 12px 20px 16px;
}
@media (max-width: 1100px) {
  .workspace { grid-template-columns: 1fr; }
  .map-wrap { height: 46vh; min-height: 320px; }
  .side { height: 60vh; min-height: 360px; }
}

/* ---------------------------------------------------------- map */
.map-wrap {
  position: relative;
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
  background: #0e1116;
  min-height: 380px;
}
#map { position: absolute; inset: 0; }
#map.unavailable { display: flex; align-items: center; justify-content: center;
                   color: var(--dim); font-size: 12px; padding: 16px; }
.map-overlay {
  position: absolute; top: 12px; right: 12px; z-index: 500;
  display: flex; flex-direction: column; gap: 4px;
  font-family: ui-monospace, monospace; font-size: 11px;
  color: var(--dim);
  background: rgba(10, 13, 19, 0.78);
  border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px;
  pointer-events: none;
  max-width: 220px;
}
.map-overlay strong { color: var(--txt); font-weight: 600; }
.legend {
  position: absolute; bottom: 12px; left: 12px; z-index: 500;
  display: flex; flex-direction: column; gap: 4px;
  font-family: ui-monospace, monospace; font-size: 11px;
  background: rgba(10,13,19,0.78);
  border: 1px solid var(--line);
  border-radius: 8px; padding: 8px 10px;
  max-height: 50%; overflow: auto;
  min-width: 130px;
}
.legend .row { display: flex; align-items: center; gap: 8px; cursor: pointer;
               color: var(--dim); padding: 1px 0; }
.legend .row.active { color: var(--txt); }
.legend .row:hover  { color: var(--txt); }
.legend .row.has-pos::after { content: '●'; color: var(--green); margin-left: auto;
                              font-size: 9px; }
.legend .row.no-pos::after  { content: '○'; color: var(--dim-2); margin-left: auto;
                              font-size: 9px; }
.legend .sw { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.legend .label { color: var(--dim-2); font-size: 10px; text-transform: uppercase;
                 letter-spacing: 0.06em; margin-bottom: 4px; }

/* ---------------------------------------------------------- side cards */
.side {
  display: flex; flex-direction: column; min-height: 0;
  border: 1px solid var(--line); border-radius: 10px;
  background: linear-gradient(180deg, var(--panel) 0%, #0c1017 100%);
}
.side-head {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; border-bottom: 1px solid var(--line);
  font-size: 11px; color: var(--dim); text-transform: uppercase;
  letter-spacing: 0.08em;
}
.side-head strong { color: var(--txt); font-weight: 600; font-size: 12px;
                    letter-spacing: 0.04em; }
.cards { flex: 1 1 auto; overflow-y: auto; padding: 10px; }
.empty {
  margin: 16px; padding: 28px 18px; text-align: center;
  border: 1px dashed var(--line-2); border-radius: 8px;
  color: var(--dim); font-size: 12px; line-height: 1.6;
}
.empty .hint { color: var(--dim-2); font-size: 11px; margin-top: 6px; }

.card {
  position: relative;
  background: linear-gradient(180deg, var(--panel-2) 0%, var(--panel) 100%);
  border: 1px solid var(--line);
  border-left: 3px solid var(--ue-color, var(--dim-2));
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.12s ease;
}
.card:hover { border-color: var(--line-2); }
.card.selected {
  border-color: var(--ue-color, var(--accent));
  box-shadow: 0 0 0 1px var(--ue-color, var(--accent)) inset,
              0 6px 22px -14px var(--ue-color, var(--accent));
}
.card.fresh { animation: flash 1.4s ease; }
@keyframes flash {
  0%   { background: linear-gradient(180deg, #1c2031, #16192a); }
  100% { background: linear-gradient(180deg, var(--panel-2), var(--panel)); }
}
.card .head {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  margin-bottom: 6px;
}
.identity { display: flex; align-items: center; gap: 8px; }
.identity .sw {
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--ue-color, var(--dim-2));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ue-color, transparent) 22%, transparent);
}
.crnti {
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-weight: 700; font-size: 18px; color: var(--txt);
  letter-spacing: 0.01em;
}
.pci-tag {
  font-size: 10px; padding: 2px 8px; border-radius: 4px;
  background: rgba(127,185,217,0.12); color: var(--blue);
  font-family: ui-monospace, monospace; font-weight: 500;
  border: 1px solid rgba(127,185,217,0.22);
}
.seen { font-size: 11px; color: var(--dim); font-variant-numeric: tabular-nums; }
.cell {
  font-size: 10px; color: var(--dim-2); margin-top: 1px;
  text-transform: uppercase; letter-spacing: 0.06em;
}
.metrics {
  display: grid; grid-template-columns: 1fr auto; gap: 12px;
  align-items: end; margin: 10px 0 2px;
}
.metric-rssi { display: flex; flex-direction: column; gap: 1px; }
.metric-rssi .label {
  font-size: 10px; color: var(--dim); text-transform: uppercase;
  letter-spacing: 0.06em;
}
.metric-rssi .value {
  font-size: 22px; font-weight: 600; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.metric-rssi .value .unit { font-size: 11px; color: var(--dim);
                            margin-left: 4px; font-weight: 400; }
.rsrp-strong { color: var(--green); }
.rsrp-mid    { color: var(--yellow); }
.rsrp-weak   { color: var(--red); }
.rsrp-none   { color: var(--dim); font-size: 15px; }
.spark { width: 130px; height: 36px; }
.spark .fill { fill: var(--ue-color, var(--blue)); fill-opacity: 0.12; }
.spark .line { stroke: var(--ue-color, var(--blue)); stroke-width: 1.6;
               fill: none; stroke-linejoin: round; stroke-linecap: round; }

.position {
  margin-top: 10px;
  background: rgba(10,13,19,0.55);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12px; color: var(--txt);
  display: flex; flex-direction: column; gap: 3px;
}
.position .row1 { display: flex; align-items: baseline; gap: 8px; }
.position .latlon { font-weight: 600; }
.position .cep { color: var(--dim); font-size: 11px; }
.position .meta-row { color: var(--dim); font-size: 10.5px;
                      letter-spacing: 0.02em; }
.position.no-pos { color: var(--dim-2); font-style: italic;
                   font-size: 11px; background: transparent; border-style: dashed; }
.position.no-pos .latlon { color: var(--dim); font-weight: normal; }

.foot {
  margin-top: 8px;
  display: flex; flex-wrap: wrap; gap: 6px;
  font-size: 11px; color: var(--dim);
  font-family: ui-monospace, monospace;
}
.chip {
  display: inline-block; padding: 1px 7px; border-radius: 4px;
  font-size: 10.5px;
  background: rgba(255,255,255,0.03);
  border: 1px solid var(--line);
}
.chip.ul  { color: var(--blue); border-color: rgba(127,185,217,0.22); }
.chip.dl  { color: #b4b8d9; border-color: rgba(180,184,217,0.18); }
.chip.dci { color: #d9a280; border-color: rgba(217,162,128,0.22); }
.chip.plain { color: var(--dim); }
.chip.motion-stationary { color: var(--green); border-color: rgba(122,217,161,0.28);
                          background: rgba(122,217,161,0.06); }
.chip.motion-mobile     { color: var(--yellow); border-color: rgba(240,194,112,0.32);
                          background: rgba(240,194,112,0.06); }
.chip.motion-indeterminate { color: var(--dim); }

/* ---------------------------------------------------------- scrubber */
.scrubber {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 12px;
  padding: 9px 20px;
  border-top: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(20,25,35,0.55), rgba(15,19,26,0.3));
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
  color: var(--dim);
}
.scrubber.replay {
  background: linear-gradient(180deg, rgba(240,194,112,0.06), rgba(15,19,26,0.3));
  border-top-color: rgba(240,194,112,0.28);
}
.scrubber .live-btn {
  font-family: inherit; font-size: 11px; padding: 4px 12px;
  border-radius: 999px; border: 1px solid rgba(122,217,161,0.3);
  background: rgba(20,25,35,0.5); color: var(--green); cursor: pointer;
  letter-spacing: 0.06em; transition: background 0.15s, color 0.15s;
  display: inline-flex; align-items: center; gap: 6px;
}
.scrubber.replay .live-btn { color: var(--yellow); border-color: rgba(240,194,112,0.4); }
.scrubber .live-btn:hover { background: rgba(122,217,161,0.08); }
.scrubber.replay .live-btn:hover { background: rgba(240,194,112,0.08); }
.scrubber .live-btn .led {
  width: 7px; height: 7px; border-radius: 50%; background: var(--green);
  box-shadow: 0 0 0 3px rgba(122,217,161,0.18);
  animation: pulse 1.6s ease-in-out infinite;
}
.scrubber.replay .live-btn .led { background: var(--yellow); box-shadow: 0 0 0 3px rgba(240,194,112,0.18);
                                  animation: none; }
.scrubber input[type=range] {
  flex: 1 1 auto; appearance: none; height: 4px; border-radius: 999px;
  background: var(--line-2);
  cursor: pointer; outline: none;
}
.scrubber input[type=range]::-webkit-slider-thumb {
  appearance: none; width: 14px; height: 14px; border-radius: 50%;
  background: var(--txt); border: 2px solid var(--green); cursor: grab;
}
.scrubber.replay input[type=range]::-webkit-slider-thumb { border-color: var(--yellow); }
.scrubber input[type=range]:active::-webkit-slider-thumb { cursor: grabbing; }
.scrubber .lbl { text-transform: uppercase; letter-spacing: 0.08em;
                 font-size: 10px; color: var(--dim-2); }
.scrubber .time { color: var(--txt); font-variant-numeric: tabular-nums; min-width: 78px; }

/* ---------------------------------------------------------- log */
footer {
  flex: 0 0 auto;
  padding: 7px 20px;
  border-top: 1px solid var(--line);
  font-family: ui-monospace, monospace; font-size: 11px;
  color: var(--dim);
  max-height: 120px; overflow-y: auto;
  background: rgba(10,13,19,0.4);
}
footer .row { padding: 1px 0; }
footer .row .ts { color: var(--dim-2); margin-right: 8px; }
footer .row b { color: var(--txt); font-weight: 600; }
</style>
</head><body>
<header>
  <div class="brand">
    <span class="led"></span>
    <h1>Cellular drones <span>· passive UE sniffer</span></h1>
  </div>
  <div class="meta">
    <div class="stat">UEs <strong id="n-ues">0</strong></div>
    <div class="stat">positioned <strong id="n-pos">0</strong></div>
    <div class="stat">grants <strong id="n-ue-sightings">0</strong></div>
    <div class="stat">uptime <strong id="uptime">0s</strong></div>
    <span class="pill gps-unknown" id="gps"><span class="led"></span><span id="gps-text">GPS: —</span></span>
    <span class="pill status-idle" id="status"><span class="led"></span><span id="status-text">idle</span></span>
  </div>
</header>
<div class="banner">
  <strong>C-RNTI is connection-scoped, not subscriber-scoped.</strong>
  <span class="dim">A handset that re-attaches will be reissued a new C-RNTI.
  Positions use UL grants only — DL grants come from the eNB.</span>
</div>
<div class="workspace">
  <div class="map-wrap">
    <div id="map"></div>
    <div class="map-overlay" id="map-overlay">map · waiting for GPS</div>
    <div class="legend" id="legend" style="display:none">
      <div class="label">UEs on map</div>
      <div id="legend-rows"></div>
    </div>
  </div>
  <aside class="side">
    <div class="side-head">
      <strong>UEs · live</strong>
      <span id="side-sum">—</span>
    </div>
    <div class="cards" id="cards">
      <div class="empty" id="empty">
        Waiting for the first PDCCH decode.
        <div class="hint">UEs appear here with their C-RNTI, live signal trend,
        and position estimate as UL grants and GPS fixes accumulate.</div>
      </div>
    </div>
  </aside>
</div>
<div class="scrubber" id="scrubber">
  <button class="live-btn" id="live-btn" type="button">
    <span class="led"></span><span id="live-btn-text">LIVE</span>
  </button>
  <span class="lbl">replay</span>
  <input type="range" id="scrub-range" min="0" max="1" value="1" step="0.001" disabled>
  <span class="time" id="scrub-time">—</span>
</div>
<footer id="log"></footer>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" crossorigin=""></script>
<script>
const cardsEl     = document.getElementById('cards');
const emptyEl     = document.getElementById('empty');
const logEl       = document.getElementById('log');
const nUesEl      = document.getElementById('n-ues');
const nPosEl      = document.getElementById('n-pos');
const nGrantsEl   = document.getElementById('n-ue-sightings');
const uptimeEl    = document.getElementById('uptime');
const statusEl    = document.getElementById('status');
const statusText  = document.getElementById('status-text');
const gpsEl       = document.getElementById('gps');
const gpsText     = document.getElementById('gps-text');
const mapEl       = document.getElementById('map');
const mapOverlay  = document.getElementById('map-overlay');
const legendEl    = document.getElementById('legend');
const legendRows  = document.getElementById('legend-rows');
const sideSumEl   = document.getElementById('side-sum');

// Stable per-UE color so map markers, card accents and legend agree.
const PALETTE = [
  '#7fb9d9', '#d9a280', '#a8d97f', '#d97fa8', '#d9d37f',
  '#9aa0d0', '#80c9d9', '#d9805a', '#a9d9bb', '#c8a3d9',
];
function colorFor(key) {
  let h = 0;
  for (const ch of String(key)) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

let totalUeSightings = 0;
let latestGps = null;
let selectedKey = null;
const ues = new Map();

// --- map ----------------------------------------------------------------
let map = null;
let droneMarker = null;
let droneTrailLine = null;
const droneTrail = [];
const ueLayers = new Map();   // key -> {marker, accuracy, label}

function initMapIfReady(centerLat, centerLon) {
  if (map || typeof L === 'undefined') return;
  map = L.map(mapEl, { zoomControl: true, attributionControl: true })
        .setView([centerLat, centerLon], 17);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png', {
    maxZoom: 19, subdomains: 'abc',
    attribution: '© OpenStreetMap, © CARTO',
  }).addTo(map);
  droneTrailLine = L.polyline(droneTrail, { color:'#7ea2c8', weight:2, opacity:0.7 }).addTo(map);
  ues.forEach((u) => updateUeLayer(u));
}
function leafletUnavailable() {
  if (typeof L !== 'undefined' || mapEl.classList.contains('unavailable')) return;
  mapEl.classList.add('unavailable');
  mapEl.textContent = 'map needs internet for tiles · positions still listed on the right';
}
setTimeout(leafletUnavailable, 4000);

function updateDroneMarker() {
  if (!map || !latestGps) return;
  const g = latestGps.gps;
  if (!droneMarker) {
    droneMarker = L.circleMarker([g.lat, g.lon], {
      radius: 6, color:'#d9e6f7', weight:2, fillColor:'#5a8fd9', fillOpacity:0.9,
    }).addTo(map).bindTooltip('drone', {permanent:false, direction:'top'});
    map.setView([g.lat, g.lon], Math.max(map.getZoom(), 17));
  } else {
    droneMarker.setLatLng([g.lat, g.lon]);
  }
  const last = droneTrail[droneTrail.length - 1];
  if (!last || last[0] !== g.lat || last[1] !== g.lon) {
    droneTrail.push([g.lat, g.lon]);
    if (droneTrail.length > 1000) droneTrail.shift();
    if (droneTrailLine) droneTrailLine.setLatLngs(droneTrail);
  }
}
function updateUeLayer(u) {
  if (!map) return;
  const p = u.est_position;
  const color = colorFor(u.key);
  let layer = ueLayers.get(u.key);
  if (!p) {
    // No estimate yet — make sure any stale marker is removed.
    if (layer) {
      map.removeLayer(layer.marker);
      map.removeLayer(layer.accuracy);
      ueLayers.delete(u.key);
    }
    return;
  }
  const selected = (u.key === selectedKey);
  const markerOpts = {
    radius: selected ? 10 : 7,
    color: color, weight: selected ? 3 : 2,
    fillColor: color, fillOpacity: selected ? 0.75 : 0.55,
  };
  if (!layer) {
    const marker = L.circleMarker([p.lat, p.lon], markerOpts).addTo(map);
    marker.bindTooltip(u.c_rnti_hex,
      {permanent: true, direction: 'right', offset: [10, 0], className: 'ue-tip'});
    marker.on('click', () => selectUe(u.key, true));
    const accuracy = L.circle([p.lat, p.lon], {
      radius: Math.max(2, p.cep95_m || 5),
      color: color, weight: 1, opacity: 0.5,
      fillColor: color, fillOpacity: 0.06,
      dashArray: '4 3',
    }).addTo(map);
    layer = {marker, accuracy};
    ueLayers.set(u.key, layer);
  } else {
    layer.marker.setLatLng([p.lat, p.lon]);
    layer.marker.setStyle(markerOpts);
    layer.accuracy.setLatLng([p.lat, p.lon]);
    layer.accuracy.setRadius(Math.max(2, p.cep95_m || 5));
    layer.accuracy.setStyle({color: color, fillColor: color,
                             opacity: selected ? 0.85 : 0.5,
                             fillOpacity: selected ? 0.12 : 0.06});
  }
}
function refreshAllLayers() {
  ues.forEach(u => updateUeLayer(u));
}
function focusUeOnMap(key) {
  const layer = ueLayers.get(key);
  if (!layer || !map) return;
  map.setView(layer.marker.getLatLng(), Math.max(map.getZoom(), 18),
              {animate: true});
}
function renderMapOverlay() {
  if (!latestGps) {
    mapOverlay.innerHTML = 'map · <span style="color:var(--dim)">waiting for GPS</span>';
    return;
  }
  const g = latestGps.gps;
  const positioned = [...ues.values()].filter(u => u.est_position).length;
  mapOverlay.innerHTML =
    `<div><strong>drone</strong> ${g.lat.toFixed(5)}, ${g.lon.toFixed(5)}</div>` +
    `<div>trail ${droneTrail.length} pts · ${positioned}/${ues.size} positioned</div>`;
}
function renderLegend() {
  if (!ues.size) { legendEl.style.display = 'none'; return; }
  legendEl.style.display = '';
  const items = [...ues.values()].sort((a, b) => (b.ul_count || 0) - (a.ul_count || 0));
  legendRows.innerHTML = items.map(u => {
    const c = colorFor(u.key);
    const cls = ['row',
                 u.est_position ? 'has-pos' : 'no-pos',
                 u.key === selectedKey ? 'active' : ''].join(' ');
    return `<div class="${cls}" data-key="${u.key}">
              <span class="sw" style="background:${c}"></span>
              <span>${u.c_rnti_hex}</span>
              <span style="color:var(--dim-2)">PCI ${u.pci}</span>
            </div>`;
  }).join('');
  legendRows.querySelectorAll('.row').forEach(el => {
    el.addEventListener('click', () => selectUe(el.getAttribute('data-key'), true));
  });
}

// --- cards --------------------------------------------------------------
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
  if (isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 2) return 'now';
  if (s < 60) return Math.round(s) + 's';
  if (s < 3600) return Math.round(s/60) + 'm';
  return Math.round(s/3600) + 'h';
}
function rsrpClass(v) {
  if (v == null) return 'rsrp-none';
  if (v >= -75) return 'rsrp-strong';
  if (v >= -95) return 'rsrp-mid';
  return 'rsrp-weak';
}
function sparkSvg(history, color) {
  if (!history || history.length < 2) {
    return `<svg class="spark" viewBox="0 0 130 36" style="--ue-color:${color}"></svg>`;
  }
  const W = 130, H = 36, pad = 2;
  const vals = history.map(p => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = Math.max(1, max - min);
  const dx = (W - 2*pad) / (history.length - 1);
  let line = '', fill = '';
  history.forEach((p, i) => {
    const x = pad + i*dx;
    const y = pad + (H - 2*pad) * (1 - (p[1] - min) / span);
    const cmd = (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
    line += cmd + ' ';
    fill += (i === 0 ? `M${x.toFixed(1)},${H} L${x.toFixed(1)},${y.toFixed(1)}`
                     : ` L${x.toFixed(1)},${y.toFixed(1)}`);
  });
  fill += ` L${(pad + (history.length-1)*dx).toFixed(1)},${H} Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" style="--ue-color:${color}">`
       + `<path class="fill" d="${fill}"/>`
       + `<path class="line" d="${line}"/></svg>`;
}
function positionBlock(u) {
  const p = u.est_position;
  if (!p) {
    let why;
    if (!latestGps) why = 'no GPS fix yet';
    else if ((u.ul_count || 0) < 2) why = `need ≥ 2 UL grants (have ${u.ul_count || 0})`;
    else if ((u.n_geo_samples || 0) < 2) why = `need ≥ 2 geo-tagged grants (have ${u.n_geo_samples || 0})`;
    else why = 'computing…';
    return `<div class="position no-pos">
              <div class="row1"><span class="latlon">no position</span></div>
              <div class="meta-row">${why}</div>
            </div>`;
  }
  const cep = p.cep95_m != null ? `±${p.cep95_m.toFixed(1)} m` : '';
  const alt = p.alt_m != null ? `, ${p.alt_m.toFixed(0)} m AGL` : '';
  return `<div class="position">
            <div class="row1">
              <span class="latlon">${p.lat.toFixed(6)}, ${p.lon.toFixed(6)}</span>
              <span class="cep">${cep}</span>
            </div>
            <div class="meta-row">${p.method} · ${p.n_samples} UL samples${alt}</div>
          </div>`;
}
function renderCard(u) {
  const color = colorFor(u.key);
  let card = document.getElementById('card-' + u.key);
  const isNew = !card;
  if (isNew) {
    card = document.createElement('div');
    card.id = 'card-' + u.key;
    card.className = 'card';
    card.addEventListener('click', () => selectUe(u.key, true));
    cardsEl.appendChild(card);
  }
  card.style.setProperty('--ue-color', color);
  card.classList.toggle('selected', u.key === selectedKey);

  const rssi = u.ul_rssi_dbm;
  const rssiCls = rsrpClass(rssi);
  const rssiBody = rssi != null
    ? `${rssi.toFixed(1)}<span class="unit">dBm UL</span>`
    : `no UL<span class="unit">DL-only</span>`;
  const ulChip  = (u.ul_count > 0) ? `<span class="chip ul">UL ${u.ul_count}</span>` : '';
  const dlChip  = (u.dl_count > 0) ? `<span class="chip dl">DL ${u.dl_count}</span>` : '';
  const dciTags = (u.dci_formats || []).map(f => `<span class="chip dci">DCI ${f}</span>`).join('');
  const mcs = u.mcs    != null ? `<span class="chip plain">MCS ${u.mcs}</span>` : '';
  const prb = u.n_prb  != null ? `<span class="chip plain">${u.n_prb} PRB</span>` : '';
  const tbs = u.tbs_bytes != null ? `<span class="chip plain">${u.tbs_bytes} B</span>` : '';
  const motion = u.motion && u.motion.label
    ? `<span class="chip motion-${u.motion.label}" title="${u.motion.note || ''}">${u.motion.label}</span>`
    : '';

  card.innerHTML = `
    <div class="head">
      <div class="identity">
        <span class="sw"></span>
        <span class="crnti">${u.c_rnti_hex}</span>
        <span class="pci-tag">PCI ${u.pci}</span>
      </div>
      <span class="seen" title="${u.last_seen || ''}">${timeAgo(u.last_seen)} ago</span>
    </div>
    <div class="cell">${fmtFreq(u.center_hz)}</div>
    <div class="metrics">
      <div class="metric-rssi">
        <div class="label">current UL RSSI</div>
        <div class="value ${rssiCls}">${rssiBody}</div>
      </div>
      ${sparkSvg(u.ul_rssi_history, color)}
    </div>
    ${positionBlock(u)}
    <div class="foot">${motion}${ulChip}${dlChip}${dciTags}${mcs}${prb}${tbs}</div>
  `;
  if (!isNew) {
    card.classList.remove('fresh');
    void card.offsetWidth;
  }
  card.classList.add('fresh');
}
function sortCards() {
  const sorted = [...ues.values()].sort((a, b) => {
    const ap = a.est_position ? 0 : 1;
    const bp = b.est_position ? 0 : 1;
    if (ap !== bp) return ap - bp;
    if ((b.ul_count || 0) !== (a.ul_count || 0))
      return (b.ul_count || 0) - (a.ul_count || 0);
    return (b.ul_rssi_dbm ?? -1e9) - (a.ul_rssi_dbm ?? -1e9);
  });
  sorted.forEach((u, i) => {
    const card = document.getElementById('card-' + u.key);
    if (card && cardsEl.children[i] !== card) cardsEl.appendChild(card);
  });
}
function selectUe(key, fromUi) {
  selectedKey = selectedKey === key ? null : key;
  document.querySelectorAll('.card').forEach(c => {
    c.classList.toggle('selected', c.id === 'card-' + selectedKey);
  });
  refreshAllLayers();
  renderLegend();
  if (fromUi && selectedKey) {
    const c = document.getElementById('card-' + selectedKey);
    if (c) c.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    focusUeOnMap(selectedKey);
  }
}

// --- chrome (status, gps, log) ------------------------------------------
function setStatus(s) {
  let text = s.phase || 'idle';
  if (s.phase === 'sniffing') {
    const mhz = s.center_hz != null ? (s.center_hz/1e6).toFixed(2) + ' MHz' : '';
    text = mhz ? `live · ${mhz}` : 'live';
  } else if (s.phase === 'simulating') {
    text = 'simulating';
  } else if (s.message) {
    text += ' — ' + s.message;
  }
  statusText.textContent = text;
  statusEl.className = 'pill status-' + (s.phase || 'idle');
}
function setGps(rec) {
  latestGps = rec;
  if (!rec) {
    gpsText.textContent = 'GPS: —';
    gpsEl.className = 'pill gps-unknown';
  } else {
    const g = rec.gps;
    gpsEl.className = 'pill gps-' + (g.fix || 'unknown');
    gpsText.textContent = `${g.lat.toFixed(5)}, ${g.lon.toFixed(5)} · ${g.fix}`;
    initMapIfReady(g.lat, g.lon);
    updateDroneMarker();
  }
  renderMapOverlay();
}
function logLine(html) {
  const d = document.createElement('div');
  d.className = 'row';
  const ts = new Date().toTimeString().slice(0, 8);
  d.innerHTML = '<span class="ts">' + ts + '</span>' + html;
  logEl.prepend(d);
  while (logEl.children.length > 80) logEl.removeChild(logEl.lastChild);
}

function updateSummary() {
  let positioned = 0;
  ues.forEach(u => { if (u.est_position) positioned += 1; });
  nUesEl.textContent = ues.size;
  nPosEl.textContent = positioned;
  nGrantsEl.textContent = totalUeSightings;
  sideSumEl.textContent = ues.size ? `${positioned}/${ues.size} positioned` : '—';
  emptyEl.style.display = ues.size ? 'none' : '';
}

// --- SSE ----------------------------------------------------------------
function applyEvent(ev) {
  if (ev.type === 'snapshot') {
    ues.clear();
    [...cardsEl.querySelectorAll('.card')].forEach(c => c.remove());
    ueLayers.forEach(({marker, accuracy}) => {
      if (map) { map.removeLayer(marker); map.removeLayer(accuracy); }
    });
    ueLayers.clear();
    droneTrail.length = 0;
    (ev.gps_trail || []).forEach(p => droneTrail.push([p.lat, p.lon]));
    if (droneTrailLine) droneTrailLine.setLatLngs(droneTrail);
    setStatus(ev.status || {phase: 'idle'});
    setGps(ev.latest_gps);
    (ev.ues || []).forEach(u => { ues.set(u.key, u); renderCard(u); updateUeLayer(u); });
    totalUeSightings = ev.total_ue_sightings || 0;
  } else if (ev.type === 'ue_sighting') {
    const u = ev.ue;
    ues.set(u.key, u);
    renderCard(u);
    updateUeLayer(u);
    totalUeSightings += 1;
    const pos = u.est_position
      ? ` · est ${u.est_position.lat.toFixed(5)},${u.est_position.lon.toFixed(5)} ±${u.est_position.cep95_m.toFixed(0)}m`
      : '';
    logLine(`<b>${u.c_rnti_hex}</b> PCI ${u.pci} · UL ${fmt(u.ul_rssi_dbm, ' dBm')}${pos}`);
  } else if (ev.type === 'gps') {
    setGps(ev.fix);
  } else if (ev.type === 'status') {
    setStatus(ev.status);
    logLine('status: ' + ev.status.phase + (ev.status.message ? ' — ' + ev.status.message : ''));
  }
  updateSummary();
  sortCards();
  renderLegend();
  renderMapOverlay();
}
let t0 = Date.now();
setInterval(() => {
  uptimeEl.textContent = Math.round((Date.now() - t0)/1000) + 's';
  ues.forEach(u => {
    const seenEl = document.querySelector('#card-' + u.key + ' .seen');
    if (seenEl) seenEl.textContent = timeAgo(u.last_seen) + ' ago';
  });
}, 1000);
// --- replay scrubber ----------------------------------------------------
const scrubberEl   = document.getElementById('scrubber');
const scrubRangeEl = document.getElementById('scrub-range');
const scrubTimeEl  = document.getElementById('scrub-time');
const liveBtnEl    = document.getElementById('live-btn');
const liveBtnText  = document.getElementById('live-btn-text');

let replayMode = false;
let bounds = null;          // {t_min_ns, t_max_ns, started_ns, now_ns}
let lastScrubAt = 0;
let scrubInflight = null;

function fmtElapsed(ns) {
  if (!bounds || !bounds.started_ns) return '—';
  const s = Math.max(0, (ns - bounds.started_ns) / 1e9);
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60), r = s - 60*m;
  if (m < 60) return m + 'm ' + r.toFixed(0).padStart(2,'0') + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m - 60*h) + 'm';
}

async function refreshBounds() {
  try {
    const r = await fetch('/bounds');
    bounds = await r.json();
    const haveSpan = bounds.t_min_ns != null && bounds.t_max_ns != null
                     && bounds.t_max_ns > bounds.t_min_ns;
    scrubRangeEl.disabled = !haveSpan;
    if (!replayMode && haveSpan) {
      // While LIVE, keep the slider pinned to the right edge.
      scrubRangeEl.value = '1';
      scrubTimeEl.textContent = 'now · ' + fmtElapsed(bounds.t_max_ns);
    }
    if (!haveSpan) scrubTimeEl.textContent = 'no GPS yet';
  } catch (_) { /* server probably gone */ }
}
refreshBounds();
setInterval(refreshBounds, 3000);

function setReplayMode(on) {
  replayMode = on;
  scrubberEl.classList.toggle('replay', on);
  liveBtnEl.classList.toggle('is-replay', on);
  liveBtnEl.classList.toggle('is-live', !on);
  liveBtnText.textContent = on ? 'GO LIVE' : 'LIVE';
}
setReplayMode(false);

async function applyReplayAt(ts) {
  if (scrubInflight) return;       // cheap rate-limit while dragging
  scrubInflight = fetch('/replay?ts_mono_ns=' + ts)
    .then(r => r.json())
    .then(snap => { applyEvent(snap); })
    .catch(() => {})
    .finally(() => { scrubInflight = null; });
}

scrubRangeEl.addEventListener('input', () => {
  if (!bounds || bounds.t_min_ns == null || bounds.t_max_ns == null) return;
  const frac = parseFloat(scrubRangeEl.value);
  const ts = Math.round(bounds.t_min_ns + frac * (bounds.t_max_ns - bounds.t_min_ns));
  scrubTimeEl.textContent = 'replay · ' + fmtElapsed(ts);
  if (!replayMode) setReplayMode(true);
  // Throttle: at most ~12 Hz of replay fetches while dragging.
  const now = performance.now();
  if (now - lastScrubAt < 80) return;
  lastScrubAt = now;
  applyReplayAt(ts);
});

liveBtnEl.addEventListener('click', async () => {
  if (!replayMode) return;
  setReplayMode(false);
  scrubRangeEl.value = '1';
  try {
    const r = await fetch('/state');
    const snap = await r.json();
    applyEvent(snap);
  } catch (_) {}
});

// SSE: skip live events while we're scrubbing through history.
const es = new EventSource('/events');
es.onmessage = (e) => {
  try {
    const ev = JSON.parse(e.data);
    if (replayMode && (ev.type === 'gps' || ev.type === 'ue_sighting'
                       || (ev.type === 'snapshot' && !ev.replay))) return;
    applyEvent(ev);
  } catch (_) {}
};
es.onerror = () => logLine('<span style="color:var(--red)">stream disconnected</span> — browser will retry');
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
        if self.path == "/bounds":
            body = json.dumps(self.state.time_bounds()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/replay"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                ts = int(qs.get("ts_mono_ns", ["0"])[0])
            except ValueError:
                self.send_response(400); self.end_headers(); return
            body = json.dumps(self.state.snapshot_at(ts)).encode("utf-8")
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
    p.add_argument("--normalize-ltesniffer", action="store_true",
                   help="pipe LTESniffer stdout through normalize_stream "
                        "(use when the binary emits human-readable lines, "
                        "not canonical DECODED key=value)")
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
                    stop=stop, normalize=args.normalize_ltesniffer,
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
