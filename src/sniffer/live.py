"""Realtime browser dashboard for HackRF + LTE-Cell-Scanner.

Spawns `CellSearch` in a restart loop, streams its stdout through the
existing `parse_cellsearch` parser, and broadcasts each `CellSighting`
to connected browsers over Server-Sent Events. Detected cells are
aggregated keyed by `(carrier_hz rounded to 100 kHz, PCI)`, with rolling
RSRP history so the UI can render a sparkline.

Stdlib only — no FastAPI, no websockets. Run:

    python -m sniffer.live --start-hz 1840e6 --end-hz 1845e6
    python -m sniffer.live --simulate           # no HackRF needed

Open http://127.0.0.1:8000/ in a browser.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from sniffer.parse_cellsearch import parse_stream
from sniffer.schema import CellSighting, mono_ns, utc_iso
from sniffer.simulate import (
    Emitter,
    SimulationConfig,
    cellsearch_lines,
)


# --------------------------------------------------------------------------
# Aggregator + broadcaster
# --------------------------------------------------------------------------


class State:
    """Per-cell rolling state plus a fan-out queue list for SSE clients."""

    def __init__(self, history_len: int = 120):
        self._lock = threading.Lock()
        # key: (round(center_hz / 1e5), pci)
        self._cells: dict[tuple[int, int], dict[str, Any]] = {}
        self._clients: list[queue.Queue[str]] = []
        self._history_len = history_len
        self._started_mono_ns = mono_ns()
        self._total_sightings = 0
        self._scan_status: dict[str, Any] = {"phase": "idle", "ts_utc": utc_iso()}

    # --- ingest -----------------------------------------------------------

    def ingest(self, sighting: CellSighting) -> dict[str, Any]:
        center = sighting.radio.center_hz or 0.0
        pci = sighting.cell.pci
        key = (int(round(center / 1e5)), pci)
        now_iso = utc_iso()
        with self._lock:
            entry = self._cells.get(key)
            if entry is None:
                entry = {
                    "key": f"{key[0]}-{key[1]}",
                    "pci": pci,
                    "center_hz": center,
                    "n_id_1": sighting.cell.n_id_1,
                    "n_id_2": sighting.cell.n_id_2,
                    "first_seen": now_iso,
                    "count": 0,
                    "rsrp_history": deque(maxlen=self._history_len),
                }
                self._cells[key] = entry
            entry["last_seen"] = now_iso
            entry["count"] += 1
            entry["rsrp_dbm"] = sighting.cell.rsrp_dbm
            entry["rsrq_db"] = sighting.cell.rsrq_db
            entry["snr_db"] = sighting.cell.snr_db
            entry["frame_offset_samples"] = sighting.cell.frame_offset_samples
            if sighting.cell.rsrp_dbm is not None:
                entry["rsrp_history"].append(
                    [now_iso, round(sighting.cell.rsrp_dbm, 2)]
                )
            self._total_sightings += 1
            payload = _entry_to_dict(entry)
        self._broadcast({"type": "sighting", "cell": payload})
        return payload

    def set_status(self, phase: str, **extra: Any) -> None:
        with self._lock:
            self._scan_status = {"phase": phase, "ts_utc": utc_iso(), **extra}
            status = dict(self._scan_status)
        self._broadcast({"type": "status", "status": status})

    # --- snapshot ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cells = [_entry_to_dict(e) for e in self._cells.values()]
            cells.sort(
                key=lambda c: (c.get("rsrp_dbm") is None, -(c.get("rsrp_dbm") or 0))
            )
            return {
                "type": "snapshot",
                "cells": cells,
                "status": dict(self._scan_status),
                "total_sightings": self._total_sightings,
                "uptime_s": (mono_ns() - self._started_mono_ns) / 1e9,
            }

    # --- fan-out ----------------------------------------------------------

    def register(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(q)
        q.put(json.dumps(self.snapshot()))
        return q

    def unregister(self, q: queue.Queue[str]) -> None:
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def _broadcast(self, event: dict[str, Any]) -> None:
        msg = json.dumps(event)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass  # slow client; drop


def _entry_to_dict(entry: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in entry.items() if k != "rsrp_history"}
    out["rsrp_history"] = list(entry["rsrp_history"])
    return out


# --------------------------------------------------------------------------
# Sink: bridges parse_stream's text-output into the State aggregator
# --------------------------------------------------------------------------


class _SightingSink(io.TextIOBase):
    """parse_stream writes JSONL strings here; we decode + push to State."""

    def __init__(self, state: State, jsonl_out: Optional[io.TextIOBase] = None):
        super().__init__()
        self._state = state
        self._jsonl_out = jsonl_out
        self._buf = ""

    def write(self, s: str) -> int:
        if self._jsonl_out is not None:
            self._jsonl_out.write(s)
            self._jsonl_out.flush()
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._ingest_record(rec)
        return len(s)

    def flush(self) -> None:  # noqa: D401
        if self._jsonl_out is not None:
            self._jsonl_out.flush()

    def _ingest_record(self, rec: dict[str, Any]) -> None:
        # Reconstruct just enough of CellSighting for State.ingest.
        radio = rec.get("radio") or {}
        cell = rec.get("cell") or {}
        from sniffer.schema import CellInfo, RadioConfig  # local import

        sighting = CellSighting(
            mission_id=rec.get("mission_id", ""),
            capture_id=rec.get("capture_id", ""),
            ts_mono_ns=rec.get("ts_mono_ns", mono_ns()),
            ts_utc=rec.get("ts_utc", utc_iso()),
            radio=RadioConfig(**{k: radio.get(k) for k in RadioConfig.__dataclass_fields__ if k in radio}),
            cell=CellInfo(**{k: cell.get(k) for k in CellInfo.__dataclass_fields__ if k in cell}),
        )
        self._state.ingest(sighting)


# --------------------------------------------------------------------------
# Producers
# --------------------------------------------------------------------------


class _ParseArgs:
    def __init__(self, mission_id: str, backend: str, device: str):
        self.mission_id = mission_id
        self.backend = backend
        self.device = device


def run_cellsearch_loop(state: State, *, start_hz: float, end_hz: float,
                        step_hz: float, mission_id: str, out_dir: str,
                        stop: threading.Event) -> None:
    """Run CellSearch repeatedly, parse stdout, ingest into State."""
    if shutil.which("CellSearch") is None:
        state.set_status(
            "error",
            message="CellSearch not on PATH — run ./scripts/install-macos.sh and "
                    "export PATH=\"$HOME/src/LTE-Cell-Scanner/build/src:$PATH\"",
        )
        return

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"scan-{mission_id}.jsonl")
    parse_args = _ParseArgs(mission_id, "lte-cell-scanner", "hackrf-0")

    while not stop.is_set():
        state.set_status(
            "scanning",
            start_hz=start_hz, end_hz=end_hz, step_hz=step_hz, out=out_path,
        )
        proc = subprocess.Popen(
            [
                "CellSearch",
                "--freq-start", f"{start_hz}",
                "--freq-end", f"{end_hz}",
                "--freq-step", f"{step_hz}",
                "--device-args", "hackrf=0",
                "--num-try", "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            with open(out_path, "a", encoding="utf-8") as jsonl_fh:
                sink = _SightingSink(state, jsonl_out=jsonl_fh)
                assert proc.stdout is not None
                parse_stream(proc.stdout, parse_args, sink)
        except Exception as exc:  # noqa: BLE001
            state.set_status("error", message=f"parse failed: {exc}")
        finally:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        if stop.is_set():
            break
        # Brief gap before re-scan to let the radio settle.
        for _ in range(5):
            if stop.is_set():
                return
            time.sleep(0.2)


def run_simulator(state: State, *, mission_id: str,
                  stop: threading.Event) -> None:
    """Drive State from sniffer.simulate, looping forever."""
    state.set_status("simulating", mission_id=mission_id)
    parse_args = _ParseArgs(mission_id, "sim", "sim-0")
    sink = _SightingSink(state, jsonl_out=None)

    # Two emitters on adjacent EARFCNs so the UI has multiple rows.
    emitters = [
        Emitter(lat=32.0853, lon=34.7818, alt_m=25.0, pci=271,
                center_hz=1_842_500_000, n_id_1=90, n_id_2=1),
        Emitter(lat=32.0855, lon=34.7825, alt_m=18.0, pci=148,
                center_hz=1_840_000_000, n_id_1=49, n_id_2=1),
    ]

    from sniffer.simulate import box_trajectory

    def paced_lines() -> Any:
        """Yield simulated CellSearch lines forever, pacing between blocks."""
        seed = 0
        while not stop.is_set():
            for em in emitters:
                if stop.is_set():
                    return
                cfg = SimulationConfig(
                    mission_id=mission_id,
                    emitter=em,
                    waypoints=box_trajectory(em, half_size_m=80.0,
                                             altitudes=(20.0,), n_per_side=4,
                                             leg_speed_mps=10.0),
                    sample_period_s=0.4,
                    seed=seed,
                )
                seed += 1
                for line in cellsearch_lines(cfg):
                    if stop.is_set():
                        return
                    yield line
                    # Blank line marks end of a detection block; pace there.
                    if line == "\n" or not line.strip():
                        time.sleep(0.6)

    # parse_stream maintains block-state across lines, so call it once with
    # the full generator (it'll stay alive until the generator ends or stops).
    parse_stream(paced_lines(), parse_args, sink)


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


_INDEX_HTML = """<!doctype html>
<html lang=\"en\"><head>
<meta charset=\"utf-8\"><title>Cellular drones · live</title>
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, system-ui,
       sans-serif; background:#0b0d10; color:#e6e8eb; }
header { padding: 14px 20px; border-bottom: 1px solid #1f242a;
         display:flex; align-items:center; justify-content:space-between; }
header h1 { font-size: 16px; font-weight: 600; margin: 0; }
.meta { font-size: 12px; color:#8a93a0; display:flex; gap:18px; }
.meta strong { color:#e6e8eb; font-weight:600; }
.status-pill { padding:2px 10px; border-radius: 999px; font-size: 11px;
              border:1px solid #2a3038; }
.status-scanning { color:#7ad9a1; border-color:#1d4032; background:#11211a; }
.status-simulating { color:#f0c270; border-color:#403118; background:#21190f; }
.status-error { color:#f08580; border-color:#4a1f1f; background:#2a1414; }
.status-idle { color:#8a93a0; }
main { padding: 16px 20px; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #1a1f25; font-size: 13px; }
th { font-weight: 600; color:#8a93a0; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
tr.fresh td { background: #16241c; transition: background 1.4s ease; }
.rsrp { font-weight: 600; }
.rsrp-strong { color:#7ad9a1; }
.rsrp-mid { color:#f0c270; }
.rsrp-weak { color:#f08580; }
.spark { width: 120px; height: 28px; vertical-align: middle; }
.empty { padding: 60px 20px; text-align: center; color:#8a93a0; font-size: 14px; }
.log { margin-top: 24px; font-family: ui-monospace, monospace; font-size:11px;
       color:#8a93a0; max-height: 160px; overflow-y: auto; padding: 8px 0;
       border-top: 1px solid #1a1f25; }
.log div { padding: 2px 0; }
.log .ts { color:#525a66; margin-right: 8px; }
.center-hz { color:#8a93a0; font-size:12px; }
</style>
</head><body>
<header>
  <h1>Cellular drones · live</h1>
  <div class=\"meta\">
    <span>cells <strong id=\"n-cells\">0</strong></span>
    <span>sightings <strong id=\"n-sightings\">0</strong></span>
    <span>uptime <strong id=\"uptime\">0s</strong></span>
    <span class=\"status-pill status-idle\" id=\"status\">idle</span>
  </div>
</header>
<main>
  <div id=\"empty\" class=\"empty\">waiting for first detection…</div>
  <table id=\"tbl\" style=\"display:none\">
    <thead><tr>
      <th>PCI</th><th>Carrier</th><th>RSRP</th><th>RSRQ</th><th>SNR</th>
      <th>Count</th><th>Last seen</th><th>RSRP trend</th>
    </tr></thead>
    <tbody id=\"rows\"></tbody>
  </table>
  <div class=\"log\" id=\"log\"></div>
</main>
<script>
const rowsEl = document.getElementById('rows');
const tblEl = document.getElementById('tbl');
const emptyEl = document.getElementById('empty');
const logEl = document.getElementById('log');
const nCellsEl = document.getElementById('n-cells');
const nSightingsEl = document.getElementById('n-sightings');
const uptimeEl = document.getElementById('uptime');
const statusEl = document.getElementById('status');

let totalSightings = 0;
const cells = new Map();

function rsrpClass(v) {
  if (v == null) return '';
  if (v >= -85) return 'rsrp-strong';
  if (v >= -100) return 'rsrp-mid';
  return 'rsrp-weak';
}

function fmt(v, suffix='', digits=1) {
  if (v == null || Number.isNaN(v)) return '—';
  return v.toFixed(digits) + suffix;
}

function fmtFreq(hz) {
  if (hz == null) return '—';
  return (hz / 1e6).toFixed(2) + ' MHz';
}

function timeAgo(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 2) return 'now';
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s/60) + 'm ago';
  return Math.round(s/3600) + 'h ago';
}

function sparkPath(history) {
  if (!history || history.length < 2) return '';
  const w = 120, h = 28, pad = 2;
  const vals = history.map(p => p[1]);
  const min = Math.min(...vals), max = Math.max(...vals);
  const span = Math.max(1, max - min);
  const dx = (w - 2*pad) / (history.length - 1);
  return history.map((p, i) => {
    const x = pad + i*dx;
    const y = pad + (h - 2*pad) * (1 - (p[1] - min) / span);
    return (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
}

function renderRow(cell) {
  let tr = document.getElementById('row-' + cell.key);
  if (!tr) {
    tr = document.createElement('tr');
    tr.id = 'row-' + cell.key;
    rowsEl.appendChild(tr);
  }
  tr.innerHTML = `
    <td><strong>${cell.pci}</strong></td>
    <td>${fmtFreq(cell.center_hz)}<div class=\"center-hz\">n1=${cell.n_id_1 ?? '—'} n2=${cell.n_id_2 ?? '—'}</div></td>
    <td class=\"rsrp ${rsrpClass(cell.rsrp_dbm)}\">${fmt(cell.rsrp_dbm, ' dBm')}</td>
    <td>${fmt(cell.rsrq_db, ' dB')}</td>
    <td>${fmt(cell.snr_db, ' dB')}</td>
    <td>${cell.count}</td>
    <td title=\"${cell.last_seen}\">${timeAgo(cell.last_seen)}</td>
    <td><svg class=\"spark\" viewBox=\"0 0 120 28\"><path d=\"${sparkPath(cell.rsrp_history)}\" fill=\"none\" stroke=\"#7ad9a1\" stroke-width=\"1.5\"/></svg></td>
  `;
  tr.classList.add('fresh');
  setTimeout(() => tr.classList.remove('fresh'), 1500);
}

function sortRows() {
  const sorted = [...cells.values()].sort((a, b) =>
    (b.rsrp_dbm ?? -1e9) - (a.rsrp_dbm ?? -1e9));
  sorted.forEach((c, i) => {
    const tr = document.getElementById('row-' + c.key);
    if (tr && rowsEl.children[i] !== tr) rowsEl.appendChild(tr);
  });
}

function setStatus(s) {
  statusEl.textContent = s.phase + (s.message ? ' — ' + s.message : '');
  statusEl.className = 'status-pill status-' + s.phase;
}

function logLine(text) {
  const d = document.createElement('div');
  const ts = new Date().toTimeString().slice(0, 8);
  d.innerHTML = '<span class=\"ts\">' + ts + '</span>' + text;
  logEl.prepend(d);
  while (logEl.children.length > 50) logEl.removeChild(logEl.lastChild);
}

function applyEvent(ev) {
  if (ev.type === 'snapshot') {
    cells.clear();
    rowsEl.innerHTML = '';
    (ev.cells || []).forEach(c => { cells.set(c.key, c); renderRow(c); });
    setStatus(ev.status || {phase: 'idle'});
    totalSightings = ev.total_sightings || 0;
  } else if (ev.type === 'sighting') {
    const c = ev.cell;
    cells.set(c.key, c);
    renderRow(c);
    totalSightings += 1;
    logLine(`PCI ${c.pci} @ ${fmtFreq(c.center_hz)} · RSRP ${fmt(c.rsrp_dbm, ' dBm')} · SNR ${fmt(c.snr_db, ' dB')}`);
  } else if (ev.type === 'status') {
    setStatus(ev.status);
    logLine('status: ' + ev.status.phase + (ev.status.message ? ' — ' + ev.status.message : ''));
  }
  nCellsEl.textContent = cells.size;
  nSightingsEl.textContent = totalSightings;
  if (cells.size > 0) { tblEl.style.display = ''; emptyEl.style.display = 'none'; }
  sortRows();
}

let t0 = Date.now();
setInterval(() => {
  uptimeEl.textContent = Math.round((Date.now() - t0)/1000) + 's';
  // re-render "last seen" cells
  cells.forEach(c => {
    const tr = document.getElementById('row-' + c.key);
    if (tr) tr.children[6].textContent = timeAgo(c.last_seen);
  });
}, 1000);

const es = new EventSource('/events');
es.onmessage = (e) => { try { applyEvent(JSON.parse(e.data)); } catch (_) {} };
es.onerror = () => logLine('stream disconnected, browser will retry');
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    state: State  # set on class before serving

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # quiet

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/state":
            body = json.dumps(self.state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/events":
            self._sse()
            return
        self.send_response(404)
        self.end_headers()

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.state.register()
        try:
            while True:
                try:
                    msg = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + msg.encode("utf-8") + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.state.unregister(q)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start-hz", type=float, default=1_840_000_000)
    p.add_argument("--end-hz", type=float, default=1_845_000_000)
    p.add_argument("--step-hz", type=float, default=100_000)
    p.add_argument("--mission-id",
                   default=time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime()))
    p.add_argument("--out-dir", default="data")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--simulate", action="store_true",
                   help="generate fake sightings (no HackRF needed)")
    args = p.parse_args()

    state = State()
    stop = threading.Event()

    if args.simulate:
        producer = threading.Thread(
            target=run_simulator,
            kwargs=dict(state=state, mission_id=args.mission_id, stop=stop),
            daemon=True,
        )
    else:
        producer = threading.Thread(
            target=run_cellsearch_loop,
            kwargs=dict(
                state=state, start_hz=args.start_hz, end_hz=args.end_hz,
                step_hz=args.step_hz, mission_id=args.mission_id,
                out_dir=args.out_dir, stop=stop,
            ),
            daemon=True,
        )
    producer.start()

    handler = type("H", (_Handler,), {"state": state})
    httpd = ThreadingHTTPServer((args.host, args.port), handler)

    def _shutdown(*_: Any) -> None:
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    url = f"http://{args.host}:{args.port}/"
    print(f"live dashboard: {url}   (mission {args.mission_id})", flush=True)
    if args.simulate:
        print("mode: simulate", flush=True)
    else:
        print(f"mode: HackRF · scan {args.start_hz:.0f}–{args.end_hz:.0f} Hz "
              f"step {args.step_hz:.0f}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
