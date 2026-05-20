"""Wrap srsRAN_4G's `cell_search` binary as `sniffer scan`.

`cell_search` (built by `scripts/install-linux.sh` as part of srsRAN_4G,
installed alongside the runtime as `srsran_cell_search` after `make
install`) sweeps a band or EARFCN range and reports detected LTE cells.
This module spawns it, parses its stdout permissively, optionally
enriches each found cell with PLMN/TAC/CGI via `sib1.decode_sib1`, and
emits a table or JSONL.

Output format drifts between srsRAN versions; the parser is permissive
— pull `key=value` pairs out of any recognisable line and ignore the rest.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Optional

DEFAULT_BINARY = "srsran_cell_search"
DEFAULT_SIB_BINARY = "pdsch_ue"

# srsran_cell_search emits keys with spaces in them ("PSS power=31.0").
# Pre-normalize those into underscored single-token keys before the
# regex runs.
_KEY_ALIASES = (
    ("PSS power", "pss_power"),
    ("SSS power", "sss_power"),
    ("DL freq",   "dl_freq"),
    ("UL freq",   "ul_freq"),
)

# Permissive `key=value` and `key: value` matcher.
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*([-+\d.eE]+)")

# Field names srsran_cell_search and friends use across versions. The
# real `Found CELL ...` line uses PHYID, not PCI; "PSS power" is the
# signal-strength surrogate (PSS detector peak in dBm).
_FIELD_MAP = {
    "earfcn":     "earfcn",
    "dl_earfcn":  "earfcn",
    "freq":       "earfcn",        # some builds emit MHz here; we keep raw
    "pci":        "pci",
    "phyid":      "pci",           # srsran_cell_search prints PHYID
    "cell_id":    "pci",
    "rsrp":       "rsrp_dbm",
    "rsrp_dbm":   "rsrp_dbm",
    "psr":        "rsrp_dbm",      # some builds label PSS power as PSR
    "pss_power":  "rsrp_dbm",      # current srsran_cell_search
    "cfo":        "cfo_hz",
    "cfo_hz":     "cfo_hz",
}


def _normalize_spaced_keys(line: str) -> str:
    """Rewrite 'PSS power=' to 'pss_power=' so the KV regex matches."""
    for src, dst in _KEY_ALIASES:
        # Match the exact token, case-insensitive, before `=` or `:`.
        line = re.sub(
            rf"\b{re.escape(src)}\b(?=\s*[:=])",
            dst, line, flags=re.IGNORECASE,
        )
    return line


@dataclass
class Cell:
    earfcn: Optional[int] = None
    pci: Optional[int] = None
    rsrp_dbm: Optional[float] = None
    cfo_hz: Optional[float] = None
    enrichment: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"earfcn": self.earfcn, "pci": self.pci,
               "rsrp_dbm": self.rsrp_dbm, "cfo_hz": self.cfo_hz}
        if self.enrichment:
            out["enrichment"] = dict(self.enrichment)
        return out


def parse_stream(lines: Iterable[str]) -> Iterator[Cell]:
    """Yield one Cell per recognisable line.

    A line "counts" once it has at least a PCI. EARFCN is preferred but
    `cell_search`'s sweep output sometimes prints PCI alone on intermediate
    lines, so we synthesise from the most recently-seen EARFCN context.
    """
    current_earfcn: Optional[int] = None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _normalize_spaced_keys(line)
        fields: dict[str, str] = {}
        for k, v in _KV_RE.findall(line):
            canon = _FIELD_MAP.get(k.lower())
            if canon is None:
                continue
            fields[canon] = v
        if "earfcn" in fields:
            try:
                current_earfcn = int(float(fields["earfcn"]))
            except ValueError:
                pass
        if "pci" not in fields:
            continue
        try:
            pci = int(float(fields["pci"]))
        except ValueError:
            continue
        cell = Cell(pci=pci, earfcn=current_earfcn)
        try:
            cell.rsrp_dbm = float(fields["rsrp_dbm"]) if "rsrp_dbm" in fields else None
        except ValueError:
            cell.rsrp_dbm = None
        try:
            cell.cfo_hz = float(fields["cfo_hz"]) if "cfo_hz" in fields else None
        except ValueError:
            cell.cfo_hz = None
        if "earfcn" in fields:
            try:
                cell.earfcn = int(float(fields["earfcn"]))
            except ValueError:
                pass
        yield cell


def _build_cmd(binary: str, band: Optional[int],
               earfcn_range: Optional[tuple[int, int]],
               rf_args: str = "", gain_db: int = 75) -> list[str]:
    """Compose the srsran_cell_search argv.

    `rf_args` is passed verbatim to `-a`. Common values:
      - ""                              (auto: UHD wins over SoapySDR)
      - "driver=hackrf"                 (force SoapySDR HackRF path)
      - "type=b200,rx_antenna=TX/RX"    (USRP B-series, TX/RX antenna port)
      - "type=b200,rx_antenna=RX2"      (USRP B-series, RX2 antenna port)

    Empty default works when srsRAN was built with -DENABLE_UHD=ON
    AND a USRP is plugged in — UHD auto-detects. For HackRF-only
    builds (UHD disabled), the caller must pass driver=hackrf.

    The USRP B-series defaults to RX2 in some UHD versions; if your
    antenna is on the TX/RX port you'll get an open device + zero
    cells found (the framework works but receives silence). Set
    `rx_antenna=TX/RX` explicitly when in doubt.
    """
    cmd = [binary]
    if rf_args:
        cmd += ["-a", rf_args]
    if gain_db is not None:
        cmd += ["-g", str(gain_db)]
    if band is not None:
        cmd += ["-b", str(band)]
    if earfcn_range is not None:
        start, end = earfcn_range
        cmd += ["-s", str(start), "-e", str(end)]
    return cmd


def _print_table(cells: list[Cell], *, fh=sys.stdout) -> None:
    if not cells:
        print("no cells found.", file=fh)
        return
    has_enrich = any(c.enrichment for c in cells)
    header = ["EARFCN", "PCI", "RSRP_dBm", "CFO_Hz"]
    if has_enrich:
        header += ["PLMN", "TAC", "CGI"]
    rows = [header]
    for c in cells:
        row = [
            "—" if c.earfcn is None else str(c.earfcn),
            "—" if c.pci is None else str(c.pci),
            "—" if c.rsrp_dbm is None else f"{c.rsrp_dbm:.1f}",
            "—" if c.cfo_hz is None else f"{c.cfo_hz:.0f}",
        ]
        if has_enrich:
            row += [
                c.enrichment.get("plmn", "—"),
                str(c.enrichment.get("tac", "—")),
                str(c.enrichment.get("cgi", "—")),
            ]
        rows.append(row)
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for i, row in enumerate(rows):
        line = "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        print(line, file=fh)
        if i == 0:
            print("  ".join("-" * w for w in widths), file=fh)


def _log_neighbors(neighbors: list[dict]) -> None:
    """Append SIB3/SIB5 neighbor cells to the known-cells store."""
    try:
        from sniffer.cells import KnownCell, append_cell
        from sniffer.lte_bands import earfcn_to_hz_dl
    except Exception:  # noqa: BLE001
        return
    import time as _time
    ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    for nb in neighbors:
        earfcn = nb.get("earfcn")
        pci = nb.get("pci")
        if earfcn is None or pci is None:
            continue
        try:
            center_hz: Optional[float] = float(earfcn_to_hz_dl(earfcn))
        except Exception:  # noqa: BLE001
            center_hz = None
        try:
            append_cell(KnownCell(
                source="sib-neighbor", ts_utc=ts,
                earfcn=earfcn, pci=pci, center_hz=center_hz,
            ))
        except Exception:  # noqa: BLE001
            pass


def _log_to_known_cells(cells: list[Cell]) -> None:
    """Append every (earfcn, pci) we just discovered to known_cells.jsonl.
    Errors here must never break the user-facing scan output."""
    try:
        from sniffer.cells import KnownCell, append_cell
        from sniffer.lte_bands import earfcn_to_hz_dl
    except Exception:  # noqa: BLE001 — running outside our venv
        return
    import time as _time
    ts = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
    for c in cells:
        if c.pci is None:
            continue
        center_hz: Optional[float] = None
        if c.earfcn is not None:
            try:
                center_hz = float(earfcn_to_hz_dl(c.earfcn))
            except Exception:  # noqa: BLE001
                center_hz = None
        enrich = c.enrichment or {}
        mcc = _safe_int(enrich.get("mcc"))
        mnc = _safe_int(enrich.get("mnc"))
        tac = _safe_int(enrich.get("tac"))
        eci = _safe_int(enrich.get("eci") or enrich.get("cgi"))
        operator = None
        if mcc is not None and mnc is not None:
            from sniffer.cells import OPERATOR_BY_MCC_MNC
            operator = OPERATOR_BY_MCC_MNC.get((mcc, mnc))
        try:
            append_cell(KnownCell(
                source="scan", ts_utc=ts,
                earfcn=c.earfcn, pci=c.pci, center_hz=center_hz,
                rsrp_dbm=c.rsrp_dbm, cfo_hz=c.cfo_hz,
                mcc=mcc, mnc=mnc, tac=tac, eci=eci, operator=operator,
            ))
        except Exception:  # noqa: BLE001 — disk full, perms, whatever
            pass


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def run_scan(*, band: Optional[int] = None,
             earfcn_range: Optional[tuple[int, int]] = None,
             decode_sib1: bool = False,
             decode_neighbors: bool = False,
             binary: str = DEFAULT_BINARY,
             sib_binary: str = DEFAULT_SIB_BINARY,
             json_out: bool = False,
             timeout_s: int = 120,
             rf_args: str = "",
             gain_db: int = 75,
             fh=sys.stdout) -> int:
    """Run cell_search, print results, return exit code.

    Returns 0 on success (even with zero cells found), 3 if the binary is
    missing, 4 if the subprocess fails to start, the subprocess's own exit
    code otherwise.
    """
    if band is None and earfcn_range is None:
        print("scan: provide --band or --earfcn-range", file=sys.stderr)
        return 2
    resolved = shutil.which(binary) or (binary if os.path.exists(binary) else None)
    if resolved is None:
        print(f"scan: `{binary}` not on PATH. Build srsRAN (sniffer install) "
              f"or pass --binary / set SRSRAN_CELL_SEARCH_BIN.",
              file=sys.stderr)
        return 3
    cmd = _build_cmd(resolved, band, earfcn_range,
                     rf_args=rf_args, gain_db=gain_db)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except FileNotFoundError:
        print(f"scan: failed to launch `{resolved}`.", file=sys.stderr)
        return 4
    except subprocess.TimeoutExpired:
        print(f"scan: `{resolved}` timed out after {timeout_s}s.",
              file=sys.stderr)
        return 5
    cells = list(parse_stream(proc.stdout.splitlines()))
    if decode_sib1 or decode_neighbors:
        from sniffer.sib1 import (decode_sib1 as _decode_sib1,
                                  decode_neighbors as _decode_neighbors)
        for c in cells:
            if c.earfcn is None or c.pci is None:
                continue
            if decode_sib1:
                info = _decode_sib1(c.earfcn, c.pci, binary=sib_binary)
                if info is not None:
                    c.enrichment.update(info)
            if decode_neighbors:
                neighbors = _decode_neighbors(c.earfcn, c.pci, binary=sib_binary)
                if neighbors:
                    print(f"  SIB3/5: {len(neighbors)} neighbor(s) from "
                          f"EARFCN={c.earfcn} PCI={c.pci}", file=sys.stderr)
                    _log_neighbors(neighbors)
    # Side-effect: log every found cell to the known-cells store so future
    # sniffer live/survey runs can reuse this discovery.
    _log_to_known_cells(cells)
    if json_out:
        for c in cells:
            print(json.dumps(c.to_dict(), separators=(",", ":")), file=fh)
    else:
        _print_table(cells, fh=fh)
    return proc.returncode
