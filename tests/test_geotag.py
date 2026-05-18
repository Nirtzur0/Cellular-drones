import json
import os
import tempfile

from sniffer.geotag import join


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _ue(ts_ns):
    return {
        "kind": "ue_sighting", "mission_id": "m", "capture_id": "u",
        "ts_mono_ns": ts_ns, "ts_utc": "", "radio": {},
        "ue": {"pci": 271, "c_rnti": 0x4ad2, "direction": "ul",
               "ul_rssi_dbm": -90.0},
    }


def test_join_picks_nearest_fix_and_drops_stale():
    with tempfile.TemporaryDirectory() as td:
        scan = os.path.join(td, "ue.jsonl")
        gps = os.path.join(td, "gps.jsonl")
        # 3 UE grants at t=0ms, 100ms, 10s
        _write_jsonl(scan, [_ue(0), _ue(100_000_000), _ue(10_000_000_000)])
        # GPS fixes at 5ms and 105ms only — the 10s grant must be dropped.
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

        # First grant → joined with first fix.
        assert records[0]["gps"]["lat"] == 1.0
        assert records[0]["gps"]["age_ms"] == 5
        # Second grant → joined with second fix.
        assert records[1]["gps"]["lat"] == 1.1
        # Third grant → too far, gps stays unset (or null).
        assert records[2].get("gps") in (None, {}, {"lat": 0, "lon": 0})
