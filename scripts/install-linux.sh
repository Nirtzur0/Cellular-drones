#!/usr/bin/env bash
# Install FALCON + srsRAN_4G dependencies + Python pipeline on Linux.
#
# Tested on Ubuntu 22.04 LTS / Raspberry Pi OS Bookworm aarch64.
# Re-runnable: skips already-installed parts.

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

echo "[1/4] APT dependencies"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  build-essential cmake git pkg-config \
  libfftw3-dev libmbedtls-dev libboost-program-options-dev libboost-system-dev \
  libconfig++-dev libsctp-dev python3-venv python3-pip python3-full \
  libusb-1.0-0-dev libpcsclite-dev \
  libuhd-dev uhd-host python3-uhd \
  libglib2.0-dev libudev-dev libcurl4-openssl-dev \
  libpcap-dev libssl-dev gpsd gpsd-clients

echo "[2/4] srsRAN_4G (provides srsran_cell_search + pdsch_ue for sniffer scan)"
if [[ ! -d "$SRC_DIR/srsRAN_4G" ]]; then
  git clone --depth 1 https://github.com/srsRAN/srsRAN_4G.git "$SRC_DIR/srsRAN_4G"
fi
mkdir -p "$SRC_DIR/srsRAN_4G/build"
( cd "$SRC_DIR/srsRAN_4G/build"
  cmake .. \
    -DENABLE_GUI=False \
    -DENABLE_UHD=ON \
    -DENABLE_BLADERF=OFF \
    -DENABLE_ZEROMQ=OFF \
    -DENABLE_HARDSIM=OFF \
    -DENABLE_SRSENB=OFF \
    -DENABLE_SRSEPC=OFF \
    -DENABLE_SOAPYSDR=OFF \
    -DENABLE_WERROR=OFF
  make -j"$(nproc)"
  sudo make install
)
sudo ldconfig

# Pull USRP FPGA images so b2xx boards firmware-load on plug-in.
if command -v uhd_images_downloader >/dev/null 2>&1; then
  sudo uhd_images_downloader -t "b2xx" 2>&1 | tail -5 || \
    echo "uhd_images_downloader failed — fetch zips manually from "\
         "files.ettus.com/binaries/cache and unzip into /usr/share/uhd/images."
fi

# srsRAN_4G's `make install` ships srsue / libsrsran_* but NOT the
# example binaries. Symlink them so the Python wrappers find them on PATH.
sudo ln -sfv "$SRC_DIR/srsRAN_4G/build/lib/examples/cell_search" \
             /usr/local/bin/srsran_cell_search 2>/dev/null || true
sudo ln -sfv "$SRC_DIR/srsRAN_4G/build/lib/examples/pdsch_ue" \
             /usr/local/bin/pdsch_ue 2>/dev/null || true

echo "[3/4] FALCON / FalconEye (the PDCCH decoder — single run path)"
# FALCON (falkenber9/falcon) emits per-DCI CSV via `-D <path>` —
# exactly the shape `sniffer.falcon` tails.
#
# Caveats (see docs/design.md "Decoder choice"):
#  * Project is dormant: last code commit November 2020. Pulls its own
#    patched srsLTE 18.09 (auto-downloaded as a submodule by cmake).
#  * Tested SDRs: USRP B210 / B205mini / LimeSDR Mini.
#  * FDD only. TDD bands (38/40/41) do NOT decode.
#  * Needs i7-class CPU (4 physical cores, HT off) for 20 MHz; ~10
#    MHz on weaker hardware. Pi 5 will be marginal at 20 MHz.
if [[ ! -d "$SRC_DIR/falcon" ]]; then
  git clone --depth 1 https://github.com/falkenber9/falcon.git \
    "$SRC_DIR/falcon"
fi
mkdir -p "$SRC_DIR/falcon/build"
( cd "$SRC_DIR/falcon/build"
  cmake .. -DUSE_GUI=False -DUSE_CAPTURE_PROBE=False
  make -j"$(nproc)" FalconEye
)
sudo ln -sfv "$SRC_DIR/falcon/build/src/FalconEye" \
             /usr/local/bin/FalconEye 2>/dev/null || true
echo "FalconEye binary: $SRC_DIR/falcon/build/src/FalconEye"

echo "[4/4] Python environment"
PY_VENV=${PY_VENV:-$(pwd)/.venv}
python3 -m venv "$PY_VENV"
# shellcheck disable=SC1091
source "$PY_VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

cat <<EOF

Done. Binaries now on PATH:
  $(command -v srsran_cell_search 2>/dev/null || echo "srsran_cell_search (open a new shell)")
  $(command -v pdsch_ue           2>/dev/null || echo "pdsch_ue           (open a new shell)")
  $(command -v FalconEye          2>/dev/null || echo "FalconEye          (open a new shell)")

Quick sanity check:

  source $PY_VENV/bin/activate
  sniffer live --simulate              # no hardware, synthetic UEs + GPS

Discover real cells (USRP B210 plugged in):

  sniffer scan --band 3                # quick sweep
  sniffer scan --band 3 --decode-sib1  # + PLMN / TAC / CGI

Then target one and stream UEs:

  sniffer live --earfcn 1850 --pci 271

EOF
