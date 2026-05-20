"""Known-cell store.

A unified JSONL log of LTE cells from three sources:
  * "opencellid" — geographic prior from a downloaded OpenCellID country dump.
                    Knows MCC/MNC/TAC/eci/lat/lon, does NOT know PCI/EARFCN.
  * "scan"       — cells decoded by `sniffer scan` (srsran_cell_search).
                    Knows EARFCN/PCI/RSRP. No lat/lon unless caller supplies.
  * "live-lock"  — cells FALCON successfully locks onto inside `sniffer live`.

Append-only at data/known_cells.jsonl. Multiple observations of the same
cell across runs are kept as separate rows — dedup/aggregation is at query
time so we never lose per-run timestamp/RSSI history. The store is plain
text so `tail -f` works.

Reading + writing are deliberately stdlib-only — this module loads inside
the Pi venv with no extra deps.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from typing import Iterator, Optional


# Default path: repo_root/data/known_cells.jsonl. Overridable via env var
# so tests can sandbox their own store.
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORE = os.environ.get(
    "SNIFFER_KNOWN_CELLS",
    os.path.normpath(os.path.join(_HERE, "..", "..", "data", "known_cells.jsonl")),
)


# MCC=425 → Israel. MNC → operator. LTE-bearing MNCs only; sources cross-check
# Wikipedia + ITU national plan + observed live MCC/MNC in the wild.
OPERATOR_BY_MCC_MNC: dict[tuple[int, int], str] = {
    (425, 1):  "Partner",
    (425, 2):  "Cellcom",
    (425, 3):  "Pelephone",
    (425, 5):  "Jawwal (PA roaming)",
    (425, 6):  "We4G / HOT-host",
    (425, 7):  "Mirs (Pelephone)",
    (425, 8):  "Golan Telecom",
    (425, 14): "HOT Mobile",
    (425, 15): "HOT Mobile",
    (425, 16): "Rami Levy",
    (425, 17): "Galei Tzahal",
}


@dataclass
class KnownCell:
    """One observation of an LTE cell.

    All identifier fields are optional because no single source supplies
    them all. The combination (mcc, mnc, tac, eci) uniquely names a sector
    when set; (earfcn, pci) names the radio-layer instance and is what
    `sniffer live`/FalconEye actually needs.
    """
    source: str               # "opencellid" | "scan" | "live-lock"
    ts_utc: str               # ISO 8601 wall-clock when this row was written
    mcc: Optional[int] = None
    mnc: Optional[int] = None
    tac: Optional[int] = None
    eci: Optional[int] = None
    operator: Optional[str] = None
    earfcn: Optional[int] = None
    center_hz: Optional[float] = None
    pci: Optional[int] = None
    rsrp_dbm: Optional[float] = None
    cfo_hz: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    range_m: Optional[float] = None
    samples: Optional[int] = None
    notes: str = ""

    def to_jsonl(self) -> str:
        d = asdict(self)
        # Drop empty fields so rows stay compact and grep-friendly.
        return json.dumps(
            {k: v for k, v in d.items() if v is not None and v != ""},
            separators=(",", ":"),
        )


def _store(path: Optional[str]) -> str:
    return path or DEFAULT_STORE


# --- write / read ----------------------------------------------------


def append_cell(cell: KnownCell, *, store_path: Optional[str] = None) -> None:
    p = _store(store_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(cell.to_jsonl() + "\n")


def load_all(*, store_path: Optional[str] = None) -> Iterator[KnownCell]:
    p = _store(store_path)
    if not os.path.exists(p):
        return
    fields = set(KnownCell.__dataclass_fields__.keys())
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                yield KnownCell(**{k: v for k, v in d.items() if k in fields})
            except TypeError:
                # Missing required fields (source, ts_utc) — skip.
                continue


# --- queries ---------------------------------------------------------


def _haversine_km(a_lat: float, a_lon: float,
                  b_lat: float, b_lon: float) -> float:
    R = 6371.0088  # Earth's volumetric mean radius (km).
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dlam = math.radians(b_lon - a_lon)
    h = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))


def nearby(lat: float, lon: float, *, radius_km: float = 5.0,
           store_path: Optional[str] = None,
           source: Optional[str] = None) -> list[tuple[float, KnownCell]]:
    """Cells within radius_km of (lat, lon), sorted by distance ascending."""
    out: list[tuple[float, KnownCell]] = []
    for c in load_all(store_path=store_path):
        if c.lat is None or c.lon is None:
            continue
        if source is not None and c.source != source:
            continue
        d = _haversine_km(lat, lon, c.lat, c.lon)
        if d <= radius_km:
            out.append((d, c))
    out.sort(key=lambda r: r[0])
    return out


def stats(*, store_path: Optional[str] = None) -> dict:
    """One-shot summary for `sniffer cells stats`."""
    by_source: dict[str, int] = {}
    by_operator: dict[str, int] = {}
    by_earfcn: dict[int, int] = {}
    have_pci = have_geo = total = 0
    for c in load_all(store_path=store_path):
        total += 1
        by_source[c.source] = by_source.get(c.source, 0) + 1
        if c.operator:
            by_operator[c.operator] = by_operator.get(c.operator, 0) + 1
        if c.earfcn is not None:
            by_earfcn[c.earfcn] = by_earfcn.get(c.earfcn, 0) + 1
        if c.pci is not None:
            have_pci += 1
        if c.lat is not None and c.lon is not None:
            have_geo += 1
    return {
        "total": total,
        "by_source": by_source,
        "by_operator": by_operator,
        "by_earfcn": by_earfcn,
        "with_pci": have_pci,
        "with_geo": have_geo,
    }


# --- OpenCellID importer ---------------------------------------------


# Positional order in the canonical OpenCellID CSV dump. Header may or
# may not be present; we detect by sniffing the first row.
OPENCELLID_FIELDS = ("radio", "mcc", "mnc", "area", "cell", "unit",
                     "lon", "lat", "range", "samples", "changeable",
                     "created", "updated", "averageSignal")


def _open_text(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"),
                                encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def import_opencellid(csv_path: str, *,
                      mcc_filter: Optional[int] = 425,
                      radio_filter: Optional[str] = "LTE",
                      store_path: Optional[str] = None,
                      progress_every: int = 50_000
                      ) -> tuple[int, int]:
    """Stream a (possibly gzipped) OpenCellID CSV into known_cells.jsonl.

    Filters: country (default Israel mcc=425), radio (default LTE).
    Dedup is in-process by (mcc,mnc,tac,eci) — we don't read the existing
    store back to check, so re-running over the same CSV will write
    duplicates. Use `sniffer cells stats` to spot if that happens.

    Returns (rows_read, rows_written).
    """
    seen: set[tuple[int, int, int, int]] = set()
    read = written = 0
    with _open_text(csv_path) as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            if row[0].lower() == "radio":
                continue  # optional header
            if len(row) < 8:
                continue
            read += 1
            d = dict(zip(OPENCELLID_FIELDS, row))
            kc = _opencellid_row_to_known(d, mcc_filter, radio_filter)
            if kc is None:
                continue
            key = (kc.mcc or -1, kc.mnc or -1, kc.tac or -1, kc.eci or -1)
            if key in seen:
                continue
            seen.add(key)
            append_cell(kc, store_path=store_path)
            written += 1
            if progress_every and written and (written % progress_every == 0):
                print(f"[opencellid] {read:,} read · {written:,} written",
                      flush=True)
    return read, written


def _opencellid_row_to_known(row: dict, mcc_filter: Optional[int],
                             radio_filter: Optional[str]) -> Optional[KnownCell]:
    try:
        radio = (row.get("radio") or "").upper()
        if radio_filter is not None and radio != radio_filter.upper():
            return None
        mcc = int(row["mcc"])
        if mcc_filter is not None and mcc != mcc_filter:
            return None
        mnc = int(row["mnc"])
        tac = int(row["area"])
        eci = int(row["cell"])
        lon = float(row["lon"])
        lat = float(row["lat"])
    except (KeyError, ValueError, TypeError):
        return None
    samples = _maybe_int(row.get("samples"))
    range_m = _maybe_float(row.get("range"))
    avg = _maybe_float(row.get("averageSignal"))
    updated = _maybe_int(row.get("updated"))
    ts_utc = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated))
              if updated else time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                            time.gmtime()))
    return KnownCell(
        source="opencellid", ts_utc=ts_utc,
        mcc=mcc, mnc=mnc, tac=tac, eci=eci,
        operator=OPERATOR_BY_MCC_MNC.get((mcc, mnc)),
        # OpenCellID averageSignal is normally negative dBm for LTE; ignore
        # zero/positive values (which mean "no measurement").
        rsrp_dbm=avg if (avg is not None and avg < 0) else None,
        lat=lat, lon=lon,
        range_m=range_m, samples=samples,
    )


def _maybe_int(v) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


def _maybe_float(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None


# --- helpers for downstream consumers (survey, scan) -----------------


def to_survey_cell_dict(c: KnownCell) -> Optional[dict]:
    """Render a KnownCell as the JSON shape `sniffer survey --cells` wants.

    Requires both earfcn AND (center_hz OR known band → earfcn->Hz), and a
    PCI. Returns None for rows that can't be tuned (OpenCellID-only rows
    without an earfcn/pci, etc).
    """
    if c.earfcn is None or c.pci is None:
        return None
    if c.center_hz is not None:
        center = int(c.center_hz)
    else:
        try:
            from sniffer.lte_bands import earfcn_to_hz_dl
            center = int(earfcn_to_hz_dl(c.earfcn))
        except Exception:  # noqa: BLE001 — band unknown, can't tune
            return None
    return {"earfcn": int(c.earfcn), "pci": int(c.pci), "center_hz": center}
