import json
import os
import tempfile

from sniffer.geotag import join


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def test_join_picks_nearest_fix_and_drops_stale():
    with tempfile.TemporaryDirectory() as td:
        scan = os.path.join(td, "scan.jsonl")
        gps = os.path.join(td, "gps.jsonl")
        # mission has 3 cell sightings at t=0ms, 100ms, 10s
        _write_jsonl(scan, [
            {"kind": "cell_sighting", "mission_id": "m", "capture_id": "h",
             "ts_mono_ns": 0, "ts_utc": "", "radio": {}, "cell": {"pci": 1}},
            {"kind": "cell_sighting", "mission_id": "m", "capture_id": "h",
             "ts_mono_ns": 100_000_000, "ts_utc": "", "radio": {},
             "cell": {"pci": 1}},
            {"kind": "cell_sighting", "mission_id": "m", "capture_id": "h",
             "ts_mono_ns": 10_000_000_000, "ts_utc": "", "radio": {},
             "cell": {"pci": 1}},
        ])
        # GPS fixes at 5ms and 105ms only — the 10s sighting must be dropped.
        _write_jsonl(gps, [
            {"kind": "geotag", "mission_id": "m", "ts_mono_ns": 5_000_000,
             "ts_utc": "", "gps": {"lat": 1.0, "lon": 2.0, "alt_m": 3.0,
                                    "fix": "3d"}},
            {"kind": "geotag", "mission_id": "m", "ts_mono_ns": 105_000_000,
             "ts_utc": "", "gps": {"lat": 1.1, "lon": 2.1, "alt_m": 3.1,
                                    "fix": "3d"}},
        ])

        out_dir = os.path.join(td, "out")
        counts = join([scan], [gps], out_dir, max_age_ms=500)
        assert counts == {"m": 3}

        records = []
        with open(os.path.join(out_dir, "geotagged-m.jsonl")) as fh:
            for line in fh:
                records.append(json.loads(line))

        # First sighting → joined with first fix.
        assert records[0]["gps"]["lat"] == 1.0
        assert records[0]["gps"]["age_ms"] == 5
        # Second sighting → joined with second fix.
        assert records[1]["gps"]["lat"] == 1.1
        # Third sighting → too far, gps stays unset (or null).
        assert records[2].get("gps") in (None, {}, {"lat": 0, "lon": 0})
