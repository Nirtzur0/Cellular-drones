"""Measurement record schema.

Single source of truth for what the airborne agents write and the
post-flight pipeline reads. Keep this module dependency-free so it can
run on a constrained airborne host.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1

# One LTE Timing Advance step is 16·Ts (Ts = 1/(15000·2048) s). Round-trip
# distance is c · 16·Ts ≈ 156.25 m, so one-way TA range is half that ≈
# 78.125 m per step. Lives in schema so it travels with the dataclass and
# doesn't pull numpy/scipy into the airborne host's record path.
TA_STEP_METERS = 78.12526041666667


@dataclass
class GpsFix:
    lat: float
    lon: float
    alt_m: float
    fix: str = "unknown"  # none | 2d | 3d | rtk_float | rtk_fix
    hdop: Optional[float] = None
    age_ms: Optional[int] = None


@dataclass
class Attitude:
    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0


@dataclass
class RadioConfig:
    backend: str  # falcon | sim
    device: str  # e.g. usrp-b210-0000...
    earfcn: Optional[int] = None
    center_hz: Optional[float] = None
    bandwidth_hz: Optional[float] = None
    sample_rate_sps: Optional[float] = None
    rx_gain_db: Optional[float] = None
    tcxo_ppm: Optional[float] = None


@dataclass
class UeEvent:
    """One PDCCH-decoded event for a single UE.

    A UE is identified by its C-RNTI within the cell (PCI). The same physical
    handset rotates C-RNTI on every RRC reconnection, so per-RNTI "tracks"
    are connection-scoped, not subscriber-scoped.
    """

    pci: int
    c_rnti: int
    direction: str = "dl"          # dl | ul
    dci_format: str = ""           # e.g. "1A", "0", "1", "1B"
    mcs: Optional[int] = None
    n_prb: Optional[int] = None
    harq_id: Optional[int] = None
    tbs_bytes: Optional[int] = None
    # 3GPP TS 36.321 §7.1 RNTI categories — derived from c_rnti value range.
    # Lets the dashboard split actual UEs (`c_rnti`) from cell-wide
    # broadcasts (`p_rnti`, `si_rnti`) so the "UE count" header reflects
    # real handsets, not paging messages.
    rnti_kind: str = "c_rnti"      # c_rnti | p_rnti | si_rnti | ra_rnti | unknown
    # New Data Indicator from the DCI. Flips when the eNB sends new
    # transport-block data on a HARQ process vs retransmitting it.
    # State.ingest_ue uses this to count new-data bytes only (HARQ
    # retransmissions don't add to throughput, just resilience).
    ndi: Optional[int] = None
    # FalconEye's RNTI-histogram bucket count for this decode. Higher =
    # this RNTI has been seen repeatedly in the recent window, so the
    # decode is much less likely to be a false positive. Per the FalconEye
    # paper, values < ~8 are noise. Surfaced in the dashboard as a per-UE
    # confidence indicator.
    confidence: Optional[int] = None
    # Energy measured by our passive receiver:
    #   ul_rssi_dbm  → for UL grants, this is the UE's transmission at our RX
    #   dl_rsrp_dbm  → for DL grants, this is the eNB's transmission (same for all UEs on cell)
    ul_rssi_dbm: Optional[float] = None
    dl_rsrp_dbm: Optional[float] = None
    # Round-trip Timing Advance. Encodes UE-to-drone (or UE-to-eNB) distance.
    #   ta_n_steps  → raw LTE TA step count (0–1282; 1 step ≈ 78.125 m one-way)
    #   ta_meters   → one-way distance derived from ta_n_steps via __post_init__.
    # Both null on real-radio PDCCH-only paths (FalconEye CSV doesn't
    # emit TA; TA lives in RAR / MAC CE on PDSCH). The simulator emits
    # them in extension columns so positioning estimators converge in
    # the hardware-free path.
    ta_n_steps: Optional[int] = None
    ta_meters: Optional[float] = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Single source of truth: ta_meters is derived. If the caller passes
        # ta_n_steps, recompute; otherwise leave whatever ta_meters was
        # passed in (e.g. round-tripped from JSONL where both were stored).
        if self.ta_n_steps is not None:
            self.ta_meters = self.ta_n_steps * TA_STEP_METERS


@dataclass
class UeSighting:
    """One row of the JSONL log for a C-RNTI sighting."""

    mission_id: str
    capture_id: str
    ts_mono_ns: int
    ts_utc: str
    radio: RadioConfig
    ue: UeEvent
    gps: Optional[GpsFix] = None
    attitude: Optional[Attitude] = None
    notes: str = ""
    schema_version: int = SCHEMA_VERSION
    kind: str = "ue_sighting"

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


@dataclass
class GeotagRecord:
    """One row of the GPS sidecar log."""

    mission_id: str
    ts_mono_ns: int
    ts_utc: str
    gps: GpsFix
    attitude: Optional[Attitude] = None
    schema_version: int = SCHEMA_VERSION
    kind: str = "geotag"

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def classify_rnti(rnti: int) -> str:
    """Bucket a 16-bit RNTI by its 3GPP TS 36.321 §7.1 reserved range.

    The dashboard treats `c_rnti` as a real UE and routes everything else
    (paging, system info, random access) to a separate "broadcast" lane
    so the UE count doesn't get inflated by cell-wide messages.
    """
    if rnti == 0xFFFF:
        return "p_rnti"
    if rnti == 0xFFFE:
        return "si_rnti"
    # 0x0001-0x003C is the RA-RNTI window (random-access response).
    if 0x0001 <= rnti <= 0x003C:
        return "ra_rnti"
    # 0xFFFC = M-RNTI (MBSFN), 0xFFFD = future. Group as broadcast-like.
    if rnti in (0xFFFC, 0xFFFD):
        return "unknown"
    # Reserved 0x0000 — should never appear from a real decode.
    if rnti == 0x0000:
        return "unknown"
    return "c_rnti"


def mono_ns() -> int:
    """Monotonic nanoseconds since an unspecified epoch (boot, usually).

    All records on a single host share this clock. Use it for joins.
    """
    return time.monotonic_ns()


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_jsonl(path: str):
    """Yield decoded records from a JSONL file."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
