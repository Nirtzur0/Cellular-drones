"""Standalone LTE PSS detector.

Generates the 3 LTE Primary Synchronization Sequences (NID2 = 0, 1, 2) in
the time domain at 1.92 Msps, then cross-correlates an IQ capture against
each. A peak whose magnitude exceeds the local noise floor by a configurable
threshold (default 8 dB) at a position consistent with the 5 ms PSS repeat
interval declares a detection.

PSS gives NID2. SSS (at the symbol immediately before PSS, +/- depending on
TDD/FDD) gives NID1 in 0..167. PCI = 3*NID1 + NID2 ∈ {0..503}.

This module implements PSS detection only. PCI extraction requires SSS,
which is added in :func:`detect_pci` by extracting the symbol before each
PSS hit and correlating against the 168*2 SSS candidates (FDD subframe 0
vs. subframe 5 differs).

Reference: 3GPP TS 36.211 §6.11.1 (PSS), §6.11.2 (SSS).
"""

from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Optional

FS = 1.92e6           # base LTE sample rate used here (= 30.72e6 / 16)
N_FFT = 128           # subframe symbol size at 1.92 Msps
CP_NORMAL = 9         # short cyclic prefix at 1.92 Msps (10 for first symbol of slot)
SUBFRAME_SAMPLES = int(FS * 1e-3)   # 1920 samples per 1ms subframe
HALF_FRAME = 5 * SUBFRAME_SAMPLES   # PSS repeats every 5 ms (FDD)
PSS_ROOTS = (25, 29, 34)


def _zc_pss(u: int) -> np.ndarray:
    """Length-62 LTE PSS frequency-domain sequence (subcarriers -31..-1, 1..31)."""
    seq = np.zeros(62, dtype=np.complex64)
    for n in range(31):
        seq[n] = np.exp(-1j * np.pi * u * n * (n + 1) / 63)
    for n in range(31, 62):
        seq[n] = np.exp(-1j * np.pi * u * (n + 1) * (n + 2) / 63)
    return seq


def pss_time_domain(nid2: int) -> np.ndarray:
    """Time-domain PSS (length N_FFT) at 1.92 Msps for the given NID2."""
    if nid2 not in (0, 1, 2):
        raise ValueError(f"nid2 must be 0,1,2 (got {nid2})")
    u = PSS_ROOTS[nid2]
    freq = _zc_pss(u)
    # Place onto an N_FFT subcarrier grid: indices -31..-1 → fft bins
    # N_FFT-31..N_FFT-1, and indices 1..31 → bins 1..31. DC (bin 0) is null.
    spec = np.zeros(N_FFT, dtype=np.complex64)
    spec[1:32] = freq[31:]
    spec[N_FFT - 31:] = freq[:31]
    # IFFT and normalize to unit energy
    td = np.fft.ifft(spec).astype(np.complex64) * N_FFT
    td /= np.sqrt(np.sum(np.abs(td) ** 2))
    return td


@dataclass
class PssHit:
    nid2: int           # 0, 1, or 2
    sample_index: int   # offset in the IQ stream (samples at 1.92 Msps)
    metric_db: float    # 10·log10(|xcorr|^2 / local_noise_power)
    freq_offset_hz: float = 0.0   # estimated coarse Doppler / TCXO drift

    @property
    def time_ms(self) -> float:
        return self.sample_index / FS * 1000.0


def _resample_to_192(iq: np.ndarray, fs_in: float) -> np.ndarray:
    """Resample input IQ to 1.92 Msps. Uses simple polyphase (rational)."""
    from math import gcd
    fs_out = FS
    if fs_in == fs_out:
        return iq.astype(np.complex64)
    # rational resampling: ratio up/down
    target = int(round(fs_out))
    src = int(round(fs_in))
    g = gcd(target, src)
    up, down = target // g, src // g
    # scipy.signal.resample_poly is the right tool
    from scipy.signal import resample_poly
    out = resample_poly(iq, up, down).astype(np.complex64)
    return out


def _xcorr_full(iq: np.ndarray, template: np.ndarray) -> np.ndarray:
    """FFT-based valid cross-correlation; returns |xcorr|^2 of length len(iq)."""
    nfft = 1 << ((len(iq) + len(template) - 1) - 1).bit_length()
    IQ = np.fft.fft(iq, nfft)
    T = np.fft.fft(template, nfft)
    return np.abs(np.fft.ifft(IQ * T)[: len(iq)]) ** 2


def detect_pss(
    iq: np.ndarray,
    fs_in: float,
    *,
    threshold_db: float = 12.0,
    freq_offsets_hz: tuple[float, ...] = (
        -100e3, -75e3, -50e3, -25e3, -10e3, 0.0, 10e3, 25e3, 50e3, 75e3, 100e3,
    ),
    require_periodic: bool = True,
    period_tol: int = 64,  # samples
    max_hits: int = 16,
) -> list[PssHit]:
    """Locate periodic PSS peaks in an IQ capture.

    Scans coarse frequency offsets (HackRF TCXO drifts up to ±50-100 kHz at
    1.8 GHz). For each (NID2, freq_offset) candidate, computes the matched
    filter response and looks for peaks that repeat with the 5 ms PSS half-
    frame period — that is the lock the receiver gets when it actually finds
    a cell, and it is what distinguishes a real PSS from a noise peak.
    """
    iq_192 = _resample_to_192(iq, fs_in).astype(np.complex64)
    n_samples = len(iq_192)
    if n_samples < 4 * HALF_FRAME:
        # Need at least 2 PSS reps for periodicity check.
        return []

    t = np.arange(n_samples, dtype=np.float32) / FS
    hits: list[PssHit] = []

    for nid2 in (0, 1, 2):
        template = pss_time_domain(nid2)[::-1].conj().astype(np.complex64)
        best_for_nid2: Optional[tuple[float, int, float]] = None  # (score, idx, foffset)
        for foffset in freq_offsets_hz:
            # Apply -foffset to the IQ before correlation
            mixer = np.exp(-2j * np.pi * foffset * t).astype(np.complex64)
            mag2 = _xcorr_full(iq_192 * mixer, template)
            noise = np.median(mag2) + 1e-12
            # For each candidate peak position p (within the first 5 ms), the
            # accumulated metric is the geometric-mean-like SUM_k log mag2(p + k*HALF_FRAME).
            n_reps = (n_samples - 1) // HALF_FRAME
            if n_reps < 2:
                continue
            stacked = np.zeros(HALF_FRAME, dtype=np.float64)
            count = 0
            for k in range(n_reps):
                segment = mag2[k * HALF_FRAME : (k + 1) * HALF_FRAME]
                if len(segment) == HALF_FRAME:
                    stacked += segment
                    count += 1
            if count < 2:
                continue
            stacked /= count
            # noise-normalized
            stacked_db = 10 * np.log10(stacked / noise + 1e-12)
            peak_idx = int(np.argmax(stacked_db))
            peak_db = float(stacked_db[peak_idx])
            if best_for_nid2 is None or peak_db > best_for_nid2[0]:
                best_for_nid2 = (peak_db, peak_idx, foffset)
        if best_for_nid2 is None:
            continue
        score, idx, foffset = best_for_nid2
        if score >= threshold_db:
            hits.append(PssHit(nid2=nid2, sample_index=int(idx), metric_db=score,
                                freq_offset_hz=foffset))
    hits.sort(key=lambda h: -h.metric_db)
    return hits[:max_hits]


# --- SSS (placeholder — full 168-candidate SSS correlation is the next step) ---

def m_sequence(initial: list[int], taps: list[int], length: int) -> np.ndarray:
    """Generate a binary m-sequence as 0/1 array of `length`."""
    state = list(initial)
    out = np.zeros(length, dtype=np.int8)
    for i in range(length):
        out[i] = state[-1]
        nxt = 0
        for t in taps:
            nxt ^= state[t]
        state = state[1:] + [nxt]
    return out


def _sss_base_sequences() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return the three length-31 base sequences (s_tilde, c_tilde, z_tilde)
    used to build the SSS, per TS 36.211 §6.11.2.1."""
    # s_tilde from x(i+5)=(x(i+2)+x(i)) mod 2, init [0,0,0,0,1]
    x = [0, 0, 0, 0, 1]
    s_bin = np.zeros(31, dtype=np.int8)
    for i in range(31):
        s_bin[i] = x[0]
        new = (x[2] + x[0]) % 2
        x = x[1:] + [new]
    s = 1 - 2 * s_bin.astype(np.int8)
    # c_tilde from x(i+5)=(x(i+3)+x(i)) mod 2, init [0,0,0,0,1]
    x = [0, 0, 0, 0, 1]
    c_bin = np.zeros(31, dtype=np.int8)
    for i in range(31):
        c_bin[i] = x[0]
        new = (x[3] + x[0]) % 2
        x = x[1:] + [new]
    c = 1 - 2 * c_bin.astype(np.int8)
    # z_tilde from x(i+5)=(x(i+4)+x(i+2)+x(i+1)+x(i)) mod 2, init [0,0,0,0,1]
    x = [0, 0, 0, 0, 1]
    z_bin = np.zeros(31, dtype=np.int8)
    for i in range(31):
        z_bin[i] = x[0]
        new = (x[4] + x[2] + x[1] + x[0]) % 2
        x = x[1:] + [new]
    z = 1 - 2 * z_bin.astype(np.int8)
    return s, c, z


def sss_frequency_domain(nid1: int, nid2: int, subframe: int) -> np.ndarray:
    """Build length-62 SSS frequency-domain sequence.

    `subframe` is 0 (subframe 0 of the frame, before PSS in slot 0) or 5
    (subframe 5, before PSS in slot 10). The two are swapped halves of the
    same construction, which is how the receiver disambiguates them.
    """
    if subframe not in (0, 5):
        raise ValueError("subframe must be 0 or 5")
    q_prime = nid1 // 30
    q = (nid1 + q_prime * (q_prime + 1) // 2) // 30
    m_prime = nid1 + q * (q + 1) // 2
    m0 = m_prime % 31
    m1 = (m0 + (m_prime // 31) + 1) % 31
    s_t, c_t, z_t = _sss_base_sequences()
    # cyclic shifts
    s0 = np.roll(s_t, -m0)
    s1 = np.roll(s_t, -m1)
    c0 = np.roll(c_t, -nid2)
    c1 = np.roll(c_t, -(nid2 + 3))
    z1_m0 = np.roll(z_t, -(m0 % 8))
    z1_m1 = np.roll(z_t, -(m1 % 8))
    # interleave even/odd subcarriers
    seq = np.zeros(62, dtype=np.complex64)
    if subframe == 0:
        seq[0::2] = s0 * c0
        seq[1::2] = s1 * c1 * z1_m0
    else:  # subframe 5: swapped
        seq[0::2] = s1 * c0
        seq[1::2] = s0 * c1 * z1_m1
    return seq


def extract_symbol_freq_domain(
    iq_192: np.ndarray, sample_index: int, *, offset_back_symbols: int = 1
) -> Optional[np.ndarray]:
    """Pull one OFDM symbol at 1.92 Msps and FFT it. PSS sits in the last
    symbol of slot 0 (symbol 6, FDD); SSS is the symbol immediately before
    it. At 1.92 Msps each symbol is 128 + 9 ≈ 137 samples (10 for first
    symbol of each slot)."""
    # Approximate: each symbol is N_FFT + CP_NORMAL = 137 samples on average.
    sym_len = N_FFT + CP_NORMAL
    start = sample_index - offset_back_symbols * sym_len - N_FFT + 1
    if start < 0 or start + N_FFT > len(iq_192):
        return None
    # Skip cyclic prefix and grab N_FFT samples
    sym = iq_192[start + CP_NORMAL : start + CP_NORMAL + N_FFT]
    if len(sym) != N_FFT:
        return None
    spec = np.fft.fft(sym)
    # Extract subcarriers -31..-1, 1..31 (skip DC)
    sc = np.zeros(62, dtype=np.complex64)
    sc[31:] = spec[1:32]
    sc[:31] = spec[N_FFT - 31:]
    return sc


def detect_pci(iq: np.ndarray, fs_in: float, **kwargs) -> list[dict]:
    """Best-effort PCI extraction: detect PSS, then attempt SSS correlation.

    Returns a list of dicts: {pci, nid1, nid2, mode, sample_index, time_ms,
    pss_db, sss_db}. If SSS cannot be recovered, `pci`/`nid1` are None and
    only PSS info is returned.
    """
    iq_192 = _resample_to_192(iq, fs_in)
    hits = detect_pss(iq_192, FS, **kwargs)
    results = []
    for h in hits:
        sym = extract_symbol_freq_domain(iq_192, h.sample_index, offset_back_symbols=1)
        entry = {
            "nid2": h.nid2,
            "sample_index": h.sample_index,
            "time_ms": h.time_ms,
            "pss_db": round(h.metric_db, 2),
            "nid1": None,
            "pci": None,
            "sss_db": None,
            "mode": "fdd",  # FDD assumption; TDD would shift SSS to symbol 2 of slot 1
        }
        if sym is not None:
            best_corr = -np.inf
            best_nid1 = None
            best_sf = None
            for nid1 in range(168):
                for sf in (0, 5):
                    ref = sss_frequency_domain(nid1, h.nid2, sf)
                    # Energy-normalized correlation
                    num = np.abs(np.vdot(ref, sym)) ** 2
                    den = (np.vdot(ref, ref).real * np.vdot(sym, sym).real) + 1e-12
                    metric = 10 * np.log10(num / den + 1e-12)
                    if metric > best_corr:
                        best_corr = metric
                        best_nid1 = nid1
                        best_sf = sf
            entry["nid1"] = int(best_nid1)
            entry["sss_db"] = round(float(best_corr), 2)
            entry["sss_subframe"] = int(best_sf)
            entry["pci"] = 3 * int(best_nid1) + h.nid2
        results.append(entry)
    return results


if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("--iq", required=True, help="Path to interleaved cs8 IQ file")
    p.add_argument("--fs", type=float, required=True, help="Sample rate of IQ file (Hz)")
    p.add_argument("--threshold-db", type=float, default=8.0)
    p.add_argument("--pci", action="store_true", help="Also run SSS / PCI extraction")
    args = p.parse_args()
    raw = np.fromfile(args.iq, dtype=np.int8)
    iq = (raw[::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)) / 128.0
    iq = iq.astype(np.complex64)
    if args.pci:
        for r in detect_pci(iq, args.fs, threshold_db=args.threshold_db):
            print(json.dumps(r))
    else:
        for h in detect_pss(iq, args.fs, threshold_db=args.threshold_db):
            print(json.dumps(dict(nid2=h.nid2, sample_index=h.sample_index,
                                  time_ms=round(h.time_ms, 4),
                                  metric_db=round(h.metric_db, 2))))
