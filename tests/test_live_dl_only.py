"""Tests for live.State's DL-only mode auto-detection.

DL-only mode is the HackRF / single-radio LTE path: PDCCH is decoded so
C-RNTIs and UL/DL grants are visible, but the UE's actual UL
transmission energy (`ul_rssi_dbm`) never reaches us — only an UL
listener tuned to the UL band can produce that. The dashboard needs to
know when we're in this mode so it can stop promising positioning.
"""

from __future__ import annotations

from typing import Optional

from sniffer.live import DL_ONLY_DETECTION_THRESHOLD, State
from sniffer.schema import RadioConfig, UeEvent, UeSighting, mono_ns, utc_iso


def _sighting(*, c_rnti: int = 0x4ad2, direction: str = "dl",
              ul_rssi_dbm: Optional[float] = None,
              dl_rsrp_dbm: Optional[float] = None) -> UeSighting:
    radio = RadioConfig(backend="test", device="test-0",
                        center_hz=1_842_500_000.0,
                        sample_rate_sps=23.04e6, rx_gain_db=50.0)
    ue = UeEvent(pci=271, c_rnti=c_rnti, direction=direction,
                 dci_format="0" if direction == "ul" else "1A",
                 mcs=10, n_prb=4, tbs_bytes=320,
                 ul_rssi_dbm=ul_rssi_dbm, dl_rsrp_dbm=dl_rsrp_dbm)
    return UeSighting(mission_id="t", capture_id="test-0",
                      ts_mono_ns=mono_ns(), ts_utc=utc_iso(),
                      radio=radio, ue=ue)


def test_dl_only_starts_unknown():
    """Before any grants land, dl_only is None ('still measuring')."""
    state = State()
    assert state.snapshot()["dl_only"] is None


def test_dl_only_flips_true_after_threshold_grants_without_ul_rssi():
    """Pure DL-only stream: no ul_rssi_dbm ever — after THRESHOLD
    grants we conclude the upstream can't produce UL energy."""
    state = State()
    for _ in range(DL_ONLY_DETECTION_THRESHOLD):
        state.ingest_ue(_sighting(direction="dl", dl_rsrp_dbm=-90.0))
    assert state.snapshot()["dl_only"] is True


def test_dl_only_flips_false_on_first_ul_rssi_observation():
    """A single grant carrying ul_rssi_dbm tells us the upstream is a
    real UL listener — dl_only locks False even before the threshold."""
    state = State()
    # Send a few grants with no UL energy:
    for _ in range(10):
        state.ingest_ue(_sighting(direction="dl", dl_rsrp_dbm=-90.0))
    assert state.snapshot()["dl_only"] is None  # not yet decided
    # Now one UL grant with measured energy:
    state.ingest_ue(_sighting(direction="ul", ul_rssi_dbm=-92.0))
    assert state.snapshot()["dl_only"] is False


def test_dl_only_stays_false_after_decision():
    """Once we've seen real UL energy, later DL-only grants don't flip
    us back to True. dl_only is sticky once decided."""
    state = State()
    state.ingest_ue(_sighting(direction="ul", ul_rssi_dbm=-92.0))
    assert state.snapshot()["dl_only"] is False
    for _ in range(DL_ONLY_DETECTION_THRESHOLD * 2):
        state.ingest_ue(_sighting(direction="dl", dl_rsrp_dbm=-90.0))
    assert state.snapshot()["dl_only"] is False


def test_dl_only_in_broadcast_ue_sighting_event():
    """Each per-sighting SSE event carries dl_only so listeners don't
    have to wait for the next snapshot to learn the mode."""
    state = State()
    seen_dl_only_values = []

    # Hook the broadcast.
    orig = state._broadcast
    def capture(event):
        if event.get("type") == "ue_sighting":
            seen_dl_only_values.append(event.get("dl_only"))
        return orig(event)
    state._broadcast = capture  # type: ignore[assignment]

    # Drive enough DL-only grants to flip the flag.
    for _ in range(DL_ONLY_DETECTION_THRESHOLD + 1):
        state.ingest_ue(_sighting(direction="dl", dl_rsrp_dbm=-90.0))

    # First grant goes out with dl_only=None (still measuring);
    # the post-threshold grant should ship dl_only=True.
    assert seen_dl_only_values[0] is None
    assert seen_dl_only_values[-1] is True
