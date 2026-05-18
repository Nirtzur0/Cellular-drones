#!/usr/bin/env bash
# Run LTESniffer against a target LTE cell, normalise its DCI lines, and
# emit schema-conformant ue_sighting JSONL.
#
# Usage: ue-sniff.sh <earfcn> <pci> [extra-ltesniffer-args]
# Example: ue-sniff.sh 1850 271 --rx-gain 50
#
# Pre-flight: pick the target cell by EARFCN + PCI. Use CellMapper /
# OpenCellID for operator deployments, or `srsRAN_cell_search` if you've
# got srsRAN installed. Requires a USRP-class SDR + a working LTESniffer
# build — see scripts/install-linux.sh.
#
# This script assumes the LTESniffer binary is `LTESniffer` (rename or
# symlink if your build puts it elsewhere). Override with LTESNIFFER_BIN.

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <earfcn> <pci> [extra-ltesniffer-args...]" >&2
  exit 1
fi

EARFCN=$1
PCI=$2
shift 2

LTESNIFFER_BIN=${LTESNIFFER_BIN:-LTESniffer}
if ! command -v "$LTESNIFFER_BIN" >/dev/null 2>&1; then
  echo "$LTESNIFFER_BIN not on PATH. Build it with ./scripts/install-linux.sh" >&2
  exit 2
fi

MISSION_ID=${MISSION_ID:-$(date -u +%Y-%m-%dT%H-%M-%SZ)}
OUT_DIR=${OUT_DIR:-data}
mkdir -p "$OUT_DIR"
OUT_FILE="$OUT_DIR/ue-$MISSION_ID.jsonl"

echo "Sniffing PCI=$PCI on EARFCN=$EARFCN → $OUT_FILE" >&2

# LTESniffer line-buffers when stdout is a TTY but block-buffers when piped.
# `stdbuf -oL` keeps lines flowing through the pipeline.
stdbuf -oL "$LTESNIFFER_BIN" \
  --earfcn "$EARFCN" \
  --target-pci "$PCI" \
  "$@" \
  | stdbuf -oL python3 -m sniffer.normalize_ltesniffer \
  | python3 -m sniffer.parse_ltesniffer \
      --mission-id "$MISSION_ID" \
      --backend ltesniffer \
      --device usrp-b210-0 \
  >> "$OUT_FILE"

echo "Wrote $(wc -l < "$OUT_FILE") records to $OUT_FILE" >&2
