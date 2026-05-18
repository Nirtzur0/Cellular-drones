#!/usr/bin/env bash
# Tail gpsd and emit one geotag record per fix to data/gps-<mission>.jsonl.
# Runs in parallel with ue-sniff.sh; the geotag module joins on ts_mono_ns.

set -euo pipefail

MISSION_ID="${MISSION_ID:-$(date -u +%Y-%m-%dT%H-%M-%SZ)}"
OUT_DIR="${OUT_DIR:-data}"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/gps-$MISSION_ID.jsonl"

if ! command -v gpspipe >/dev/null 2>&1; then
  echo "gpspipe not found. Install gpsd (apt install gpsd gpsd-clients)." >&2
  exit 1
fi

echo "Logging GPS fixes → $OUT_FILE"
gpspipe -w | python3 -m sniffer.parse_gpsd --mission-id "$MISSION_ID" >> "$OUT_FILE"
