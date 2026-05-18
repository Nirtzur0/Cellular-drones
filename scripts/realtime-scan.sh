#!/usr/bin/env bash
# Continuously scan LTE downlink bands with the HackRF and stream identified
# cells (PCI, frequency, RX power, antenna ports, nRB) to stdout in JSONL
# plus a human summary on stderr.
#
# Usage:
#   ./scripts/realtime-scan.sh                  # default: cycle through Israeli LTE bands
#   ./scripts/realtime-scan.sh 1830e6 1840e6    # focus on a narrow range
#
# Env:
#   GAIN       HackRF RX gain in dB (default 60, max useful is ~62)
#   CELLSEARCH path to CellSearch binary (auto-detected)
#   MISSION_ID label written into every record (default: utc timestamp)
#   OUT_DIR    where to drop scan-*.jsonl (default: ./data)

set -euo pipefail

CELLSEARCH=${CELLSEARCH:-}
if [[ -z "$CELLSEARCH" ]]; then
  for candidate in \
    "$HOME/src/LTE-Cell-Scanner/build/src/CellSearch" \
    "/usr/local/bin/CellSearch" \
    "$(command -v CellSearch 2>/dev/null || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      CELLSEARCH="$candidate"; break
    fi
  done
fi
if [[ -z "$CELLSEARCH" || ! -x "$CELLSEARCH" ]]; then
  echo "CellSearch binary not found. Build LTE-Cell-Scanner first." >&2
  exit 1
fi

GAIN=${GAIN:-60}
MISSION_ID=${MISSION_ID:-$(date -u +%Y-%m-%dT%H-%M-%SZ)}
OUT_DIR=${OUT_DIR:-data}
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/scan-$MISSION_ID.jsonl"

# Resolve the project root so `python -m sniffer.parse_cellsearch` can find
# the package regardless of where the user invokes this script from.
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# Israeli LTE downlink slices in descending likelihood of carrier presence.
# Format: "label start_hz end_hz".
DEFAULT_SLICES=(
  "B3-mid   1825e6 1845e6"   # Cellcom + Partner heart of B3
  "B3-high  1845e6 1875e6"   # Pelephone block + Partner upper
  "B3-low   1805e6 1825e6"   # remaining B3
  "B1       2110e6 2155e6"   # 2100 MHz
  "B7       2620e6 2680e6"   # 2600 MHz
  "B28      758e6  788e6"    # 700 MHz APT
  "B8       925e6  960e6"    # 900 MHz
)

run_slice () {
  local label=$1 start=$2 end=$3
  echo "[scan] $label  $start → $end Hz  gain=$GAIN" >&2
  "$CELLSEARCH" -s "$start" -e "$end" -n 1 -g "$GAIN" 2>&1 \
    | python3 -m sniffer.parse_cellsearch \
        --mission-id "$MISSION_ID" \
        --backend lte-cell-scanner \
        --device hackrf-0 \
        --rx-gain-db "$GAIN" \
        --human \
    | tee -a "$OUT_FILE"
}

if [[ $# -ge 2 ]]; then
  run_slice "custom" "$1" "$2"
  exit 0
fi

echo "[scan] mission=$MISSION_ID  output=$OUT_FILE" >&2
while true; do
  for slice in "${DEFAULT_SLICES[@]}"; do
    # shellcheck disable=SC2086
    set -- $slice
    run_slice "$1" "$2" "$3" || true
  done
done
