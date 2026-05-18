#!/usr/bin/env bash
# Install HackRF + LTE-Cell-Scanner + Python pipeline deps on macOS.
# Re-runnable: skips already-installed components.

set -euo pipefail

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install from https://brew.sh first." >&2
  exit 1
fi

echo "[1/4] Installing HackRF + GNU Radio toolchain"
brew install hackrf gpsd

echo "[2/4] Installing LTE-Cell-Scanner build dependencies"
brew install cmake boost fftw itpp openblas pkg-config

echo "[3/4] Building LTE-Cell-Scanner from source (HackRF backend)"
SRC_DIR="${SRC_DIR:-$HOME/src}"
mkdir -p "$SRC_DIR"
if [[ ! -d "$SRC_DIR/LTE-Cell-Scanner" ]]; then
  git clone https://github.com/JiaoXianjun/LTE-Cell-Scanner.git "$SRC_DIR/LTE-Cell-Scanner"
fi
cd "$SRC_DIR/LTE-Cell-Scanner"
mkdir -p build && cd build
# OpenCL off — Apple Silicon support is flaky for this codebase.
cmake .. -DUSE_HACKRF=1 -DUSE_OPENCL=0
make -j"$(sysctl -n hw.ncpu)"
echo "Binaries built at: $SRC_DIR/LTE-Cell-Scanner/build/src/"

echo "[4/4] Python environment"
PY_VENV="${PY_VENV:-$(pwd)/../../.venv}"
cd - >/dev/null
python3 -m venv "$PY_VENV"
# shellcheck disable=SC1090
source "$PY_VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done."
echo "Activate the venv with: source $PY_VENV/bin/activate"
echo "Add LTE-Cell-Scanner to PATH for convenience:"
echo "  export PATH=\"$SRC_DIR/LTE-Cell-Scanner/build/src:\$PATH\""
