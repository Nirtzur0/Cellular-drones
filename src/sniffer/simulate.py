"""Synthetic data sources for end-to-end pipeline testing.

We can't plug a HackRF into a remote runner, so this module produces
text streams that look like:

  • `CellSearch` stdout (parsed by `sniffer.parse_cellsearch`)
  • `gpspipe -w` JSON stream (parsed by `sniffer.parse_gpsd`)

…for a configurable emitter and drone trajectory under a log-distance
path-loss model with optional shadow fading.

The same module powers the `demo` entry point and the `tests/test_e2e.py`
integration test.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Iterator

from pyproj import Geod

GEOD = Geod(ellps="WGS84")


@dataclass
class Emitter:
    """A virtual eNB used by the simulator."""

    lat: float
    lon: float
    alt_m: float
    pci: int = 271
    n_id_1: int = 90
    n_id_2: int = 1
    earfcn: int = 1850
    center_hz: float = 1_842_500_000
    tx_power_dbm: float = 24.0
    antenna_gain_db: float = 14.0
    n_path_loss: float = 3.0
    cgi: int = 67_305_473
    tac: int = 18_452
    plmn: str = "42501"


@dataclass
class Waypoint:
    lat: float
    lon: float
    alt_m: float
    t_offset_s: float  # seconds from mission start


@dataclass
class SimulationConfig:
    mission_id: str = "sim-mission"
    emitter: Emitter = field(
        default_factory=lambda: Emitter(lat=32.0853, lon=34.7818, alt_m=25.0)
    )
    waypoints: list[Waypoint] = field(default_factory=list)
    sample_period_s: float = 0.5  # one CellSearch line every 0.5 s
    gps_period_s: float = 0.1     # one gpsd TPV every 0.1 s
    shadow_fading_db: float = 1.5
    seed: int = 0
    detect_threshold_rsrp_dbm: float = -110.0


def box_trajectory(emitter: Emitter, half_size_m: float = 150.0,
                   altitudes: tuple[float, ...] = (15.0, 30.0, 60.0),
                   n_per_side: int = 15, leg_speed_mps: float = 5.0
                   ) -> list[Waypoint]:
    """A 4-sided box at multiple altitudes around the emitter.

    Geometrically well-conditioned for both centroid and WLS localization.
    """
    waypoints: list[Waypoint] = []
    t = 0.0
    for z in altitudes:
        sides = [
            (-half_size_m, half_size_m, half_size_m, half_size_m),     # W → E (north edge)
            (half_size_m, half_size_m, half_size_m, -half_size_m),     # N → S (east edge)
            (half_size_m, -half_size_m, -half_size_m, -half_size_m),   # E → W (south edge)
            (-half_size_m, -half_size_m, -half_size_m, half_size_m),   # S → N (west edge)
        ]
        for dx0, dy0, dx1, dy1 in sides:
            for i in range(n_per_side):
                f = i / max(1, n_per_side - 1)
                dx = dx0 + f * (dx1 - dx0)
                dy = dy0 + f * (dy1 - dy0)
                lat = emitter.lat + dy / 111_320.0
                lon = emitter.lon + dx / (111_320.0 * math.cos(math.radians(emitter.lat)))
                waypoints.append(Waypoint(lat=lat, lon=lon, alt_m=z, t_offset_s=t))
                t += (2 * half_size_m / max(1, n_per_side - 1)) / leg_speed_mps
    return waypoints


def line_trajectory(emitter: Emitter, length_m: float = 400.0,
                    offset_m: float = 200.0, n_points: int = 40,
                    altitude_m: float = 30.0, leg_speed_mps: float = 5.0
                    ) -> list[Waypoint]:
    """Single straight pass alongside the emitter — useful for AoA tests."""
    waypoints: list[Waypoint] = []
    step_m = length_m / max(1, n_points - 1)
    dt = step_m / leg_speed_mps
    for i in range(n_points):
        along = -length_m / 2 + i * step_m
        cross = offset_m
        lat = emitter.lat + along / 111_320.0
        lon = emitter.lon + cross / (111_320.0 * math.cos(math.radians(emitter.lat)))
        waypoints.append(
            Waypoint(lat=lat, lon=lon, alt_m=altitude_m, t_offset_s=i * dt)
        )
    return waypoints


def _interp_waypoint(waypoints: list[Waypoint], t: float) -> Waypoint | None:
    if not waypoints or t < waypoints[0].t_offset_s or t > waypoints[-1].t_offset_s:
        return None
    for i in range(1, len(waypoints)):
        if waypoints[i].t_offset_s >= t:
            w0 = waypoints[i - 1]
            w1 = waypoints[i]
            span = w1.t_offset_s - w0.t_offset_s
            f = 0.0 if span == 0 else (t - w0.t_offset_s) / span
            return Waypoint(
                lat=w0.lat + f * (w1.lat - w0.lat),
                lon=w0.lon + f * (w1.lon - w0.lon),
                alt_m=w0.alt_m + f * (w1.alt_m - w0.alt_m),
                t_offset_s=t,
            )
    return waypoints[-1]


def _rsrp_dbm(em: Emitter, w: Waypoint, rng: random.Random,
              shadow_db: float) -> float:
    """Log-distance path loss + log-normal shadow fading.

    FSPL at 1 m reference: L0 = 20·log10(4π·f/c) (≈ 37.7 dB at 1.842 GHz).
    RSRP = EIRP − L0 − 10·n·log10(d/1m) + shadow.
    """
    _, _, ground_d = GEOD.inv(em.lon, em.lat, w.lon, w.lat)
    d = math.sqrt(ground_d ** 2 + (w.alt_m - em.alt_m) ** 2)
    d = max(1.0, d)
    eirp = em.tx_power_dbm + em.antenna_gain_db
    l0 = 20.0 * math.log10(4.0 * math.pi * em.center_hz / 299_792_458.0)
    rsrp = eirp - l0 - 10.0 * em.n_path_loss * math.log10(d)
    if shadow_db > 0:
        rsrp += rng.gauss(0.0, shadow_db)
    return rsrp


def _rsrq_db(rsrp_dbm: float) -> float:
    # Crude mapping: link gets worse with weaker RSRP, capped between -20..-3
    return max(-20.0, min(-3.0, -10.0 + 0.2 * (rsrp_dbm + 90)))


def _snr_db(rsrp_dbm: float, rng: random.Random) -> float:
    return max(-5.0, rsrp_dbm + 100.0 + rng.gauss(0.0, 1.0))


def cellsearch_lines(cfg: SimulationConfig) -> Iterator[str]:
    """Yield text lines as if from `CellSearch` stdout."""
    rng = random.Random(cfg.seed + 1)
    em = cfg.emitter
    if not cfg.waypoints:
        return
    yield f"# simulated CellSearch stream for mission {cfg.mission_id}\n"
    yield f"Scanning EARFCN {em.earfcn} ({em.center_hz / 1e6:.1f} MHz)\n"
    t = cfg.waypoints[0].t_offset_s
    t_end = cfg.waypoints[-1].t_offset_s
    sample_idx = 0
    while t <= t_end:
        w = _interp_waypoint(cfg.waypoints, t)
        if w is None:
            t += cfg.sample_period_s
            continue
        rsrp = _rsrp_dbm(em, w, rng, cfg.shadow_fading_db)
        if rsrp >= cfg.detect_threshold_rsrp_dbm:
            rsrq = _rsrq_db(rsrp)
            snr = _snr_db(rsrp, rng)
            frame_off = int(rng.uniform(0, 30720))
            yield "Found LTE cell:\n"
            yield f"  Carrier frequency: {em.center_hz:.0f}\n"
            yield f"  n_id_1: {em.n_id_1}\n"
            yield f"  n_id_2: {em.n_id_2}\n"
            yield f"  PCI: {em.pci}\n"
            yield f"  RSRP: {rsrp:.2f} dBm\n"
            yield f"  RSRQ: {rsrq:.2f} dB\n"
            yield f"  SNR: {snr:.2f} dB\n"
            yield f"  Frame offset samples: {frame_off}\n"
            yield f"  CP: normal\n"
            yield "\n"
        t += cfg.sample_period_s
        sample_idx += 1


def _utc_iso(mission_start_unix: float, t_offset: float) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mission_start_unix + t_offset)
    )


def gpsd_lines(cfg: SimulationConfig,
               mission_start_unix: float | None = None) -> Iterator[str]:
    """Yield gpsd `class=TPV` JSON lines."""
    if mission_start_unix is None:
        mission_start_unix = time.time()
    rng = random.Random(cfg.seed + 2)
    if not cfg.waypoints:
        return
    t = cfg.waypoints[0].t_offset_s
    t_end = cfg.waypoints[-1].t_offset_s
    while t <= t_end:
        w = _interp_waypoint(cfg.waypoints, t)
        if w is None:
            t += cfg.gps_period_s
            continue
        # add a small ~0.5 m GPS noise
        lat = w.lat + rng.gauss(0.0, 5e-6)
        lon = w.lon + rng.gauss(0.0, 5e-6)
        alt = w.alt_m + rng.gauss(0.0, 0.3)
        msg = {
            "class": "TPV",
            "time": _utc_iso(mission_start_unix, t),
            "lat": lat,
            "lon": lon,
            "altHAE": alt,
            "mode": 3,
            "status": 4,  # rtk_fix
            "hdop": 0.6,
        }
        yield json.dumps(msg) + "\n"
        t += cfg.gps_period_s
