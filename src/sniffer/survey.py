"""Multi-cell sweep + dwell orchestrator.

C-RNTI is cell-scoped — a single-cell sniffer (LTESniffer / FalconEye)
only enumerates UEs on that one PCI. To get a *census* of UEs visible
from the airspace, you have to visit each cell in turn. This module is
the "visit each cell in turn" loop.

Architecture:

    sniffer scan                                (one-shot, finds cells)
         │
         ▼
    cells = [(earfcn1, pci1), (earfcn2, pci2), ...]
         │
         ▼
    for each cycle until total_seconds elapsed:
      for each cell in cells:
        spawn FalconEye on cell             ┐
        tail its DCI CSV → State sink       │  for dwell_seconds
        SIGTERM the subprocess              ┘
        next cell

The accumulated dashboard State carries every C-RNTI we've ever seen
across every cell visited. The simulator's single-cell model degenerates
this loop to one entry — useful for unit testing the orchestrator
without real hardware.

LTESniffer support is structurally present but practically a no-op:
LTESniffer writes PCAP files, not the line-oriented CSV/text our parser
consumes. FalconEye is the working decoder for this flow.
"""

from __future__ import annotations

import io
import os
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from sniffer.falcon import parse_stream as parse_falcon_stream, tail_csv
from sniffer.parse_ltesniffer import (
    normalize_stream,
    parse_stream as parse_ltesniffer_stream,
)

if TYPE_CHECKING:
    from sniffer.live import State, _UeSightingSink, _ParseArgs


@dataclass(frozen=True)
class SurveyCell:
    """A single cell to dwell on: EARFCN + PCI + the DL center freq in Hz."""
    earfcn: int
    pci: int
    center_hz: int


def _spawn_decoder(decoder: str, cell: SurveyCell, antennas: int,
                   threads: int, csv_path: Optional[str]) -> subprocess.Popen:
    """Build and launch the decoder subprocess for one cell."""
    if decoder == "falcon":
        binname = os.environ.get("FALCON_BIN", "FalconEye")
        cmd = [binname, "-f", str(int(cell.center_hz)), "-D", str(csv_path)]
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    if decoder == "ltesniffer":
        binname = os.environ.get("LTESNIFFER_BIN", "LTESniffer")
        cmd = [binname,
               "-A", str(antennas), "-W", str(threads),
               "-f", str(int(cell.center_hz)),
               "-I", str(cell.pci),
               "-m", "0",
               "-a", "num_recv_frames=512"]
        return subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    raise ValueError(f"unknown decoder: {decoder!r}")


def _consume_cell_dwell(decoder: str, cell: SurveyCell, *,
                        proc: subprocess.Popen,
                        csv_path: Optional[str],
                        sink: "_UeSightingSink",
                        parse_args: "_ParseArgs",
                        dwell_stop: threading.Event) -> None:
    """Parse the decoder's output into `sink` until `dwell_stop` fires.

    Returns when the dwell timer ends or the decoder exits. Caller is
    responsible for terminating the subprocess.
    """
    if decoder == "falcon":
        assert csv_path is not None
        stream = tail_csv(csv_path, stop=dwell_stop, poll_interval_s=0.05)
        parse_falcon_stream(stream, parse_args, sink, pci=cell.pci)
        return
    if decoder == "ltesniffer":
        # LTESniffer's stdout statistics aren't per-DCI text — we
        # consume it to keep the pipe from filling, but no records
        # will land. PCAP tailing is a separate task.
        assert proc.stdout is not None
        for line in proc.stdout:
            if dwell_stop.is_set():
                return
            # Pass through the normalize stage in case a custom
            # build does emit DECODED key=value lines.
            _ = normalize_stream(iter([line]))
        return
    raise ValueError(f"unknown decoder: {decoder!r}")


def run_survey_loop(state: "State", *,
                    cells: list[SurveyCell],
                    dwell_seconds: float,
                    total_seconds: float,
                    decoder: str = "falcon",
                    antennas: int = 2,
                    threads: int = 4,
                    mission_id: str,
                    out_dir: str,
                    stop: threading.Event) -> None:
    """Run the survey loop until `stop` or `total_seconds`.

    Updates State's survey status before each cell visit + on completion.
    All decoded C-RNTIs land in the same per-mission JSONL — so the
    consumer sees a single flat stream regardless of how many cells the
    survey crossed.
    """
    # Late import — `live` imports `survey`, we don't want to circle.
    from sniffer.live import _ParseArgs, _UeSightingSink

    if not cells:
        state.set_status("error",
                         message="survey received an empty cell list")
        return
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"ue-{mission_id}.jsonl")
    started = time.monotonic()
    cycle = 0
    while not stop.is_set():
        cycle += 1
        for i, cell in enumerate(cells):
            if stop.is_set():
                return
            elapsed = time.monotonic() - started
            remaining = max(0.0, total_seconds - elapsed)
            if remaining <= 0:
                state.set_status("survey_done",
                                 cycles=cycle - 1, cells=len(cells))
                return
            this_dwell = min(dwell_seconds, remaining)
            state.set_survey_status({
                "phase": "dwelling",
                "decoder": decoder,
                "cycle": cycle,
                "cell_idx": i + 1,
                "cells_total": len(cells),
                "current_earfcn": cell.earfcn,
                "current_pci": cell.pci,
                "current_center_hz": cell.center_hz,
                "dwell_seconds": this_dwell,
                "cell_started_ns": time.monotonic_ns(),
                "total_remaining_s": remaining,
            })
            parse_args = _ParseArgs(
                mission_id=mission_id,
                backend=decoder,
                device=f"survey-{decoder}",
                rx_gain_db=50.0,
                center_hz=cell.center_hz,
                sample_rate_sps=23.04e6,
            )
            dwell_stop = threading.Event()
            t_dwell = threading.Timer(this_dwell, dwell_stop.set)
            t_dwell.daemon = True
            t_dwell.start()
            # Watch the parent stop too — if the user kills the dashboard
            # mid-cell, we want to abort the dwell promptly.
            parent_watch = threading.Thread(
                target=_wait_then_set, args=(stop, dwell_stop),
                daemon=True,
            )
            parent_watch.start()

            csv_path: Optional[str] = None
            tmpdir = None
            try:
                if decoder == "falcon":
                    tmpdir = tempfile.TemporaryDirectory(prefix="falcon_survey_")
                    csv_path = os.path.join(tmpdir.name, "dci.csv")
                proc = _spawn_decoder(decoder, cell, antennas, threads,
                                      csv_path)
            except (FileNotFoundError, ValueError) as exc:
                state.set_status("error",
                                 message=f"survey decoder spawn failed: {exc}")
                t_dwell.cancel()
                if tmpdir is not None:
                    tmpdir.cleanup()
                return

            try:
                with open(out_path, "a", encoding="utf-8") as jsonl_fh:
                    sink = _UeSightingSink(state, jsonl_out=jsonl_fh)
                    _consume_cell_dwell(decoder, cell, proc=proc,
                                        csv_path=csv_path, sink=sink,
                                        parse_args=parse_args,
                                        dwell_stop=dwell_stop)
            except Exception as exc:  # noqa: BLE001
                state.set_status("error",
                                 message=f"survey parse failed: {exc}")
            finally:
                t_dwell.cancel()
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if tmpdir is not None:
                    tmpdir.cleanup()
    state.set_status("survey_done", cycles=cycle, cells=len(cells))


def _wait_then_set(watch: threading.Event, target: threading.Event) -> None:
    """Block on `watch`; when it fires, also fire `target`."""
    watch.wait()
    target.set()
