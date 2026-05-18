#!/usr/bin/env bash
# Launch the realtime UE-sniffing dashboard.
#
# Usage:
#   ./scripts/live-dashboard.sh --simulate              # no radio, fake UEs
#   ./scripts/live-dashboard.sh --ue <earfcn> <pci>     # LTESniffer on PCI
# Extra args after the mode flag are forwarded to `python -m sniffer.live`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -d "$REPO_ROOT/.venv" ]]; then
  # shellcheck disable=SC1091
  source "$REPO_ROOT/.venv/bin/activate"
fi
if [[ -d "$HOME/src/LTESniffer/build/src" ]]; then
  export PATH="$HOME/src/LTESniffer/build/src:$PATH"
fi

usage() {
  sed -n '2,7p' "$0"
  exit 1
}

case "${1:-}" in
  --simulate)
    shift
    exec python3 -m sniffer.live --simulate "$@"
    ;;
  --ue)
    EARFCN=${2:-}
    PCI=${3:-}
    if [[ -z "$EARFCN" || -z "$PCI" ]]; then
      echo "Need <earfcn> <pci>." >&2
      usage
    fi
    shift 3
    if ! command -v LTESniffer >/dev/null 2>&1; then
      echo "LTESniffer not on PATH. Run ./scripts/install-linux.sh." >&2
      exit 1
    fi
    LTECMD="$REPO_ROOT/scripts/ue-sniff.sh $EARFCN $PCI"
    exec python3 -m sniffer.live --ltesniffer-cmd "$LTECMD" "$@"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    echo "unrecognised: $1" >&2
    usage
    ;;
esac
