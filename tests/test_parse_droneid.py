"""Unit tests for sniffer.parse_droneid.

The DroneID decoder ecosystem is small but the two notable
projects emit subtly different JSON schemas. We test that both shapes
land in the same canonical GeotagRecord output.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from sniffer.parse_droneid import (
    _crc_ok,
    _frame_to_gpsfix,
    normalize_frame,
    parse_stream,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _drain(path: str, mission_id: str = "t-droneid", **kw) -> list[dict]:
    text = (FIXTURES / path).read_text()
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), mission_id, out, **kw)
    rows = [json.loads(l) for l in out.getvalue().strip().splitlines()]
    assert len(rows) == n
    return rows


def test_normalize_handles_dronesecurity_schema():
    raw = {"serial_number": "X", "latitude": 1.0, "longitude": 2.0,
           "altitude": 3.0, "v_north": 4, "app_lat": 5.0, "app_lon": 6.0,
           "latitude_home": 7.0, "longitude_home": 8.0, "gps_time": 12345}
    f = normalize_frame(raw)
    assert f == {"serial": "X", "lat": 1.0, "lon": 2.0, "alt_m": 3.0,
                 "vel_n": 4, "app_lat": 5.0, "app_lon": 6.0,
                 "home_lat": 7.0, "home_lon": 8.0, "gps_time_ms": 12345}


def test_normalize_handles_samples2djidroneid_schema():
    raw = {"serial_no": "Y", "latitude": 1.5, "longitude": 2.5,
           "altitude": 3.5, "velocity_north": 4, "phone_app_latitude": 5.5,
           "phone_app_longitude": 6.5, "home_latitude": 7.5,
           "home_longitude": 8.5, "phone_app_gps_time": 67890}
    f = normalize_frame(raw)
    # Critically, the canonical fields collapse the two schemas:
    assert f["serial"] == "Y"
    assert f["lat"] == 1.5 and f["lon"] == 2.5 and f["alt_m"] == 3.5
    assert f["vel_n"] == 4
    assert f["app_lat"] == 5.5 and f["app_lon"] == 6.5
    assert f["home_lat"] == 7.5 and f["home_lon"] == 8.5
    assert f["gps_time_ms"] == 67890


def test_normalize_drops_unknown_fields():
    raw = {"latitude": 1.0, "longitude": 2.0, "WAVELENGTH": 9999}
    assert "WAVELENGTH" not in normalize_frame(raw)


def test_crc_ok_accepts_matching_pair():
    assert _crc_ok({"crc-packet": "c935", "crc-calculated": "C935"})


def test_crc_ok_rejects_mismatched_pair():
    assert not _crc_ok({"crc-packet": "d985", "crc-calculated": "9b01"})


def test_crc_ok_accepts_when_pair_absent():
    # samples2djidroneid emits a single `crc` field (the packet's value),
    # not a validation result. Without a pair, we trust the decoder.
    assert _crc_ok({"crc": 8143})
    assert _crc_ok({})


def test_frame_to_gpsfix_rejects_null_island():
    # Drones broadcast 0,0 before GPS lock — that's not a position.
    assert _frame_to_gpsfix({"lat": 0.0, "lon": 0.0, "alt_m": 0.0}) is None


def test_frame_to_gpsfix_requires_lat_and_lon():
    assert _frame_to_gpsfix({"lon": 1.0}) is None
    assert _frame_to_gpsfix({"lat": 1.0}) is None


def test_parse_stream_dronesecurity_drops_bad_crc_and_null_island():
    # Fixture has 5 rows: 2 good, 1 bad CRC, 1 different serial, 1 null-island.
    rows = _drain("droneid_dronesecurity.jsonl")
    # CRC mismatch + null-island both drop → 3 GeotagRecords remain.
    assert len(rows) == 3
    assert all(r["kind"] == "geotag" for r in rows)
    assert all(r["gps"]["fix"] == "droneid" for r in rows)
    # Verify lat/lon round-trip correctly:
    assert abs(rows[0]["gps"]["lat"] - 51.446866781640146) < 1e-12


def test_parse_stream_samples2djidroneid_passes_all():
    rows = _drain("droneid_samples2djidroneid.jsonl")
    assert len(rows) == 2
    assert all(r["gps"]["fix"] == "droneid" for r in rows)
    assert abs(rows[0]["gps"]["alt_m"] - 39.32) < 1e-9


def test_parse_stream_serial_filter_restricts_to_our_drone():
    rows = _drain("droneid_dronesecurity.jsonl",
                  serial_filter="1ZNCJ4ABC123")
    # 5 rows total - 1 different serial - 1 bad CRC - 1 null-island = 2
    assert len(rows) == 2


def test_parse_stream_serial_filter_substring_match():
    # Substring match: "ABC" appears only in 1ZNCJ4ABC123 records.
    rows = _drain("droneid_dronesecurity.jsonl", serial_filter="ABC")
    assert len(rows) == 2  # same as full-string filter


def test_parse_stream_serial_filter_no_match_yields_nothing():
    rows = _drain("droneid_dronesecurity.jsonl", serial_filter="NOPE")
    assert rows == []


def test_parse_stream_ignores_malformed_lines():
    text = (
        "not valid json\n"
        '{"latitude": 1.0, "longitude": 2.0, "altitude": 3.0}\n'
        "# a comment\n"
        "\n"
        '{"latitude": 4.0, "longitude": 5.0, "altitude": 6.0}\n'
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), "t", out)
    assert n == 2


def test_parse_stream_emits_geotag_kind_record():
    rows = _drain("droneid_samples2djidroneid.jsonl")
    r = rows[0]
    assert r["kind"] == "geotag"
    assert "ts_mono_ns" in r and "ts_utc" in r
    assert r["mission_id"] == "t-droneid"


def test_parse_stream_handles_dronesecurity_pretty_printed_output():
    """DroneSecurity prints json.dumps(..., indent=4) — multi-line JSON
    interleaved with banner/log text. The parser must accumulate JSON
    blocks across lines via brace counting."""
    rows = _drain("droneid_dronesecurity_pretty.stdout")
    # 3 packets in the fixture: 2 good CRCs, 1 mismatched CRC.
    assert len(rows) == 2
    assert all(r["kind"] == "geotag" for r in rows)
    assert all(r["gps"]["fix"] == "droneid" for r in rows)
    # The bad-CRC packet must NOT appear (its CRC fields don't match).
    lats = [r["gps"]["lat"] for r in rows]
    assert abs(lats[0] - 51.446866781640146) < 1e-12
    assert abs(lats[1] - 51.446880000000000) < 1e-12


def test_iter_json_objects_handles_compact_and_pretty_mixed():
    """A single stream can mix one-per-line JSONL with multi-line pretty."""
    from sniffer.parse_droneid import _iter_json_objects
    text = (
        "log line that should be ignored\n"
        '{"latitude": 1.0, "longitude": 2.0, "altitude": 3.0}\n'
        "another log line\n"
        "{\n"
        '    "latitude": 4.0,\n'
        '    "longitude": 5.0,\n'
        '    "altitude": 6.0\n'
        "}\n"
        "trailing noise\n"
    )
    objs = list(_iter_json_objects(text.splitlines(keepends=True)))
    assert len(objs) == 2
    assert objs[0]["latitude"] == 1.0
    assert objs[1]["latitude"] == 4.0
