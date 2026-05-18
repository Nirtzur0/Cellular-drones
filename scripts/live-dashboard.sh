#!/usr/bin/env bash
# Launch the realtime web dashboard around CellSearch + HackRF.
#
# Usage:
#   ./scripts/live-dashboard.sh                       # defaults: 1840–1845 MHz
#   ./scripts/live-dashboard.sh 1840e6 1845e6 100e3
#   ./scripts/live-dashboard.sh --simulate            # no radio, fake cells

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Make sure the user's venv + LTE-Cell-Scanner build dir are on PATH if present.
if [[ -d "$REPO_ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi
if [[ -d "$HOME/src/LTE-Cell-Scanner/build/src" ]]; then
  export PATH="$HOME/src/LTE-Cell-Scanner/build/src:$PATH"
fi

if [[ "${1:-}" == "--simulate" ]]; then
  exec python -m sniffer.live --simulate "${@:2}"
fi

START_HZ=${1:-1840e6}
END_HZ=${2:-1845e6}
STEP_HZ=${3:-100e3}

if ! command -v CellSearch >/dev/null 2>&1; then
  echo "CellSearch not on PATH. Either:"
  echo "  1) run ./scripts/install-macos.sh  (one-time setup), or"
  echo "  2) try the UI without hardware:  ./scripts/live-dashboard.sh --simulate"
  exit 1
fi

exec python -m sniffer.live \
  --start-hz "$START_HZ" \
  --end-hz "$END_HZ" \
  --step-hz "$STEP_HZ"
