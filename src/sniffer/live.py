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
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator, Optional

from sniffer.localize import weighted_centroid_ue
from sniffer.ta_multilateration import ta_multilateration_ue
from sniffer.parse_droneid import parse_stream as parse_droneid_stream
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
from sniffer.parse_ltesniffer import (
    normalize_stream,
    parse_stream as parse_ltesniffer_stream,
)
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
    SimulationConfig,
    box_trajectory,
    gpsd_lines,
    ltesniffer_lines,
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
                    "ta_geo_history": deque(maxlen=UE_HISTORY_MAX),
                    "est_position": None,
                    "est_position_ta": None,
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

            if ue.direction == "ul" and (
                ue.ul_rssi_dbm is not None or ue.ta_meters is not None
            ):
                hit = self._nearest_gps_locked(sighting.ts_mono_ns)
                if hit is not None:
                    gps_rec, age_ms = hit
                    gps_with_age = {**gps_rec["gps"], "age_ms": age_ms}
                    if ue.ul_rssi_dbm is not None:
                        entry["geo_history"].append({
                            "kind": "ue_sighting",
                            "ts_mono_ns": sighting.ts_mono_ns,
                            "gps": gps_with_age,
                            "ue": {"pci": pci, "c_rnti": c_rnti,
                                   "direction": "ul",
                                   "ul_rssi_dbm": ue.ul_rssi_dbm},
                        })
                        _recompute_ue_position(entry)
                    if ue.ta_meters is not None:
                        entry["ta_geo_history"].append({
                            "kind": "ue_sighting",
                            "ts_mono_ns": sighting.ts_mono_ns,
                            "gps": gps_with_age,
                            "ue": {"pci": pci, "c_rnti": c_rnti,
                                   "direction": "ul",
                                   "ta_meters": ue.ta_meters},
                        })
                        _recompute_ue_position_ta(entry)

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
                    (u.get("est_position") is None
                     and u.get("est_position_ta") is None),
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
                past_ta = [g for g in entry["ta_geo_history"]
                           if g["ts_mono_ns"] <= at_ts_mono_ns]
                if not past and not past_ta:
                    continue
                last_g = past[-1] if past else past_ta[-1]
                cent = weighted_centroid_ue(past) if past else None
                est_pos = None
                if cent is not None:
                    est_pos = {
                        "lat": cent.lat, "lon": cent.lon,
                        "alt_m": cent.alt_m, "cep95_m": cent.cep95_m,
                        "method": "weighted_centroid",
                        "n_samples": cent.n_samples,
                        "altitude_estimated": cent.altitude_estimated,
                    }
                ta_res = (ta_multilateration_ue(past_ta)
                          if len(past_ta) >= 4 else None)
                est_pos_ta = None
                if ta_res is not None:
                    est_pos_ta = {
                        "lat": ta_res.lat, "lon": ta_res.lon,
                        "alt_m": ta_res.alt_m, "cep95_m": ta_res.cep95_m,
                        "method": "ta_multilateration",
                        "n_samples": ta_res.n_samples,
                        "altitude_estimated": ta_res.altitude_estimated,
                    }
                ues_out.append({
                    "key": entry["key"],
                    "pci": entry["pci"],
                    "c_rnti": entry["c_rnti"],
                    "c_rnti_hex": entry["c_rnti_hex"],
                    "center_hz": entry["center_hz"],
                    "first_seen": entry["first_seen"],
                    "last_seen": last_g.get("ts_utc", entry["first_seen"]),
                    "count": len(past) or len(past_ta),
                    "ul_count": len(past) or len(past_ta),
                    "dl_count": 0,
                    "dci_formats": entry.get("dci_formats", []),
                    "mcs": None, "n_prb": None, "tbs_bytes": None,
                    "ul_rssi_dbm": last_g["ue"].get("ul_rssi_dbm"),
                    "dl_rsrp_dbm": None,
                    "ul_rssi_history": [],
                    "n_geo_samples": len(past),
                    "n_ta_samples": len(past_ta),
                    "trail": [{"lat": g["gps"]["lat"],
                               "lon": g["gps"]["lon"],
                               "ul_rssi_dbm": g["ue"]["ul_rssi_dbm"]}
                              for g in past[-40:]],
                    "est_position": est_pos,
                    "est_position_ta": est_pos_ta,
                })
            ues_out.sort(
                key=lambda u: ((u.get("est_position") is None
                                and u.get("est_position_ta") is None),
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
           if k not in ("ul_rssi_history", "geo_history", "ta_geo_history")}
    out["ul_rssi_history"] = list(entry["ul_rssi_history"])
    out["n_geo_samples"] = len(entry["geo_history"])
    out["n_ta_samples"] = len(entry["ta_geo_history"])
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


def _recompute_ue_position_ta(entry: dict[str, Any]) -> None:
    """Mirror of `_recompute_ue_position` for the TA estimator.

    Independent estimator (TA range-based multilateration), not a fallback —
    runs alongside the centroid when TA-bearing UL grants are available and
    is rendered as a separately labelled position. Stays None until the
    solver has enough geometry to refuse-or-converge.
    """
    records = list(entry["ta_geo_history"])
    if len(records) < 4:
        return
    res = ta_multilateration_ue(records)
    if res is None:
        return
    entry["est_position_ta"] = {
        "lat": res.lat, "lon": res.lon,
        "alt_m": res.alt_m, "cep95_m": res.cep95_m,
        "method": "ta_multilateration",
        "n_samples": res.n_samples,
        "altitude_estimated": res.altitude_estimated,
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


def run_droneid_loop(state: State, *, droneid_cmd: list[str],
                     mission_id: str, stop: threading.Event,
                     serial_filter: Optional[str] = None) -> None:
    """Spawn a DroneID decoder, feed its JSON into State as a GPS source.

    `droneid_cmd` is the full argv to a DroneID decoder that prints one
    JSON object per decoded frame on stdout. Two known-supported shapes:

      * `RUB-SysSec/DroneSecurity` (USRP B2xx, 50 MSPS) — its live
        receiver emits the JSON natively, so the cmd is roughly
        `["python", ".../droneid_receiver_live.py", "-g", "40"]`.

      * `anarkiwi/samples2djidroneid` (HackRF, 15.36 MSPS) — file-based,
        wrap with `python -m sniffer.droneid_hackrf --decoder-cmd "..."`
        to get a continuous JSON stream from the HackRF capture loop.

    `serial_filter` restricts to frames matching that serial (substring),
    useful when several drones are airborne and only one is yours.

    The decoded position lands on State as a GpsFix with fix='droneid'
    — same code path as gpsd. Use multiple producers (one per HackRF /
    one per band) by starting the loop multiple times.
    """
    if not droneid_cmd:
        return
    if (shutil.which(droneid_cmd[0]) is None
            and not os.path.exists(droneid_cmd[0])):
        state.set_status(
            "error",
            message=(f"`{droneid_cmd[0]}` not found. Build the decoder "
                     f"(scripts/install-linux.sh) or pass --droneid-cmd."),
        )
        return
    while not stop.is_set():
        proc = subprocess.Popen(
            droneid_cmd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        try:
            sink = _GpsSink(state)
            assert proc.stdout is not None
            parse_droneid_stream(proc.stdout, mission_id, sink,
                                 serial_filter=serial_filter)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            proc.wait()
        if stop.is_set():
            return
        time.sleep(1.0)


_SIM_TICK_RE = re.compile(r"^#\s*TICK\s+t=([-\d.]+)")


def _paced_tick(line_iter: Iterator[str],
                stop: threading.Event) -> Iterator[str]:
    """Wall-clock-pace a `simulate.ltesniffer_lines` stream.

    The simulator interleaves `# TICK t=X` markers (in simulated seconds)
    with content lines. We sleep until wall-clock matches each TICK, then
    pass the content lines through verbatim. Comment lines are swallowed
    here so the downstream parser only sees the same DECODED text that
    LTESniffer itself would emit on the wire.
    """
    t_start = time.monotonic()
    for line in line_iter:
        if stop.is_set():
            return
        m = _SIM_TICK_RE.match(line)
        if m:
            target = t_start + float(m.group(1))
            while not stop.is_set():
                delta = target - time.monotonic()
                if delta <= 0:
                    break
                time.sleep(min(0.05, delta))
            continue
        if line.startswith("#"):
            continue  # banner / header from simulate.* — not on the wire
        yield line


def _paced_fixed(line_iter: Iterator[str], period_s: float,
                 stop: threading.Event) -> Iterator[str]:
    """Wall-clock-pace a stream that has no TICK markers (e.g. gpsd_lines)."""
    t_start = time.monotonic()
    for i, line in enumerate(line_iter):
        if stop.is_set():
            return
        target = t_start + i * period_s
        while not stop.is_set():
            delta = target - time.monotonic()
            if delta <= 0:
                break
            time.sleep(min(0.05, delta))
        yield line


def _looped(factory: Callable[[], Iterator[str]],
            stop: threading.Event) -> Iterator[str]:
    """Re-invoke a finite line-generator factory until stop fires."""
    while not stop.is_set():
        yield from factory()


def _build_sim_config(mission_id: str) -> SimulationConfig:
    emitter = Emitter(lat=32.0853, lon=34.7818, alt_m=25.0, pci=271,
                      center_hz=1_842_500_000, n_id_1=90, n_id_2=1,
                      tx_power_dbm=24.0)
    waypoints = box_trajectory(emitter, half_size_m=100.0,
                               altitudes=(15.0, 30.0, 60.0),
                               n_per_side=20, leg_speed_mps=8.0)
    return SimulationConfig(mission_id=mission_id,
                            emitter=emitter, waypoints=waypoints)


def run_simulator(state: State, *, mission_id: str, out_dir: str,
                  stop: threading.Event) -> None:
    """Hardware-free producer for the dashboard.

    This is the same code path as `run_ltesniffer_loop` + `run_gpsd_loop`,
    just with synthetic upstreams instead of LTESniffer/gpspipe subprocesses:

        simulate.ltesniffer_lines → parse_ltesniffer.parse_stream → _UeSightingSink → State
        simulate.gpsd_lines       → parse_gpsd.parse_stream       → _GpsSink         → State

    The wall-clock pacers below replay the simulated trajectory in real
    time so SSE clients see events arrive at the cadence they would in a
    real flight. The trajectory is looped indefinitely.
    """
    state.set_status("simulating", mission_id=mission_id)
    os.makedirs(out_dir, exist_ok=True)
    ue_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")

    cfg = _build_sim_config(mission_id)
    parse_args = _ParseArgs(mission_id, "sim", "sim-ltesniffer",
                            rx_gain_db=0.0, center_hz=cfg.emitter.center_hz,
                            sample_rate_sps=23.04e6)

    def ue_worker() -> None:
        try:
            with open(ue_path, "a", encoding="utf-8") as fh:
                sink = _UeSightingSink(state, jsonl_out=fh)
                stream = _looped(
                    lambda: _paced_tick(ltesniffer_lines(cfg), stop), stop)
                parse_ltesniffer_stream(stream, parse_args, sink)
        except Exception as exc:  # noqa: BLE001
            state.set_status("error",
                             message=f"simulator parse failed: {exc}")

    def gps_worker() -> None:
        try:
            sink = _GpsSink(state)
            stream = _looped(
                lambda: _paced_fixed(gpsd_lines(cfg), cfg.gps_period_s, stop),
                stop)
            parse_gpsd_stream(stream, sink, mission_id)
        except Exception:  # noqa: BLE001
            pass

    workers = [threading.Thread(target=ue_worker, daemon=True),
               threading.Thread(target=gps_worker, daemon=True)]
    for t in workers:
        t.start()

    # Block until stop fires; workers exit naturally when their streams drain.
    while not stop.is_set():
        stop.wait(0.5)
    for t in workers:
        t.join(timeout=1.0)


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
  --bg: #04070a;
  --panel: #080c11;
  --panel-2: #0c1117;
  --line: #15202c;
  --line-2: #1f2c3b;
  --line-3: #2a3b4f;
  --dim: #6b7785;
  --dim-2: #424d5b;
  --dim-3: #2c343f;
  --txt: #d5dce4;
  --txt-2: #aab3bd;
  --accent: #5dd5ff;          /* primary cyan — single accent */
  --accent-dim: #1f4d63;
  --green: #58c98a;
  --yellow: #e6a851;
  --red: #ff6d6d;
  --blue: #5dd5ff;
  --grid: rgba(93, 213, 255, 0.025);
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%;
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
  background:
    linear-gradient(var(--grid) 1px, transparent 1px) 0 0 / 32px 32px,
    linear-gradient(90deg, var(--grid) 1px, transparent 1px) 0 0 / 32px 32px,
    var(--bg);
  color: var(--txt); -webkit-font-smoothing: antialiased; font-size: 12.5px;
}
body { display: flex; flex-direction: column; min-height: 100vh; }

/* ---------------------------------------------------------- header */
header {
  flex: 0 0 auto;
  padding: 9px 18px 8px;
  border-bottom: 1px solid var(--line-2);
  display: flex; align-items: center; justify-content: space-between;
  gap: 14px; flex-wrap: wrap;
  background: var(--panel);
  position: relative;
}
header::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: -2px;
  height: 1px; background: var(--accent-dim); opacity: 0.6;
}
.brand { display: flex; align-items: center; gap: 10px;
         font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace; }
.brand .led {
  width: 7px; height: 7px;
  background: var(--accent);
  box-shadow: 0 0 6px var(--accent);
}
header h1 { font-size: 12px; font-weight: 600; margin: 0;
            letter-spacing: 0.16em; text-transform: uppercase; color: var(--txt); }
header h1 span { color: var(--dim); font-weight: 400; margin-left: 8px;
                 letter-spacing: 0.12em; }
.meta { font-size: 10.5px; color: var(--dim); display: flex; gap: 18px;
        flex-wrap: wrap; align-items: center;
        font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace; }
.stat { display: flex; gap: 8px; align-items: baseline;
        text-transform: uppercase; letter-spacing: 0.14em; }
.stat strong { color: var(--txt); font-weight: 600; font-size: 13px;
               font-variant-numeric: tabular-nums; letter-spacing: 0;
               text-transform: none; }
.pill {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 3px 9px 3px 8px;
  font-size: 10.5px; border: 1px solid var(--line-2);
  background: var(--panel-2); color: var(--dim);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  letter-spacing: 0.06em;
}
.pill .led { width: 5px; height: 5px; background: var(--dim-2); }
.pill.status-sniffing { color: var(--accent); border-color: var(--accent-dim);
                        background: rgba(93,213,255,0.04); }
.pill.status-sniffing .led { background: var(--accent);
                              box-shadow: 0 0 6px var(--accent);
                              animation: pulse 1.6s ease-in-out infinite; }
.pill.status-simulating { color: var(--yellow); border-color: #4a3a17; background: rgba(230,168,81,0.04); }
.pill.status-simulating .led { background: var(--yellow); }
.pill.status-error { color: var(--red); border-color: #4a1f1f; background: rgba(255,109,109,0.04); }
.pill.status-error .led { background: var(--red); }
.pill.status-idle { color: var(--dim); }
.pill.gps-3d, .pill.gps-rtk_fix, .pill.gps-rtk_float {
  color: var(--green); border-color: #1f4a32; background: rgba(88,201,138,0.04); }
.pill.gps-3d .led, .pill.gps-rtk_fix .led, .pill.gps-rtk_float .led {
  background: var(--green); box-shadow: 0 0 5px var(--green); }
.pill.gps-2d { color: var(--yellow); border-color: #4a3a17; }
.pill.gps-2d .led { background: var(--yellow); }
.pill.gps-none, .pill.gps-unknown { color: var(--dim); }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

/* ---------------------------------------------------------- advisory */
.banner {
  margin: 0; padding: 5px 18px;
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
  color: var(--dim); font-size: 10.5px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  letter-spacing: 0.04em;
  display: flex; align-items: center; gap: 10px;
}
.banner::before {
  content: '!'; display: inline-flex; align-items: center; justify-content: center;
  width: 14px; height: 14px; border: 1px solid var(--yellow);
  color: var(--yellow); font-weight: 700; font-size: 10px; flex-shrink: 0;
}
.banner strong { color: var(--yellow); font-weight: 600; letter-spacing: 0.04em; }
.banner .dim { color: var(--dim); }

/* ---------------------------------------------------------- main grid */
.workspace {
  flex: 1 1 auto; min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(380px, 0.95fr);
  gap: 10px;
  padding: 10px 18px 12px;
}
@media (max-width: 1100px) {
  .workspace { grid-template-columns: 1fr; }
  .map-wrap { height: 46vh; min-height: 320px; }
  .side { height: 60vh; min-height: 360px; }
}

/* ---------------------------------------------------------- panel chrome */
.panel-tab {
  display: inline-flex; align-items: center; gap: 8px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase;
  color: var(--dim);
}
.panel-tab .dot {
  width: 6px; height: 6px; background: var(--accent); flex-shrink: 0;
}

/* ---------------------------------------------------------- map */
.map-wrap {
  position: relative;
  border: 1px solid var(--line-2);
  background: var(--panel);
  min-height: 380px;
}
.map-wrap::before, .map-wrap::after,
.map-wrap > .br-tl, .map-wrap > .br-tr,
.map-wrap > .br-bl, .map-wrap > .br-br {
  /* corner brackets */
  position: absolute; width: 10px; height: 10px; z-index: 600;
  pointer-events: none; border: 1px solid var(--accent);
}
.map-wrap::before { content: ''; top: -1px; left: -1px; border-right: none; border-bottom: none; }
.map-wrap::after  { content: ''; top: -1px; right: -1px; border-left: none; border-bottom: none; }
.map-wrap > .br-bl { bottom: -1px; left: -1px; border-right: none; border-top: none; }
.map-wrap > .br-br { bottom: -1px; right: -1px; border-left: none; border-top: none; }
#map { position: absolute; inset: 0; }
#map.unavailable { display: flex; align-items: center; justify-content: center;
                   color: var(--dim); font-size: 12px; padding: 16px;
                   font-family: ui-monospace, monospace; }
.map-overlay {
  position: absolute; top: 10px; right: 10px; z-index: 500;
  display: flex; flex-direction: column; gap: 3px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 10.5px;
  color: var(--dim);
  background: rgba(8, 12, 17, 0.88);
  border: 1px solid var(--line-2);
  padding: 7px 10px;
  pointer-events: none;
  max-width: 230px;
  letter-spacing: 0.02em;
}
.map-overlay strong { color: var(--accent); font-weight: 600;
                      text-transform: uppercase; letter-spacing: 0.1em;
                      font-size: 10px; }
.legend {
  position: absolute; bottom: 10px; left: 10px; z-index: 500;
  display: flex; flex-direction: column; gap: 3px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 10.5px;
  background: rgba(8, 12, 17, 0.88);
  border: 1px solid var(--line-2);
  padding: 7px 10px;
  max-height: 50%; overflow: auto;
  min-width: 140px;
}
.legend .row { display: flex; align-items: center; gap: 8px; cursor: pointer;
               color: var(--dim); padding: 2px 0; letter-spacing: 0.02em; }
.legend .row.active { color: var(--txt); }
.legend .row:hover  { color: var(--txt); }
.legend .row.has-pos::after { content: 'LOC'; color: var(--green); margin-left: auto;
                              font-size: 9px; letter-spacing: 0.1em; }
.legend .row.has-pos-ta::after { content: 'TA'; color: var(--yellow); margin-left: auto;
                                 font-size: 9px; letter-spacing: 0.1em; }
.legend .row.no-pos::after  { content: '---'; color: var(--dim-2); margin-left: auto;
                              font-size: 9px; letter-spacing: 0.1em; }
.legend .sw { width: 8px; height: 8px; flex-shrink: 0; }
.legend .label { color: var(--accent); font-size: 9.5px; text-transform: uppercase;
                 letter-spacing: 0.18em; margin-bottom: 5px; font-weight: 600; }

/* Leaflet tooltip override */
.leaflet-tooltip.ue-tip {
  background: rgba(8, 12, 17, 0.92) !important;
  border: 1px solid var(--line-3) !important;
  border-radius: 0 !important;
  color: var(--txt) !important;
  font-family: ui-monospace, "JetBrains Mono", monospace !important;
  font-size: 10.5px !important;
  padding: 2px 6px !important;
  box-shadow: none !important;
  letter-spacing: 0.02em !important;
}
.leaflet-tooltip.ue-tip::before { display: none !important; }
.leaflet-control-attribution {
  background: rgba(8, 12, 17, 0.75) !important;
  color: var(--dim-2) !important;
  font-size: 9px !important;
}
.leaflet-control-attribution a { color: var(--dim) !important; }
.leaflet-bar a {
  background: rgba(8, 12, 17, 0.92) !important;
  color: var(--txt) !important;
  border: 1px solid var(--line-2) !important;
  border-radius: 0 !important;
}

/* ---------------------------------------------------------- side cards */
.side {
  position: relative;
  display: flex; flex-direction: column; min-height: 0;
  border: 1px solid var(--line-2);
  background: var(--panel);
}
.side::before, .side::after,
.side > .br-bl, .side > .br-br {
  position: absolute; width: 10px; height: 10px; z-index: 5;
  pointer-events: none; border: 1px solid var(--accent);
}
.side::before { content: ''; top: -1px; left: -1px; border-right: none; border-bottom: none; }
.side::after  { content: ''; top: -1px; right: -1px; border-left: none; border-bottom: none; }
.side > .br-bl { bottom: -1px; left: -1px; border-right: none; border-top: none; }
.side > .br-br { bottom: -1px; right: -1px; border-left: none; border-top: none; }
.side-head {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between;
  padding: 9px 14px; border-bottom: 1px solid var(--line-2);
  font-size: 10px; color: var(--dim); text-transform: uppercase;
  letter-spacing: 0.16em; background: var(--panel-2);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
}
.side-head .panel-tab strong {
  color: var(--txt); font-weight: 600; font-size: 11px;
  letter-spacing: 0.18em; }
.side-head #side-sum {
  color: var(--dim); font-size: 10px; letter-spacing: 0.08em;
  font-variant-numeric: tabular-nums;
}
.cards { flex: 1 1 auto; overflow-y: auto; padding: 10px; }
.cards::-webkit-scrollbar { width: 8px; }
.cards::-webkit-scrollbar-track { background: transparent; }
.cards::-webkit-scrollbar-thumb { background: var(--line-2); }
.cards::-webkit-scrollbar-thumb:hover { background: var(--line-3); }
.empty {
  margin: 16px; padding: 28px 18px; text-align: center;
  border: 1px dashed var(--line-2);
  color: var(--dim); font-size: 11.5px; line-height: 1.65;
  font-family: ui-monospace, monospace; letter-spacing: 0.02em;
}
.empty .hint { color: var(--dim-2); font-size: 10.5px; margin-top: 8px;
               font-family: inherit; }

.card {
  position: relative;
  background: var(--panel-2);
  border: 1px solid var(--line-2);
  padding: 11px 13px 12px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}
.card::before {
  content: ''; position: absolute; top: 0; left: 0; bottom: 0;
  width: 2px; background: var(--ue-color, var(--dim-2));
}
.card:hover { border-color: var(--line-3); background: #0e1419; }
.card.selected {
  border-color: var(--ue-color, var(--accent));
  background: #0e1419;
}
.card.selected::after {
  content: ''; position: absolute;
  top: -1px; right: -1px; width: 8px; height: 8px;
  border-top: 1px solid var(--ue-color, var(--accent));
  border-right: 1px solid var(--ue-color, var(--accent));
}
.card.fresh { animation: flash 1.2s ease; }
@keyframes flash {
  0%   { background: rgba(93, 213, 255, 0.08); }
  100% { background: var(--panel-2); }
}
.card .head {
  display: flex; justify-content: space-between; align-items: center; gap: 10px;
  margin-bottom: 4px;
}
.identity { display: flex; align-items: center; gap: 9px; }
.identity .sw {
  width: 9px; height: 9px;
  background: var(--ue-color, var(--dim-2));
  box-shadow: 0 0 4px var(--ue-color, transparent);
}
.crnti {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-weight: 600; font-size: 17px; color: var(--txt);
  letter-spacing: 0.02em;
}
.pci-tag {
  font-size: 9.5px; padding: 2px 7px;
  background: transparent; color: var(--accent);
  font-family: ui-monospace, "JetBrains Mono", monospace; font-weight: 500;
  border: 1px solid var(--accent-dim);
  letter-spacing: 0.1em; text-transform: uppercase;
}
.seen { font-size: 10.5px; color: var(--dim); font-variant-numeric: tabular-nums;
        font-family: ui-monospace, "JetBrains Mono", monospace; letter-spacing: 0.04em; }
.cell {
  font-size: 9.5px; color: var(--dim-2); margin-top: 2px;
  text-transform: uppercase; letter-spacing: 0.14em;
  font-family: ui-monospace, "JetBrains Mono", monospace;
}
.cell::before { content: 'F · '; color: var(--dim-2); }
.metrics {
  display: grid; grid-template-columns: 1fr auto; gap: 12px;
  align-items: end; margin: 9px 0 2px;
}
.metric-rssi { display: flex; flex-direction: column; gap: 2px; }
.metric-rssi .label {
  font-size: 9.5px; color: var(--dim); text-transform: uppercase;
  letter-spacing: 0.14em;
  font-family: ui-monospace, "JetBrains Mono", monospace;
}
.metric-rssi .value {
  font-size: 21px; font-weight: 500; letter-spacing: -0.01em;
  font-variant-numeric: tabular-nums;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
}
.metric-rssi .value .unit { font-size: 10px; color: var(--dim);
                            margin-left: 5px; font-weight: 400;
                            letter-spacing: 0.08em; text-transform: uppercase; }
.rsrp-strong { color: var(--green); }
.rsrp-mid    { color: var(--yellow); }
.rsrp-weak   { color: var(--red); }
.rsrp-none   { color: var(--dim); font-size: 14px; }
.spark { width: 132px; height: 38px;
         background: rgba(93,213,255,0.02);
         border: 1px solid var(--line); }
.spark .fill { fill: var(--ue-color, var(--accent)); fill-opacity: 0.07; }
.spark .line { stroke: var(--ue-color, var(--accent)); stroke-width: 1.2;
               fill: none; stroke-linejoin: miter; stroke-linecap: square; }
.spark .grid { stroke: var(--line-2); stroke-width: 0.5; fill: none;
               stroke-dasharray: 2 3; }

.position {
  margin-top: 9px;
  background: rgba(8, 12, 17, 0.55);
  border: 1px solid var(--line-2);
  padding: 7px 10px;
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 11.5px; color: var(--txt);
  display: flex; flex-direction: column; gap: 3px;
  position: relative;
}
.position::before {
  content: 'EST · LAT/LON'; position: absolute;
  top: -7px; left: 8px; padding: 0 5px;
  background: var(--panel-2);
  color: var(--accent); font-size: 8.5px;
  letter-spacing: 0.18em; text-transform: uppercase;
}
.position .row1 { display: flex; align-items: baseline; gap: 8px; margin-top: 2px; }
.position .latlon { font-weight: 600; letter-spacing: 0.02em; }
.position .cep { color: var(--dim); font-size: 10.5px; }
.position .meta-row { color: var(--dim); font-size: 10px;
                      letter-spacing: 0.06em; text-transform: uppercase; }
.position.no-pos { color: var(--dim-2);
                   font-size: 10.5px; border-style: dashed;
                   background: transparent; }
.position.no-pos::before { content: 'NO FIX'; color: var(--dim); }
.position.no-pos .latlon { color: var(--dim); font-weight: normal; }

.foot {
  margin-top: 9px;
  display: flex; flex-wrap: wrap; gap: 4px;
  font-size: 10px; color: var(--dim);
  font-family: ui-monospace, "JetBrains Mono", monospace;
}
.chip {
  display: inline-block; padding: 1px 6px;
  font-size: 10px; letter-spacing: 0.08em;
  background: transparent;
  border: 1px solid var(--line-2);
  color: var(--dim);
}
.chip.ul  { color: var(--accent); border-color: var(--accent-dim); }
.chip.dl  { color: #aab3bd; border-color: var(--line-3); }
.chip.dci { color: var(--yellow); border-color: #4a3a17; }
.chip.plain { color: var(--dim); }

/* ---------------------------------------------------------- scrubber */
.scrubber {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 12px;
  padding: 8px 18px;
  border-top: 1px solid var(--line-2);
  background: var(--panel);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 10.5px;
  color: var(--dim);
}
.scrubber.replay {
  background: rgba(230,168,81,0.04);
  border-top-color: rgba(230,168,81,0.4);
}
.scrubber .live-btn {
  font-family: inherit; font-size: 10px;
  padding: 4px 11px;
  border: 1px solid var(--accent-dim);
  background: transparent; color: var(--accent); cursor: pointer;
  letter-spacing: 0.18em; text-transform: uppercase;
  transition: background 0.12s, color 0.12s, border-color 0.12s;
  display: inline-flex; align-items: center; gap: 7px;
}
.scrubber.replay .live-btn { color: var(--yellow); border-color: rgba(230,168,81,0.4); }
.scrubber .live-btn:hover { background: rgba(93,213,255,0.06); }
.scrubber.replay .live-btn:hover { background: rgba(230,168,81,0.06); }
.scrubber .live-btn .led {
  width: 6px; height: 6px; background: var(--accent);
  box-shadow: 0 0 5px var(--accent);
  animation: pulse 1.6s ease-in-out infinite;
}
.scrubber.replay .live-btn .led { background: var(--yellow); box-shadow: 0 0 4px var(--yellow);
                                  animation: none; }
.scrubber input[type=range] {
  flex: 1 1 auto; appearance: none; height: 2px;
  background: var(--line-2);
  cursor: pointer; outline: none;
}
.scrubber input[type=range]::-webkit-slider-thumb {
  appearance: none; width: 10px; height: 14px;
  background: var(--accent); border: none; cursor: grab;
}
.scrubber.replay input[type=range]::-webkit-slider-thumb { background: var(--yellow); }
.scrubber input[type=range]:active::-webkit-slider-thumb { cursor: grabbing; }
.scrubber .lbl { text-transform: uppercase; letter-spacing: 0.18em;
                 font-size: 9.5px; color: var(--dim-2); }
.scrubber .time { color: var(--txt); font-variant-numeric: tabular-nums;
                  min-width: 78px; letter-spacing: 0.04em; }

/* ---------------------------------------------------------- log */
footer {
  flex: 0 0 auto;
  padding: 6px 18px 8px;
  border-top: 1px solid var(--line-2);
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 10.5px;
  color: var(--dim);
  max-height: 120px; overflow-y: auto;
  background: var(--panel);
  position: relative;
}
footer::before {
  content: 'TELEMETRY'; position: absolute;
  top: -7px; left: 14px; padding: 0 6px;
  background: var(--bg); color: var(--accent);
  font-size: 9px; letter-spacing: 0.22em; font-weight: 600;
}
footer::-webkit-scrollbar { width: 6px; }
footer::-webkit-scrollbar-track { background: transparent; }
footer::-webkit-scrollbar-thumb { background: var(--line-2); }
footer .row { padding: 1px 0; letter-spacing: 0.02em; }
footer .row .ts { color: var(--dim-2); margin-right: 10px; }
footer .row b { color: var(--accent); font-weight: 600; }
</style>
</head><body>
<header>
  <div class="brand">
    <span class="led"></span>
    <h1>CELLULAR-DRONES <span>// UE INTERCEPT</span></h1>
  </div>
  <div class="meta">
    <div class="stat">CONTACTS <strong id="n-ues">0</strong></div>
    <div class="stat">LOC <strong id="n-pos">0</strong></div>
    <div class="stat">GRANTS <strong id="n-ue-sightings">0</strong></div>
    <div class="stat">T+ <strong id="uptime">0s</strong></div>
    <span class="pill gps-unknown" id="gps"><span class="led"></span><span id="gps-text">NAV —</span></span>
    <span class="pill status-idle" id="status"><span class="led"></span><span id="status-text">IDLE</span></span>
  </div>
</header>
<div class="banner">
  <strong>C-RNTI IS CONNECTION-SCOPED.</strong>
  <span class="dim">A handset that re-attaches will be reissued a new C-RNTI · positions use UL grants only · DL grants are eNB-side.</span>
</div>
<div class="workspace">
  <div class="map-wrap">
    <span class="br-bl"></span><span class="br-br"></span>
    <div id="map"></div>
    <div class="map-overlay" id="map-overlay"><strong>NAV</strong> · waiting for fix</div>
    <div class="legend" id="legend" style="display:none">
      <div class="label">// TRACKED</div>
      <div id="legend-rows"></div>
    </div>
  </div>
  <aside class="side">
    <span class="br-bl"></span><span class="br-br"></span>
    <div class="side-head">
      <span class="panel-tab"><span class="dot"></span><strong>TARGETS</strong></span>
      <span id="side-sum">—</span>
    </div>
    <div class="cards" id="cards">
      <div class="empty" id="empty">
        // STANDBY · awaiting first PDCCH decode
        <div class="hint">Contacts populate as UL grants and GPS fixes accumulate.<br>
        Each row: C-RNTI · cell · live signal trend · position estimate.</div>
      </div>
    </div>
  </aside>
</div>
<div class="scrubber" id="scrubber">
  <button class="live-btn" id="live-btn" type="button">
    <span class="led"></span><span id="live-btn-text">LIVE</span>
  </button>
  <span class="lbl">REPLAY</span>
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
// Palette: cool-leaning, single-cyan-accent vibe (no rainbow).
const PALETTE = [
  '#5dd5ff', '#e6a851', '#58c98a', '#ff8a6d', '#a89dff',
  '#7fb9d9', '#d9a86d', '#74d4be', '#d97fa8', '#9bb0c5',
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
  droneTrailLine = L.polyline(droneTrail, { color:'#5dd5ff', weight:1.5, opacity:0.55, dashArray:'4 4' }).addTo(map);
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
      radius: 5, color:'#5dd5ff', weight:2, fillColor:'#04070a', fillOpacity:1,
    }).addTo(map).bindTooltip('SELF', {permanent:false, direction:'top', className:'ue-tip'});
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
  const pTa = u.est_position_ta;
  const color = colorFor(u.key);
  let layer = ueLayers.get(u.key);
  if (!p && !pTa) {
    // No estimate of either kind — drop any stale layer.
    if (layer) {
      if (layer.marker) map.removeLayer(layer.marker);
      if (layer.accuracy) map.removeLayer(layer.accuracy);
      if (layer.markerTa) map.removeLayer(layer.markerTa);
      if (layer.accuracyTa) map.removeLayer(layer.accuracyTa);
      ueLayers.delete(u.key);
    }
    return;
  }
  if (!layer) {
    layer = {};
    ueLayers.set(u.key, layer);
  }
  const selected = (u.key === selectedKey);

  // --- Centroid estimate (filled marker) ---------------------------------
  if (p) {
    const markerOpts = {
      radius: selected ? 10 : 7,
      color: color, weight: selected ? 3 : 2,
      fillColor: color, fillOpacity: selected ? 0.75 : 0.55,
    };
    if (!layer.marker) {
      layer.marker = L.circleMarker([p.lat, p.lon], markerOpts).addTo(map);
      layer.marker.bindTooltip(u.c_rnti_hex,
        {permanent: true, direction: 'right', offset: [10, 0], className: 'ue-tip'});
      layer.marker.on('click', () => selectUe(u.key, true));
      layer.accuracy = L.circle([p.lat, p.lon], {
        radius: Math.max(2, p.cep95_m || 5),
        color: color, weight: 1, opacity: 0.5,
        fillColor: color, fillOpacity: 0.06,
        dashArray: '4 3',
      }).addTo(map);
    } else {
      layer.marker.setLatLng([p.lat, p.lon]);
      layer.marker.setStyle(markerOpts);
      layer.accuracy.setLatLng([p.lat, p.lon]);
      layer.accuracy.setRadius(Math.max(2, p.cep95_m || 5));
      layer.accuracy.setStyle({color: color, fillColor: color,
                               opacity: selected ? 0.85 : 0.5,
                               fillOpacity: selected ? 0.12 : 0.06});
    }
  } else if (layer.marker) {
    map.removeLayer(layer.marker); delete layer.marker;
    map.removeLayer(layer.accuracy); delete layer.accuracy;
  }

  // --- TA estimate (hollow dashed marker) --------------------------------
  // Rendered alongside, not in place of, the centroid. The two estimators
  // are independent; the dashboard does not pick a winner.
  if (pTa) {
    const taOpts = {
      radius: selected ? 9 : 6,
      color: color, weight: selected ? 3 : 2,
      fillOpacity: 0,
      dashArray: '3 3',
    };
    if (!layer.markerTa) {
      layer.markerTa = L.circleMarker([pTa.lat, pTa.lon], taOpts).addTo(map);
      // Anchor the C-RNTI tooltip to the centroid marker when both are
      // present; only label the TA marker if it's standing alone.
      if (!layer.marker) {
        layer.markerTa.bindTooltip(u.c_rnti_hex + ' · TA',
          {permanent: true, direction: 'right', offset: [10, 0], className: 'ue-tip'});
        layer.markerTa.on('click', () => selectUe(u.key, true));
      }
      layer.accuracyTa = L.circle([pTa.lat, pTa.lon], {
        radius: Math.max(2, pTa.cep95_m || 5),
        color: color, weight: 1, opacity: 0.4,
        fill: false,
        dashArray: '2 4',
      }).addTo(map);
    } else {
      layer.markerTa.setLatLng([pTa.lat, pTa.lon]);
      layer.markerTa.setStyle(taOpts);
      layer.accuracyTa.setLatLng([pTa.lat, pTa.lon]);
      layer.accuracyTa.setRadius(Math.max(2, pTa.cep95_m || 5));
    }
  } else if (layer.markerTa) {
    map.removeLayer(layer.markerTa); delete layer.markerTa;
    map.removeLayer(layer.accuracyTa); delete layer.accuracyTa;
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
  const positioned = [...ues.values()]
    .filter(u => u.est_position || u.est_position_ta).length;
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
    // 'has-pos' wins when both estimates are present; falls back to
    // 'has-pos-ta' for TA-only UEs so they still register as positioned.
    const posCls = u.est_position ? 'has-pos'
                 : u.est_position_ta ? 'has-pos-ta'
                 : 'no-pos';
    const cls = ['row', posCls,
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
  const pTa = u.est_position_ta;
  function row(label, est) {
    const cep = est.cep95_m != null ? `±${est.cep95_m.toFixed(1)} m` : '';
    const alt = est.alt_m != null ? `, ${est.alt_m.toFixed(0)} m AGL` : '';
    return `<div class="row1">
              <span class="latlon">${label}${est.lat.toFixed(6)}, ${est.lon.toFixed(6)}</span>
              <span class="cep">${cep}</span>
            </div>
            <div class="meta-row">${est.method} · ${est.n_samples} samples${alt}</div>`;
  }
  if (!p && !pTa) {
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
  let body = '';
  // Two independent estimators, each labelled; not a primary/fallback.
  if (p) body += row(p && pTa ? 'RSSI · ' : '', p);
  if (pTa) body += row('TA · ', pTa);
  return `<div class="position">${body}</div>`;
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
    <div class="foot">${ulChip}${dlChip}${dciTags}${mcs}${prb}${tbs}</div>
  `;
  if (!isNew) {
    card.classList.remove('fresh');
    void card.offsetWidth;
  }
  card.classList.add('fresh');
}
function sortCards() {
  const sorted = [...ues.values()].sort((a, b) => {
    const ap = (a.est_position || a.est_position_ta) ? 0 : 1;
    const bp = (b.est_position || b.est_position_ta) ? 0 : 1;
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
  ues.forEach(u => { if (u.est_position || u.est_position_ta) positioned += 1; });
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
    ueLayers.forEach(layer => {
      if (!map) return;
      ['marker', 'accuracy', 'markerTa', 'accuracyTa'].forEach(k => {
        if (layer[k]) map.removeLayer(layer[k]);
      });
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
    const est = u.est_position || u.est_position_ta;
    const tag = u.est_position ? 'rssi' : (u.est_position_ta ? 'ta' : '');
    const pos = est
      ? ` · ${tag} ${est.lat.toFixed(5)},${est.lon.toFixed(5)} ±${est.cep95_m.toFixed(0)}m`
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
    p.add_argument("--droneid-cmd", action="append", default=None,
                   help="argv (space-split) for a DJI DroneID decoder that "
                        "prints one JSON object per decoded frame. Repeat "
                        "the flag for multiple radios (e.g. one HackRF per "
                        "band). Alternative GPS source — coexists with gpsd.")
    p.add_argument("--droneid-serial", default=None,
                   help="restrict DroneID frames to those whose serial "
                        "(substring-match) equals this value. Use when "
                        "several drones may be airborne and you only want "
                        "yours feeding the geotag stream.")
    args = p.parse_args()

    # `--simulate` is off by default: the dashboard refuses to start with
    # no producer configured rather than silently sitting empty (or worse,
    # quietly entering simulation when real radio was expected).
    if not args.simulate and not args.ltesniffer_cmd:
        print("sniffer.live needs --simulate or --ltesniffer-cmd. "
              "Use the `sniffer live` CLI wrapper, which validates this.",
              file=sys.stderr)
        return 2

    state = State()
    stop = threading.Event()
    threads: list[threading.Thread] = []

    if args.simulate:
        threads.append(threading.Thread(
            target=run_simulator,
            kwargs=dict(state=state, mission_id=args.mission_id,
                        out_dir=args.out_dir, stop=stop),
            daemon=True,
        ))
    else:
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
        for droneid_cmd_str in (args.droneid_cmd or []):
            cmd = droneid_cmd_str.split()
            threads.append(threading.Thread(
                target=run_droneid_loop,
                kwargs=dict(
                    state=state, droneid_cmd=cmd,
                    mission_id=args.mission_id, stop=stop,
                    serial_filter=args.droneid_serial,
                ),
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
        print("mode: simulate (synthetic UEs + GPS, via the standard "
              "parse_ltesniffer + parse_gpsd pipeline)", flush=True)
    else:
        print(f"mode: LTESniffer · cmd={args.ltesniffer_cmd!r} "
              f"@ {args.center_hz/1e6:.2f} MHz gain {args.rx_gain_db} dB",
              flush=True)
        print("GPS: gpspipe -w (only ingested if gpsd is running)", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
