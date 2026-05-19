"""Cellular-drones UE sniffer pipeline.

The primary flow is C-RNTI extraction across multiple cells. A drone
flies over an area; the SDR + dashboard collect every C-RNTI visible
from each cell along the trajectory. C-RNTI is cell-scoped (so single-
cell decoders only see one cell's UEs) — `survey` cycles through cells
to give you a census.

Entry points (CLI subcommands):
  sniffer scan          — find LTE cells in a band (one-shot)
  sniffer survey        — sweep + dwell, harvest C-RNTIs from every cell
  sniffer live          — single-cell mode (or --simulate for hardware-free)
  sniffer install       — Linux dependency installer

Modules:
  schema             — JSONL record dataclasses + IO helpers
  scan               — wrap srsran_cell_search for cell discovery
  sib1               — wrap pdsch_ue to extract PLMN/TAC/CGI from SIB1
  survey             — multi-cell sweep + dwell orchestrator
  falcon             — tail FalconEye's per-DCI CSV → ue_sighting JSONL
  parse_ltesniffer   — turn LTESniffer text into ue_sighting JSONL
                       (note: stock LTESniffer writes PCAP, not text;
                       FalconEye is the working text-output decoder)
  lte_bands          — EARFCN ↔ Hz helpers per 3GPP TS 36.101
  parse_gpsd         — turn gpsd JSON stream into geotag records
  parse_droneid      — turn DroneID decoder JSON into geotag records
                       (alternative GPS: sniff the drone's own RemoteID)
  droneid_hackrf     — HackRF capture-loop wrapper for file-based
                       DroneID decoders
  spectrum           — hackrf_sweep wrapper, live RF waterfall
  localize           — per-UE RSSI weighted-centroid positioning
                       (needs UL energy → 2× USRP setup, not HackRF)
  ta_multilateration — per-UE TA-range multilateration (UL TA when
                       upstream emits it; simulator-only today)
  live               — realtime browser dashboard
  simulate           — synthetic eNB / UE / GPS streams for tests
"""

__version__ = "0.2.0"
