"""Unit tests for sniffer.droneid_hackrf — the HackRF capture-loop wrapper."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from sniffer import droneid_hackrf


def _fake_capture_writes_file(returncode: int = 0,
                              raw_bytes: bytes = b"\x10\x20" * 512):
    """Return a subprocess.run stub that writes raw IQ to the -r path."""
    def _runner(cmd, **kw):
        # Find the path after `-r`
        if "-r" in cmd:
            i = cmd.index("-r")
            with open(cmd[i + 1], "wb") as fh:
                fh.write(raw_bytes)
        return SimpleNamespace(stdout="", stderr="", returncode=returncode,
                               args=cmd)
    return _runner


def _fake_decoder(stdout_text: str, returncode: int = 0):
    """Return a subprocess.run stub for the decoder leg."""
    def _runner(cmd, **kw):
        return SimpleNamespace(stdout=stdout_text, stderr="",
                               returncode=returncode, args=cmd)
    return _runner


def test_main_capture_decode_once_streams_decoder_stdout(monkeypatch, capsys):
    """Smoke: capture → convert → decode → stdout, no real hardware."""
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd[0])
        if "hackrf_transfer" in cmd[0]:
            # Write a tiny i8 IQ file.
            i = cmd.index("-r")
            with open(cmd[i + 1], "wb") as fh:
                fh.write(b"\x10\x20" * 256)
            return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)
        # decoder leg
        return SimpleNamespace(
            stdout='{"latitude":51.5,"longitude":7.3,"altitude":40.0}\n',
            stderr="", returncode=0, args=cmd)

    monkeypatch.setattr(droneid_hackrf.subprocess, "run", runner)

    rc = droneid_hackrf.main([
        "--center-hz", "2434500000",
        "--sample-rate", "256",            # tiny for fast test
        "--chunk-seconds", "1",
        "--decoder-cmd", "/usr/bin/fake_decoder {iq}",
        "--once",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "51.5" in out and "7.3" in out
    # Both legs were invoked.
    assert any("hackrf_transfer" in c for c in calls)
    assert any("fake_decoder" in c for c in calls)


def test_main_handles_decoder_timeout(monkeypatch, capsys):
    """Decoder timeout becomes a stderr warning, no JSON to stdout."""
    def runner(cmd, **kw):
        if "hackrf_transfer" in cmd[0]:
            i = cmd.index("-r")
            with open(cmd[i + 1], "wb") as fh:
                fh.write(b"\x00\x00" * 16)
            return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)
        raise subprocess.TimeoutExpired(cmd=cmd[0], timeout=0.1)

    monkeypatch.setattr(droneid_hackrf.subprocess, "run", runner)

    rc = droneid_hackrf.main([
        "--center-hz", "2434500000",
        "--sample-rate", "16",
        "--chunk-seconds", "1",
        "--decoder-cmd", "/usr/bin/fake_decoder {iq}",
        "--decoder-timeout-s", "0.1",
        "--once",
    ])
    assert rc == 0
    err = capsys.readouterr()
    assert "timed out" in err.err
    assert err.out == ""  # no JSON


def test_main_substitutes_iq_dir_and_iq_name(monkeypatch):
    """{iq_dir} / {iq_name} substitution works for Docker-style mounts."""
    seen_cmds = []

    def runner(cmd, **kw):
        seen_cmds.append(list(cmd))
        if "hackrf_transfer" in cmd[0]:
            i = cmd.index("-r")
            with open(cmd[i + 1], "wb") as fh:
                fh.write(b"\x00\x01" * 8)
            return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)
        return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)

    monkeypatch.setattr(droneid_hackrf.subprocess, "run", runner)

    droneid_hackrf.main([
        "--center-hz", "5771500000",
        "--sample-rate", "8",
        "--chunk-seconds", "1",
        "--decoder-cmd", "docker run -v {iq_dir}:/data img /data/{iq_name}",
        "--once",
    ])

    decoder_call = [c for c in seen_cmds if "docker" in c[0]][0]
    # Find the two substituted tokens
    args_str = " ".join(decoder_call)
    assert "/data/chunk.cf32" in args_str
    # The iq_dir should be an absolute path containing the temp dir name
    assert "-v" in decoder_call
    v_arg = decoder_call[decoder_call.index("-v") + 1]
    assert ":/data" in v_arg
    host_dir = v_arg.split(":")[0]
    assert os.path.isabs(host_dir)


def test_main_passes_hackrf_serial_and_gains(monkeypatch):
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        if "hackrf_transfer" in cmd[0]:
            i = cmd.index("-r")
            with open(cmd[i + 1], "wb") as fh:
                fh.write(b"\x00\x00" * 8)
            return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)
        return SimpleNamespace(stdout="", stderr="", returncode=0, args=cmd)

    monkeypatch.setattr(droneid_hackrf.subprocess, "run", runner)

    droneid_hackrf.main([
        "--center-hz", "2434500000",
        "--sample-rate", "8",
        "--chunk-seconds", "1",
        "--decoder-cmd", "/bin/true {iq}",
        "--device-serial", "DEADBEEF",
        "--rx-gain-db", "40",
        "--lna-gain-db", "24",
        "--once",
    ])
    capture = [c for c in seen if "hackrf_transfer" in c[0]][0]
    assert "-d" in capture and "DEADBEEF" in capture
    assert "-g" in capture and "40" in capture
    assert "-l" in capture and "24" in capture
