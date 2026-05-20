"""3GPP LTE EARFCN ↔ frequency helpers.

LTESniffer (and FALCON, and srsRAN's example tools) take downlink
frequency in **Hz**, not the EARFCN integer commonly used to refer to
a cell. Our `sniffer live --earfcn N --pci P` CLI surface uses EARFCN
because that's what cell-search tools and OpenCellID report — we
convert to Hz right before invoking LTESniffer.

Per 3GPP TS 36.101 § 5.7.3, for each E-UTRA operating band the DL
center frequency is:

    F_DL = F_DL_low + 0.1 * (N_DL - N_offs_DL)        (MHz)

where N_DL is the EARFCN and (F_DL_low, N_offs_DL) are band-specific.
We hard-code the bands most relevant for this project (commonly
deployed FDD bands in EU/US/APAC + the LTE-TDD bands used in China).
For bands not listed, callers get a clear error and can either add the
band here or pass `--center-hz` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Band:
    band: int
    dl_low_mhz: float
    dl_n_offs: int
    dl_earfcn_lo: int
    dl_earfcn_hi: int


# Subset of bands. Add more as needed; per 3GPP TS 36.101 Table 5.7.3-1.
_BANDS: tuple[_Band, ...] = (
    _Band(band=1,  dl_low_mhz=2110.0, dl_n_offs=0,     dl_earfcn_lo=0,     dl_earfcn_hi=599),
    _Band(band=2,  dl_low_mhz=1930.0, dl_n_offs=600,   dl_earfcn_lo=600,   dl_earfcn_hi=1199),
    _Band(band=3,  dl_low_mhz=1805.0, dl_n_offs=1200,  dl_earfcn_lo=1200,  dl_earfcn_hi=1949),
    _Band(band=4,  dl_low_mhz=2110.0, dl_n_offs=1950,  dl_earfcn_lo=1950,  dl_earfcn_hi=2399),
    _Band(band=5,  dl_low_mhz=869.0,  dl_n_offs=2400,  dl_earfcn_lo=2400,  dl_earfcn_hi=2649),
    _Band(band=7,  dl_low_mhz=2620.0, dl_n_offs=2750,  dl_earfcn_lo=2750,  dl_earfcn_hi=3449),
    _Band(band=8,  dl_low_mhz=925.0,  dl_n_offs=3450,  dl_earfcn_lo=3450,  dl_earfcn_hi=3799),
    _Band(band=12, dl_low_mhz=729.0,  dl_n_offs=5010,  dl_earfcn_lo=5010,  dl_earfcn_hi=5179),
    _Band(band=13, dl_low_mhz=746.0,  dl_n_offs=5180,  dl_earfcn_lo=5180,  dl_earfcn_hi=5279),
    _Band(band=17, dl_low_mhz=734.0,  dl_n_offs=5730,  dl_earfcn_lo=5730,  dl_earfcn_hi=5849),
    _Band(band=20, dl_low_mhz=791.0,  dl_n_offs=6150,  dl_earfcn_lo=6150,  dl_earfcn_hi=6449),
    _Band(band=25, dl_low_mhz=1930.0, dl_n_offs=8040,  dl_earfcn_lo=8040,  dl_earfcn_hi=8689),
    _Band(band=26, dl_low_mhz=859.0,  dl_n_offs=8690,  dl_earfcn_lo=8690,  dl_earfcn_hi=9039),
    _Band(band=28, dl_low_mhz=758.0,  dl_n_offs=9210,  dl_earfcn_lo=9210,  dl_earfcn_hi=9659),
    # TDD bands (note: the same formula applies; for TDD bands UL == DL freq)
    _Band(band=38, dl_low_mhz=2570.0, dl_n_offs=37750, dl_earfcn_lo=37750, dl_earfcn_hi=38249),
    _Band(band=40, dl_low_mhz=2300.0, dl_n_offs=38650, dl_earfcn_lo=38650, dl_earfcn_hi=39649),
    _Band(band=41, dl_low_mhz=2496.0, dl_n_offs=39650, dl_earfcn_lo=39650, dl_earfcn_hi=41589),
    _Band(band=42, dl_low_mhz=3400.0, dl_n_offs=41590, dl_earfcn_lo=41590, dl_earfcn_hi=43589),
    _Band(band=43, dl_low_mhz=3600.0, dl_n_offs=43590, dl_earfcn_lo=43590, dl_earfcn_hi=45589),
)


def earfcn_to_hz_dl(earfcn: int) -> int:
    """Return the DL center frequency in Hz for an LTE EARFCN.

    Raises ValueError if the EARFCN doesn't fall in any of the bands
    we know about. The error message lists the supported bands so the
    user can either add their band to `_BANDS` or pass `--center-hz`
    directly.
    """
    for b in _BANDS:
        if b.dl_earfcn_lo <= earfcn <= b.dl_earfcn_hi:
            f_mhz = b.dl_low_mhz + 0.1 * (earfcn - b.dl_n_offs)
            return int(round(f_mhz * 1e6))
    known = ", ".join(str(b.band) for b in _BANDS)
    raise ValueError(
        f"EARFCN {earfcn} doesn't match any band we have a table for. "
        f"Supported bands: {known}. Either extend sniffer/lte_bands.py "
        f"or pass --center-hz explicitly."
    )


def hz_to_earfcn_dl(freq_hz: float) -> int:
    """Return the EARFCN for a DL center frequency in Hz.

    Inverse of earfcn_to_hz_dl. Raises ValueError if the frequency
    doesn't fall within any known band.
    """
    freq_mhz = freq_hz / 1e6
    for b in _BANDS:
        dl_hi_mhz = b.dl_low_mhz + 0.1 * (b.dl_earfcn_hi - b.dl_n_offs)
        if b.dl_low_mhz - 0.05 <= freq_mhz <= dl_hi_mhz + 0.05:
            earfcn = b.dl_n_offs + int(round((freq_mhz - b.dl_low_mhz) / 0.1))
            earfcn = max(b.dl_earfcn_lo, min(b.dl_earfcn_hi, earfcn))
            return earfcn
    raise ValueError(
        f"{freq_hz:.0f} Hz doesn't match any known LTE DL band."
    )


def band_of_earfcn(earfcn: int) -> int:
    """Return the LTE band number for a DL EARFCN.

    Raises ValueError if unknown (same constraint as `earfcn_to_hz_dl`).
    """
    for b in _BANDS:
        if b.dl_earfcn_lo <= earfcn <= b.dl_earfcn_hi:
            return b.band
    raise ValueError(f"EARFCN {earfcn} not in any known band table.")
