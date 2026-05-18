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
    backend: str  # lte-cell-scanner | falcon | ltesniffer | srsran
    device: str  # e.g. hackrf-0000...
    earfcn: Optional[int] = None
    center_hz: Optional[float] = None
    bandwidth_hz: Optional[float] = None
    sample_rate_sps: Optional[float] = None
    rx_gain_db: Optional[float] = None
    tcxo_ppm: Optional[float] = None


@dataclass
class CellInfo:
    pci: int
    n_id_1: Optional[int] = None
    n_id_2: Optional[int] = None
    mode: str = "fdd"  # fdd | tdd
    cp: str = "normal"  # normal | extended
    n_ports: Optional[int] = None
    rsrp_dbm: Optional[float] = None
    rsrq_db: Optional[float] = None
    snr_db: Optional[float] = None
    frame_offset_samples: Optional[int] = None
    mib: dict[str, Any] = field(default_factory=dict)
    sib1: dict[str, Any] = field(default_factory=dict)


@dataclass
class CellSighting:
    """One row of the JSONL log."""

    mission_id: str
    capture_id: str
    ts_mono_ns: int
    ts_utc: str
    radio: RadioConfig
    cell: CellInfo
    gps: Optional[GpsFix] = None
    attitude: Optional[Attitude] = None
    rnti: Optional[list[dict[str, Any]]] = None  # populated on USRP only
    notes: str = ""
    schema_version: int = SCHEMA_VERSION
    kind: str = "cell_sighting"

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
