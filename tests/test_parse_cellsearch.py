import io
from argparse import Namespace

from sniffer.parse_cellsearch import parse_stream


SAMPLE = """\
Scanning EARFCN 1850 (1842.5 MHz)
Found LTE cell:
  Carrier frequency: 1842500000
  n_id_1: 90
  n_id_2: 1
  PCI: 271
  RSRP: -84.2 dBm
  RSRQ: -10.1 dB
  SNR: 12.3 dB
  Frame offset samples: 30412
  CP: normal

Some unrelated line
Found LTE cell:
  Carrier frequency: 1842500000
  PCI: 314
  RSRP: -91.0 dBm
  SNR: 6.2 dB
"""


def test_parses_two_cells():
    args = Namespace(
        mission_id="test-mission",
        backend="lte-cell-scanner",
        device="hackrf-0",
    )
    out = io.StringIO()
    n = parse_stream(io.StringIO(SAMPLE), args, out)
    assert n == 2
    lines = [line for line in out.getvalue().splitlines() if line]
    assert len(lines) == 2

    import json
    r0 = json.loads(lines[0])
    assert r0["cell"]["pci"] == 271
    assert r0["cell"]["n_id_1"] == 90
    assert r0["cell"]["rsrp_dbm"] == -84.2
    assert r0["cell"]["frame_offset_samples"] == 30412
    assert r0["radio"]["center_hz"] == 1842500000
    assert r0["mission_id"] == "test-mission"

    r1 = json.loads(lines[1])
    assert r1["cell"]["pci"] == 314
    assert r1["cell"]["rsrp_dbm"] == -91.0
    # missing fields stay None
    assert r1["cell"]["frame_offset_samples"] is None
