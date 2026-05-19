"""Unit tests for sniffer.scan.

We don't try to run srsran_cell_search itself — instead we monkeypatch
subprocess.run to return a recorded stdout fixture and assert the parser
extracts the expected (EARFCN, PCI, RSRP, CFO) rows.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniffer import scan

FIXTURE = Path(__file__).parent / "fixtures" / "srsran_cell_search.stdout"


def _fake_run(stdout: str, returncode: int = 0):
    def _runner(cmd, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr="",
                               returncode=returncode, args=cmd)
    return _runner


def test_parse_stream_extracts_earfcn_pci_rsrp_cfo():
    text = FIXTURE.read_text()
    cells = list(scan.parse_stream(text.splitlines()))
    # 3 cells in the fixture (two on 1850, one on 1800).
    assert len(cells) == 3
    by_pci = {c.pci: c for c in cells}

    assert by_pci[271].earfcn == 1800
    assert by_pci[271].rsrp_dbm == pytest.approx(-85.4)
    assert by_pci[271].cfo_hz == pytest.approx(-1234.5)

    assert by_pci[24].earfcn == 1850
    assert by_pci[24].rsrp_dbm == pytest.approx(-91.2)

    assert by_pci[378].earfcn == 1850
    assert by_pci[378].rsrp_dbm == pytest.approx(-105.7)


def test_parse_stream_handles_colon_separated_fields():
    text = "Found cell: PCI: 42, RSRP: -100.0, EARFCN: 2350\n"
    cells = list(scan.parse_stream(text.splitlines()))
    assert len(cells) == 1
    assert cells[0].pci == 42
    assert cells[0].earfcn == 2350
    assert cells[0].rsrp_dbm == pytest.approx(-100.0)


def test_parse_stream_handles_real_srsran_cell_search_output():
    """Real srsran_cell_search prints `PHYID=` (not `PCI=`) and uses
    `PSS power=` (with a space) for the signal-strength surrogate.
    Both were absent from the original fixture and the parser dropped
    those fields silently."""
    text = (Path(__file__).parent / "fixtures"
            / "srsran_cell_search_real.stdout").read_text()
    cells = list(scan.parse_stream(text.splitlines()))
    assert len(cells) == 2
    by_pci = {c.pci: c for c in cells}
    # Both cells must come out with PCI populated (via PHYID synonym).
    assert 257 in by_pci and 88 in by_pci
    # And with PSS-power-derived RSSI:
    assert by_pci[257].rsrp_dbm == pytest.approx(31.0)
    assert by_pci[88].rsrp_dbm == pytest.approx(18.4)
    # EARFCN comes from the same line, not the preceding sweep status:
    assert by_pci[257].earfcn == 6253
    assert by_pci[88].earfcn == 6300


def test_run_scan_returns_2_when_no_band_or_range():
    rc = scan.run_scan(fh=io.StringIO())
    assert rc == 2


def test_run_scan_returns_3_when_binary_missing(monkeypatch):
    monkeypatch.setattr(scan.shutil, "which", lambda _name: None)
    monkeypatch.setattr(scan.os.path, "exists", lambda _p: False)
    rc = scan.run_scan(band=3, fh=io.StringIO())
    assert rc == 3


def test_run_scan_jsonl_emits_one_row_per_cell(monkeypatch):
    monkeypatch.setattr(scan.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(scan.subprocess, "run",
                        _fake_run(FIXTURE.read_text()))
    fh = io.StringIO()
    rc = scan.run_scan(band=3, json_out=True, fh=fh)
    assert rc == 0
    rows = [json.loads(l) for l in fh.getvalue().strip().splitlines()]
    assert len(rows) == 3
    pcis = {r["pci"] for r in rows}
    assert pcis == {271, 24, 378}
    # No --decode-sib1 → no enrichment key.
    assert all("enrichment" not in r for r in rows)


def test_run_scan_table_lists_cells(monkeypatch):
    monkeypatch.setattr(scan.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(scan.subprocess, "run",
                        _fake_run(FIXTURE.read_text()))
    fh = io.StringIO()
    rc = scan.run_scan(band=3, fh=fh)
    assert rc == 0
    out = fh.getvalue()
    assert "EARFCN" in out
    assert "271" in out and "1800" in out
    assert "PLMN" not in out  # no enrichment column


def test_run_scan_decode_sib1_silently_skips_when_pdsch_ue_missing(monkeypatch):
    # cell_search binary exists; pdsch_ue does not. Scan should still produce
    # rows; absent enrichment is the warning surface.
    def which(name):
        return f"/usr/bin/{name}" if name == "srsran_cell_search" else None
    monkeypatch.setattr(scan.shutil, "which", which)
    monkeypatch.setattr(scan.subprocess, "run",
                        _fake_run(FIXTURE.read_text()))
    fh = io.StringIO()
    rc = scan.run_scan(band=3, decode_sib1=True, json_out=True, fh=fh)
    assert rc == 0
    rows = [json.loads(l) for l in fh.getvalue().strip().splitlines()]
    assert len(rows) == 3
    assert all("enrichment" not in r for r in rows)


def test_run_scan_propagates_subprocess_returncode(monkeypatch):
    monkeypatch.setattr(scan.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(scan.subprocess, "run",
                        _fake_run("no cells found\n", returncode=7))
    fh = io.StringIO()
    rc = scan.run_scan(band=3, fh=fh)
    assert rc == 7


def test_run_scan_decode_sib1_attaches_enrichment(monkeypatch):
    # cell_search returns 3 cells; pdsch_ue returns the SIB1 fixture for each.
    sib_path = Path(__file__).parent / "fixtures" / "pdsch_ue_sib1.stdout"
    sib_blob = sib_path.read_text()
    cell_blob = FIXTURE.read_text()

    monkeypatch.setattr(scan.shutil, "which", lambda n: f"/usr/bin/{n}")

    def fake_run(cmd, **kwargs):
        bin_name = Path(cmd[0]).name
        if bin_name == "srsran_cell_search":
            return SimpleNamespace(stdout=cell_blob, stderr="",
                                   returncode=0, args=cmd)
        return SimpleNamespace(stdout=sib_blob, stderr="",
                               returncode=0, args=cmd)
    monkeypatch.setattr(scan.subprocess, "run", fake_run)
    # The sib1 module is imported lazily inside run_scan; patch its
    # subprocess.run too via the same module reference.
    from sniffer import sib1
    monkeypatch.setattr(sib1.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(sib1.subprocess, "run", fake_run)

    fh = io.StringIO()
    rc = scan.run_scan(band=3, decode_sib1=True, json_out=True, fh=fh)
    assert rc == 0
    rows = [json.loads(l) for l in fh.getvalue().strip().splitlines()]
    assert len(rows) == 3
    for r in rows:
        assert r["enrichment"]["plmn"] == "42501"
        assert r["enrichment"]["tac"] == 18452
        assert r["enrichment"]["cgi"] == 67305473


def test_run_scan_timeout_returns_5(monkeypatch):
    monkeypatch.setattr(scan.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "x", timeout=1)
    monkeypatch.setattr(scan.subprocess, "run", _raise_timeout)
    fh = io.StringIO()
    rc = scan.run_scan(band=3, timeout_s=1, fh=fh)
    assert rc == 5
