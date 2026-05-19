"""Unit tests for sniffer.falcon — FalconEye CSV parser + file tailer."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from sniffer.falcon import parse_csv_row, parse_stream, tail_csv

FIXTURE = Path(__file__).parent / "fixtures" / "falcon_dci.csv"


def _args(**over) -> Namespace:
    defaults = dict(
        mission_id="t-falcon",
        backend="falcon",
        device="usrp-b210-falcon",
        center_hz=1_870_000_000.0,
        sample_rate_sps=23.04e6,
        rx_gain_db=50.0,
    )
    defaults.update(over)
    return Namespace(**defaults)


# --- parse_csv_row -----------------------------------------------------------


def test_parse_csv_row_dl_grant():
    # DL row from the fixture: direction=1, mcs=15, prb=8, tbs=752, format=2 ("1A").
    cols = ("1700000123.456789\t0042\t3\t19154\t1\t15\t8\t752\t-1\t-1\t2\t"
            "0\t-1\t5\t17\t2\t1\t8\t39\tdeadbeef").split("\t")
    r = parse_csv_row(cols)
    assert r is not None
    assert r["c_rnti"] == 19154
    assert r["direction"] == "dl"
    assert r["mcs"] == 15
    assert r["n_prb"] == 8
    assert r["tbs_bytes"] == 752
    assert r["dci_format"] == "1A"
    assert r["harq_id"] == 5
    # `raw` carries fields we don't promote to typed columns:
    assert r["raw"]["ncce"] == "17"
    assert r["raw"]["L"] == "2"
    assert r["raw"]["sfn"] == "0042"


def test_parse_csv_row_ul_grant():
    cols = ("1700000123.501234\t0042\t4\t19154\t0\t12\t2\t408\t-1\t-1\t0\t"
            "1\t-1\t5\t19\t4\t1\t8\t23\tcafe").split("\t")
    r = parse_csv_row(cols)
    assert r is not None
    assert r["direction"] == "ul"
    assert r["dci_format"] == "0"
    assert r["tbs_bytes"] == 408


def test_parse_csv_row_normalises_negative_tbs_to_none():
    """FALCON writes -1 for 'no value here' — translate to None."""
    cols = ("1700000123.5\t0042\t5\t12345\t1\t8\t4\t-1\t-1\t-1\t2\t"
            "0\t-1\t3\t10\t2\t1\t8\t39\tabcd").split("\t")
    r = parse_csv_row(cols)
    assert r is not None
    assert r["tbs_bytes"] is None


def test_parse_csv_row_unknown_dci_format_passes_through():
    cols = ("1700000123.5\t0042\t5\t12345\t1\t8\t4\t100\t-1\t-1\t99\t"
            "0\t-1\t3\t10\t2\t1\t8\t39\tabcd").split("\t")
    r = parse_csv_row(cols)
    assert r is not None
    assert r["dci_format"] == "99"


def test_parse_csv_row_too_few_columns_returns_none():
    assert parse_csv_row(["only", "five", "cols", "in", "this"]) is None


def test_parse_csv_row_bad_rnti_returns_none():
    cols = ("1700000123.5\t0042\t5\tNOT_AN_INT\t1\t8\t4\t100\t-1\t-1\t2\t"
            "0\t-1\t3\t10\t2\t1\t8\t39\tabcd").split("\t")
    assert parse_csv_row(cols) is None


# --- parse_stream ------------------------------------------------------------


def test_parse_stream_emits_one_record_per_row():
    out = io.StringIO()
    text = FIXTURE.read_text()
    n = parse_stream(iter(text.splitlines(keepends=True)),
                     _args(), out, pci=271)
    assert n == 6
    rows = [json.loads(l) for l in out.getvalue().strip().splitlines()]
    assert len(rows) == 6
    assert all(r["kind"] == "ue_sighting" for r in rows)
    assert all(r["ue"]["pci"] == 271 for r in rows)
    # First row: DL grant for 0x4ad2 = 19154.
    assert rows[0]["ue"]["c_rnti"] == 19154
    assert rows[0]["ue"]["direction"] == "dl"
    assert rows[0]["ue"]["dci_format"] == "1A"
    # Source tag so downstream can tell where this came from.
    assert "source=falcon" in rows[0]["notes"]


def test_parse_stream_ignores_blank_and_comment_lines():
    text = (
        "# a comment\n"
        "\n"
        "1700000123.456789\t0042\t3\t19154\t1\t15\t8\t752\t-1\t-1\t2\t"
        "0\t-1\t5\t17\t2\t1\t8\t39\tdeadbeef\n"
        "\n"
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)),
                     _args(), out, pci=42)
    assert n == 1


def test_parse_stream_stamps_pci_from_caller():
    """FALCON's CSV has no PCI column — the caller knows it from -f Hz."""
    out = io.StringIO()
    n = parse_stream(iter(FIXTURE.read_text().splitlines(keepends=True)),
                     _args(), out, pci=503)
    assert n == 6
    rows = [json.loads(l) for l in out.getvalue().strip().splitlines()]
    assert all(r["ue"]["pci"] == 503 for r in rows)


def test_parse_stream_skips_malformed_lines_silently():
    text = (
        "garbage line\n"
        "1700000123.456789\t0042\t3\t19154\t1\t15\t8\t752\t-1\t-1\t2\t"
        "0\t-1\t5\t17\t2\t1\t8\t39\tdeadbeef\n"
        "another\tjunk\trow\n"
    )
    out = io.StringIO()
    n = parse_stream(iter(text.splitlines(keepends=True)),
                     _args(), out, pci=42)
    assert n == 1


# --- tail_csv ----------------------------------------------------------------


def test_tail_csv_yields_lines_appended_after_open(tmp_path):
    """The common path: file exists, lines arrive while we tail."""
    csv_path = tmp_path / "dci.csv"
    csv_path.write_text("")  # exists but empty
    stop = threading.Event()
    yielded: list[str] = []

    def reader():
        for line in tail_csv(str(csv_path), stop=stop,
                             poll_interval_s=0.01):
            yielded.append(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    # Append two complete lines + one partial line.
    with open(csv_path, "a") as fh:
        fh.write("row1\trnti=1\n")
        fh.write("row2\trnti=2\n")
        fh.write("incomplete_no_newline_yet")
        fh.flush()
    # Give the tailer a moment to read.
    time.sleep(0.1)
    # Complete the partial:
    with open(csv_path, "a") as fh:
        fh.write("\n")
        fh.flush()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=1.0)

    assert yielded == ["row1\trnti=1", "row2\trnti=2",
                       "incomplete_no_newline_yet"]


def test_tail_csv_waits_for_file_to_appear(tmp_path):
    """FalconEye creates the CSV on the first DCI — until then we
    should poll, not error."""
    csv_path = tmp_path / "delayed.csv"
    stop = threading.Event()
    yielded: list[str] = []

    def reader():
        for line in tail_csv(str(csv_path), stop=stop,
                             poll_interval_s=0.01,
                             startup_wait_s=5.0):
            yielded.append(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    # File doesn't exist yet — tailer is in the wait loop.
    time.sleep(0.05)
    # Now create it with content.
    csv_path.write_text("first_line\n")
    time.sleep(0.1)
    stop.set()
    t.join(timeout=1.0)

    assert yielded == ["first_line"]


def test_tail_csv_handles_file_recreation(tmp_path):
    """Decoder restart most commonly means the CSV is deleted + the
    new FalconEye process recreates it (often in a fresh tempdir, but
    we still cover the same-path delete+recreate case). The tailer
    should notice the inode change and start reading the new file
    from the beginning."""
    csv_path = tmp_path / "rotated.csv"
    csv_path.write_text("pre_rotate_row\n")
    stop = threading.Event()
    yielded: list[str] = []

    def reader():
        for line in tail_csv(str(csv_path), stop=stop,
                             poll_interval_s=0.01):
            yielded.append(line)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.1)  # read the initial line
    # Simulate FalconEye restart: delete + recreate (different inode).
    csv_path.unlink()
    time.sleep(0.05)
    csv_path.write_text("fresh_row1\nfresh_row2\n")
    time.sleep(0.15)
    stop.set()
    t.join(timeout=1.0)

    assert "pre_rotate_row" in yielded
    assert "fresh_row1" in yielded
    assert "fresh_row2" in yielded


def test_tail_csv_returns_cleanly_on_startup_wait_timeout(tmp_path):
    """If the file never appears within startup_wait_s, the generator
    exits gracefully so the caller can surface a real error rather
    than blocking forever."""
    csv_path = tmp_path / "never_created.csv"
    stop = threading.Event()
    start = time.monotonic()
    yielded = list(tail_csv(str(csv_path), stop=stop,
                            poll_interval_s=0.05,
                            startup_wait_s=0.2))
    elapsed = time.monotonic() - start
    assert yielded == []
    assert elapsed < 1.0  # quick exit, not stuck
