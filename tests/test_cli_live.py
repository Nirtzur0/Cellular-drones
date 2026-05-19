"""Smoke tests for `sniffer.cli._cmd_live` argv construction.

Critical because LTESniffer's CLI uses positional flags (-A, -W, -f, -I,
-m, -a) — previous code passed invented long flags (--earfcn,
--target-pci, --rx-gain) that LTESniffer rejects immediately.
"""

from __future__ import annotations

from argparse import Namespace
from unittest import mock

import pytest

from sniffer import cli


def _ns(**over) -> Namespace:
    base = dict(
        host="127.0.0.1", port=8000, out_dir="data", mission_id="t",
        droneid_cmd=None, droneid_serial=None,
        simulate=False, earfcn=None, pci=None,
        antennas=2, threads=4,
    )
    base.update(over)
    return Namespace(**base)


def test_cmd_live_builds_correct_ltesniffer_argv_for_band3(monkeypatch):
    """EARFCN 1850 (band 3) → -f 1870000000. Flags -A, -W, -I, -m, -a
    are the real ones from LTESniffer's README."""
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/local/bin/LTESniffer")
    captured = {}

    def fake_delegate(module: str, argv: list[str]) -> int:
        captured["module"] = module
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_delegate", fake_delegate)
    rc = cli._cmd_live(_ns(earfcn=1850, pci=271))
    assert rc == 0
    assert captured["module"] == "sniffer.live"

    # Find the --ltesniffer-cmd arg and split into its components.
    argv = captured["argv"]
    idx = argv.index("--ltesniffer-cmd")
    ltecmd = argv[idx + 1].split()

    # Expected: ["LTESniffer", "-A", "2", "-W", "4", "-f", "1870000000",
    #            "-I", "271", "-m", "0", "-a", "num_recv_frames=512"]
    assert ltecmd[0].endswith("LTESniffer")
    assert "-A" in ltecmd and ltecmd[ltecmd.index("-A") + 1] == "2"
    assert "-W" in ltecmd and ltecmd[ltecmd.index("-W") + 1] == "4"
    assert "-f" in ltecmd and ltecmd[ltecmd.index("-f") + 1] == "1870000000"
    assert "-I" in ltecmd and ltecmd[ltecmd.index("-I") + 1] == "271"
    assert "-m" in ltecmd and ltecmd[ltecmd.index("-m") + 1] == "0"
    assert "-a" in ltecmd and ltecmd[ltecmd.index("-a") + 1] == "num_recv_frames=512"
    # Invented flags must NOT appear:
    for bad in ("--earfcn", "--target-pci", "--rx-gain", "--pci"):
        assert bad not in ltecmd

    # The --center-hz forwarded to sniffer.live should match the same Hz:
    chz_idx = argv.index("--center-hz")
    assert argv[chz_idx + 1] == "1870000000"


def test_cmd_live_rejects_unknown_earfcn(monkeypatch, capsys):
    """If the user supplies an EARFCN we don't have band tables for,
    we fail fast with a clear error — not silently send 0 Hz."""
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/local/bin/LTESniffer")
    rc = cli._cmd_live(_ns(earfcn=99999, pci=271))
    assert rc == 2
    err = capsys.readouterr().err
    assert "99999" in err


def test_cmd_live_simulate_does_not_build_ltesniffer_cmd(monkeypatch):
    """--simulate path must not require --earfcn/--pci, and must not
    emit any --ltesniffer-cmd argument."""
    captured = {}

    def fake_delegate(module: str, argv: list[str]) -> int:
        captured["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "_delegate", fake_delegate)
    rc = cli._cmd_live(_ns(simulate=True))
    assert rc == 0
    assert "--simulate" in captured["argv"]
    assert "--ltesniffer-cmd" not in captured["argv"]


def test_cmd_live_requires_earfcn_and_pci_when_not_simulating(capsys):
    rc = cli._cmd_live(_ns())  # no earfcn, no pci, no simulate
    assert rc == 2
    err = capsys.readouterr().err
    assert "--earfcn" in err and "--pci" in err


def test_cmd_live_returns_3_when_ltesniffer_binary_missing(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda _n: None)
    rc = cli._cmd_live(_ns(earfcn=1850, pci=271))
    assert rc == 3
    err = capsys.readouterr().err
    assert "LTESniffer not on PATH" in err
