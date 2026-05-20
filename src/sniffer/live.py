"""Realtime browser dashboard for C-RNTI sniffing + per-UE positioning.

FalconEye feeds DCI events (real radio) or the simulator does (no
hardware). gpsd feeds positions. The aggregator joins them in
monotonic time, runs the per-UE localizer, and pushes per-UE state to
connected browsers over Server-Sent Events. Stdlib HTTP only.

Run:
    python -m sniffer.live --simulate                    # no radio needed
    python -m sniffer.live --falcon-cmd "FalconEye -f 2650000000" \
                           --falcon-pci 275              # real radio

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
from sniffer.falcon import parse_stream as parse_falcon_stream, tail_csv
from sniffer.parse_gpsd import parse_stream as parse_gpsd_stream
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
    falcon_lines,
    gpsd_lines,
)
from sniffer.spectrum import SpectrumScanner


# Knobs --------------------------------------------------------------------
GPS_BUFFER_MAX = 4000          # ~10 min at 10 Hz
GEOTAG_MAX_AGE_MS = 500        # drop sightings >500 ms from nearest fix
UE_HISTORY_MAX = 400           # per-UE UL-grant history retained for positioning
RSSI_HISTORY_MAX = 120         # rolling sparkline points
GPS_TRAIL_MAX = 600            # drone trail points sent in /state snapshot

# How many UE-sightings to observe before concluding "this upstream isn't
# emitting UE transmit energy at all". FalconEye on the real radio sees
# every C-RNTI on PDCCH but never measures `ul_rssi_dbm` — only an actual
# UL listener (2× USRP or X310) tuned to the UL band can. After this many
# grants with zero UL-energy hits, we flip a flag so the dashboard can
# stop promising positioning that will never arrive. 50 is a conservative
# threshold — at ~10 grants/sec on a busy cell that's ~5 seconds of
# evidence. The simulator emits synthetic ul_rssi_dbm in extension
# columns, so it never trips this.
DL_ONLY_DETECTION_THRESHOLD = 50

# Rolling-window length (seconds) for the cell PRB-utilization metric.
# 5 s is long enough to smooth subframe-to-subframe burstiness but short
# enough to track real load changes (handovers, congestion onset).
CELL_LOAD_WINDOW_S = 5.0

# How many MCS samples to keep per UE for the channel-quality sparkline.
# 60 grants on a busy UE = ~6 s of recent history, enough to spot a
# UE losing signal mid-flight.
MCS_HISTORY_MAX = 60


# --------------------------------------------------------------------------
# Aggregator + broadcaster
# --------------------------------------------------------------------------


class State:
    """Per-UE rolling state plus a fan-out queue list for SSE clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key: (pci, c_rnti) — real UEs only (rnti_kind == "c_rnti").
        # P-RNTI / SI-RNTI / RA-RNTI decodes are cell-wide broadcasts; they
        # land in _broadcasts so the UE list and counts reflect actual handsets.
        self._ues: dict[tuple[int, int], dict[str, Any]] = {}
        # key: (pci, rnti_kind) → counters for p_rnti / si_rnti / ra_rnti / unknown.
        self._broadcasts: dict[tuple[int, str], dict[str, Any]] = {}
        # Per-cell PRB occupancy window. key=pci → deque[(ts_ns, n_prb)],
        # plus the running max(n_prb) seen for that cell — used as the
        # estimated total PRB capacity (cells of 6/15/25/50/75/100 PRB
        # all show up as max-observed once a UE gets a full-width grant).
        self._cell_prb_windows: dict[int, deque] = {}
        self._cell_prb_max: dict[int, int] = {}
        self._clients: list[queue.Queue[str]] = []
        self._started_mono_ns = mono_ns()
        self._total_ue_sightings = 0
        # DL-only mode auto-detection. Stays None until we've seen enough
        # grants to make a call; then True (HackRF-style DL-only, no UE
        # transmit energy observable) or False (UL energy is arriving, so
        # the localizers can do their thing).
        self._dl_only: Optional[bool] = None
        self._grants_with_ul_rssi: int = 0
        self._spectrum: Optional[SpectrumScanner] = None
        # Survey progress (sweep + dwell across multiple cells). None
        # outside of survey mode; a dict per cell visit otherwise.
        self._survey_status: Optional[dict[str, Any]] = None
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
        now_iso = sighting.ts_utc or utc_iso()
        ue = sighting.ue
        kind = ue.rnti_kind or "c_rnti"

        # Cell-wide broadcasts (paging, SI, RACH responses) inflate the UE
        # count if treated as UEs. Route them to a separate counter bucket.
        if kind != "c_rnti":
            with self._lock:
                bkey = (pci, kind)
                bucket = self._broadcasts.setdefault(bkey, {
                    "pci": pci,
                    "kind": kind,
                    "count": 0,
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                })
                bucket["count"] += 1
                bucket["last_seen"] = now_iso
                self._total_ue_sightings += 1
                broadcasts = self._broadcasts_payload_locked()
            self._broadcast({"type": "broadcast",
                             "broadcasts": broadcasts})
            return {"pci": pci, "kind": kind, "rnti": c_rnti}

        key = (pci, c_rnti)
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
                    "first_seen_ns": sighting.ts_mono_ns,
                    "last_seen_ns": sighting.ts_mono_ns,
                    "count": 0,
                    "ul_count": 0,
                    "dl_count": 0,
                    "dci_formats": [],
                    "ul_rssi_history": deque(maxlen=RSSI_HISTORY_MAX),
                    "geo_history": deque(maxlen=UE_HISTORY_MAX),
                    "mcs_history": deque(maxlen=MCS_HISTORY_MAX),
                    "est_position": None,
                    # HARQ-aware throughput: track the last NDI per
                    # (direction, harq_id) so we count tbs_bytes only on
                    # grants that carry NEW data (NDI flipped), not on
                    # HARQ retransmissions (NDI unchanged).
                    "ndi_state": {},
                    "new_data_bytes_dl": 0,
                    "new_data_bytes_ul": 0,
                    # FalconEye decode confidence (histval). max = best
                    # decode we've ever gotten for this RNTI; recent =
                    # the latest. The UI uses max as the "trust" badge
                    # since FalconEye's RNTI histogram is cumulative.
                    "confidence_max": 0,
                    "confidence_recent": 0,
                }
                self._ues[key] = entry

            entry["last_seen"] = now_iso
            entry["last_seen_ns"] = sighting.ts_mono_ns
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
            # Channel-quality sparkline: per-UE rolling MCS history.
            # An MCS drop over time is the cleanest signal that a UE has
            # moved away or hit a fade; rendered as a small inline chart
            # next to the RSSI sparkline.
            if ue.mcs is not None:
                entry["mcs_history"].append(int(ue.mcs))
            # Cell-load metric: append this grant's PRB count to the
            # per-cell rolling window. We compute "% of cell PRBs in use,
            # last 5s" on demand in snapshot() — instant cell-busy gauge.
            if ue.n_prb is not None and ue.n_prb > 0:
                win = self._cell_prb_windows.setdefault(pci, deque())
                win.append((sighting.ts_mono_ns, int(ue.n_prb)))
                # Track the cell's apparent PRB capacity. Cells advertise
                # 6/15/25/50/75/100 PRB widths; the largest single grant
                # we ever see ≈ the cell's full width.
                cur_max = self._cell_prb_max.get(pci, 0)
                if int(ue.n_prb) > cur_max:
                    self._cell_prb_max[pci] = int(ue.n_prb)
            if ue.confidence is not None:
                entry["confidence_recent"] = int(ue.confidence)
                if int(ue.confidence) > entry["confidence_max"]:
                    entry["confidence_max"] = int(ue.confidence)
            # HARQ-aware new-data accounting. Only when we have all three
            # pieces (HARQ id, NDI, tbs_bytes). On first sight of a HARQ
            # process, treat the grant as new data (initial NDI doesn't
            # have a prior to compare to — the cautious alternative would
            # be to wait for the first flip, but most cells start a UE on
            # a new transport block).
            if (ue.harq_id is not None and ue.ndi is not None
                    and ue.tbs_bytes is not None and ue.tbs_bytes > 0):
                hkey = (ue.direction, int(ue.harq_id))
                prev_ndi = entry["ndi_state"].get(hkey)
                is_new_data = (prev_ndi is None or prev_ndi != int(ue.ndi))
                entry["ndi_state"][hkey] = int(ue.ndi)
                if is_new_data:
                    if ue.direction == "ul":
                        entry["new_data_bytes_ul"] += int(ue.tbs_bytes)
                    elif ue.direction == "dl":
                        entry["new_data_bytes_dl"] += int(ue.tbs_bytes)
            if ue.ul_rssi_dbm is not None:
                entry["ul_rssi_dbm"] = ue.ul_rssi_dbm
                entry["ul_rssi_history"].append(
                    [now_iso, round(ue.ul_rssi_dbm, 2)]
                )
            if ue.dl_rsrp_dbm is not None:
                entry["dl_rsrp_dbm"] = ue.dl_rsrp_dbm
            self._total_ue_sightings += 1
            if ue.ul_rssi_dbm is not None:
                self._grants_with_ul_rssi += 1
            # Auto-flip DL-only mode once we have enough evidence either way.
            # Stays None until we cross the threshold so the dashboard can
            # show "still measuring" instead of either claim prematurely.
            if self._dl_only is None:
                if self._grants_with_ul_rssi > 0:
                    self._dl_only = False
                elif self._total_ue_sightings >= DL_ONLY_DETECTION_THRESHOLD:
                    self._dl_only = True

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

            payload = _entry_to_dict_ue(entry)
            dl_only = self._dl_only
        self._broadcast({"type": "ue_sighting", "ue": payload,
                         "dl_only": dl_only})
        return payload

    def _broadcasts_payload_locked(self) -> list[dict[str, Any]]:
        """Render the broadcast counters for SSE / snapshot. Caller holds lock."""
        return [
            {"pci": b["pci"], "kind": b["kind"], "count": b["count"],
             "first_seen": b["first_seen"], "last_seen": b["last_seen"]}
            for b in sorted(self._broadcasts.values(),
                            key=lambda x: (x["pci"], x["kind"]))
        ]

    def _cell_load_payload_locked(self) -> list[dict[str, Any]]:
        """Compute "% of cell PRBs in use, rolling 5s" per PCI.

        Caller holds lock. Prunes the per-cell PRB window in place.
        We compare PRB·subframes used to PRB·subframes available
        (cell_max_prb × window_s × 1000 subframes/s).
        """
        out: list[dict[str, Any]] = []
        cutoff_ns = mono_ns() - int(CELL_LOAD_WINDOW_S * 1e9)
        for pci, win in self._cell_prb_windows.items():
            # Drop samples older than the window.
            while win and win[0][0] < cutoff_ns:
                win.popleft()
            if not win:
                continue
            cell_prb = self._cell_prb_max.get(pci, 0)
            if cell_prb <= 0:
                continue
            used_prb_subframes = sum(n for _, n in win)
            avail_prb_subframes = cell_prb * CELL_LOAD_WINDOW_S * 1000.0
            load_pct = round(
                100.0 * used_prb_subframes / avail_prb_subframes, 1)
            # Clamp — heavy grant overlap or short windows can over-count.
            if load_pct > 100.0:
                load_pct = 100.0
            out.append({
                "pci": pci,
                "load_pct": load_pct,
                "cell_prb": cell_prb,
                "grants_in_window": len(win),
                "window_s": CELL_LOAD_WINDOW_S,
            })
        return out

    def set_status(self, phase: str, **extra: Any) -> None:
        with self._lock:
            self._scan_status = {"phase": phase, "ts_utc": utc_iso(), **extra}
            status = dict(self._scan_status)
        self._broadcast({"type": "status", "status": status})

    def set_survey_status(self, survey: Optional[dict[str, Any]]) -> None:
        """Replace the survey-progress payload. Pass None to clear it
        (when survey exits or never started)."""
        with self._lock:
            self._survey_status = (dict(survey) if survey is not None
                                   else None)
            snap = (dict(self._survey_status)
                    if self._survey_status is not None else None)
        self._broadcast({"type": "survey", "survey": snap})

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
            spectrum = (self._spectrum.snapshot()
                        if self._spectrum is not None else None)
            return {
                "type": "snapshot",
                "ues": ues,
                # P-RNTI / SI-RNTI / RA-RNTI counters per (pci, kind).
                # The UI renders these in a dedicated "Broadcast" pill
                # so they don't get confused with real UE rows.
                "broadcasts": self._broadcasts_payload_locked(),
                # Per-cell PRB occupancy %, rolling 5s. Tells you if the
                # cell is busy (>50%) vs idle — useful context when 0
                # DCIs flow ("decoder broken" vs "cell idle"). Renders
                # as a header pill.
                "cell_load": self._cell_load_payload_locked(),
                "status": dict(self._scan_status),
                "latest_gps": self._latest_gps,
                "gps_trail": gps_trail,
                "total_ue_sightings": self._total_ue_sightings,
                "uptime_s": (mono_ns() - self._started_mono_ns) / 1e9,
                "time_bounds": {"t_min_ns": t_min, "t_max_ns": t_max,
                                "started_ns": self._started_mono_ns},
                "spectrum": spectrum,
                # None until we've seen enough grants to know.
                # True = DL-only upstream (FalconEye on real radio).
                # False = UL energy is arriving (simulator, or future
                # UL-sniffing 2×USRP setup), positioning estimators
                # can do their job.
                "dl_only": self._dl_only,
                # Survey orchestrator progress (None outside survey mode).
                "survey": (dict(self._survey_status)
                           if self._survey_status is not None else None),
            }

    def attach_spectrum_scanner(self, scanner: SpectrumScanner) -> None:
        self._spectrum = scanner

    def spectrum_snapshot(self) -> Optional[dict[str, Any]]:
        return self._spectrum.snapshot() if self._spectrum is not None else None

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
                if cent is not None:
                    est_pos = {
                        "lat": cent.lat, "lon": cent.lon,
                        "alt_m": cent.alt_m, "cep95_m": cent.cep95_m,
                        "method": "weighted_centroid",
                        "n_samples": cent.n_samples,
                        "altitude_estimated": cent.altitude_estimated,
                    }
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
    # Skip internal-only fields: rolling histories (sent as derived
    # summaries below) and ndi_state (per-HARQ scratchpad, of no use
    # to the dashboard).
    out = {k: v for k, v in entry.items()
           if k not in ("ul_rssi_history", "geo_history", "ndi_state",
                        "mcs_history")}
    out["ul_rssi_history"] = list(entry["ul_rssi_history"])
    out["n_geo_samples"] = len(entry["geo_history"])
    # Channel-quality sparkline data. Bare int list — the dashboard
    # renders it as a tiny MCS-over-time chart in the UE card.
    out["mcs_history"] = list(entry["mcs_history"])
    trail = list(entry["geo_history"])[-40:]
    out["trail"] = [{"lat": g["gps"]["lat"], "lon": g["gps"]["lon"],
                     "ul_rssi_dbm": g["ue"]["ul_rssi_dbm"]} for g in trail]
    # HARQ-aware throughput: count only new-data DCIs (NDI flipped),
    # divide by the UE's observation window. Pre-MIB grants and
    # retransmissions are excluded by design so the number reflects
    # actual payload throughput, not raw PDCCH activity.
    first_ns = entry.get("first_seen_ns")
    last_ns = entry.get("last_seen_ns")
    if first_ns and last_ns and last_ns > first_ns:
        elapsed_s = (last_ns - first_ns) / 1e9
        # bytes → kbps; tiny windows can spike to silly numbers, so we
        # require ≥1 s of observation before reporting anything.
        if elapsed_s >= 1.0:
            out["throughput_dl_kbps"] = round(
                entry["new_data_bytes_dl"] * 8 / 1000 / elapsed_s, 1)
            out["throughput_ul_kbps"] = round(
                entry["new_data_bytes_ul"] * 8 / 1000 / elapsed_s, 1)
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


# --------------------------------------------------------------------------
# Sinks: bridge parse_stream's JSONL output into the State aggregator
# --------------------------------------------------------------------------


class _UeSightingSink(io.TextIOBase):
    """falcon.parse_stream writes JSONL strings here; we decode + push to State."""

    def __init__(self, state: State, jsonl_out: Optional[io.TextIOBase] = None,
                 on_lock: Optional[Callable[[], None]] = None):
        super().__init__()
        self._state = state
        self._jsonl_out = jsonl_out
        self._buf = ""
        self._on_lock = on_lock
        self._lock_fired = False

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
        if self._on_lock is not None and not self._lock_fired:
            self._lock_fired = True
            try:
                self._on_lock()
            except Exception:  # noqa: BLE001
                pass
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
                 sample_rate_sps: Optional[float] = None,
                 earfcn: Optional[int] = None):
        self.mission_id = mission_id
        self.backend = backend
        self.device = device
        self.rx_gain_db = rx_gain_db
        self.center_hz = center_hz
        self.sample_rate_sps = sample_rate_sps
        self.earfcn = earfcn


def _parse_falcon_argv(cmd: list[str]) -> dict[str, Any]:
    """Pull useful cell metadata out of the FalconEye argv so the dashboard
    can display gain/antennas/sample-rate without piping them separately."""
    out: dict[str, Any] = {}
    i = 0
    while i < len(cmd):
        flag = cmd[i]
        if flag in ("-g", "-A", "-f") and i + 1 < len(cmd):
            val = cmd[i + 1]
            try:
                if flag == "-g":
                    out["gain_db"] = float(val)
                elif flag == "-A":
                    out["antennas"] = int(val)
                elif flag == "-f":
                    out["falcon_center_hz"] = float(val)
            except ValueError:
                pass
            i += 2
            continue
        i += 1
    return out


def run_falcon_loop(state: State, *, falcon_cmd: list[str], pci: int,
                    mission_id: str, out_dir: str, center_hz: Optional[float],
                    stop: threading.Event,
                    spectrum_tap_path: Optional[str] = None) -> None:
    """Spawn FalconEye, tail its DCI CSV, feed records into State.

    FalconEye (falkenber9/falcon, the LTESniffer ancestor) writes a
    tab-separated per-DCI tracefile via `-D <path>`. Unlike LTESniffer
    — which writes PCAP only — FALCON's CSV is designed for tailing,
    which is the entire reason we wire it here.

    We allocate a fresh temp dir per spawn (so file-rotation logic in
    the tailer is rarely exercised, but works), append `-D <tmp>/dci.csv`
    to the user-supplied argv, spawn the binary, and parse rows in a
    parallel thread. FalconEye's own stdout/stderr is forwarded to ours
    so build / cell-lock errors are visible.

    PCI is mandatory because FALCON locks to a single cell and never
    writes the PCI in its rows — the caller knows it because they
    passed `-f <hz>` for that exact cell.
    """
    import tempfile

    if not falcon_cmd:
        state.set_status("error", message="no --falcon-cmd provided")
        return
    if (shutil.which(falcon_cmd[0]) is None
            and not os.path.exists(falcon_cmd[0])):
        state.set_status(
            "error",
            message=(f"`{falcon_cmd[0]}` not found. Build FALCON "
                     f"(see scripts/install-linux.sh) or set FALCON_BIN."),
        )
        return

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")
    # FALCON's stdout+stderr → a log file so we can diagnose lock failures.
    # FalconEye writes "Searching for cell...", "Found Cell_id", "Entering
    # main loop" and decode errors to stderr; without this we run blind.
    falcon_log_path = os.path.join(out_dir, f"falcon-{mission_id}.log")
    # Extract real gain from the FalconEye argv (-g flag) and derive EARFCN
    # from the center frequency so RadioConfig records carry accurate metadata.
    _cmd_meta = _parse_falcon_argv(falcon_cmd)
    _rx_gain = _cmd_meta.get("gain_db", 50.0)
    _earfcn: Optional[int] = None
    if center_hz is not None:
        try:
            from sniffer.lte_bands import hz_to_earfcn_dl
            _earfcn = hz_to_earfcn_dl(center_hz)
        except (ValueError, ImportError):
            pass
    parse_args = _ParseArgs(mission_id, "falcon", "usrp-falcon-0",
                            rx_gain_db=_rx_gain, center_hz=center_hz,
                            sample_rate_sps=23.04e6, earfcn=_earfcn)

    # Write a "live-lock" record to the known-cells store on the first
    # successfully decoded DCI — proof that FalconEye actually locked.
    _lock_logged = threading.Event()

    def _on_lock() -> None:
        if _lock_logged.is_set():
            return
        _lock_logged.set()
        try:
            from sniffer.cells import KnownCell, append_cell
            import time as _t
            append_cell(KnownCell(
                source="live-lock",
                ts_utc=_t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime()),
                earfcn=_earfcn, center_hz=center_hz, pci=pci,
            ))
        except Exception:  # noqa: BLE001
            pass

    # Stable path for FALCON's per-DCI CSV — survives FALCON respawns so
    # the tail-and-parse loop can replay anything FalconEye managed to
    # write before it died. The old TemporaryDirectory approach destroyed
    # the file every respawn cycle, so any unparsed rows were lost.
    persistent_csv_path = os.path.join(out_dir, f"dci-{mission_id}.csv")

    # Two-phase stall threshold. Cell-search (PSS sweep + SSS + PBCH + RF
    # rate switch) on Pi-class hardware can chew 2-3 min before the FIRST
    # DCI lands. After the first DCI we're "locked" — if the file then
    # goes quiet, the cell is lost and we should respawn.
    FALCON_INITIAL_GRACE_SECONDS = 360.0   # before any DCI ever (PBCH+rate
                                            # switch on Pi can be 4-5 min)
    FALCON_POST_LOCK_STALL_SECONDS = 45.0  # after we've seen DCIs

    while not stop.is_set():
        td = None  # tempdir no longer needed for the CSV path
        if True:  # keep indentation level same as the old `with` block
            csv_path = persistent_csv_path
            full_cmd = list(falcon_cmd) + ["-D", csv_path]
            # Cellular-drones SpectrumTap: ask FALCON to publish per-cell
            # FFT rows so the dashboard waterfall reflects the actual cell
            # being decoded (vs a separate USRP-wide sweep).
            if spectrum_tap_path:
                os.makedirs(os.path.dirname(spectrum_tap_path), exist_ok=True)
                full_cmd += ["-X", spectrum_tap_path]
            # Per-iteration stall flag: when set, breaks the parse loop so the
            # outer `while` can respawn FALCON. FALCON's PSS-search hang ("alive
            # but stuck") would otherwise wedge the dashboard forever.
            iter_stop = threading.Event()
            cell_extras = _parse_falcon_argv(full_cmd)
            state.set_status("sniffing", center_hz=center_hz,
                             decoder="falcon", out=out_path,
                             pci=pci, falcon_log=falcon_log_path,
                             **cell_extras)
            falcon_log_fh = open(falcon_log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(
                full_cmd,
                stdout=falcon_log_fh,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            # Stall watchdog. Two phases:
            #   * Initial grace (FALCON_INITIAL_GRACE_SECONDS): cell-search +
            #     PBCH + RF rate switch can chew 2-3 min on Pi before the
            #     first DCI lands. Don't restart during this — kill the run
            #     only if FALCON itself dies or grace runs out completely.
            #   * Post-lock (FALCON_POST_LOCK_STALL_SECONDS): once at least
            #     one DCI has been written, dci.csv stopping = cell lost.
            #     Short threshold here so the dashboard self-heals.
            def _watchdog() -> None:
                # "Alive" = EITHER dci.csv grows (decoded grants) OR the
                # spectrum tap file grows (FALCON in main loop + producing
                # FFTs). Only BOTH-silent = real hang. A quiet cell that
                # produces no PDCCH for a while is still healthy as long
                # as the SpectrumTap is firing.
                last_dci = -1
                last_spec = -1
                last_change = time.time()
                spawn_started = last_change
                ever_dci = False
                while not iter_stop.is_set() and not stop.is_set():
                    if proc.poll() is not None:
                        iter_stop.set()
                        return
                    try:
                        dci_sz = os.path.getsize(csv_path)
                    except OSError:
                        dci_sz = -1
                    spec_sz = -1
                    if spectrum_tap_path:
                        try:
                            spec_sz = os.path.getsize(spectrum_tap_path)
                        except OSError:
                            spec_sz = -1
                    now = time.time()
                    if dci_sz > last_dci:
                        last_dci = dci_sz
                        last_change = now
                        if dci_sz > 0:
                            ever_dci = True
                    if spec_sz > last_spec:
                        last_spec = spec_sz
                        last_change = now
                    threshold = (FALCON_POST_LOCK_STALL_SECONDS if ever_dci
                                 else FALCON_INITIAL_GRACE_SECONDS)
                    elapsed = now - last_change
                    if elapsed > threshold:
                        reason = ("post-DCI stall — cell may be lost" if ever_dci
                                  else "no DCI AND no spectrum-tap growth — "
                                       "FALCON wedged")
                        state.set_status(
                            "sniffing",
                            message=f"FALCON {reason} after {int(elapsed)}s — restarting",
                            pci=pci, center_hz=center_hz, decoder="falcon",
                        )
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                        iter_stop.set()
                        return
                    time.sleep(3.0)

            watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
            watchdog_thread.start()
            try:
                with open(out_path, "a", encoding="utf-8") as jsonl_fh:
                    sink = _UeSightingSink(state, jsonl_out=jsonl_fh,
                                          on_lock=_on_lock)
                    stream = tail_csv(csv_path, stop=iter_stop)
                    parse_falcon_stream(stream, parse_args, sink, pci=pci)
            except Exception as exc:  # noqa: BLE001
                state.set_status("error", message=f"falcon parse failed: {exc}")
            finally:
                iter_stop.set()
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                proc.wait()
                watchdog_thread.join(timeout=2.0)
                try:
                    falcon_log_fh.close()
                except Exception:  # noqa: BLE001
                    pass
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


_SIM_TICK_RE = re.compile(r"^#\s*TICK\s+t=([-\d.]+)")


def _paced_tick(line_iter: Iterator[str],
                stop: threading.Event) -> Iterator[str]:
    """Wall-clock-pace a `simulate.falcon_lines` stream.

    The simulator interleaves `# TICK t=X` markers (in simulated seconds)
    with content lines. We sleep until wall-clock matches each TICK, then
    pass the content lines through verbatim. Comment lines are swallowed
    here so the downstream parser only sees the same TSV that FalconEye
    itself would emit on the wire.
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

    Same code path as `run_falcon_loop` + `run_gpsd_loop`, with
    synthetic upstreams instead of FalconEye/gpspipe subprocesses:

        simulate.falcon_lines → falcon.parse_stream → _UeSightingSink → State
        simulate.gpsd_lines   → parse_gpsd.parse_stream → _GpsSink → State

    The wall-clock pacers below replay the simulated trajectory in real
    time so SSE clients see events arrive at the cadence they would in a
    real flight. The trajectory is looped indefinitely.

    The simulator emits FalconEye-shaped TSV with two extension columns
    (ul_rssi_dbm and ta_n_steps) past the standard 20 — the Falcon
    parser tolerates extras and reads them when present, so the
    simulator can still light up the positioning estimators that real
    DL-only FalconEye output cannot.
    """
    state.set_status("simulating", mission_id=mission_id)
    os.makedirs(out_dir, exist_ok=True)
    ue_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")

    cfg = _build_sim_config(mission_id)
    parse_args = _ParseArgs(mission_id, "sim", "sim-falcon",
                            rx_gain_db=0.0, center_hz=cfg.emitter.center_hz,
                            sample_rate_sps=23.04e6)

    def ue_worker() -> None:
        try:
            with open(ue_path, "a", encoding="utf-8") as fh:
                sink = _UeSightingSink(state, jsonl_out=fh)
                stream = _looped(
                    lambda: _paced_tick(falcon_lines(cfg), stop), stop)
                parse_falcon_stream(stream, parse_args, sink,
                                    pci=cfg.emitter.pci)
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


_INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><title>UE tracker · cellular drones</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- Leaflet: loaded async so a slow/blocked tile server doesn't hang page
     render. If the script never loads (offline Pi field deployment), we
     fall back to the "MAP UNAVAILABLE" placeholder after a 5 s grace. -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin="" defer></script>
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

/* Radio + Spectrum pills (cellular-drones state visibility). */
.pill.radio-active { color: var(--green); border-color: #1f4a32; background: rgba(88,201,138,0.04); }
.pill.radio-active .led { background: var(--green); box-shadow: 0 0 5px var(--green); }
.pill.radio-sim { color: var(--yellow); border-color: #4a3a17; background: rgba(230,168,81,0.04); }
.pill.radio-sim .led { background: var(--yellow); }
.pill.radio-error { color: var(--red); border-color: #4a1f1f; background: rgba(255,109,109,0.04); }
.pill.radio-error .led { background: var(--red); }
.pill.radio-unknown { color: var(--dim); }
.pill.spec-tap { color: var(--accent); border-color: var(--accent-dim); background: rgba(93,213,255,0.04); }
.pill.spec-tap .led { background: var(--accent); box-shadow: 0 0 5px var(--accent); }
.pill.spec-sweep { color: var(--green); border-color: #1f4a32; background: rgba(88,201,138,0.04); }
.pill.spec-sweep .led { background: var(--green); }
.pill.spec-stale { color: var(--yellow); border-color: #4a3a17; background: rgba(230,168,81,0.04); }
.pill.spec-stale .led { background: var(--yellow); }
.pill.spec-off, .pill.spec-unknown { color: var(--dim); }

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
.banner.banner-mode {
  background: rgba(255, 109, 109, 0.04);
  border-bottom: 1px solid #4a1c1c;
}
.banner.banner-mode::before {
  border-color: var(--red); color: var(--red);
}
.banner.banner-mode strong { color: var(--red); }
.banner.banner-survey {
  background: rgba(93, 213, 255, 0.04);
  border-bottom: 1px solid var(--accent-dim);
}
.banner.banner-survey::before {
  border-color: var(--accent); color: var(--accent);
}
.banner.banner-survey strong { color: var(--accent); }
.banner.banner-survey .dim { color: var(--txt-2); }
.banner.banner-survey .countdown {
  font-variant-numeric: tabular-nums;
  color: var(--txt); padding: 0 4px;
}
.banner.banner-coach {
  background: rgba(93, 213, 255, 0.025);
  border-bottom: 1px solid var(--line-2);
  color: var(--txt-2);
}
.banner.banner-coach::before {
  border-color: var(--accent); color: var(--accent); content: '?';
}
.banner.banner-coach strong { color: var(--accent); font-weight: 500; }

/* Currently-tuned-cell panel (cellular-drones). */
#cell-panel {
  margin: 8px 14px 0; padding: 8px 12px;
  border: 1px solid var(--line-2);
  border-radius: 6px;
  background: var(--panel-2);
  display: none;          /* shown by JS when a cell is locked */
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace;
  font-size: 11px;
}
#cell-panel .cp-head {
  display: flex; align-items: center; gap: 14px;
  color: var(--dim); margin-bottom: 6px;
}
#cell-panel .cp-head strong { color: var(--green); letter-spacing: 0.06em; }
#cell-panel .cp-row {
  display: flex; flex-wrap: wrap; gap: 16px 20px;
}
#cell-panel .cp-field { display: flex; gap: 7px; align-items: baseline; }
#cell-panel .cp-field .lbl { color: var(--dim); text-transform: uppercase;
                              letter-spacing: 0.12em; font-size: 10px; }
#cell-panel .cp-field .val { color: var(--txt); font-variant-numeric: tabular-nums; }

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
/* When leaflet was never loaded (Pi offline), collapse the map area so
   the scrubber + footer aren't pushed off-screen on narrow viewports. */
.map-wrap:has(#map.unavailable) { min-height: 80px; height: auto; }
@media (max-width: 1100px) {
  .map-wrap:has(#map.unavailable) { height: auto; min-height: 80px; }
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
#map.unavailable { position: static; display: flex; align-items: center;
                   justify-content: center; color: var(--dim); font-size: 12px;
                   padding: 16px; font-family: ui-monospace, monospace;
                   min-height: 60px; }
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
/* HARQ-aware new-data throughput. Green-leaning to stand out as the
   "this UE is actually transferring data" signal in a busy card. */
.chip.tp  { color: var(--green); border-color: #2f5d3e;
            background: rgba(88,201,138,0.08); font-weight: 600; }
/* FalconEye decode confidence indicator (histval). One dot, three colors:
   high (≥8) = trusted, mid (4-7) = borderline, low (<4) = likely noise. */
.conf-dot { width: 6px; height: 6px; border-radius: 50%;
            display: inline-block; margin-right: 4px;
            vertical-align: middle; }
.conf-dot.conf-hi  { background: var(--green); box-shadow: 0 0 4px var(--green); }
.conf-dot.conf-mid { background: var(--yellow); }
.conf-dot.conf-lo  { background: var(--red); opacity: 0.6; }
/* Broadcast pill in the header — counts P-RNTI / SI-RNTI / RA-RNTI hits
   that are NOT real UEs (cell-wide pages, system info, RACH responses). */
.pill.broadcast { color: var(--yellow); border-color: #4a3a17;
                  background: rgba(230,168,81,0.05); }
/* Cell load pill — rolling % of cell PRBs in use. Color shifts with
   utilization so a glance tells you congested vs idle. */
.pill.cell-load { color: var(--accent); border-color: var(--accent-dim);
                  font-variant-numeric: tabular-nums; }
.pill.cell-load.load-mid { color: var(--yellow); border-color: #4a3a17;
                           background: rgba(230,168,81,0.05); }
.pill.cell-load.load-hi  { color: var(--red); border-color: #4a1f1f;
                           background: rgba(255,109,109,0.06); }
/* MCS sparkline — small inline chart of per-UE modulation-and-coding
   index over recent grants. Drops indicate channel quality degradation. */
.mcs-spark { display: block; margin-top: 6px; opacity: 0.85; }
.mcs-spark .lbl { font-size: 8.5px; color: var(--dim);
                  letter-spacing: 0.1em; text-transform: uppercase;
                  margin-bottom: 2px; }
.mcs-spark svg { display: block; }

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

/* ---------------------------------------------------------- phone / narrow viewport */
@media (max-width: 640px) {
  /* Header: wrap brand + meta vertically; smaller pills */
  header {
    flex-direction: column; align-items: stretch; gap: 6px;
    padding: 8px 10px;
  }
  .brand { gap: 8px; }
  header h1 { font-size: 11px; }
  header h1 span { display: block; margin-left: 0; font-size: 10px; }
  .meta {
    gap: 8px; flex-wrap: wrap; font-size: 10px;
    justify-content: flex-start;
  }
  .stat { gap: 4px; }
  .stat strong { font-size: 12px; }
  .pill { padding: 2px 6px; font-size: 9.5px; letter-spacing: 0.02em; }

  /* Workspace: stack vertical, tune for phone height */
  .workspace {
    padding: 6px 8px 8px; gap: 8px;
    grid-template-columns: 1fr;
  }
  .map-wrap { height: 36vh; min-height: 240px; }
  .side { height: auto; min-height: 0; max-height: 50vh; }

  /* Banners + survey: tighter */
  .banner { padding: 6px 10px; font-size: 10.5px; }

  /* Side cards: stack with smaller padding */
  .side-head { padding: 8px 10px; font-size: 10px; }
  .cards { padding: 6px; }
  .card { padding: 8px 10px; gap: 6px; }
  .card .head { font-size: 10.5px; }

  /* Map overlays: shrink, lower z-stack */
  .map-overlay {
    top: 6px; right: 6px;
    font-size: 9.5px; padding: 4px 6px;
    max-width: 150px;
  }
  .legend {
    bottom: 6px; left: 6px;
    font-size: 9.5px; padding: 4px 6px;
    min-width: 96px; max-height: 36%;
  }
  .legend .label { font-size: 8.5px; margin-bottom: 3px; }

  /* Spectrum: full-width canvas, smaller header */
  #spectrum-panel {
    margin: 6px 8px 0 !important; padding: 6px 8px !important;
  }
  #spectrum-panel canvas {
    height: 96px !important;
  }
  #spectrum-panel > div:first-child {
    font-size: 9.5px; gap: 8px;
  }

  /* Scrubber: wrap on a second row if it overflows */
  .scrubber {
    flex-wrap: wrap; gap: 8px; padding: 6px 10px;
  }
  .scrubber .lbl { font-size: 8.5px; }
  .scrubber .time { min-width: 0; font-size: 11px; }
  .scrubber input[type=range] { flex: 1 1 100%; order: 99; }
  .scrubber .live-btn { padding: 4px 8px; font-size: 9px; }

  /* Footer log: smaller, lower height */
  footer {
    max-height: 80px; padding: 6px 10px;
    font-size: 9.5px;
  }
  footer::before { font-size: 8.5px; }

  /* Hide hover-only chrome that doesn't apply on touch */
  .card:hover { background: var(--panel); }
}

/* very small phones / portrait iPhones */
@media (max-width: 380px) {
  header h1 { font-size: 10px; }
  .pill { font-size: 9px; padding: 1px 5px; }
  .map-wrap { height: 32vh; min-height: 200px; }
  #spectrum-panel canvas { height: 84px !important; }
}
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
    <span class="pill cell-load" id="cell-load-pill" style="display:none" title="">
      <span class="led"></span><span id="cell-load-text">CELL —</span>
    </span>
    <span class="pill broadcast" id="bcast-pill" style="display:none" title="">
      <span class="led"></span><span id="bcast-text">BCAST —</span>
    </span>
    <span class="pill radio-unknown" id="radio"><span class="led"></span><span id="radio-text">RF —</span></span>
    <span class="pill spec-unknown" id="spec-pill"><span class="led"></span><span id="spec-pill-text">FFT —</span></span>
    <span class="pill gps-unknown" id="gps"><span class="led"></span><span id="gps-text">NAV —</span></span>
    <span class="pill status-idle" id="status"><span class="led"></span><span id="status-text">IDLE</span></span>
    <span class="pill" id="sse-pill" style="color: var(--dim);"><span class="led"></span><span id="sse-text">SSE —</span></span>
  </div>
</header>
<div class="banner">
  <strong>C-RNTI IS CONNECTION-SCOPED.</strong>
  <span class="dim">A handset that re-attaches will be reissued a new C-RNTI · positions use UL grants only · DL grants are eNB-side.</span>
</div>
<div id="dl-only-banner" class="banner banner-mode" style="display:none">
  <strong>DL-ONLY MODE.</strong>
  <span class="dim">FalconEye emits PDCCH only — no UE transmit energy observable. Positioning estimators inactive; C-RNTI surface still live. Positioning needs UL sniffing hardware (2× USRP + GPSDO, or X310).</span>
</div>
<div id="survey-banner" class="banner banner-survey" style="display:none">
  <strong>SURVEYING.</strong>
  <span id="survey-progress" class="dim">—</span>
  <span id="survey-countdown" class="countdown"></span>
</div>
<div id="coach-banner" class="banner banner-coach" style="display:none">
  <strong id="coach-label">DIAG.</strong>
  <span id="coach-text" class="dim">—</span>
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
<section id="cell-panel">
  <div class="cp-head">
    <strong>LOCKED CELL</strong>
    <span id="cp-decoder">—</span>
    <span id="cp-uptime" style="margin-left:auto;">T+ —</span>
  </div>
  <div class="cp-row">
    <div class="cp-field"><span class="lbl">PCI</span><span class="val" id="cp-pci">—</span></div>
    <div class="cp-field"><span class="lbl">EARFCN</span><span class="val" id="cp-earfcn">—</span></div>
    <div class="cp-field"><span class="lbl">f<sub>c</sub></span><span class="val" id="cp-fc">—</span></div>
    <div class="cp-field"><span class="lbl">RATE</span><span class="val" id="cp-rate">—</span></div>
    <div class="cp-field"><span class="lbl">GAIN</span><span class="val" id="cp-gain">—</span></div>
    <div class="cp-field"><span class="lbl">RX</span><span class="val" id="cp-rx">—</span></div>
    <div class="cp-field"><span class="lbl">GRANTS</span><span class="val" id="cp-grants">—</span></div>
    <div class="cp-field"><span class="lbl">DCI/s</span><span class="val" id="cp-rate-dci">—</span></div>
  </div>
</section>
<section id="spectrum-panel" style="margin:8px 14px 0; padding:8px 12px; border:1px solid #2a3038; border-radius:6px; background:#0b0d10;">
  <div style="display:flex; align-items:center; gap:14px; font-size:11px; color:#8a93a0; margin-bottom:6px;">
    <strong style="color:#7ad9a1; letter-spacing:0.06em;">SPECTRUM</strong>
    <span id="spec-source">— mode</span>
    <span id="spec-range">— MHz</span>
    <span id="spec-peak">peak —</span>
    <span id="spec-err" style="color:#f08580;"></span>
  </div>
  <canvas id="spec-canvas" width="900" height="140" style="width:100%; height:140px; display:block; background:#000;"></canvas>
</section>
<div class="scrubber" id="scrubber">
  <button class="live-btn" id="live-btn" type="button">
    <span class="led"></span><span id="live-btn-text">LIVE</span>
  </button>
  <span class="lbl">REPLAY</span>
  <input type="range" id="scrub-range" min="0" max="1" value="1" step="0.001" disabled>
  <span class="time" id="scrub-time">—</span>
</div>
<footer id="log"></footer>
<!-- leaflet script removed: see CSS removal note above. Map calls guard on typeof L !== 'undefined'. -->
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
const radioEl     = document.getElementById('radio');
const radioText   = document.getElementById('radio-text');
const specPillEl  = document.getElementById('spec-pill');
const specPillTxt = document.getElementById('spec-pill-text');
const cellPanel   = document.getElementById('cell-panel');
const coachBanner = document.getElementById('coach-banner');
const coachText   = document.getElementById('coach-text');
const coachLabel  = document.getElementById('coach-label');
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
// null = still measuring; true = DL-only upstream (no UE-side energy);
// false = UL energy is arriving, positioning estimators can converge.
let dlOnly = null;
const ues = new Map();

function setDlOnly(v) {
  if (v === undefined) return;
  if (dlOnly === v) return;
  dlOnly = v;
  const el = document.getElementById('dl-only-banner');
  if (el) el.style.display = (v === true) ? '' : 'none';
}

// Cell PRB-utilization pill (rolling 5s window). Useful context for
// "the decoder is quiet" — high cell load + zero UEs = decoder broken;
// low cell load + zero UEs = the cell really is idle right now.
function setCellLoad(loads) {
  const pill = document.getElementById('cell-load-pill');
  const text = document.getElementById('cell-load-text');
  if (!pill || !text) return;
  if (!loads || loads.length === 0) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = '';
  // Single-cell view (most common: single sniffer cell). For survey
  // mode with several PCIs visited, show the max-load one.
  const top = loads.reduce((a, b) =>
    (a.load_pct >= b.load_pct ? a : b));
  text.textContent = `CELL ${top.load_pct.toFixed(0)}% · ${top.cell_prb}PRB`;
  pill.classList.remove('load-mid', 'load-hi');
  if (top.load_pct >= 70) pill.classList.add('load-hi');
  else if (top.load_pct >= 30) pill.classList.add('load-mid');
  pill.title = loads.map(l =>
    `PCI ${l.pci}: ${l.load_pct.toFixed(1)}% of ${l.cell_prb} PRB ` +
    `(${l.grants_in_window} grants in last ${l.window_s}s)`).join('\n');
}

// Mini MCS sparkline — render last N MCS values as a thin polyline.
// MCS is 0-28 (LTE-A). A descending trend = UE channel degrading.
function mcsSparkSvg(history, color) {
  if (!history || history.length < 2) return '';
  const W = 110, H = 18;
  const pad = 1;
  const n = history.length;
  const stepX = (W - 2 * pad) / Math.max(1, n - 1);
  const points = history.map((v, i) => {
    const x = pad + i * stepX;
    // MCS 0-28 → bottom to top. Higher = better.
    const y = H - pad - (Math.max(0, Math.min(28, v)) / 28) * (H - 2 * pad);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const last = history[n - 1];
  return `<div class="mcs-spark">
    <div class="lbl">MCS (${n}) · now ${last}</div>
    <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
      <polyline points="${points}" fill="none"
                stroke="${color}" stroke-width="1.2" opacity="0.9"/>
    </svg>
  </div>`;
}

// Broadcast counters (P-RNTI paging, SI-RNTI system info, RA-RNTI
// random-access response). Not real UEs — they're cell-wide messages.
// Render as a single pill in the header so you can see "the cell is
// healthy / paging is flowing" without those decodes polluting the UE
// list.
function setBroadcasts(broadcasts) {
  const pill = document.getElementById('bcast-pill');
  const text = document.getElementById('bcast-text');
  if (!pill || !text) return;
  if (!broadcasts || broadcasts.length === 0) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = '';
  const totals = {};
  broadcasts.forEach(b => {
    totals[b.kind] = (totals[b.kind] || 0) + (b.count || 0);
  });
  const order = ['p_rnti', 'si_rnti', 'ra_rnti', 'unknown'];
  const labels = {p_rnti: 'PAGE', si_rnti: 'SIB',
                  ra_rnti: 'RACH', unknown: 'OTH'};
  const parts = order
    .filter(k => totals[k] > 0)
    .map(k => `${labels[k]} ${totals[k]}`);
  text.textContent = 'BCAST · ' + parts.join(' · ');
  pill.title = broadcasts.map(b =>
    `PCI ${b.pci} ${labels[b.kind] || b.kind} ×${b.count}`).join('\n');
}

// Survey progress (multi-cell sweep). `survey` is either null (not
// surveying) or {phase, cycle, cell_idx, cells_total, current_earfcn,
// current_pci, dwell_seconds, cell_started_ns, ...}.
let surveyState = null;
function setSurvey(s) {
  surveyState = s ?? null;
  const banner = document.getElementById('survey-banner');
  if (!banner) return;
  if (!surveyState) { banner.style.display = 'none'; return; }
  banner.style.display = '';
  const prog = document.getElementById('survey-progress');
  if (prog) {
    const decoder = surveyState.decoder ? surveyState.decoder + ' · ' : '';
    prog.textContent =
      `${decoder}cycle ${surveyState.cycle} · ` +
      `cell ${surveyState.cell_idx}/${surveyState.cells_total} · ` +
      `EARFCN ${surveyState.current_earfcn} · PCI ${surveyState.current_pci}`;
  }
  renderSurveyCountdown();
}
function renderSurveyCountdown() {
  const el = document.getElementById('survey-countdown');
  if (!el || !surveyState) return;
  // We approximate "time left on this cell" client-side because the
  // server only sends the start ts + dwell; rendering a smooth
  // countdown without a per-second event flow keeps SSE quiet.
  const dwell = surveyState.dwell_seconds || 0;
  const startedMs = (surveyState.cell_started_ns || 0) / 1e6;
  // Browser monotonic clock isn't aligned with server's, so fall back
  // to elapsed-since-render if the math goes negative.
  const elapsed = (performance.now() - (surveyState._anchor_ms || performance.now())) / 1000;
  if (!surveyState._anchor_ms) surveyState._anchor_ms = performance.now();
  const left = Math.max(0, dwell - elapsed);
  el.textContent = `${left.toFixed(1)}s left on this cell`;
}
setInterval(renderSurveyCountdown, 200);

// --- map ----------------------------------------------------------------
let map = null;
let droneMarker = null;
let droneTrailLine = null;
let firstGpsFix = true;          // snap once, then track without re-centering
const droneTrail = [];
const ueLayers = new Map();   // key -> {marker, accuracy, label}

// Default view when no GPS yet — Tel Aviv area, the testing locale.
// First real GPS fix snaps to that location and clears the placeholder.
const DEFAULT_CENTER = [32.0853, 34.7818];
const DEFAULT_ZOOM   = 13;

function initMapIfReady(centerLat, centerLon, zoom) {
  if (map || typeof L === 'undefined') return;
  map = L.map(mapEl, { zoomControl: true, attributionControl: true })
        .setView([centerLat, centerLon], zoom || DEFAULT_ZOOM);
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
  mapEl.innerHTML = '<div style="padding:24px;color:var(--dim);font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.7;">// MAP UNAVAILABLE<br><span style="color:var(--dim-2);">leaflet tiles need internet · running offline</span><br><br>positions for tracked UEs are still being computed and shown in the right-hand panel when GPS is fixed.</div>';
}
// Try to bring up the map with default view immediately. If Leaflet hasn't
// loaded yet (deferred script still parsing), poll briefly; if still missing
// after 5 s assume offline and show the placeholder.
function tryBootMap() {
  if (typeof L !== 'undefined') {
    initMapIfReady(DEFAULT_CENTER[0], DEFAULT_CENTER[1], DEFAULT_ZOOM);
    return;
  }
  if ((tryBootMap.elapsed = (tryBootMap.elapsed || 0) + 100) > 5000) {
    leafletUnavailable();
    return;
  }
  setTimeout(tryBootMap, 100);
}
setTimeout(tryBootMap, 0);

function updateDroneMarker() {
  if (!map || !latestGps) return;
  const g = latestGps.gps;
  if (!droneMarker) {
    droneMarker = L.circleMarker([g.lat, g.lon], {
      radius: 5, color:'#5dd5ff', weight:2, fillColor:'#04070a', fillOpacity:1,
    }).addTo(map).bindTooltip('SELF', {permanent:false, direction:'top', className:'ue-tip'});
  } else {
    droneMarker.setLatLng([g.lat, g.lon]);
  }
  // Snap once on the first real fix — pan/zoom from the default-view start.
  if (firstGpsFix) {
    firstGpsFix = false;
    map.setView([g.lat, g.lon], 17, { animate: true });
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
    if (layer) {
      if (layer.marker) map.removeLayer(layer.marker);
      if (layer.accuracy) map.removeLayer(layer.accuracy);
      ueLayers.delete(u.key);
    }
    return;
  }
  if (!layer) {
    layer = {};
    ueLayers.set(u.key, layer);
  }
  const selected = (u.key === selectedKey);
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
    mapOverlay.innerHTML = 'map · <span style="color:var(--dim)">default view · waiting for GPS fix</span>';
    return;
  }
  const g = latestGps.gps;
  const positioned = [...ues.values()]
    .filter(u => u.est_position).length;
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
    const posCls = u.est_position ? 'has-pos' : 'no-pos';
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
function fmtKbps(kbps) {
  if (kbps == null) return '—';
  if (kbps >= 1000) return (kbps / 1000).toFixed(2) + ' Mbps';
  if (kbps >= 10) return kbps.toFixed(0) + ' kbps';
  return kbps.toFixed(1) + ' kbps';
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
    if (dlOnly === true) {
      why = 'DL-only mode · no UL energy to integrate';
    } else if (!latestGps) {
      why = 'no GPS fix yet';
    } else if ((u.ul_count || 0) < 2) {
      why = `need ≥ 2 UL grants (have ${u.ul_count || 0})`;
    } else if ((u.n_geo_samples || 0) < 2) {
      why = `need ≥ 2 geo-tagged grants (have ${u.n_geo_samples || 0})`;
    } else {
      why = 'computing…';
    }
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
            <div class="meta-row">${p.method} · ${p.n_samples} samples${alt}</div>
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
  // HARQ-aware throughput: only includes grants where NDI flipped
  // (new-data), so retransmissions don't inflate the number.
  const tpDl = u.throughput_dl_kbps != null && u.throughput_dl_kbps > 0
    ? `<span class="chip tp">DL ${fmtKbps(u.throughput_dl_kbps)}</span>` : '';
  const tpUl = u.throughput_ul_kbps != null && u.throughput_ul_kbps > 0
    ? `<span class="chip tp">UL ${fmtKbps(u.throughput_ul_kbps)}</span>` : '';
  // FalconEye histogram confidence (paper: ≥8 = trust, <4 = likely noise).
  // Map to a 3-level dot so the eye can scan a busy list.
  const conf = u.confidence_max || 0;
  const confCls = conf >= 8 ? 'conf-hi' : conf >= 4 ? 'conf-mid' : 'conf-lo';
  const confTitle = `FalconEye decode confidence (histval) max ${conf}` +
    (conf >= 8 ? ' — trusted' : conf >= 4 ? ' — borderline' : ' — likely noise');

  card.innerHTML = `
    <div class="head">
      <div class="identity">
        <span class="sw"></span>
        <span class="conf-dot ${confCls}" title="${confTitle}"></span>
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
    ${mcsSparkSvg(u.mcs_history, color)}
    ${positionBlock(u)}
    <div class="foot">${tpDl}${tpUl}${ulChip}${dlChip}${dciTags}${mcs}${prb}${tbs}</div>
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
  lastStatus = s;
  updateRadioPill(s);
  updateCellPanel(s);
  updateCoach();
}

// --- pills, cell panel, coach (cellular-drones state visibility) ----
let lastStatus = {phase: 'idle'};
let lastSpectrumEv = null;        // last spectrum payload (snap)
let dciRateHistory = [];          // [{t_ms, total}] kept ~30s for rate calc
let cellLockSinceMs = null;       // wall-clock ms when first sniffing seen

function updateRadioPill(s) {
  if (!radioEl) return;
  const phase = (s && s.phase) || 'idle';
  if (phase === 'sniffing') {
    radioEl.className = 'pill radio-active';
    const dec = (s.decoder || 'falcon').toUpperCase();
    const mhz = s.center_hz != null ? ` · ${(s.center_hz/1e6).toFixed(1)} MHz` : '';
    radioText.textContent = `RF ${dec}${mhz}`;
  } else if (phase === 'simulating') {
    radioEl.className = 'pill radio-sim';
    radioText.textContent = 'RF sim';
  } else if (phase === 'error') {
    radioEl.className = 'pill radio-error';
    radioText.textContent = 'RF err';
  } else {
    radioEl.className = 'pill radio-unknown';
    radioText.textContent = 'RF —';
  }
}

function updateSpectrumPill(snap) {
  if (!specPillEl) return;
  if (!snap) {
    specPillEl.className = 'pill spec-off';
    specPillTxt.textContent = 'FFT —';
    return;
  }
  const src = snap.source || 'sweep';
  const age = snap.last_row_age_s;
  if (age != null && age > 5.0) {
    specPillEl.className = 'pill spec-stale';
    specPillTxt.textContent = `FFT stale ${age.toFixed(0)}s`;
  } else if (src === 'file') {
    specPillEl.className = 'pill spec-tap';
    specPillTxt.textContent = 'FFT tap';
  } else {
    specPillEl.className = 'pill spec-sweep';
    specPillTxt.textContent = 'FFT sweep';
  }
}

function updateCellPanel(s) {
  if (!cellPanel) return;
  if (!s || s.phase !== 'sniffing' || s.pci == null) {
    cellPanel.style.display = 'none';
    cellLockSinceMs = null;
    return;
  }
  if (cellLockSinceMs == null) cellLockSinceMs = Date.now();
  cellPanel.style.display = 'block';
  document.getElementById('cp-decoder').textContent = (s.decoder || 'falcon').toUpperCase();
  document.getElementById('cp-pci').textContent = s.pci;
  // EARFCN isn't stamped on status; we derive only if known elsewhere. Leave em-dash.
  document.getElementById('cp-earfcn').textContent = s.earfcn != null ? s.earfcn : '—';
  const fc = (s.center_hz != null ? s.center_hz : s.falcon_center_hz);
  document.getElementById('cp-fc').textContent = fc != null
    ? (fc/1e6).toFixed(2) + ' MHz' : '—';
  // FALCON's internal sample rate at the cell PRB count isn't piped through
  // status today; show — until we plumb it.
  document.getElementById('cp-rate').textContent = s.sample_rate_sps != null
    ? (s.sample_rate_sps/1e6).toFixed(2) + ' MS/s' : '—';
  document.getElementById('cp-gain').textContent = s.gain_db != null
    ? s.gain_db.toFixed(0) + ' dB' : '—';
  document.getElementById('cp-rx').textContent = s.antennas != null ? s.antennas + '×' : '—';
}

function setCellGrantStats() {
  if (cellPanel && cellPanel.style.display !== 'none') {
    document.getElementById('cp-grants').textContent = totalUeSightings;
    // DCI rate over the last ~10s of samples
    const now = Date.now();
    dciRateHistory.push({t: now, total: totalUeSightings});
    while (dciRateHistory.length && now - dciRateHistory[0].t > 30000) {
      dciRateHistory.shift();
    }
    if (dciRateHistory.length >= 2) {
      const first = dciRateHistory[0];
      const dt = (now - first.t) / 1000;
      const dN = totalUeSightings - first.total;
      const rate = dt > 0 ? dN / dt : 0;
      document.getElementById('cp-rate-dci').textContent = rate.toFixed(2);
    } else {
      document.getElementById('cp-rate-dci').textContent = '—';
    }
    if (cellLockSinceMs != null) {
      const secs = Math.round((Date.now() - cellLockSinceMs) / 1000);
      document.getElementById('cp-uptime').textContent = 'T+ ' + secs + 's';
    }
  }
}

// Rule-based "what's wrong?" coach. Updates from the same state that drives
// the pills. Keep it terse and actionable.
function updateCoach() {
  if (!coachBanner) return;
  const s = lastStatus || {phase: 'idle'};
  let msg = null, label = 'DIAG.';
  const hasGps = !!latestGps;
  const ulCount = (() => {
    let n = 0; ues.forEach(u => n += (u.ul_count || 0)); return n;
  })();
  if (s.phase === 'idle' || !s.phase) {
    coachBanner.style.display = 'none'; return;
  }
  if (s.phase === 'error') {
    label = 'ERROR';
    msg = s.message || 'Producer failed — check log.';
  } else if (s.phase === 'simulating') {
    coachBanner.style.display = 'none'; return;
  } else if (s.phase === 'sniffing') {
    if (totalUeSightings === 0) {
      label = 'NO DCIs YET.';
      msg = 'FALCON started; either still doing cell-search, or PDCCH not '
          + 'decoding. Confirm cell freq/PCI and that you’re close enough.';
    } else if (ulCount === 0) {
      label = 'DL-ONLY DECODES.';
      msg = `Got ${totalUeSightings} DL grants but 0 UL — UEs may be idle, `
          + 'or cell asymmetric. UE positioning needs UL grants with RSSI.';
    } else if (!hasGps) {
      label = 'NO GPS.';
      msg = 'Decoding UL but no GPS fix — positioning will publish without '
          + 'coordinates. Check gpsd / antenna.';
    } else {
      coachBanner.style.display = 'none'; return;
    }
  }
  if (msg) {
    coachLabel.textContent = label;
    coachText.textContent = msg;
    coachBanner.style.display = '';
  }
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
// --- spectrum waterfall ------------------------------------------------
const specCanvas = document.getElementById('spec-canvas');
const specCtx = specCanvas ? specCanvas.getContext('2d') : null;
const specRangeEl = document.getElementById('spec-range');
const specPeakEl = document.getElementById('spec-peak');
const specErrEl = document.getElementById('spec-err');
const specHistory = [];   // each entry: array of {mhz, dbfs}
const SPEC_HISTORY_MAX = 80;

function renderSpectrum(snap) {
  if (!specCtx || !snap) return;
  if (snap.error) { specErrEl.textContent = snap.error; return; }
  else specErrEl.textContent = '';
  const bins = snap.latest || [];
  if (!bins.length) return;
  // Push to rolling history, trim
  specHistory.push(bins);
  if (specHistory.length > SPEC_HISTORY_MAX) specHistory.shift();
  // Frequency range
  const fLow = snap.freq_start_mhz, fHigh = snap.freq_end_mhz;
  specRangeEl.textContent = `${fLow}–${fHigh} MHz`;
  // dB scale for color: clamp -90 → -10 dBFS to 0–1
  const dbMin = -90, dbMax = -10;
  const w = specCanvas.width, h = specCanvas.height;
  // Track max peak for label
  let peak = {mhz: 0, dbfs: -1e9};
  for (const b of bins) if (b.dbfs > peak.dbfs) peak = b;
  specPeakEl.textContent = `peak ${peak.dbfs.toFixed(1)} dBFS @ ${peak.mhz} MHz`;
  // Draw waterfall: shift existing image up 1px, paint new row at bottom
  const img = specCtx.getImageData(0, 1, w, h - 1);
  specCtx.putImageData(img, 0, 0);
  // Build new row
  const row = specCtx.createImageData(w, 1);
  for (let x = 0; x < w; x++) {
    const f = fLow + (fHigh - fLow) * (x / w);
    // Find nearest bin to this frequency
    let bestIdx = 0, bestDiff = 1e9;
    for (let i = 0; i < bins.length; i++) {
      const d = Math.abs(bins[i].mhz - f);
      if (d < bestDiff) { bestDiff = d; bestIdx = i; }
    }
    let t = (bins[bestIdx].dbfs - dbMin) / (dbMax - dbMin);
    t = Math.max(0, Math.min(1, t));
    // viridis-ish gradient: dark blue (low) → cyan → green → yellow → red (high)
    const r = Math.round(255 * Math.min(1, Math.max(0, 4*t - 2.5)));
    const g = Math.round(255 * Math.min(1, Math.max(0, 4*t - 0.5)) * Math.min(1, 4 - 4*t));
    const b = Math.round(255 * Math.min(1, Math.max(0, 1.5 - 4*t + 0.5)));
    const off = x * 4;
    row.data[off]     = r;
    row.data[off + 1] = g;
    row.data[off + 2] = b;
    row.data[off + 3] = 255;
  }
  specCtx.putImageData(row, 0, h - 1);
}

function applyEvent(ev) {
  if (ev.type === 'spectrum') {
    renderSpectrum(ev.spectrum);
    updateSpectrumPill(ev.spectrum);
    return;
  }
  if (ev.type === 'snapshot') {
    if (ev.spectrum) { renderSpectrum(ev.spectrum); updateSpectrumPill(ev.spectrum); }
    else updateSpectrumPill(null);
    ues.clear();
    [...cardsEl.querySelectorAll('.card')].forEach(c => c.remove());
    ueLayers.forEach(layer => {
      if (!map) return;
      ['marker', 'accuracy'].forEach(k => {
        if (layer[k]) map.removeLayer(layer[k]);
      });
    });
    ueLayers.clear();
    droneTrail.length = 0;
    (ev.gps_trail || []).forEach(p => droneTrail.push([p.lat, p.lon]));
    if (droneTrailLine) droneTrailLine.setLatLngs(droneTrail);
    setStatus(ev.status || {phase: 'idle'});
    setGps(ev.latest_gps);
    setDlOnly(ev.dl_only);
    setSurvey(ev.survey);
    setBroadcasts(ev.broadcasts || []);
    setCellLoad(ev.cell_load || []);
    (ev.ues || []).forEach(u => { ues.set(u.key, u); renderCard(u); updateUeLayer(u); });
    totalUeSightings = ev.total_ue_sightings || 0;
  } else if (ev.type === 'survey') {
    setSurvey(ev.survey);
  } else if (ev.type === 'broadcast') {
    setBroadcasts(ev.broadcasts || []);
  } else if (ev.type === 'ue_sighting') {
    const u = ev.ue;
    ues.set(u.key, u);
    renderCard(u);
    updateUeLayer(u);
    setDlOnly(ev.dl_only);
    totalUeSightings += 1;
    updateCoach();
    const est = u.est_position;
    const pos = est
      ? ` · ${est.lat.toFixed(5)},${est.lon.toFixed(5)} ±${est.cep95_m.toFixed(0)}m`
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
  setCellGrantStats();
  updateCoach();
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
window.__sseEvents = 0;
window.__sseErrors = 0;
window.__sseLastError = null;
const ssePill = document.getElementById('sse-pill');
const sseText = document.getElementById('sse-text');
function updateSsePill() {
  if (!sseText) return;
  if (es.readyState === 1) {
    sseText.textContent = `SSE ${window.__sseEvents} ev` + (window.__sseErrors ? ` (${window.__sseErrors} err)` : '');
    ssePill.style.color = window.__sseErrors ? 'var(--red)' : 'var(--green)';
  } else if (es.readyState === 0) {
    sseText.textContent = 'SSE connecting';
    ssePill.style.color = 'var(--yellow)';
  } else {
    sseText.textContent = 'SSE CLOSED';
    ssePill.style.color = 'var(--red)';
  }
}
setInterval(updateSsePill, 1000);
es.onopen = () => { updateSsePill(); };
es.onmessage = (e) => {
  window.__sseEvents++;
  try {
    const ev = JSON.parse(e.data);
    if (replayMode && (ev.type === 'gps' || ev.type === 'ue_sighting'
                       || (ev.type === 'snapshot' && !ev.replay))) return;
    applyEvent(ev);
  } catch (err) {
    window.__sseErrors++;
    window.__sseLastError = String(err) + ' :: ' + (err && err.stack ? err.stack : '');
    if (window.__sseErrors <= 3) {
      console.error('SSE applyEvent threw:', err, 'event:', e.data && e.data.slice(0, 300));
    }
  }
};
es.onerror = () => {
  logLine('<span style="color:var(--red)">stream disconnected</span> — browser will retry');
  updateSsePill();
};
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    state: State  # set on class before serving

    # Chromium on some configurations refuses to render the page when
    # served via HTTP/1.0 (the BaseHTTPServer default) — we end up with
    # an empty DOM even though curl gets a valid 200 response. Bump to
    # HTTP/1.1 so Chromium parses the response.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            # HTML hot-reload: if <repo>/data/dashboard.html exists, serve
            # IT instead of the embedded _INDEX_HTML. This lets us edit the
            # dashboard layout without restarting sniffer.live (which would
            # reset FALCON's cell lock and force a 3-5 min re-acquire).
            html_override = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "..", "data", "dashboard.html",
            )
            if os.path.exists(html_override):
                try:
                    with open(html_override, "rb") as f:
                        body = f.read()
                except OSError:
                    body = _INDEX_HTML.encode("utf-8")
            else:
                body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # Without explicit close, BaseHTTPServer holds the connection
            # open after the response and chromium sees a truncated body.
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
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
        if self.path == "/spectrum":
            snap = self.state.spectrum_snapshot() or {"latest": [], "error": "no scanner"}
            body = json.dumps(snap).encode("utf-8")
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
    p.add_argument("--center-hz", type=float, default=1_842_500_000,
                   help="target cell DL carrier frequency for FalconEye")
    p.add_argument("--rx-gain-db", type=float, default=50.0)
    p.add_argument("--mission-id",
                   default=time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()))
    p.add_argument("--out-dir", default="data")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--simulate", action="store_true",
                   help="generate fake UEs + GPS (no hardware needed)")
    p.add_argument("--falcon-cmd", default=None,
                   help="argv (space-split) for falkenber9/falcon's "
                        "FalconEye. We append `-D <tmpdir>/dci.csv` and "
                        "tail the file.")
    p.add_argument("--falcon-pci", type=int, default=None,
                   help="target PCI to stamp on FALCON-decoded grants. "
                        "FALCON's CSV has no PCI column; the caller "
                        "knows the cell from --falcon-cmd's -f freq.")
    p.add_argument("--survey-cells", default=None,
                   help="JSON list of cells to sweep through, e.g. "
                        "'[{\"earfcn\":1850,\"pci\":271,\"center_hz\":1870000000}]'. "
                        "Drives sniffer.survey.run_survey_loop instead "
                        "of locking to a single cell.")
    p.add_argument("--survey-dwell-seconds", type=float, default=15.0,
                   help="how long to dwell on each cell per cycle "
                        "(default 15s)")
    p.add_argument("--survey-total-seconds", type=float, default=1800.0,
                   help="total survey duration before exiting (default 30 min)")
    p.add_argument("--spectrum", action="store_true",
                   help="run a UHD-based background spectrum sweep on a "
                        "spare USRP and push live RF spectrum to the "
                        "dashboard. The sweep needs its own SDR — usable "
                        "alongside --falcon-cmd only when a second radio "
                        "is plugged in (one SDR = one process at a time).")
    p.add_argument("--spectrum-freq-mhz", default="700:2700",
                   help="sweep range as start:end MHz (default 700:2700)")
    p.add_argument("--spectrum-gain-db", type=float, default=60.0,
                   help="USRP RX gain for the spectrum sweep")
    args = p.parse_args()

    # Exactly one upstream producer must be selected. FALCON live mode
    # and survey-mode are mutually exclusive — both want exclusive radio.
    producers = sum(bool(x) for x in
                    (args.simulate, args.falcon_cmd, args.survey_cells))
    if producers > 1:
        print("sniffer.live: pick exactly one producer "
              "(--simulate | --falcon-cmd | --survey-cells).",
              file=sys.stderr)
        return 2
    if producers == 0:
        print("sniffer.live needs --simulate, --falcon-cmd, "
              "or --survey-cells.", file=sys.stderr)
        return 2
    if args.falcon_cmd and args.falcon_pci is None:
        print("sniffer.live: --falcon-cmd requires --falcon-pci "
              "(FALCON's CSV has no PCI column).", file=sys.stderr)
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
    elif args.survey_cells:
        from sniffer.survey import SurveyCell, run_survey_loop
        try:
            raw_cells = json.loads(args.survey_cells)
            cells = [SurveyCell(earfcn=int(c["earfcn"]),
                                pci=int(c["pci"]),
                                center_hz=int(c["center_hz"]))
                     for c in raw_cells]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            print(f"sniffer.live: bad --survey-cells JSON: {exc}",
                  file=sys.stderr)
            return 2
        threads.append(threading.Thread(
            target=run_survey_loop,
            kwargs=dict(
                state=state, cells=cells,
                dwell_seconds=args.survey_dwell_seconds,
                total_seconds=args.survey_total_seconds,
                mission_id=args.mission_id, out_dir=args.out_dir,
                stop=stop,
            ),
            daemon=True,
        ))
        threads.append(threading.Thread(
            target=run_gpsd_loop,
            kwargs=dict(state=state, mission_id=args.mission_id, stop=stop),
            daemon=True,
        ))
    elif args.falcon_cmd:
        cmd = args.falcon_cmd.split()
        # When --spectrum is also set, route the spectrogram through
        # FALCON's SpectrumTap rather than spawning a parallel uhd_sweep
        # (impossible anyway on a single radio). FALCON writes per-cell
        # FFT rows; SpectrumScanner tails the file.
        falcon_tap_path = None
        if args.spectrum:
            os.makedirs(args.out_dir, exist_ok=True)
            falcon_tap_path = os.path.join(
                args.out_dir, f"spectrum-{args.mission_id}.csv")
        threads.append(threading.Thread(
            target=run_falcon_loop,
            kwargs=dict(
                state=state, falcon_cmd=cmd, pci=args.falcon_pci,
                mission_id=args.mission_id, out_dir=args.out_dir,
                center_hz=args.center_hz, stop=stop,
                spectrum_tap_path=falcon_tap_path,
            ),
            daemon=True,
        ))
        threads.append(threading.Thread(
            target=run_gpsd_loop,
            kwargs=dict(state=state, mission_id=args.mission_id, stop=stop),
            daemon=True,
        ))
    # Spectrum scanner. Two modes:
    #   * --falcon-cmd + --spectrum → tail FALCON's SpectrumTap CSV (real cell)
    #   * otherwise + --spectrum    → spawn USRP-wide uhd_sweep (needs idle radio)
    if args.spectrum:
        try:
            f_start, f_end = (int(x) for x in args.spectrum_freq_mhz.split(":"))
        except ValueError:
            print(f"bad --spectrum-freq-mhz '{args.spectrum_freq_mhz}', want start:end",
                  file=sys.stderr)
            return 2
        if args.falcon_cmd:
            scanner = SpectrumScanner(
                on_snapshot=lambda payload: state._broadcast(
                    {"type": "spectrum", "spectrum": payload}),
                source="file",
                file_path=falcon_tap_path,
                freq_start_mhz=f_start, freq_end_mhz=f_end,
            )
        else:
            scanner = SpectrumScanner(
                on_snapshot=lambda payload: state._broadcast(
                    {"type": "spectrum", "spectrum": payload}),
                source="sweep",
                freq_start_mhz=f_start, freq_end_mhz=f_end,
                gain_db=args.spectrum_gain_db,
            )
        state.attach_spectrum_scanner(scanner)
        scanner.start()

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
              "falcon + parse_gpsd pipeline)", flush=True)
    elif args.survey_cells:
        print(f"mode: survey · {args.survey_dwell_seconds}s/cell × "
              f"{args.survey_total_seconds/60:.1f}min total", flush=True)
        print("GPS: gpspipe -w (only ingested if gpsd is running)", flush=True)
    else:
        print(f"mode: FalconEye · cmd={args.falcon_cmd!r} "
              f"@ {args.center_hz/1e6:.2f} MHz · PCI {args.falcon_pci}",
              flush=True)
        print("GPS: gpspipe -w (only ingested if gpsd is running)", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
