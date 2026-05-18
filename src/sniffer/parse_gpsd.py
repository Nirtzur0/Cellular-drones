"""Read gpsd's JSON stream and emit GeotagRecord JSONL.

Subset of gpsd's TPV (time-position-velocity) message we care about:
  {"class":"TPV","time":"2026-05-18T07:55:01.234Z",
   "lat":32.0853,"lon":34.7818,"altHAE":42.7,"mode":3,"hdop":0.6,...}
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable

from sniffer.schema import GeotagRecord, GpsFix, mono_ns

_MODE_MAP = {0: "none", 1: "none", 2: "2d", 3: "3d"}


def _fix_label(tpv: dict) -> str:
    status = tpv.get("status")
    # gpsd reports RTK in `status`: 2=DGPS, 3=RTK float, 4=RTK fix
    if status == 4:
        return "rtk_fix"
    if status == 3:
        return "rtk_float"
    return _MODE_MAP.get(int(tpv.get("mode", 0)), "none")


def parse_stream(input_stream, output_stream, mission_id: str,
                 clock_ns: Callable[[], int] = mono_ns) -> int:
    n = 0
    for raw in input_stream:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("class") != "TPV":
            continue
        if "lat" not in msg or "lon" not in msg:
            continue
        rec = GeotagRecord(
            mission_id=mission_id,
            ts_mono_ns=clock_ns(),
            ts_utc=msg.get("time", ""),
            gps=GpsFix(
                lat=float(msg["lat"]),
                lon=float(msg["lon"]),
                alt_m=float(msg.get("altHAE", msg.get("alt", 0.0))),
                fix=_fix_label(msg),
                hdop=msg.get("hdop"),
            ),
        )
        output_stream.write(rec.to_jsonl() + "\n")
        output_stream.flush()
        n += 1
    return n


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mission-id", required=True)
    args = p.parse_args()
    parse_stream(sys.stdin, sys.stdout, args.mission_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
