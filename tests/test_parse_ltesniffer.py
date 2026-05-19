"""Unit tests for sniffer.parse_ltesniffer (normalize + parse)."""

from __future__ import annotations

import io
import json
from argparse import Namespace

from sniffer.parse_ltesniffer import normalize_line, parse_stream


def _args(**over) -> Namespace:
    defaults = dict(
        mission_id="t-ues",
        backend="ltesniffer",
        device="usrp-b210-test",
        center_hz=1_842_500_000.0,
        sample_rate_sps=23.04e6,
        rx_gain_db=50.0,
    )
    defaults.update(over)
    return Namespace(**defaults)


def test_normalize_human_readable_line():
    out = normalize_line(
        "[SFN=512 SF=3] PCI=271 RNTI=0x4ad2 DCI=1A MCS=15 RBs=4 dir=DL RSRP=-85.2"
    )
    assert out == (
        "DECODED frame=512 subframe=3 pci=271 c_rnti=0x4ad2 format=1A "
        "direction=DL mcs=15 prb=4 dl_rsrp_dbm=-85.2"
    )


def test_normalize_ul_line():
    out = normalize_line(
        "PCI=271 RNTI=0x4ad2 DCI=0 MCS=12 RBs=2 dir=UL UL_RSSI=-92.1"
    )
    assert out == (
        "DECODED pci=271 c_rnti=0x4ad2 format=0 direction=UL mcs=12 "
        "prb=2 ul_rssi_dbm=-92.1"
    )


def test_normalize_drops_noise_lines():
    assert normalize_line("loading config from /etc/lte.conf...") is None
    assert normalize_line("") is None
    assert normalize_line("# this is a comment") is None


def test_normalize_infers_direction_for_ul_when_omitted():
    out = normalize_line("PCI=42 RNTI=0xbeef MCS=5 RBs=1 ul_rssi_dbm=-110.0")
    assert "direction=UL" in out


def test_parse_stream_emits_ue_sighting_jsonl():
    text = (
        "DECODED pci=271 c_rnti=0x4ad2 format=1A direction=DL "
        "mcs=15 prb=4 dl_rsrp_dbm=-85.2\n"
        "DECODED pci=271 c_rnti=0x4ad2 format=0 direction=UL "
        "mcs=12 prb=2 ul_rssi_dbm=-92.1\n"
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), _args(), out)
    assert n == 2
    records = [json.loads(line) for line in out.getvalue().strip().splitlines()]
    assert len(records) == 2
    assert all(r["kind"] == "ue_sighting" for r in records)
    assert records[0]["ue"]["c_rnti"] == 0x4ad2
    assert records[0]["ue"]["direction"] == "dl"
    assert records[0]["ue"]["dl_rsrp_dbm"] == -85.2
    assert records[1]["ue"]["direction"] == "ul"
    assert records[1]["ue"]["ul_rssi_dbm"] == -92.1


def test_parse_stream_ignores_non_decoded_lines():
    text = (
        "# some comment\n"
        "DECODED pci=42 c_rnti=0x1 direction=UL ul_rssi_dbm=-100\n"
        "noise\n"
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), _args(), out)
    assert n == 1


def test_parse_stream_requires_pci_and_rnti():
    text = "DECODED direction=UL mcs=5 prb=1 ul_rssi_dbm=-90\n"
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), _args(), out)
    assert n == 0
    assert out.getvalue() == ""


def test_normalize_picks_up_ta_step_synonyms():
    out = normalize_line(
        "PCI=42 RNTI=0xbeef DCI=0 dir=UL ul_rssi_dbm=-100 TA=42"
    )
    assert "ta_n_steps=42" in out
    out2 = normalize_line(
        "PCI=42 RNTI=0xbeef DCI=0 dir=UL ul_rssi_dbm=-100 timing_advance=7"
    )
    assert "ta_n_steps=7" in out2


def test_parse_stream_extracts_ta_steps_and_derives_meters():
    text = (
        "DECODED pci=271 c_rnti=0x4ad2 format=0 direction=UL "
        "mcs=10 prb=2 ul_rssi_dbm=-90 ta_n_steps=42\n"
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)), _args(), out)
    assert n == 1
    rec = json.loads(out.getvalue().strip())
    assert rec["ue"]["ta_n_steps"] == 42
    # Schema's __post_init__ derives ta_meters from ta_n_steps:
    assert rec["ue"]["ta_meters"] == 42 * 78.12526041666667
