"""Unit tests for sniffer.cells — JSONL round-trip, nearby distance math,
OpenCellID parser smoke test."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sniffer import cells


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> str:
    p = tmp_path / "known_cells.jsonl"
    monkeypatch.setenv("SNIFFER_KNOWN_CELLS", str(p))
    # cells.DEFAULT_STORE was resolved at import time; patch it too so the
    # module-level constant matches the env override for this test.
    monkeypatch.setattr(cells, "DEFAULT_STORE", str(p))
    return str(p)


def test_append_and_load_round_trip(store: str) -> None:
    cells.append_cell(cells.KnownCell(
        source="scan", ts_utc="2026-05-20T10:00:00Z",
        earfcn=1850, pci=271, rsrp_dbm=-95.0,
    ))
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T10:01:00Z",
        mcc=425, mnc=2, tac=4321, eci=10101010,
        operator="Cellcom", lat=32.0861, lon=34.7815,
    ))
    rows = list(cells.load_all())
    assert len(rows) == 2
    assert rows[0].source == "scan"
    assert rows[0].pci == 271
    assert rows[1].source == "opencellid"
    assert rows[1].lat == pytest.approx(32.0861)
    assert rows[1].operator == "Cellcom"
    # Fields not set should round-trip as None.
    assert rows[0].lat is None
    assert rows[1].earfcn is None


def test_to_jsonl_drops_empty_fields(store: str) -> None:
    c = cells.KnownCell(source="scan", ts_utc="2026-05-20T00:00:00Z",
                        pci=42)
    decoded = json.loads(c.to_jsonl())
    # Required + set fields are present...
    assert decoded == {"source": "scan", "ts_utc": "2026-05-20T00:00:00Z",
                       "pci": 42}
    # ...everything that's None is omitted, and "notes" (default "") too.
    assert "lat" not in decoded
    assert "notes" not in decoded


def test_nearby_haversine_and_radius_filter(store: str) -> None:
    # Tel Aviv-ish coords. Cells at increasing distances.
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T10:00:00Z",
        lat=32.0861, lon=34.7815, operator="Cellcom",
    ))
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T10:00:00Z",
        lat=32.0870, lon=34.7820,  # ~100 m north-east
        operator="Pelephone",
    ))
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T10:00:00Z",
        lat=32.5000, lon=34.7815,  # ~46 km north
        operator="HOT Mobile",
    ))
    # No-lat row: must be silently skipped (no crash).
    cells.append_cell(cells.KnownCell(
        source="scan", ts_utc="2026-05-20T10:00:00Z",
        earfcn=1850, pci=271,
    ))

    near = cells.nearby(32.0861, 34.7815, radius_km=1.0)
    assert len(near) == 2
    # Closest comes first.
    assert near[0][1].operator == "Cellcom"
    assert near[1][1].operator == "Pelephone"
    assert near[0][0] == pytest.approx(0.0, abs=0.01)
    assert near[1][0] < 0.5  # well under 1 km

    far = cells.nearby(32.0861, 34.7815, radius_km=100.0)
    assert len(far) == 3
    # Source filter works.
    only_opencellid = cells.nearby(32.0861, 34.7815, radius_km=100.0,
                                   source="opencellid")
    assert len(only_opencellid) == 3
    only_scan_with_geo = cells.nearby(32.0861, 34.7815, radius_km=100.0,
                                      source="scan")
    assert only_scan_with_geo == []


def test_stats_breakdown(store: str) -> None:
    cells.append_cell(cells.KnownCell(
        source="scan", ts_utc="2026-05-20T00:00:00Z",
        earfcn=1850, pci=271, rsrp_dbm=-90.0,
    ))
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T00:00:00Z",
        mcc=425, mnc=2, operator="Cellcom", lat=32.0, lon=34.7,
    ))
    cells.append_cell(cells.KnownCell(
        source="opencellid", ts_utc="2026-05-20T00:00:00Z",
        mcc=425, mnc=3, operator="Pelephone", lat=32.1, lon=34.8,
    ))
    s = cells.stats()
    assert s["total"] == 3
    assert s["by_source"] == {"scan": 1, "opencellid": 2}
    assert s["by_operator"] == {"Cellcom": 1, "Pelephone": 1}
    assert s["by_earfcn"] == {1850: 1}
    assert s["with_pci"] == 1
    assert s["with_geo"] == 2


def test_import_opencellid_filters_country_and_radio(tmp_path: Path,
                                                      store: str) -> None:
    csv_path = tmp_path / "ocid.csv"
    csv_path.write_text(
        # Header is optional but tolerated.
        "radio,mcc,mnc,area,cell,unit,lon,lat,range,samples,changeable,created,updated,averageSignal\n"
        # Israeli LTE — should import (1).
        "LTE,425,2,4321,12345,0,34.7815,32.0861,500,12,1,1700000000,1700100000,-92\n"
        # Israeli LTE — should import (2).
        "LTE,425,3,8765,55555,0,34.79,32.10,800,5,1,1700000000,1700100000,-100\n"
        # Israeli LTE — duplicate of row 1, must be deduped.
        "LTE,425,2,4321,12345,0,34.7815,32.0861,500,12,1,1700000000,1700100000,-92\n"
        # German GSM — wrong country + wrong radio, drop.
        "GSM,262,1,1111,2222,0,13.4,52.5,200,3,1,1700000000,1700100000,-80\n"
        # Israeli UMTS — right country, wrong radio, drop.
        "UMTS,425,2,4444,9999,0,34.8,32.1,300,8,1,1700000000,1700100000,-95\n"
    )
    read, written = cells.import_opencellid(str(csv_path), progress_every=0)
    assert read == 5
    assert written == 2  # one Israeli LTE skipped as duplicate
    rows = list(cells.load_all())
    assert len(rows) == 2
    assert {r.eci for r in rows} == {12345, 55555}
    assert all(r.source == "opencellid" for r in rows)
    assert {r.operator for r in rows} == {"Cellcom", "Pelephone"}
    # averageSignal is negative → kept as rsrp_dbm.
    assert {r.rsrp_dbm for r in rows} == {-92.0, -100.0}


def test_to_survey_cell_dict_skips_untunable(store: str) -> None:
    no_pci = cells.KnownCell(source="opencellid",
                             ts_utc="2026-05-20T00:00:00Z",
                             mcc=425, mnc=2, lat=32.0, lon=34.7)
    assert cells.to_survey_cell_dict(no_pci) is None

    no_earfcn = cells.KnownCell(source="scan",
                                ts_utc="2026-05-20T00:00:00Z",
                                pci=271)
    assert cells.to_survey_cell_dict(no_earfcn) is None

    full = cells.KnownCell(source="scan",
                           ts_utc="2026-05-20T00:00:00Z",
                           earfcn=1850, pci=271,
                           center_hz=1870000000.0)
    out = cells.to_survey_cell_dict(full)
    assert out == {"earfcn": 1850, "pci": 271, "center_hz": 1870000000}


def test_load_skips_malformed_rows(store: str) -> None:
    # Hand-write a mix of valid and corrupt rows.
    with open(store, "w") as f:
        f.write('{"source":"scan","ts_utc":"2026-05-20T00:00:00Z","pci":1}\n')
        f.write('this-is-not-json\n')
        f.write('{"missing":"required-fields"}\n')
        f.write('{"source":"opencellid","ts_utc":"2026-05-20T00:00:00Z","pci":2}\n')
    rows = list(cells.load_all())
    assert [r.pci for r in rows] == [1, 2]
