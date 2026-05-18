"""Cellular-drones sniffer pipeline.

Modules:
  schema           — JSONL record dataclasses, validation, IO helpers
  parse_cellsearch — turn LTE-Cell-Scanner stdout into schema records
  parse_gpsd       — turn gpsd JSON stream into geotag records
  geotag           — join cell sightings with the nearest GPS fix
  localize         — RSSI-based emitter localization
"""

__version__ = "0.1.0"
