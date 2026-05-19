#!/usr/bin/env bash
# End-to-end smoke test, intended to run on the Pi (or any Linux host
# with the same toolchain installed). Each phase prints PASS / FAIL
# with the expected output and a retry hint, then keeps going so you
# get a full picture in one run.
#
# Usage:
#   ./scripts/pi-smoke-test.sh              # all phases
#   ./scripts/pi-smoke-test.sh --no-radio   # skip phases that need HackRF
#   ./scripts/pi-smoke-test.sh --band 3 --pci 271 --earfcn 1850
#                                            # pin a specific cell
#
# Designed to be re-runnable. Survives missing optional pieces (GPS,
# USRP). The C-RNTI extraction phase is the one that must pass for
# the framework to be useful — everything else is supporting cast.

set -u   # NOT -e: we explicitly want to continue past failures.

BAND_DEFAULT=3
EARFCN_PIN=""
PCI_PIN=""
SKIP_RADIO=false
PORT=18900
DASHBOARD_URL="http://127.0.0.1:$PORT"
SURVEY_TOTAL_MIN=2
SURVEY_DWELL_S=10
SINGLE_CELL_S=20

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-radio) SKIP_RADIO=true; shift ;;
    --band) BAND="$2"; shift 2 ;;
    --earfcn) EARFCN_PIN="$2"; shift 2 ;;
    --pci) PCI_PIN="$2"; shift 2 ;;
    --port) PORT="$2"; DASHBOARD_URL="http://127.0.0.1:$PORT"; shift 2 ;;
    --total-min) SURVEY_TOTAL_MIN="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
BAND="${BAND:-$BAND_DEFAULT}"

# --- helpers ---------------------------------------------------------------

GREEN=$'\e[32m'; RED=$'\e[31m'; YEL=$'\e[33m'; DIM=$'\e[2m'; RST=$'\e[0m'
PASS_COUNT=0; FAIL_COUNT=0; SKIP_COUNT=0
FAILED_PHASES=()

phase() {
  echo
  echo "${DIM}────────────────────────────────────────${RST}"
  echo "${DIM}phase:${RST} $*"
}
pass() { echo "${GREEN}  PASS${RST} $*"; PASS_COUNT=$((PASS_COUNT+1)); }
fail() { echo "${RED}  FAIL${RST} $*"; FAIL_COUNT=$((FAIL_COUNT+1)); FAILED_PHASES+=("$*"); }
skip() { echo "${YEL}  SKIP${RST} $*"; SKIP_COUNT=$((SKIP_COUNT+1)); }
note() { echo "${DIM}        $*${RST}"; }

# Curl /state, return one snapshot's JSON; empty string on failure.
fetch_state() {
  curl -sS --max-time 3 "$DASHBOARD_URL/state" 2>/dev/null || echo ""
}

# Run a command in the background, wait until the dashboard responds.
start_bg() {
  local cmd="$1" name="$2" timeout="${3:-30}"
  echo "${DIM}        spawning: $cmd${RST}"
  eval "$cmd" >/tmp/${name}.log 2>&1 &
  local pid=$!
  for _ in $(seq 1 $timeout); do
    if curl -sS --max-time 1 "$DASHBOARD_URL/state" >/dev/null 2>&1; then
      echo "$pid"
      return 0
    fi
    sleep 1
  done
  # Failed to come up
  kill "$pid" 2>/dev/null || true
  return 1
}

# Stop a backgrounded sniffer cleanly.
stop_bg() {
  local pid="$1"
  [[ -n "${pid:-}" ]] || return 0
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 5); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
}

# --- phase 0: environment --------------------------------------------------

phase "0. Environment"
if [[ -f /etc/os-release ]]; then
  . /etc/os-release
  note "OS: ${PRETTY_NAME:-unknown}"
fi
note "arch: $(uname -m)"
note "kernel: $(uname -r)"
note "user: $(whoami)"
note "python: $(python3 --version 2>&1)"

# --- phase 1: code sanity (no hardware) ------------------------------------

phase "1. Code sanity — imports + CLI parse"
if python3 -c "
import sniffer.cli, sniffer.live, sniffer.scan, sniffer.sib1, sniffer.survey
import sniffer.falcon, sniffer.parse_ltesniffer, sniffer.parse_gpsd
import sniffer.parse_droneid, sniffer.droneid_hackrf, sniffer.spectrum
import sniffer.localize, sniffer.ta_multilateration
import sniffer.simulate, sniffer.schema, sniffer.lte_bands
" 2>&1; then
  pass "all sniffer.* modules import"
else
  fail "module import failed — fix Python env before continuing"
fi
for sub in scan survey live install; do
  if python3 -m sniffer.cli "$sub" --help >/dev/null 2>&1; then
    pass "sniffer $sub --help parses"
  else
    fail "sniffer $sub --help failed"
  fi
done

# --- phase 2: unit + integration tests -------------------------------------

phase "2. pytest"
if python3 -m pytest tests/ -q 2>&1 | tail -3 | tee /tmp/pytest.log \
     | grep -qE "[0-9]+ passed"; then
  PASSED=$(grep -oE "[0-9]+ passed" /tmp/pytest.log | head -1)
  pass "$PASSED"
else
  fail "pytest failed — see /tmp/pytest.log"
fi

# --- phase 3: simulator smoke (no radio) -----------------------------------

phase "3. Simulator smoke — sniffer live --simulate"
SIM_PID=$(start_bg "python3 -m sniffer.live --simulate --port $PORT \
                    --out-dir /tmp/sniffer-sim" "sim" 30 || true)
if [[ -z "${SIM_PID:-}" ]]; then
  fail "dashboard didn't come up on $DASHBOARD_URL within 30s "
  note "  check /tmp/sim.log"
else
  # Wait for sim to accumulate UEs.
  sleep 8
  UE_COUNT=$(fetch_state | python3 -c "
import sys, json
try: print(len(json.load(sys.stdin).get('ues', [])))
except Exception: print(0)
")
  if [[ "$UE_COUNT" -ge 3 ]]; then
    pass "simulator dashboard shows $UE_COUNT UEs"
  else
    fail "expected ≥3 simulated UEs, got $UE_COUNT"
  fi
  stop_bg "$SIM_PID"
fi

# --- phase 4: hardware detection ------------------------------------------

phase "4. Radio hardware"
if $SKIP_RADIO; then
  skip "--no-radio flag set; not checking SDRs"
else
  if command -v hackrf_info >/dev/null 2>&1; then
    if hackrf_info 2>&1 | grep -q "Serial number"; then
      SERIAL=$(hackrf_info 2>&1 | grep "Serial number" | head -1 | awk '{print $3}')
      pass "HackRF detected (serial: $SERIAL)"
    else
      fail "hackrf_info found no HackRF — is it plugged in?"
      note "  check: lsusb | grep 1d50:6089"
    fi
  else
    skip "hackrf_info not installed (apt install hackrf)"
  fi

  if command -v uhd_find_devices >/dev/null 2>&1; then
    if timeout 5 uhd_find_devices 2>&1 | grep -q "Device Address"; then
      pass "USRP detected via UHD"
    else
      skip "no USRP found (or UHD daemon not running)"
    fi
  else
    skip "uhd_find_devices not installed (apt install uhd-host)"
  fi
fi

# --- phase 5: decoder binaries on PATH ------------------------------------

phase "5. Decoder binaries"
for bin in srsran_cell_search FalconEye LTESniffer hackrf_sweep; do
  if command -v "$bin" >/dev/null 2>&1; then
    pass "$bin -> $(command -v $bin)"
  else
    fail "$bin not on PATH — run \`sniffer install\` (Linux)"
  fi
done

# --- phase 6: spectrum sweep (HackRF, optional) ---------------------------

phase "6. Spectrum sweep — hackrf_sweep"
if $SKIP_RADIO; then
  skip "--no-radio set"
elif ! command -v hackrf_sweep >/dev/null 2>&1; then
  skip "hackrf_sweep not installed"
else
  # 5-second sweep across 1800-1900 MHz; should print at least one CSV row.
  ROWS=$(timeout 8 hackrf_sweep -f 1800:1900 2>/dev/null | head -100 | wc -l)
  if [[ "$ROWS" -ge 10 ]]; then
    pass "hackrf_sweep produced $ROWS rows (>10 = healthy)"
  else
    fail "hackrf_sweep produced only $ROWS rows; check HackRF + permissions"
  fi
fi

# --- phase 7: cell discovery — sniffer scan -------------------------------

phase "7. Cell discovery — sniffer scan --band $BAND"
if $SKIP_RADIO; then
  skip "--no-radio set"
elif ! command -v srsran_cell_search >/dev/null 2>&1; then
  skip "srsran_cell_search not installed"
else
  # JSONL output; one cell per line; need at least one for survey to work.
  SCAN_OUT=$(timeout 60 python3 -m sniffer.cli scan --band "$BAND" --jsonl 2>/dev/null)
  CELLS_FOUND=$(echo "$SCAN_OUT" | grep -c "earfcn" || true)
  if [[ "$CELLS_FOUND" -ge 1 ]]; then
    pass "scan found $CELLS_FOUND cell(s) on band $BAND"
    # Stash first cell for the C-RNTI test, if user didn't pin one.
    if [[ -z "$EARFCN_PIN" ]]; then
      FIRST_CELL=$(echo "$SCAN_OUT" | head -1)
      EARFCN_PIN=$(echo "$FIRST_CELL" | python3 -c "
import sys, json
try: print(json.loads(sys.stdin.read())['earfcn'])
except Exception: pass
")
      PCI_PIN=$(echo "$FIRST_CELL" | python3 -c "
import sys, json
try: print(json.loads(sys.stdin.read())['pci'])
except Exception: pass
")
      note "auto-picked: EARFCN=$EARFCN_PIN PCI=$PCI_PIN"
    fi
  else
    fail "scan found 0 cells on band $BAND — try another band or check antenna"
    note "  full output:"; echo "$SCAN_OUT" | sed 's/^/    /'
  fi
fi

# --- phase 8: C-RNTI extraction (single cell) — THE CORE TEST -------------

phase "8. C-RNTI extraction (single cell) — sniffer live --decoder falcon"
if $SKIP_RADIO; then
  skip "--no-radio set"
elif [[ -z "$EARFCN_PIN" || -z "$PCI_PIN" ]]; then
  skip "no cell to target (phase 7 found none, none pinned via --earfcn/--pci)"
elif ! command -v FalconEye >/dev/null 2>&1; then
  fail "FalconEye not installed — this is the working decoder. \`sniffer install\` should build it; if it fails, see scripts/install-linux.sh phase 3.5."
else
  LIVE_PID=$(start_bg "python3 -m sniffer.cli live --decoder falcon \
                       --earfcn $EARFCN_PIN --pci $PCI_PIN --port $PORT \
                       --out-dir /tmp/sniffer-live" "live" 30 || true)
  if [[ -z "${LIVE_PID:-}" ]]; then
    fail "dashboard didn't come up — see /tmp/live.log"
  else
    note "dwelling on cell for ${SINGLE_CELL_S}s, accumulating C-RNTIs..."
    sleep "$SINGLE_CELL_S"
    UE_COUNT=$(fetch_state | python3 -c "
import sys, json
try: print(len(json.load(sys.stdin).get('ues', [])))
except Exception: print(0)
")
    TOTAL_SIGHTINGS=$(fetch_state | python3 -c "
import sys, json
try: print(json.load(sys.stdin).get('total_ue_sightings', 0))
except Exception: print(0)
")
    if [[ "$UE_COUNT" -ge 1 ]]; then
      pass "extracted $UE_COUNT C-RNTI(s), $TOTAL_SIGHTINGS total grants"
      note "dashboard: $DASHBOARD_URL"
      note "JSONL on disk: /tmp/sniffer-live/ue-*.jsonl"
    else
      fail "0 C-RNTIs after ${SINGLE_CELL_S}s. Causes:"
      note "  • cell EARFCN=$EARFCN_PIN PCI=$PCI_PIN may have no active UEs right now"
      note "  • FalconEye may not be locking — check /tmp/live.log"
      note "  • HackRF antenna placement / band mismatch"
    fi
    stop_bg "$LIVE_PID"
  fi
fi

# --- phase 9: multi-cell survey (the headline workflow) -------------------

phase "9. Multi-cell survey — sniffer survey --band $BAND"
if $SKIP_RADIO; then
  skip "--no-radio set"
elif ! command -v FalconEye >/dev/null 2>&1 || ! command -v srsran_cell_search >/dev/null 2>&1; then
  skip "need both FalconEye + srsran_cell_search"
else
  SURVEY_PID=$(start_bg "python3 -m sniffer.cli survey --band $BAND \
                          --dwell-seconds $SURVEY_DWELL_S \
                          --total-minutes $SURVEY_TOTAL_MIN \
                          --port $PORT \
                          --out-dir /tmp/sniffer-survey" "survey" 60 || true)
  if [[ -z "${SURVEY_PID:-}" ]]; then
    fail "survey dashboard didn't come up — see /tmp/survey.log"
  else
    note "running survey for ${SURVEY_TOTAL_MIN}min — open $DASHBOARD_URL to watch"
    note "expect: SURVEYING banner cycling through cells, C-RNTIs accumulating"
    SLEEP_S=$((SURVEY_TOTAL_MIN * 60 + 15))
    sleep "$SLEEP_S"
    UE_COUNT=$(fetch_state | python3 -c "
import sys, json
try: print(len(json.load(sys.stdin).get('ues', [])))
except Exception: print(0)
")
    PCI_COUNT=$(fetch_state | python3 -c "
import sys, json
try:
  pcis = {u['pci'] for u in json.load(sys.stdin).get('ues', [])}
  print(len(pcis))
except Exception: print(0)
")
    if [[ "$UE_COUNT" -ge 1 && "$PCI_COUNT" -ge 1 ]]; then
      pass "survey gathered $UE_COUNT C-RNTI(s) across $PCI_COUNT PCI(s)"
      note "with longer dwell + healthier cells you should see many more"
      note "JSONL: /tmp/sniffer-survey/ue-*.jsonl"
    else
      fail "survey gathered $UE_COUNT C-RNTIs / $PCI_COUNT PCIs — too few"
      note "  • is the cell active? quiet cells produce no DCIs"
      note "  • check /tmp/survey.log for FalconEye build / lock issues"
    fi
    stop_bg "$SURVEY_PID"
  fi
fi

# --- phase 10: GPS (optional) ---------------------------------------------

phase "10. GPS — gpsd (optional)"
if $SKIP_RADIO; then
  skip "--no-radio set"
elif ! command -v gpspipe >/dev/null 2>&1; then
  skip "gpspipe not installed (apt install gpsd-clients) — not required"
elif ! systemctl is-active --quiet gpsd 2>/dev/null && ! pgrep -x gpsd >/dev/null 2>&1; then
  skip "gpsd daemon not running — not required"
else
  if timeout 5 gpspipe -w -n 5 2>/dev/null | grep -q '"class":"TPV"'; then
    pass "gpsd publishing TPV (position) messages"
  else
    skip "gpsd up but not yet locked — needs sky view"
  fi
fi

# --- summary --------------------------------------------------------------

echo
echo "${DIM}══════════════════════════════════════════${RST}"
echo "summary: ${GREEN}$PASS_COUNT pass${RST}, ${RED}$FAIL_COUNT fail${RST}, ${YEL}$SKIP_COUNT skip${RST}"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  echo "${RED}failures:${RST}"
  for f in "${FAILED_PHASES[@]}"; do echo "  • $f"; done
fi
echo "logs: /tmp/sim.log /tmp/live.log /tmp/survey.log /tmp/pytest.log"
echo
exit "$FAIL_COUNT"
