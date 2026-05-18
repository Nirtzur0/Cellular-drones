"""Human-readable summary + trajectory/RSRP/localization plot.

Reads the same `geotagged-*.jsonl` files the localizer reads. Plot uses
matplotlib's default Agg backend so it runs headless.
"""

from __future__ import annotations

import argparse
import glob
import math
import os
from collections import defaultdict

from sniffer.localize import locate_file, weighted_centroid, path_loss_wls
from sniffer.schema import read_jsonl


def _summarize(records: list[dict]) -> dict:
    by_pci: dict[int, list[dict]] = defaultdict(list)
    geotagged = 0
    untagged = 0
    for r in records:
        if r.get("kind") != "cell_sighting":
            continue
        if not r.get("gps"):
            untagged += 1
            continue
        geotagged += 1
        by_pci[int(r["cell"]["pci"])].append(r)
    return {
        "total": geotagged + untagged,
        "geotagged": geotagged,
        "untagged": untagged,
        "by_pci": by_pci,
    }


def text_summary(path: str) -> str:
    records = list(read_jsonl(path))
    summary = _summarize(records)
    lines = []
    lines.append(f"# Report for {path}")
    lines.append(f"  total sightings:    {summary['total']}")
    lines.append(f"  geotagged:          {summary['geotagged']}")
    lines.append(f"  untagged (dropped): {summary['untagged']}")
    lines.append(f"  unique PCIs:        {len(summary['by_pci'])}")
    lines.append("")
    for pci, recs in sorted(summary["by_pci"].items()):
        rsrp_vals = [r["cell"]["rsrp_dbm"] for r in recs if r["cell"].get("rsrp_dbm") is not None]
        lines.append(f"PCI {pci}: {len(recs)} samples, "
                     f"RSRP min={min(rsrp_vals):.1f} max={max(rsrp_vals):.1f} dBm")
        cent = weighted_centroid(recs)
        wls = path_loss_wls(recs)
        if cent is not None:
            alt = f"{cent.alt_m:.1f}" if cent.alt_m is not None else "—"
            lines.append(f"  centroid: lat={cent.lat:.6f} lon={cent.lon:.6f} "
                         f"alt={alt}m cep95={cent.cep95_m:.1f}m")
            if cent.notes:
                lines.append(f"    {cent.notes}")
        if wls is not None:
            alt = f"{wls.alt_m:.1f}" if wls.alt_m is not None else "—"
            lines.append(f"  WLS:      lat={wls.lat:.6f} lon={wls.lon:.6f} "
                         f"alt={alt}m cep95={wls.cep95_m:.1f}m")
    return "\n".join(lines)


def make_plot(path: str, out_png: str,
              ground_truth_lat: float | None = None,
              ground_truth_lon: float | None = None,
              title: str | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    records = list(read_jsonl(path))
    by_pci: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("kind") != "cell_sighting":
            continue
        if not r.get("gps"):
            continue
        by_pci[int(r["cell"]["pci"])].append(r)

    fig, ax = plt.subplots(figsize=(8, 8))

    for pci, recs in sorted(by_pci.items()):
        lats = np.array([r["gps"]["lat"] for r in recs])
        lons = np.array([r["gps"]["lon"] for r in recs])
        rsrp = np.array([r["cell"]["rsrp_dbm"] for r in recs])
        sc = ax.scatter(lons, lats, c=rsrp, cmap="viridis", s=18,
                        label=f"PCI {pci} (n={len(recs)})", alpha=0.85)
        cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("RSRP (dBm)")

        cent = weighted_centroid(recs)
        if cent is not None:
            ax.plot(cent.lon, cent.lat, marker="x", color="red",
                    markersize=14, mew=3,
                    label=f"PCI {pci} centroid")
        wls = path_loss_wls(recs)
        if wls is not None:
            ax.plot(wls.lon, wls.lat, marker="+", color="darkorange",
                    markersize=18, mew=3,
                    label=f"PCI {pci} WLS")

    if ground_truth_lat is not None and ground_truth_lon is not None:
        ax.plot(ground_truth_lon, ground_truth_lat, marker="*",
                color="black", markersize=18, label="ground truth")

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(title or "Drone trajectory, RSRP, and emitter estimate")
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="datalim")
    # Tight legend, dedupe duplicate scatter color entries
    handles, labels = ax.get_legend_handles_labels()
    seen = set()
    keep = []
    for h, l in zip(handles, labels):
        if l in seen:
            continue
        seen.add(l)
        keep.append((h, l))
    ax.legend(*zip(*keep), loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input_glob", help="glob for geotagged-*.jsonl files")
    p.add_argument("--plot", help="path to write PNG (optional)")
    p.add_argument("--truth-lat", type=float, default=None)
    p.add_argument("--truth-lon", type=float, default=None)
    args = p.parse_args()
    paths = sorted(glob.glob(args.input_glob))
    if not paths:
        print(f"no files match {args.input_glob}")
        return 1
    for path in paths:
        print(text_summary(path))
        print()
        if args.plot:
            out = args.plot if len(paths) == 1 else (
                args.plot.replace(".png", f"-{os.path.basename(path)}.png")
            )
            make_plot(path, out, args.truth_lat, args.truth_lon)
            print(f"wrote plot: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
