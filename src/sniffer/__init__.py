"""Cellular-drones UE sniffer pipeline.

One run path: a USRP-driven `FalconEye` decode of one LTE downlink cell,
fanned into a realtime browser dashboard with per-UE C-RNTI tracking
and weighted-centroid positioning.

Entry points (CLI subcommands):
  sniffer scan          — find LTE cells in a band (one-shot)
  sniffer survey        — sweep + dwell, harvest C-RNTIs from every cell
  sniffer live          — single-cell mode (or --simulate for hardware-free)
  sniffer install       — Linux dependency installer

Modules:
  schema       — JSONL record dataclasses + IO helpers
  scan         — wrap srsran_cell_search for cell discovery
  sib1         — wrap pdsch_ue to extract PLMN/TAC/CGI from SIB1
  survey       — multi-cell sweep + dwell orchestrator (FalconEye)
  falcon       — tail FalconEye's per-DCI CSV → ue_sighting JSONL
                 (the single PDCCH decoder for real radio)
  lte_bands    — EARFCN ↔ Hz helpers per 3GPP TS 36.101
  parse_gpsd   — turn gpsd JSON stream into geotag records
  spectrum     — uhd_sweep wrapper, live RF waterfall
  uhd_sweep    — UHD-based spectrum sweep (USRP B-series)
  localize     — per-UE RSSI weighted-centroid positioning
  live         — realtime browser dashboard
  simulate     — synthetic FalconEye TSV + gpsd streams for tests
"""

__version__ = "0.2.0"
