"""Unit tests for sniffer.sib1 — pdsch_ue wrapper + output parser."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniffer import sib1

FIXTURE = Path(__file__).parent / "fixtures" / "pdsch_ue_sib1.stdout"


def _fake_run(stdout: str, stderr: str = "", returncode: int = 0):
    def _runner(cmd, **kwargs):
        return SimpleNamespace(stdout=stdout, stderr=stderr,
                               returncode=returncode, args=cmd)
    return _runner


def test_parse_picks_up_space_separated_plmn_pair():
    info = sib1.parse_sib1_output(FIXTURE.read_text())
    assert info["plmn"] == "42501"
    assert info["tac"] == 18452
    assert info["cgi"] == 67305473


def test_parse_handles_mcc_mnc_equals_form():
    info = sib1.parse_sib1_output(
        "Decoded SIB1.\nMCC=425 MNC=01\nTAC=0x4814\ncell_identity=0x4031801\n"
    )
    assert info["plmn"] == "42501"
    assert info["tac"] == 0x4814
    assert info["cgi"] == 0x4031801


def test_parse_returns_empty_when_no_fields_match():
    assert sib1.parse_sib1_output("RF tuning failed\n") == {}


def test_decode_sib1_returns_none_when_binary_missing(monkeypatch, capsys):
    monkeypatch.setattr(sib1.shutil, "which", lambda _n: None)
    monkeypatch.setattr(sib1.os.path, "exists", lambda _p: False)
    info = sib1.decode_sib1(1850, 271)
    assert info is None
    err = capsys.readouterr().err
    assert "pdsch_ue" in err and "not on PATH" in err


def test_decode_sib1_returns_dict_when_output_parses(monkeypatch):
    monkeypatch.setattr(sib1.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(sib1.subprocess, "run",
                        _fake_run(FIXTURE.read_text()))
    info = sib1.decode_sib1(1850, 271)
    assert info is not None
    assert info["plmn"] == "42501"
    assert info["tac"] == 18452


def test_decode_sib1_returns_none_on_timeout(monkeypatch, capsys):
    monkeypatch.setattr(sib1.shutil, "which", lambda n: f"/usr/bin/{n}")

    def _raise_timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="pdsch_ue", timeout=1)
    monkeypatch.setattr(sib1.subprocess, "run", _raise_timeout)
    info = sib1.decode_sib1(1850, 271, timeout_s=1)
    assert info is None
    assert "timed out" in capsys.readouterr().err


def test_decode_sib1_returns_none_when_output_is_empty(monkeypatch):
    monkeypatch.setattr(sib1.shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(sib1.subprocess, "run",
                        _fake_run("RF tuning failed\n", returncode=1))
    info = sib1.decode_sib1(1850, 271)
    assert info is None  # empty parse → silent None, not {}
