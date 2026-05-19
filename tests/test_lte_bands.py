"""EARFCN ↔ Hz conversions for common LTE bands."""

from __future__ import annotations

import pytest

from sniffer.lte_bands import band_of_earfcn, earfcn_to_hz_dl


# Verified against well-known EARFCN calculators (3GPP TS 36.101 § 5.7.3).
KNOWN_GOOD: list[tuple[int, int, int]] = [
    # (earfcn, expected_hz, expected_band)
    (300,    2140_000_000, 1),     # band 1, mid
    (1200,   1805_000_000, 3),     # band 3, low edge
    (1850,   1870_000_000, 3),     # band 3, common DCS 1800
    (1949,   1879_900_000, 3),     # band 3, high edge
    (3050,   2650_000_000, 7),     # band 7
    (6253,    801_300_000, 20),    # band 20, the srsran_cell_search README example
    (6300,    806_000_000, 20),    # band 20, also
    (38100,  2605_000_000, 38),    # band 38 TDD
    (39150,  2350_000_000, 40),    # band 40 TDD
]


@pytest.mark.parametrize("earfcn,expected_hz,expected_band", KNOWN_GOOD)
def test_earfcn_to_hz_dl_matches_3gpp_table(earfcn, expected_hz, expected_band):
    assert earfcn_to_hz_dl(earfcn) == expected_hz
    assert band_of_earfcn(earfcn) == expected_band


def test_earfcn_to_hz_dl_raises_on_unknown_earfcn():
    with pytest.raises(ValueError) as exc:
        earfcn_to_hz_dl(99999)
    assert "Supported bands" in str(exc.value)


def test_band_of_earfcn_raises_on_unknown():
    with pytest.raises(ValueError):
        band_of_earfcn(99999)
