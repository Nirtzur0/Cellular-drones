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
  libconfig++-dev libsctp-dev python3-venv python3-pip python3-full \
  libusb-1.0-0-dev libpcsclite-dev \
  hackrf libhackrf-dev \
  libsoapysdr-dev soapysdr-tools soapysdr-module-hackrf \
  libglib2.0-dev libudev-dev libcurl4-openssl-dev \
  libpcap-dev libssl-dev gpsd gpsd-clients

echo "[2/5] srsRAN_4G"
if [[ ! -d "$SRC_DIR/srsRAN_4G" ]]; then
  git clone --depth 1 https://github.com/srsRAN/srsRAN_4G.git "$SRC_DIR/srsRAN_4G"
fi
mkdir -p "$SRC_DIR/srsRAN_4G/build"
( cd "$SRC_DIR/srsRAN_4G/build"
  # SoapySDR gives us the HackRF backend. UHD off — no USRP here.
  # srsENB/srsEPC off — they pull SCTP-heavy S1AP code we never run.
  cmake .. \
    -DENABLE_GUI=False \
    -DENABLE_UHD=OFF \
    -DENABLE_BLADERF=OFF \
    -DENABLE_ZEROMQ=OFF \
    -DENABLE_HARDSIM=OFF \
    -DENABLE_SRSENB=OFF \
    -DENABLE_SRSEPC=OFF \
    -DENABLE_SOAPYSDR=ON \
    -DENABLE_WERROR=OFF
  make -j"$(nproc)"
  sudo make install
)
sudo ldconfig

echo "[3/4] LTESniffer"
# Upstream is SysSec-KAIST/LTESniffer (the published research repo).
if [[ ! -d "$SRC_DIR/LTESniffer" ]]; then
  git clone --depth 1 https://github.com/SysSec-KAIST/LTESniffer.git "$SRC_DIR/LTESniffer"
  # The upstream SIMD-detection block only matches `arm` (arm32), so on
  # aarch64 (Pi 4 64-bit OS) it sets HAVE_NEON=False and aborts with
  # "no SIMD instructions found". Match aarch64 too, and drop the
  # arm32-only `-mfpu=neon` flag on aarch64 where clang/gcc reject it.
  sed -i.bak '
    s|if(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")|if(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm" OR ${CMAKE_SYSTEM_PROCESSOR} MATCHES "aarch64")|;
    s|set(CMAKE_C_FLAGS  "${CMAKE_C_FLAGS} -mfpu=neon -march=native -DIS_ARM -DHAVE_NEON")|if(${CMAKE_SYSTEM_PROCESSOR} MATCHES "aarch64")\n      set(CMAKE_C_FLAGS  "${CMAKE_C_FLAGS} -DIS_ARM -DHAVE_NEON")\n    else()\n      set(CMAKE_C_FLAGS  "${CMAKE_C_FLAGS} -mfpu=neon -march=native -DIS_ARM -DHAVE_NEON")\n    endif()|;
    s|else(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")|else(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm" OR ${CMAKE_SYSTEM_PROCESSOR} MATCHES "aarch64")|;
    s|endif(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm")$|endif(${CMAKE_SYSTEM_PROCESSOR} MATCHES "arm" OR ${CMAKE_SYSTEM_PROCESSOR} MATCHES "aarch64")|
  ' "$SRC_DIR/LTESniffer/CMakeLists.txt"

  # GCC 14 rejects `for (auto i = 0; ...)` in C99 — LTESniffer uses this
  # C23-only syntax in falcon_dci.c.
  find "$SRC_DIR/LTESniffer/lib" -name '*.c' \
    -exec sed -i 's|for *(auto |for (int |g' {} +
fi
mkdir -p "$SRC_DIR/LTESniffer/build"
( cd "$SRC_DIR/LTESniffer/build"
  cmake ..
  # LTESniffer's CMake clones its own srsRAN as an ExternalProject and the
  # vendored fmt/core.h doesn't include <array> — newer libstdc++ (gcc 14+)
  # no longer pulls it in transitively. Patch in advance of the first make.
  FMT_CORE="$SRC_DIR/LTESniffer/build/srsRAN-src/lib/include/srsran/srslog/bundled/fmt/core.h"
  if [[ -f "$FMT_CORE" ]] && ! grep -q '^#include <array>' "$FMT_CORE"; then
    sed -i '/^#define FMT_CORE_H_/a #include <array>' "$FMT_CORE"
  fi
  make -j"$(nproc)"
)
echo "LTESniffer binary at: $SRC_DIR/LTESniffer/build/src/LTESniffer"

# srsRAN_4G's `make install` ships srsue / libsrsran_* but NOT the
# example binaries (cell_search, pdsch_ue, …) under /usr/local/bin.
# LTESniffer never had a `make install` step. Symlink both so the
# Python wrappers find them on PATH.
echo "[3.5/5] Symlinking decoder binaries onto PATH"
sudo ln -sfv "$SRC_DIR/srsRAN_4G/build/lib/examples/cell_search" \
             /usr/local/bin/srsran_cell_search 2>/dev/null || true
sudo ln -sfv "$SRC_DIR/srsRAN_4G/build/lib/examples/pdsch_ue" \
             /usr/local/bin/pdsch_ue 2>/dev/null || true
sudo ln -sfv "$SRC_DIR/LTESniffer/build/src/LTESniffer" \
             /usr/local/bin/LTESniffer 2>/dev/null || true

echo "[3.5/5] FALCON (alternative DL-only LTE decoder with text output)"
# FALCON / FalconEye (falkenber9/falcon) is what LTESniffer is built on.
# Unlike LTESniffer (which writes PCAP only), FalconEye supports per-DCI
# CSV output via `-D <path>` — exactly the shape `sniffer.falcon` tails.
#
# Caveats (verified upstream — see docs/design.md "Decoder choice"):
#  * Project is dormant: last code commit November 2020. Builds on its
#    own patched srsLTE 18.09 (auto-downloaded as submodule by cmake).
#  * Tested SDRs: USRP B210 / B205mini / LimeSDR Mini. HackRF is
#    "should work via srsLTE" but unverified by upstream. Expect to
#    debug if you point it at HackRF.
#  * FDD only. TDD bands (38/40/41) do NOT decode.
#  * Needs i7-class CPU (4 physical cores, HT off) for 20 MHz; ~10
#    MHz on weaker hardware. Pi 5 will be marginal at 20 MHz.
if [[ ! -d "$SRC_DIR/falcon" ]]; then
  git clone --depth 1 https://github.com/falkenber9/falcon.git \
    "$SRC_DIR/falcon"
fi
mkdir -p "$SRC_DIR/falcon/build"
( cd "$SRC_DIR/falcon/build"
  # FALCON pulls its own srsLTE patch + c-mnalib as cmake subprojects.
  cmake .. -DUSE_GUI=False -DUSE_CAPTURE_PROBE=False || {
    echo "FALCON cmake failed — likely a dependency drift on a recent"
    echo "Ubuntu. The patched srsLTE 18.09 is old. Skipping for now;"
    echo "FALCON is optional (LTESniffer is the primary path)."
    exit 0
  }
  make -j"$(nproc)" FalconEye || {
    echo "FALCON make failed; FalconEye not available. See above."
    exit 0
  }
)
if [[ -x "$SRC_DIR/falcon/build/src/FalconEye" ]]; then
  echo "FalconEye binary at: $SRC_DIR/falcon/build/src/FalconEye"
  echo "  Use with: sniffer live --decoder falcon --earfcn N --pci P"
fi

echo "[4/5] DroneID decoders (alternative GPS source — pick one)"
# Two open-source DroneID decoders are supported in-tree:
#
#   * DroneSecurity (RUB-SysSec)   — USRP B2xx only, live receiver via UHD.
#                                    The most complete decoder; produces JSON
#                                    natively.
#   * samples2djidroneid (anarkiwi) — HackRF-compatible via the bundled
#                                    sniffer.droneid_hackrf capture loop. File-
#                                    based (Docker / Octave under the hood),
#                                    slower + lossier, but the only path that
#                                    runs on HackRF.
#
# We clone both. Building / running each is on the operator: DroneSecurity
# needs `sudo apt install libuhd-dev uhd-host python3-uhd`; samples2djidroneid
# needs Docker (or local Octave + the proto17 dji_droneid scripts).

if [[ ! -d "$SRC_DIR/DroneSecurity" ]]; then
  git clone --depth 1 https://github.com/RUB-SysSec/DroneSecurity.git \
    "$SRC_DIR/DroneSecurity"
fi
( cd "$SRC_DIR/DroneSecurity"
  # Requirements are pinned to old versions; install into the project venv.
  # The live receiver also needs UHD: `sudo apt install libuhd-dev uhd-host
  # python3-uhd`. We don't pull that automatically — gate on USRP hardware.
  echo "  DroneSecurity at $SRC_DIR/DroneSecurity"
  echo "    live (USRP):    ./src/droneid_receiver_live.py"
  echo "    offline (file): ./src/droneid_receiver_offline.py -i samples/mavic_air_2"
)

if [[ ! -d "$SRC_DIR/samples2djidroneid" ]]; then
  git clone --depth 1 https://github.com/anarkiwi/samples2djidroneid.git \
    "$SRC_DIR/samples2djidroneid"
fi
echo "  samples2djidroneid at $SRC_DIR/samples2djidroneid"
echo "    build the Docker image:"
echo "      ( cd $SRC_DIR/samples2djidroneid && docker build -f Dockerfile . -t samples2djidroneid )"
echo "    drive from HackRF (see sniffer.droneid_hackrf --help)"

echo "[5/5] Python environment"
PY_VENV=${PY_VENV:-$(pwd)/.venv}
python3 -m venv "$PY_VENV"
# shellcheck disable=SC1091
source "$PY_VENV/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
pip install -e .

cat <<EOF

Done. srsRAN example binaries installed to PATH (sudo make install):
  $(command -v srsran_cell_search 2>/dev/null || echo "srsran_cell_search (run \`hash -r\` or open a new shell)")
  $(command -v pdsch_ue           2>/dev/null || echo "pdsch_ue           (run \`hash -r\` or open a new shell)")

Quick sanity check:

  source $PY_VENV/bin/activate
  export PATH="$SRC_DIR/LTESniffer/build/src:\$PATH"
  sniffer live --simulate              # no hardware, synthetic UEs + GPS

Discover real cells (USRP B210 plugged in):

  sniffer scan --band 3                # quick sweep
  sniffer scan --band 3 --decode-sib1  # + PLMN / TAC / CGI

Then target one and stream UEs:

  sniffer live --earfcn 1850 --pci 271

DJI DroneID as alternative GPS source (no USB GPS / no MAVLink):

  # USRP B2xx (best path) — wrap DroneSecurity's live receiver:
  sniffer live --earfcn 1850 --pci 271 \\
    --droneid-cmd "python3 $SRC_DIR/DroneSecurity/src/droneid_receiver_live.py -g 40"

  # HackRF (best-effort) — one decoder per band:
  sniffer live --earfcn 1850 --pci 271 \\
    --droneid-cmd "python3 -m sniffer.droneid_hackrf --center-hz 2434500000 --decoder-cmd 'docker run --rm -v {iq_dir}:/data -i samples2djidroneid /data/{iq_name}'" \\
    --droneid-cmd "python3 -m sniffer.droneid_hackrf --center-hz 5771500000 --decoder-cmd 'docker run --rm -v {iq_dir}:/data -i samples2djidroneid /data/{iq_name}'"

EOF
