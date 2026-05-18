"""Join UE sightings with the nearest GPS fix on monotonic time.

Inputs:
  one or more `ue-*.jsonl`   (UE sightings, gps possibly null)
  one or more `gps-*.jsonl`  (geotag-only records)

Output:
  one `geotagged-<mission>.jsonl` per mission_id, with `gps` and
  `attitude` populated from the temporally nearest GPS record within
  a configurable max-age window.

The join is single-pass and O(n+m): both streams are timestamp-ordered.
"""

from __future__ import annotations

import argparse
import bisect
import glob
import json
import os
from collections import defaultdict
from typing import Iterable

from sniffer.schema import read_jsonl

MAX_AGE_MS_DEFAULT = 500  # drop sightings >500 ms from nearest fix


def _load_gps(paths: Iterable[str]) -> dict[str, list[dict]]:
    """Return {mission_id: [gps_record, ...]} sorted by ts_mono_ns."""
    out: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        for rec in read_jsonl(p):
            if rec.get("kind") != "geotag":
                continue
            out[rec["mission_id"]].append(rec)
    for mid, recs in out.items():
        recs.sort(key=lambda r: r["ts_mono_ns"])
    return out


def _nearest(gps_list: list[dict], ts_mono_ns: int, max_age_ms: int):
    """Return the gps record nearest to ts_mono_ns, or None if too far."""
    if not gps_list:
        return None
    keys = [r["ts_mono_ns"] for r in gps_list]
    idx = bisect.bisect_left(keys, ts_mono_ns)
    candidates = []
    if idx < len(keys):
        candidates.append(gps_list[idx])
    if idx > 0:
        candidates.append(gps_list[idx - 1])
    best = min(candidates, key=lambda r: abs(r["ts_mono_ns"] - ts_mono_ns))
    age_ms = abs(best["ts_mono_ns"] - ts_mono_ns) / 1e6
    if age_ms > max_age_ms:
        return None
    return best, int(age_ms)


def join(scan_paths: list[str], gps_paths: list[str], out_dir: str,
         max_age_ms: int = MAX_AGE_MS_DEFAULT) -> dict[str, int]:
    os.makedirs(out_dir, exist_ok=True)
    gps_by_mission = _load_gps(gps_paths)
    counts: dict[str, int] = defaultdict(int)
    handles: dict[str, object] = {}
    try:
        for p in scan_paths:
            for rec in read_jsonl(p):
                if rec.get("kind") != "ue_sighting":
                    continue
                mid = rec["mission_id"]
                gps_list = gps_by_mission.get(mid, [])
                hit = _nearest(gps_list, rec["ts_mono_ns"], max_age_ms)
                if hit is not None:
                    g, age = hit
                    rec["gps"] = g["gps"]
                    rec["gps"]["age_ms"] = age
                    if g.get("attitude"):
                        rec["attitude"] = g["attitude"]
                fh = handles.get(mid)
                if fh is None:
                    fh = open(os.path.join(out_dir, f"geotagged-{mid}.jsonl"), "w",
                              encoding="utf-8")
                    handles[mid] = fh
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                counts[mid] += 1
    finally:
        for fh in handles.values():
            fh.close()
    return dict(counts)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("scan_glob", help="glob for scan-*.jsonl files")
    p.add_argument("--gps-glob", default="data/gps-*.jsonl")
    p.add_argument("--out-dir", default="data")
    p.add_argument("--max-age-ms", type=int, default=MAX_AGE_MS_DEFAULT)
    args = p.parse_args()
    scan_paths = sorted(glob.glob(args.scan_glob))
    gps_paths = sorted(glob.glob(args.gps_glob))
    if not scan_paths:
        print(f"No scan files matched {args.scan_glob}")
        return 1
    counts = join(scan_paths, gps_paths, args.out_dir, args.max_age_ms)
    for mid, n in counts.items():
        print(f"{mid}: {n} geotagged records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
