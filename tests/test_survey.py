"""Tests for sniffer.survey — the multi-cell sweep orchestrator.

We never actually spawn FalconEye here. Instead we monkey-patch
`_spawn_decoder` to return a stub subprocess and let the FALCON CSV
tailer read from a file we write to ourselves. That gives us
end-to-end coverage of the cycling logic, dwell timing, status
broadcasts, and subprocess cleanup, without depending on real radio.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from types import SimpleNamespace
from typing import Optional

import pytest

from sniffer import survey
from sniffer.live import State


class _FakeProc:
    """A subprocess.Popen stand-in. Stays alive until terminate()."""

    def __init__(self):
        self._alive = True
        self._lock = threading.Lock()

    def poll(self):
        with self._lock:
            return None if self._alive else 0

    def terminate(self):
        with self._lock:
            self._alive = False

    def wait(self, timeout=None):
        # Block briefly so the caller's terminate→wait pattern works.
        for _ in range(int((timeout or 1.0) * 100)):
            with self._lock:
                if not self._alive:
                    return 0
            time.sleep(0.01)
        return 0

    def kill(self):
        self.terminate()

    stdout = None
    stderr = None


def _cell(earfcn=1850, pci=271, center_hz=1_870_000_000):
    return survey.SurveyCell(earfcn=earfcn, pci=pci, center_hz=center_hz)


def test_run_survey_loop_visits_each_cell_per_cycle(monkeypatch, tmp_path):
    """Two cells, dwell=0.2s, total=0.5s → should visit each cell at
    least once and broadcast a survey-status payload for each."""
    visited: list[tuple[int, int]] = []

    def spawn(decoder, cell, *_a, **_kw):
        visited.append((cell.earfcn, cell.pci))
        # Touch the CSV so tail_csv finds it immediately.
        from sniffer import falcon  # noqa
        return _FakeProc()

    monkeypatch.setattr(survey, "_spawn_decoder", spawn)
    # Replace the consume step with a no-op so dwell is purely timer-driven.
    monkeypatch.setattr(survey, "_consume_cell_dwell",
                        lambda *a, **kw: kw["dwell_stop"].wait())

    state = State()
    survey_events: list[dict] = []
    orig_broadcast = state._broadcast
    def capture(ev):
        if ev.get("type") == "survey":
            survey_events.append(ev.get("survey"))
        return orig_broadcast(ev)
    state._broadcast = capture

    stop = threading.Event()
    cells = [_cell(1850, 271), _cell(1851, 88)]
    survey.run_survey_loop(state, cells=cells,
                           dwell_seconds=0.2, total_seconds=0.5,
                           decoder="falcon", mission_id="t",
                           out_dir=str(tmp_path), stop=stop)

    # Both cells were visited at least once.
    pcis_visited = {pci for _, pci in visited}
    assert 271 in pcis_visited
    assert 88 in pcis_visited

    # Survey-status events landed for each visit (and a "done" status
    # broadcast as a regular status, not a 'survey' event).
    payloads_with_a_pci = [p for p in survey_events if p and p.get("current_pci")]
    assert len(payloads_with_a_pci) >= 2


def test_run_survey_loop_exits_when_stop_fires(monkeypatch, tmp_path):
    """User kills the dashboard mid-survey → loop returns promptly."""

    def spawn(*_a, **_kw):
        return _FakeProc()

    monkeypatch.setattr(survey, "_spawn_decoder", spawn)
    # Block forever in the dwell consumer — only the parent stop should
    # unblock us.
    def block(*a, **kw):
        kw["dwell_stop"].wait()
    monkeypatch.setattr(survey, "_consume_cell_dwell", block)

    state = State()
    stop = threading.Event()
    cells = [_cell(1850, 271)] * 5

    def killer():
        time.sleep(0.1)
        stop.set()
    threading.Thread(target=killer, daemon=True).start()

    t0 = time.monotonic()
    survey.run_survey_loop(state, cells=cells,
                           dwell_seconds=60.0, total_seconds=60.0,
                           decoder="falcon", mission_id="t",
                           out_dir=str(tmp_path), stop=stop)
    elapsed = time.monotonic() - t0
    # Should exit within a fraction of a second after stop fires.
    assert elapsed < 2.0


def test_run_survey_loop_terminates_subprocess_on_dwell_end(monkeypatch, tmp_path):
    """Every spawned subprocess must be terminated before we move to
    the next cell. No leaked decoders."""
    spawned: list[_FakeProc] = []

    def spawn(*_a, **_kw):
        p = _FakeProc()
        spawned.append(p)
        return p

    monkeypatch.setattr(survey, "_spawn_decoder", spawn)
    monkeypatch.setattr(survey, "_consume_cell_dwell",
                        lambda *a, **kw: kw["dwell_stop"].wait())

    state = State()
    stop = threading.Event()
    cells = [_cell(1850, 271), _cell(1851, 88), _cell(1852, 42)]
    survey.run_survey_loop(state, cells=cells,
                           dwell_seconds=0.1, total_seconds=0.4,
                           decoder="falcon", mission_id="t",
                           out_dir=str(tmp_path), stop=stop)

    # All but possibly the very last spawned process should be terminated.
    # In practice every one should be, since terminate runs in finally.
    for p in spawned:
        assert p.poll() == 0  # i.e. not alive


def test_run_survey_loop_handles_empty_cell_list(tmp_path):
    state = State()
    errors: list[str] = []
    orig = state.set_status
    def capture(phase, **kw):
        errors.append(phase + ":" + kw.get("message", ""))
        return orig(phase, **kw)
    state.set_status = capture
    stop = threading.Event()
    survey.run_survey_loop(state, cells=[],
                           dwell_seconds=1.0, total_seconds=1.0,
                           decoder="falcon", mission_id="t",
                           out_dir=str(tmp_path), stop=stop)
    assert any("empty cell list" in e for e in errors)


def test_run_survey_loop_unknown_decoder_raises_during_spawn(monkeypatch,
                                                              tmp_path):
    """_spawn_decoder rejects unknown decoders; survey surfaces the
    error via State.set_status and returns rather than looping forever."""
    # Use the real _spawn_decoder so we hit its ValueError path. To
    # avoid actually trying to find the binary, monkey out subprocess.Popen.
    monkeypatch.setattr(survey.subprocess, "Popen",
                        lambda *a, **kw: _FakeProc())
    state = State()
    errors: list[str] = []
    def capture_status(phase, **kw):
        errors.append(phase + ":" + str(kw.get("message", "")))
    state.set_status = capture_status
    stop = threading.Event()
    survey.run_survey_loop(state, cells=[_cell()],
                           dwell_seconds=0.5, total_seconds=1.0,
                           decoder="not-a-real-decoder",
                           mission_id="t", out_dir=str(tmp_path),
                           stop=stop)
    assert any("spawn failed" in e for e in errors)


def test_state_set_survey_status_broadcasts_and_clears():
    """State exposes set_survey_status, broadcasts on every call, and
    accepts None to clear."""
    state = State()
    seen: list[Optional[dict]] = []
    orig = state._broadcast
    def cap(ev):
        if ev.get("type") == "survey":
            seen.append(ev.get("survey"))
        return orig(ev)
    state._broadcast = cap

    state.set_survey_status({"phase": "dwelling", "cell_idx": 1})
    state.set_survey_status({"phase": "dwelling", "cell_idx": 2})
    state.set_survey_status(None)

    assert len(seen) == 3
    assert seen[0]["cell_idx"] == 1
    assert seen[1]["cell_idx"] == 2
    assert seen[2] is None
