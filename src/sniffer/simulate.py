"""Synthetic data sources for end-to-end pipeline testing.

We can't plug a USRP into a CI runner, so this module produces text
streams that look like:

  • LTESniffer `DECODED key=value` lines (parsed by `sniffer.parse_ltesniffer`)
  • `gpspipe -w` JSON stream (parsed by `sniffer.parse_gpsd`)

…for a configurable eNB + UE roster + drone trajectory under a
log-distance path-loss model with optional shadow fading.

Drives `sniffer live --simulate` and the `tests/test_e2e.py` integration
test.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Iterator

from pyproj import Geod

from sniffer.schema import TA_STEP_METERS

GEOD = Geod(ellps="WGS84")

# LTE caps TA_n_steps at 1282 (≈100 km one-way). Clamp simulated TA so we
# never emit a value that would never appear on a real link.
_TA_N_STEPS_MAX = 1282


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
class UeProfile:
    """A virtual UE attached to a cell. Has its own location + activity rate.

    `waypoints`, if set, makes the UE mobile; otherwise it sits at (lat, lon, alt_m).
    `activity_rate_hz` is the average rate of PDCCH grants visible for this UE.
    `c_rnti` is the temporary connection ID — we expose it as configurable so
    simulator tests can pin it.
    """

    c_rnti: int
    lat: float
    lon: float
    alt_m: float = 1.5
    tx_power_dbm: float = 23.0       # typical UE max-power Cat-4
    antenna_gain_db: float = 0.0
    n_path_loss: float = 3.2          # UL link tends to be slightly worse than DL
    activity_rate_hz: float = 1.0     # average grants per second (DL+UL combined)
    ul_share: float = 0.4             # fraction of grants that are UL
    waypoints: list[Waypoint] = field(default_factory=list)


@dataclass
class SimulationConfig:
    mission_id: str = "sim-mission"
    emitter: Emitter = field(
        default_factory=lambda: Emitter(lat=32.0853, lon=34.7818, alt_m=25.0)
    )
    waypoints: list[Waypoint] = field(default_factory=list)
    ues: list[UeProfile] = field(default_factory=list)
    gps_period_s: float = 0.1     # one gpsd TPV every 0.1 s
    ltesniffer_period_s: float = 0.05  # PDCCH decode tick (20 Hz)
    shadow_fading_db: float = 1.5
    seed: int = 0
    # eNB-DL is the gate: if the drone can't even hear the cell, no PDCCH
    # decode happens. UL grants then gate further on UE→drone link budget.
    detect_threshold_rsrp_dbm: float = -110.0
    ul_detect_threshold_dbm: float = -120.0


def box_trajectory(emitter: Emitter, half_size_m: float = 150.0,
                   altitudes: tuple[float, ...] = (15.0, 30.0, 60.0),
                   n_per_side: int = 15, leg_speed_mps: float = 5.0
                   ) -> list[Waypoint]:
    """A 4-sided box at multiple altitudes around the emitter.

    Geometrically well-conditioned for centroid localization.
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


def _utc_iso(mission_start_unix: float, t_offset: float) -> str:
    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(mission_start_unix + t_offset)
    )


def _ue_position(ue: UeProfile, t: float) -> Waypoint:
    """Where the UE is at simulation time `t`. Stationary UEs return their
    fixed location; mobile UEs interpolate along their waypoint list."""
    if not ue.waypoints:
        return Waypoint(lat=ue.lat, lon=ue.lon, alt_m=ue.alt_m, t_offset_s=t)
    interp = _interp_waypoint(ue.waypoints, t)
    if interp is None:
        # Before/after the UE's track — clamp to nearest endpoint.
        anchor = ue.waypoints[0] if t < ue.waypoints[0].t_offset_s else ue.waypoints[-1]
        return Waypoint(lat=anchor.lat, lon=anchor.lon, alt_m=anchor.alt_m,
                        t_offset_s=t)
    return interp


def _ul_rssi_dbm(ue: UeProfile, ue_pos: Waypoint, drone: Waypoint,
                 carrier_hz: float, rng: random.Random,
                 shadow_db: float) -> float:
    """UL signal energy at the drone receiver from the UE.

    Same log-distance model as the DL link, but with UE-side EIRP and a
    slightly steeper path-loss exponent (UEs are at ground level, dense
    scatter dominates).
    """
    _, _, ground_d = GEOD.inv(ue_pos.lon, ue_pos.lat, drone.lon, drone.lat)
    d = math.sqrt(ground_d ** 2 + (drone.alt_m - ue_pos.alt_m) ** 2)
    d = max(1.0, d)
    eirp = ue.tx_power_dbm + ue.antenna_gain_db
    l0 = 20.0 * math.log10(4.0 * math.pi * carrier_hz / 299_792_458.0)
    rssi = eirp - l0 - 10.0 * ue.n_path_loss * math.log10(d)
    if shadow_db > 0:
        rssi += rng.gauss(0.0, shadow_db)
    return rssi


def _default_ues(emitter: Emitter) -> list[UeProfile]:
    """Three synthetic UEs around the emitter.

    Two stationary (localizable), one moving across the scene (intentionally
    biased — exposes the limit of single-RX positioning for mobile targets).
    """
    cos_lat = math.cos(math.radians(emitter.lat))
    # Stationary UE just NE of the eNB, at street level.
    ue_a = UeProfile(
        c_rnti=0x4ad2,
        lat=emitter.lat + 50.0 / 111_320.0,
        lon=emitter.lon + 60.0 / (111_320.0 * cos_lat),
        alt_m=1.5, activity_rate_hz=1.8, ul_share=0.4,
    )
    # Stationary UE further SW, weaker UL.
    ue_b = UeProfile(
        c_rnti=0x73a1,
        lat=emitter.lat - 80.0 / 111_320.0,
        lon=emitter.lon - 40.0 / (111_320.0 * cos_lat),
        alt_m=1.5, activity_rate_hz=1.2, ul_share=0.3,
    )
    # Mobile UE: walks from W → E across the cell during the mission.
    track: list[Waypoint] = []
    for i in range(20):
        f = i / 19.0
        dx = -100.0 + f * 200.0
        dy = -10.0 + f * 20.0
        track.append(Waypoint(
            lat=emitter.lat + dy / 111_320.0,
            lon=emitter.lon + dx / (111_320.0 * cos_lat),
            alt_m=1.5,
            t_offset_s=f * 240.0,
        ))
    ue_c = UeProfile(
        c_rnti=0x91ff,
        lat=track[0].lat, lon=track[0].lon, alt_m=1.5,
        activity_rate_hz=2.4, ul_share=0.5, waypoints=track,
    )
    return [ue_a, ue_b, ue_c]


def ltesniffer_lines(cfg: SimulationConfig) -> Iterator[str]:
    """Yield DECODED key=value lines as if from `LTESniffer --dl-only`.

    Each tick (`ltesniffer_period_s`), every UE *may* be granted a PDCCH DCI
    according to its activity rate. UL grants carry `ul_rssi_dbm` (UE energy
    at the drone). DL grants carry `dl_rsrp_dbm` (eNB energy at the drone)
    and are mostly informational — DL energy is the same for all UEs on this
    cell, so it doesn't help per-UE positioning.
    """
    rng = random.Random(cfg.seed + 3)
    em = cfg.emitter
    if not cfg.waypoints:
        return
    ues = cfg.ues if cfg.ues else _default_ues(em)
    yield f"# simulated LTESniffer stream for mission {cfg.mission_id}\n"
    yield (f"# target cell PCI={em.pci} EARFCN={em.earfcn} "
           f"@ {em.center_hz/1e6:.1f} MHz\n")
    t = cfg.waypoints[0].t_offset_s
    t_end = cfg.waypoints[-1].t_offset_s
    frame = 0
    subframe = 0
    while t <= t_end:
        # A simulated-clock marker on every tick. Parser ignores comment
        # lines; the test driver picks these up to advance its simulated
        # mono_ns clock so GPS↔UE timestamp joins line up.
        yield f"# TICK t={t:.6f}\n"
        drone = _interp_waypoint(cfg.waypoints, t)
        if drone is None:
            t += cfg.ltesniffer_period_s
            subframe = (subframe + 1) % 10
            if subframe == 0:
                frame = (frame + 1) % 1024
            continue

        dl_rsrp = _rsrp_dbm(em, drone, rng, cfg.shadow_fading_db)
        # Cell unreachable from the drone in this position — no PDCCH decode.
        if dl_rsrp < cfg.detect_threshold_rsrp_dbm:
            t += cfg.ltesniffer_period_s
            subframe = (subframe + 1) % 10
            if subframe == 0:
                frame = (frame + 1) % 1024
            continue

        for ue in ues:
            # Bernoulli scheduling per tick.
            p_grant = ue.activity_rate_hz * cfg.ltesniffer_period_s
            if rng.random() >= p_grant:
                continue
            ue_pos = _ue_position(ue, t)
            is_ul = rng.random() < ue.ul_share
            if is_ul:
                rssi = _ul_rssi_dbm(ue, ue_pos, drone, em.center_hz, rng,
                                    cfg.shadow_fading_db)
                if rssi < cfg.ul_detect_threshold_dbm:
                    continue
                mcs = rng.randint(2, 24)
                n_prb = rng.choice([1, 2, 4, 8, 16])
                tbs = 50 + mcs * n_prb * 8
                # TA round-trips the UE↔drone link, but ta_n_steps encodes
                # the one-way distance (steps × 78.125 m). Simulate that.
                _, _, ground_d = GEOD.inv(drone.lon, drone.lat,
                                          ue_pos.lon, ue_pos.lat)
                slant_m = math.sqrt(
                    ground_d ** 2 + (drone.alt_m - ue_pos.alt_m) ** 2
                )
                ta_n = max(0, min(_TA_N_STEPS_MAX,
                                  round(slant_m / TA_STEP_METERS)))
                yield (
                    f"DECODED frame={frame} subframe={subframe} "
                    f"pci={em.pci} c_rnti={ue.c_rnti:#06x} "
                    f"format=0 direction=UL "
                    f"mcs={mcs} prb={n_prb} tbs={tbs} "
                    f"ul_rssi_dbm={rssi:.2f} ta_n_steps={ta_n}\n"
                )
            else:
                mcs = rng.randint(4, 27)
                n_prb = rng.choice([2, 4, 8, 16, 25, 50])
                tbs = 80 + mcs * n_prb * 10
                yield (
                    f"DECODED frame={frame} subframe={subframe} "
                    f"pci={em.pci} c_rnti={ue.c_rnti:#06x} "
                    f"format=1A direction=DL "
                    f"mcs={mcs} prb={n_prb} tbs={tbs} "
                    f"dl_rsrp_dbm={dl_rsrp:.2f}\n"
                )

        t += cfg.ltesniffer_period_s
        subframe = (subframe + 1) % 10
        if subframe == 0:
            frame = (frame + 1) % 1024


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
