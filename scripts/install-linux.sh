#!/usr/bin/env bash
# Install LTESniffer + srsRAN_4G dependencies + Python pipeline on Linux.
#
# The drone-mounted real-radio path lives on Linux because LTESniffer has
# never been smooth on macOS — UHD support is patchy and the build pulls
# in dependencies (libsctp, mbedTLS) that prefer apt.
#
# Tested on Ubuntu 22.04 LTS. Re-runnable: skips already-installed parts.

set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "This script targets Linux. For macOS (cell discovery only), use ./scripts/install-macos.sh." >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "apt-get not found. This installer assumes Debian/Ubuntu." >&2
  exit 1
fi

SRC_DIR=${SRC_DIR:-$HOME/src}
mkdir -p "$SRC_DIR"

echo "[1/5] APT dependencies"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential cmake git pkg-config \
  libfftw3-dev libmbedtls-dev libboost-program-options-dev libboost-system-dev \
  libconfig++-dev libsctp-dev libuhd-dev uhd-host python3-venv python3-pip \
  libusb-1.0-0-dev libpcsclite-dev

# UHD images (USRP firmware) — harmless to re-run.
sudo uhd_images_downloader || true

echo "[2/5] srsRAN_4G"
if [[ ! -d "$SRC_DIR/srsRAN_4G" ]]; then
  git clone --depth 1 https://github.com/srsRAN/srsRAN_4G.git "$SRC_DIR/srsRAN_4G"
fi
mkdir -p "$SRC_DIR/srsRAN_4G/build"
( cd "$SRC_DIR/srsRAN_4G/build"
  cmake .. -DENABLE_GUI=False
  make -j"$(nproc)"
  sudo make install
)
sudo ldconfig

echo "[3/5] LTESniffer"
# Upstream lives at SPRITZ-Research-Group/LTESniffer. Forks come and go;
# pin if you need reproducibility.
if [[ ! -d "$SRC_DIR/LTESniffer" ]]; then
  git clone --depth 1 https://github.com/SPRITZ-Research-Group/LTESniffer.git "$SRC_DIR/LTESniffer"
fi
mkdir -p "$SRC_DIR/LTESniffer/build"
( cd "$SRC_DIR/LTESniffer/build"
  cmake ..
  make -j"$(nproc)"
)
echo "LTESniffer binary at: $SRC_DIR/LTESniffer/build/src/LTESniffer"

echo "[4/5] gpsd"
sudo apt-get install -y --no-install-recommends gpsd gpsd-clients

echo "[5/5] Python environment"
PY_VENV=${PY_VENV:-$(pwd)/.venv}
python3 -m venv "$PY_VENV"
# shellcheck disable=SC1091
source "$PY_VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

cat <<EOF

Done. Quick sanity check:

  source $PY_VENV/bin/activate
  export PATH="$SRC_DIR/LTESniffer/build/src:\$PATH"
  sniffer demo --plot data/demo/ues.png

To run against real hardware (USRP B210 + cell on EARFCN 1850 / PCI 271):

  sniffer live --earfcn 1850 --pci 271 --rx-gain 50

EOF
