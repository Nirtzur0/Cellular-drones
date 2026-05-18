import io
import json
from argparse import Namespace

from sniffer.parse_cellsearch import parse_stream


# Two realtime detection blocks (one full, one without k_factor / RX power)
# followed by the end-of-scan summary table. Mirrors what the JiaoXianjun
# LTE-Cell-Scanner fork prints on stdout for a HackRF scan.
SAMPLE = """\
Scanning EARFCN 1850 (1842.5 MHz)
Detected a FDD cell! At freqeuncy 1842.5MHz, try 0
  cell ID: 271
   PSS ID: 1
  RX power level: -84.2 dB
  residual frequency offset: 234.1 Hz
                   k_factor: 0.99999987

Detected a FDD cell! At freqeuncy 1842.5MHz, try 1
  cell ID: 314
   PSS ID: 2
  RX power level: -91.0 dB
  residual frequency offset: -110.5 Hz
                   k_factor: 1.00000005

Detected the following cells:
DPX:TDD/FDD; A: #antenna ports; CP: normal/extended; PR: PHICH resource
DPX CID A      fc   freq-offset RXPWR C nRB P  PR CrystalCorrectionFactor
FDD 271  2  1842.5M   234.1Hz   -84.2 N  50 N 1/6 0.99999987
FDD 314  1  1842.5M   -110.5Hz  -91.0 E  25 N 1/2 1.00000005
"""


def _args():
    return Namespace(
        mission_id="test-mission",
        backend="lte-cell-scanner",
        device="hackrf-0",
        rx_gain_db=40.0,
    )


def test_parses_realtime_blocks_and_summary():
    out = io.StringIO()
    n = parse_stream(io.StringIO(SAMPLE), _args(), out)
    # 2 realtime detections + 2 summary rows = 4 records.
    assert n == 4
    records = [json.loads(line) for line in out.getvalue().splitlines() if line]
    assert len(records) == 4

    # First two come from the realtime blocks (PCI 271 then 314).
    rt0, rt1, sm0, sm1 = records

    assert rt0["cell"]["pci"] == 271
    assert rt0["cell"]["n_id_2"] == 1
    assert rt0["cell"]["mode"] == "fdd"
    assert rt0["cell"]["rsrp_dbm"] == -84.2
    assert rt0["radio"]["center_hz"] == 1_842_500_000.0
    assert rt0["cell"]["mib"]["residual_freq_offset_hz"] == 234.1
    assert rt0["cell"]["mib"]["k_factor"] == 0.99999987
    assert rt0["notes"] == "source=realtime"

    assert rt1["cell"]["pci"] == 314
    assert rt1["cell"]["n_id_2"] == 2
    assert rt1["cell"]["rsrp_dbm"] == -91.0
    assert rt1["notes"] == "source=realtime"

    # Summary rows add the bandwidth/n_ports/CP that the realtime block lacks.
    assert sm0["cell"]["pci"] == 271
    assert sm0["cell"]["n_ports"] == 2
    assert sm0["cell"]["cp"] == "normal"
    assert sm0["cell"]["mib"]["n_rb_dl"] == 50
    assert sm0["cell"]["mib"]["crystal_correction"] == 0.99999987
    assert sm0["notes"] == "source=summary"

    assert sm1["cell"]["pci"] == 314
    assert sm1["cell"]["n_ports"] == 1
    assert sm1["cell"]["cp"] == "extended"
    assert sm1["cell"]["mib"]["n_rb_dl"] == 25


def test_human_stream_emits_one_line_per_record():
    human = io.StringIO()
    parse_stream(io.StringIO(SAMPLE), _args(), io.StringIO(), human_stream=human)
    lines = [ln for ln in human.getvalue().splitlines() if ln]
    assert len(lines) == 4
    assert any("PCI=271" in ln and "realtime" in ln for ln in lines)
    assert any("PCI=271" in ln and "summary" in ln for ln in lines)
