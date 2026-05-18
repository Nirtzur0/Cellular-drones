#!/usr/bin/env bash
# Run LTE-Cell-Scanner CellSearch with HackRF over a frequency range and
# emit JSONL records that conform to the framework schema.
#
# Usage: cell-scan.sh <start_hz> <end_hz> [step_hz]
# Example: cell-scan.sh 1840e6 1845e6 100e3

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <start_hz> <end_hz> [step_hz]" >&2
  exit 1
fi

START_HZ=$1
END_HZ=$2
STEP_HZ=${3:-100e3}

MISSION_ID="${MISSION_ID:-$(date -u +%Y-%m-%dT%H-%M-%SZ)}"
OUT_DIR="${OUT_DIR:-data}"
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/scan-$MISSION_ID.jsonl"

# CellSearch ships with LTE-Cell-Scanner; expect it on PATH.
if ! command -v CellSearch >/dev/null 2>&1; then
  echo "CellSearch not in PATH. Source ./scripts/install-macos.sh first." >&2
  exit 1
fi

echo "Scanning $START_HZ → $END_HZ Hz (step $STEP_HZ Hz) → $OUT_FILE"

# CellSearch streams human-readable output. Pipe through the parser to
# produce schema-conformant JSONL with a monotonic timestamp on every line.
CellSearch \
  --freq-start "$START_HZ" \
  --freq-end "$END_HZ" \
  --freq-step "$STEP_HZ" \
  --device-args "hackrf=0" \
  --num-try 1 \
  | python3 -m sniffer.parse_cellsearch \
      --mission-id "$MISSION_ID" \
      --backend lte-cell-scanner \
      --device hackrf-0 \
  >> "$OUT_FILE"

echo "Wrote $(wc -l < "$OUT_FILE") records to $OUT_FILE"
