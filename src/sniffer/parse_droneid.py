"""DJI DroneID JSON → `geotag` JSONL (the same record GPS sources emit).

Two open-source DroneID decoders exist with slightly different JSON
schemas. We normalise both into a canonical shape and then emit
`GeotagRecord` rows — i.e. DroneID is wired as just another GPS
source, indistinguishable downstream once parsed.

Supported decoder outputs (one JSON object per line):

* **RUB-SysSec/DroneSecurity** (USRP B2xx only) — emits
  `{serial_number, latitude, longitude, altitude, app_lat, app_lon,
  longitude_home, latitude_home, gps_time, crc-packet, crc-calculated, …}`
  We honour the CRC fields when present — frames with mismatched CRC
  are dropped, matching the decoder's own "CRC Check FAILED" stance.

* **anarkiwi/samples2djidroneid** (HackRF-capable, file-based,
  Docker/Octave) — emits `{serial_no, latitude, longitude, altitude,
  phone_app_latitude, phone_app_longitude, home_latitude, home_longitude,
  crc, …}`. No validation field, so every parsable frame is trusted.

Canonical fields produced:
    serial, lat, lon, alt_m, height_m,
    app_lat, app_lon, home_lat, home_lon,
    vel_n, vel_e, vel_up, yaw_rad,
    gps_time_ms

`parse_stream` writes one `GeotagRecord` per accepted frame; the live
dashboard's existing `_GpsSink` consumes that without modification. The
DroneID origin survives via `gps.fix = "droneid"`.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, Iterator, Optional

from sniffer.schema import (
    GeotagRecord,
    GpsFix,
    mono_ns,
    utc_iso,
)

# Field synonyms across the two known decoders. Maps decoder-emitted
# field name → canonical name. Anything not listed is dropped.
_FIELD_MAP: dict[str, str] = {
    # Identity
    "serial_number": "serial",
    "serial_no":     "serial",
    # Drone position
    "latitude":  "lat",
    "longitude": "lon",
    "altitude":  "alt_m",
    "height":    "height_m",
    # App / RC position
    "app_lat":               "app_lat",
    "app_lon":               "app_lon",
    "phone_app_latitude":    "app_lat",
    "phone_app_longitude":   "app_lon",
    # Home position (last takeoff)
    "latitude_home":  "home_lat",
    "longitude_home": "home_lon",
    "home_latitude":  "home_lat",
    "home_longitude": "home_lon",
    # Velocity (different decoders use different units; we just pass through)
    "v_north":       "vel_n",
    "v_east":        "vel_e",
    "v_up":          "vel_up",
    "velocity_north": "vel_n",
    "velocity_east":  "vel_e",
    "velocity_up":    "vel_up",
    "yaw":           "yaw_rad",
    "gps_time":            "gps_time_ms",
    "phone_app_gps_time":  "gps_time_ms",
}


def normalize_frame(raw: dict) -> dict:
    """Map a decoder's per-frame JSON to canonical field names."""
    out: dict = {}
    for k, v in raw.items():
        canon = _FIELD_MAP.get(k)
        if canon is None:
            continue
        out[canon] = v
    return out


def _crc_ok(raw: dict) -> bool:
    """True if the frame either lacks a CRC pair or has matching pair.

    DroneSecurity emits `crc-packet` + `crc-calculated` as hex strings;
    we accept only matching pairs. samples2djidroneid emits a single
    `crc` integer (the packet's own CRC field, not a validation result),
    so we treat absence-of-pair as "trust the decoder".
    """
    pkt = raw.get("crc-packet")
    calc = raw.get("crc-calculated")
    if pkt is None or calc is None:
        return True
    return str(pkt).strip().lower() == str(calc).strip().lower()


def _frame_to_gpsfix(frame: dict) -> Optional[GpsFix]:
    lat = frame.get("lat")
    lon = frame.get("lon")
    alt = frame.get("alt_m")
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return None
    if lat == 0.0 and lon == 0.0:
        # Drones broadcast 0,0 before GPS lock. Skip — not a position.
        return None
    alt_m = 0.0
    if alt is not None:
        try:
            alt_m = float(alt)
        except (TypeError, ValueError):
            alt_m = 0.0
    return GpsFix(lat=lat, lon=lon, alt_m=alt_m, fix="droneid", hdop=None)


def _iter_json_objects(lines: Iterable[str]) -> Iterator[dict]:
    """Yield one dict per top-level JSON object found in `lines`.

    Handles two real-world output shapes from supported decoders:

    1. **One JSON per line** (samples2djidroneid, JSONL files): each line
       parses standalone.
    2. **Multi-line pretty-printed JSON** (DroneSecurity prints frames
       via `json.dumps(..., indent=4)`): the object spans many lines.

    We accumulate any text starting with `{` and use a brace-depth
    counter to find the matching `}`. The counter is naive — it doesn't
    track string literals — but DroneID JSON values contain only digits,
    decimals, simple ASCII identifiers, and short hex strings, none of
    which carry unbalanced braces. Decoder log lines, banners, and
    blank lines that appear between/around JSON blocks are ignored.

    Yields the parsed dict; silently drops anything that fails parsing.
    """
    buf_parts: list[str] = []
    depth = 0
    for raw in lines:
        # If we're not in a JSON block, skip until we see an opening brace.
        if depth == 0:
            stripped = raw.lstrip()
            if not stripped.startswith("{"):
                continue
            # Trim leading non-JSON text on the same line, e.g.
            # `Received frame: { ... }` — start from the first `{`.
            raw = stripped
        # Update depth across the full text we add to the buffer.
        for ch in raw:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    # Stray closer — reset and skip.
                    buf_parts = []
                    depth = 0
                    break
        buf_parts.append(raw)
        if depth == 0 and buf_parts:
            blob = "".join(buf_parts)
            buf_parts = []
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def parse_stream(
    input_stream: Iterable[str],
    mission_id: str,
    output_stream,
    *,
    clock_ns: Callable[[], int] = mono_ns,
    serial_filter: Optional[str] = None,
) -> int:
    """Consume decoder output, write GeotagRecord JSONL.

    `serial_filter` restricts to frames where the serial matches
    (case-sensitive substring match; useful when several drones are in
    the air and only one is ours). When None, all valid frames pass.

    Returns the number of GeotagRecords emitted.
    """
    n = 0
    for raw in _iter_json_objects(input_stream):
        if not _crc_ok(raw):
            continue
        frame = normalize_frame(raw)
        if serial_filter is not None:
            serial = frame.get("serial")
            if serial is None or serial_filter not in str(serial):
                continue
        fix = _frame_to_gpsfix(frame)
        if fix is None:
            continue
        rec = GeotagRecord(
            mission_id=mission_id,
            ts_mono_ns=clock_ns(),
            ts_utc=utc_iso(),
            gps=fix,
        )
        output_stream.write(rec.to_jsonl() + "\n")
        output_stream.flush()
        n += 1
    return n
