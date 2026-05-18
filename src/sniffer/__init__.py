"""Cellular-drones UE sniffer pipeline.

Modules:
  schema               — JSONL record dataclasses + IO helpers
  parse_ltesniffer     — turn `DECODED key=value` lines into ue_sighting JSONL
  normalize_ltesniffer — translate LTESniffer text output to the canonical form
  parse_gpsd           — turn gpsd JSON stream into geotag records
  geotag               — join ue_sighting with the nearest GPS fix
  localize             — per-UE RSSI-based positioning (UL grants only)
  live                 — realtime browser dashboard
  report               — text summary + PNG plot
  simulate             — synthetic eNB / UE / GPS streams for tests + demo
  demo                 — end-to-end pipeline driver
"""

__version__ = "0.2.0"
