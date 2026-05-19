"""Cellular-drones UE sniffer pipeline.

Modules:
  schema             — JSONL record dataclasses + IO helpers
  parse_ltesniffer   — turn LTESniffer text into ue_sighting JSONL
  parse_gpsd         — turn gpsd JSON stream into geotag records
  localize           — per-UE RSSI weighted-centroid positioning (UL only)
  ta_multilateration — per-UE TA-range multilateration (UL TA when present)
  live               — realtime browser dashboard
  scan               — wrap srsran_cell_search for cell discovery
  sib1               — wrap pdsch_ue to extract PLMN/TAC/CGI from SIB1
  simulate           — synthetic eNB / UE / GPS streams for tests
"""

__version__ = "0.2.0"
