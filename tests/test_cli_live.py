"""Smoke tests for `sniffer.cli._cmd_live` argv construction.

After consolidation to the single FalconEye runpath, `sniffer live`
always builds a `FalconEye -f <hz>` argv plus `--falcon-pci <pci>`,
never an `--ltesniffer-cmd`.
"""

from __future__ import annotations

from argparse import Namespace
from unittest import mock

import pytest

from sniffer import cli


def _ns(**over) -> Namespace:
    base = dict(
        host="127.0.0.1", port=8000, out_dir="data", mission_id="t",
        simulate=False, earfcn=None, pci=None,
        antennas=1, gain_db=70,
    )
    base.update(over)
    return Namespace(**base)


def test_cmd_live_builds_falcon_argv_for_band3(monkeypatch):
    """EARFCN 1850 (band 3) → FalconEye -f 1870000000, with PCI stamped
    via --falcon-pci."""
    monkeypatch.setattr(cli.shutil, "which",
                        lambda _n: "/usr/local/bin/FalconEye")
    captured = {}

    def fake_delegate(module: str, argv: list[str]) -> int:
        captured["module"] = module
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_delegate", fake_delegate)
    rc = cli._cmd_live(_ns(earfcn=1850, pci=271))
    assert rc == 0
    assert captured["module"] == "sniffer.live"

    argv = captured["argv"]
    idx = argv.index("--falcon-cmd")
    falcon_cmd = argv[idx + 1].split()
    # Real FalconEye CLI is `FalconEye -f <hz>` — no -I, no -A, no -W.
    assert falcon_cmd[0].endswith("FalconEye")
    assert "-f" in falcon_cmd
    assert falcon_cmd[falcon_cmd.index("-f") + 1] == "1870000000"
    # PCI is passed alongside (FALCON's CSV doesn't carry it).
    assert "--falcon-pci" in argv
    assert argv[argv.index("--falcon-pci") + 1] == "271"
    # The --center-hz forwarded to sniffer.live should match the same Hz:
    assert argv[argv.index("--center-hz") + 1] == "1870000000"

    # No LTESniffer flags must appear after consolidation:
    for bad in ("--ltesniffer-cmd", "--normalize-ltesniffer", "--decoder"):
        assert bad not in argv


def test_cmd_live_rejects_unknown_earfcn(monkeypatch, capsys):
    """If the user supplies an EARFCN we don't have band tables for,
    we fail fast with a clear error — not silently send 0 Hz."""
    monkeypatch.setattr(cli.shutil, "which",
                        lambda _n: "/usr/local/bin/FalconEye")
    rc = cli._cmd_live(_ns(earfcn=99999, pci=271))
    assert rc == 2
    err = capsys.readouterr().err
    assert "99999" in err


def test_cmd_live_simulate_does_not_build_falcon_cmd(monkeypatch):
    """--simulate path must not require --earfcn/--pci, and must not
    emit any --falcon-cmd argument."""
    captured = {}

    def fake_delegate(module: str, argv: list[str]) -> int:
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_delegate", fake_delegate)
    rc = cli._cmd_live(_ns(simulate=True))
    assert rc == 0
    assert "--simulate" in captured["argv"]
    assert "--falcon-cmd" not in captured["argv"]


def test_cmd_live_requires_earfcn_and_pci_when_not_simulating(capsys):
    rc = cli._cmd_live(_ns())  # no earfcn, no pci, no simulate
    assert rc == 2
    err = capsys.readouterr().err
    assert "--earfcn" in err and "--pci" in err


def test_cmd_live_returns_3_when_falcon_binary_missing(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    rc = cli._cmd_live(_ns(earfcn=1850, pci=271))
    assert rc == 3
    err = capsys.readouterr().err
    assert "FalconEye not on PATH" in err
