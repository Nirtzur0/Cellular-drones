"""Decode MIB / SIB1 / SIB3 / SIB5 for a target cell via srsRAN's `pdsch_ue`.

`pdsch_ue` can be pointed at a center frequency + PCI and asked to decode
SI-RNTI (i.e. System Information broadcasts). We use it for two things:

  * SIB1 — PLMN/TAC/CGI, broadcast every 80 ms. Fast: 10 s is enough.
  * Neighbor discovery — SIB3 (intrafreq neighbor PCIs) and SIB5
    (interfreq neighbor EARFCN+PCI pairs), broadcast every 320–2560 ms.
    Needs 30 s to be reliable.

Both paths run pdsch_ue as a one-shot subprocess with a hard timeout.
Never raises — failure silently skips enrichment.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Optional

DEFAULT_BINARY = "pdsch_ue"
DEFAULT_TIMEOUT_S = 10
DEFAULT_NEIGHBOR_TIMEOUT_S = 30

# Lines pdsch_ue prints when SIB1 decodes successfully. Format drifts
# across srsRAN versions, so use permissive regexes anchored on the
# field name. Examples observed in the wild:
#
#   PLMN: 425 01           ┐ early versions
#   MCC=425 MNC=01         ┘
#   TAC=18452 / "TAC: 18452" / "tac: 0x4814"
#   cellIdentity: 67305473 / cell_identity=0x4031801
# Match MCC/MNC literally — preserve leading zeros, since MNC width
# is part of the identifier (2-digit MNCs commonly start with 0).
# SIB3/SIB5 neighbor parsing. srsRAN's ASN.1 printer format drifts across
# versions; these regexes are permissive on separators and case.
_SIB_HDR_RE = re.compile(r"\bSIB(\d+)\b", re.IGNORECASE)
_DL_CARRIER_RE = re.compile(
    r"\bdl[-_]?[Cc]arrier[Ff]req\s*[:=]\s*(\d+)", re.IGNORECASE
)
_PHYS_CELL_RE = re.compile(r"\bphys[Cc]ell[Ii][Dd]\s*[:=]\s*(\d+)", re.IGNORECASE)

_MCC_RE = re.compile(r"\bMCC\s*[:=]\s*([0-9]{1,3})", re.IGNORECASE)
_MNC_RE = re.compile(r"\bMNC\s*[:=]\s*([0-9]{1,3})", re.IGNORECASE)
_PLMN_RE = re.compile(r"\bPLMN\s*[:=]\s*([0-9]{5,6})", re.IGNORECASE)
_PLMN_PAIR_RE = re.compile(r"\bPLMN\s*[:=]\s*([0-9]{3})\s+([0-9]{2,3})",
                           re.IGNORECASE)
_TAC_RE = re.compile(r"\bTAC\s*[:=]\s*(0x[0-9a-fA-F]+|[0-9]+)", re.IGNORECASE)
_CID_RE = re.compile(
    r"\b(?:cell[_ ]?identity|cellId|cgi|cell[_ ]?id)\s*[:=]\s*"
    r"(0x[0-9a-fA-F]+|[0-9]+)",
    re.IGNORECASE,
)


def _to_int(v: str) -> Optional[int]:
    try:
        return int(v, 16) if v.lower().startswith("0x") else int(v)
    except ValueError:
        return None


def parse_sib1_output(text: str) -> dict:
    """Extract MCC/MNC/PLMN/TAC/CGI from pdsch_ue stdout.

    Returns a dict with whatever fields were recoverable. Empty dict if
    nothing matched (the caller treats absence as "decoder didn't lock").
    """
    out: dict = {}
    mcc = mnc = None
    m = _PLMN_PAIR_RE.search(text)
    if m:
        mcc, mnc = m.group(1), m.group(2)
    else:
        m = _PLMN_RE.search(text)
        if m:
            raw = m.group(1)
            # Most operators are 3-digit MCC + 2-digit MNC. 3+3 exists
            # (some North American MNOs); we keep the original string in
            # that case and split as 3+rest.
            mcc = raw[:3]
            mnc = raw[3:]
    if mcc is None:
        m = _MCC_RE.search(text)
        if m:
            mcc = m.group(1)
    if mnc is None:
        m = _MNC_RE.search(text)
        if m:
            mnc = m.group(1)
    if mcc and mnc:
        out["plmn"] = f"{mcc}{mnc}"
    m = _TAC_RE.search(text)
    if m:
        v = _to_int(m.group(1))
        if v is not None:
            out["tac"] = v
    m = _CID_RE.search(text)
    if m:
        v = _to_int(m.group(1))
        if v is not None:
            out["cgi"] = v
    return out


def _run_pdsch_ue(freq_hz: int, pci: int, *,
                  resolved: str, timeout_s: int) -> Optional[str]:
    """Invoke pdsch_ue; return combined stdout+stderr, or None on failure."""
    cmd = [resolved, "-r", "0xffff", "-f", str(freq_hz), "-P", str(pci),
           "-n", str(timeout_s * 1000)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s + 5, check=False)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return None
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def decode_sib1(earfcn: int, pci: int, *,
                binary: str = DEFAULT_BINARY,
                timeout_s: int = DEFAULT_TIMEOUT_S) -> Optional[dict]:
    """Run pdsch_ue once for (earfcn, pci); return PLMN/TAC/CGI or None.

    Returns a dict with whatever fields matched, or None if pdsch_ue is
    missing, fails to start, or times out.  Never raises.
    """
    from sniffer.lte_bands import earfcn_to_hz_dl
    resolved = shutil.which(binary) or (binary if os.path.exists(binary) else None)
    if resolved is None:
        print(f"sib1: `{binary}` not on PATH — skipping SIB1 decode "
              f"for EARFCN={earfcn} PCI={pci}.", file=sys.stderr)
        return None
    try:
        freq_hz = int(earfcn_to_hz_dl(earfcn))
    except (ValueError, KeyError):
        print(f"sib1: unknown EARFCN {earfcn} — skipping.", file=sys.stderr)
        return None
    blob = _run_pdsch_ue(freq_hz, pci, resolved=resolved, timeout_s=timeout_s)
    if blob is None:
        print(f"sib1: pdsch_ue failed/timed out for EARFCN={earfcn} PCI={pci}.",
              file=sys.stderr)
        return None
    info = parse_sib1_output(blob)
    return info or None


def parse_neighbors(text: str, parent_earfcn: int) -> list[dict]:
    """Extract intrafreq (SIB3) and interfreq (SIB5) neighbor cells.

    Returns a list of {"earfcn": int, "pci": int} dicts, deduplicated.
    SIB3 physCellId entries use parent_earfcn; SIB5 entries use the
    dl-CarrierFreq value that precedes them.
    """
    context: Optional[str] = None  # "sib3" | "sib5" | None
    current_earfcn: Optional[int] = None
    seen: set[tuple[int, int]] = set()
    results: list[dict] = []

    for line in text.splitlines():
        m = _SIB_HDR_RE.search(line)
        if m:
            n = int(m.group(1))
            context = "sib3" if n == 3 else ("sib5" if n == 5 else None)
            current_earfcn = None
            continue

        if context == "sib5":
            m = _DL_CARRIER_RE.search(line)
            if m:
                current_earfcn = int(m.group(1))
                continue

        m = _PHYS_CELL_RE.search(line)
        if m:
            pci = int(m.group(1))
            if context == "sib3":
                key = (parent_earfcn, pci)
                if key not in seen:
                    seen.add(key)
                    results.append({"earfcn": parent_earfcn, "pci": pci})
            elif context == "sib5" and current_earfcn is not None:
                key = (current_earfcn, pci)
                if key not in seen:
                    seen.add(key)
                    results.append({"earfcn": current_earfcn, "pci": pci})

    return results


def decode_neighbors(earfcn: int, pci: int, *,
                     binary: str = DEFAULT_BINARY,
                     timeout_s: int = DEFAULT_NEIGHBOR_TIMEOUT_S) -> list[dict]:
    """Run pdsch_ue long enough to capture SIB3/SIB5; return neighbor cells.

    Each item: {"earfcn": int, "pci": int}.  Returns [] on any failure.
    SIB3/SIB5 are broadcast every 320-2560 ms so 30 s gives many windows.
    """
    from sniffer.lte_bands import earfcn_to_hz_dl
    resolved = shutil.which(binary) or (binary if os.path.exists(binary) else None)
    if resolved is None:
        return []
    try:
        freq_hz = int(earfcn_to_hz_dl(earfcn))
    except (ValueError, KeyError):
        return []
    blob = _run_pdsch_ue(freq_hz, pci, resolved=resolved, timeout_s=timeout_s)
    if blob is None:
        return []
    return parse_neighbors(blob, parent_earfcn=earfcn)
