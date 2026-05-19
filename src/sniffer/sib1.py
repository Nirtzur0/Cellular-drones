"""Decode MIB / SIB1 for a target cell via srsRAN's `pdsch_ue` example.

`pdsch_ue` (built alongside srsRAN_4G; installed as `srsran_pdsch_ue`
in some packaging) can be pointed at an EARFCN+PCI and asked to decode
SI-RNTI (i.e. SIB1) for a few seconds. SIB1 carries the PLMN list,
TAC, and cell identity — the cell-side identifiers `srsran_cell_search`
alone doesn't surface.

This is **scan-time only**. Live `LTESniffer` doesn't do PDSCH, so the
enrichment can't ride the live pipeline; we run it as a one-shot per
cell when the user passes `--decode-sib1`. The binary is fragile, so
we cap it with a hard timeout and kill it on expiry.
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


def decode_sib1(earfcn: int, pci: int, *,
                binary: str = DEFAULT_BINARY,
                timeout_s: int = DEFAULT_TIMEOUT_S) -> Optional[dict]:
    """Run pdsch_ue once for (earfcn, pci); return PLMN/TAC/CGI or None.

    Returns:
      - dict with whatever fields matched (may be empty if pdsch_ue ran
        but didn't lock cleanly).
      - None if pdsch_ue is missing, fails to start, or times out.

    Never raises — failure is the silent-skip surface for `--decode-sib1`.
    """
    resolved = shutil.which(binary) or (binary if os.path.exists(binary) else None)
    if resolved is None:
        print(f"sib1: `{binary}` not on PATH — skipping SIB1 decode "
              f"for EARFCN={earfcn} PCI={pci}.", file=sys.stderr)
        return None
    cmd = [resolved, "--rnti=SI", "-f", str(earfcn), "-P", str(pci)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, check=False)
    except FileNotFoundError:
        print(f"sib1: failed to launch `{resolved}` for EARFCN={earfcn} "
              f"PCI={pci}.", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print(f"sib1: pdsch_ue timed out after {timeout_s}s on "
              f"EARFCN={earfcn} PCI={pci}.", file=sys.stderr)
        return None
    # pdsch_ue interleaves stdout/stderr; parse both.
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    info = parse_sib1_output(blob)
    return info or None
