"""Human-readable summary + per-UE positioning plot.

Reads the same `geotagged-*.jsonl` files the localizer reads. Plot uses
matplotlib's Agg backend so it runs headless.
"""

from __future__ import annotations

import argparse
import glob
import os
from collections import defaultdict

from sniffer.localize import path_loss_wls_ue, weighted_centroid_ue
from sniffer.schema import read_jsonl


def _summarize(records: list[dict]) -> dict:
    """Group ue_sighting records by (PCI, C-RNTI)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    untagged = 0
    geotagged = 0
    for r in records:
        if r.get("kind") != "ue_sighting":
            continue
        if not r.get("gps"):
            untagged += 1
            continue
        geotagged += 1
        key = (int(r["ue"]["pci"]), int(r["ue"]["c_rnti"]))
        groups[key].append(r)
    return {
        "total": geotagged + untagged,
        "geotagged": geotagged,
        "untagged": untagged,
        "by_ue": groups,
    }


def text_summary(path: str, default_ues=None) -> str:
    records = list(read_jsonl(path))
    summary = _summarize(records)
    truth_by_rnti = {ue.c_rnti: ue for ue in (default_ues or [])}
    lines = [
        f"# UE report for {path}",
        f"  total UE sightings:    {summary['total']}",
        f"  geotagged:             {summary['geotagged']}",
        f"  untagged (dropped):    {summary['untagged']}",
        f"  unique (PCI, C-RNTI):  {len(summary['by_ue'])}",
        "",
    ]
    if not summary["by_ue"]:
        lines.append("  (no UE sightings — confirm the LTESniffer feed is live)")
        return "\n".join(lines)
    for (pci, rnti), recs in sorted(summary["by_ue"].items()):
        ul = [r for r in recs
              if (r.get("ue") or {}).get("direction", "").lower() == "ul"
              and r["ue"].get("ul_rssi_dbm") is not None]
        dl = [r for r in recs
              if (r.get("ue") or {}).get("direction", "").lower() == "dl"]
        lines.append(f"PCI {pci} C-RNTI {rnti:#06x}: "
                     f"{len(recs)} grants ({len(ul)} UL, {len(dl)} DL)")
        if ul:
            ul_vals = [r["ue"]["ul_rssi_dbm"] for r in ul]
            lines.append(f"  UL RSSI min={min(ul_vals):.1f} max={max(ul_vals):.1f} dBm")
            cent = weighted_centroid_ue(recs)
            wls = path_loss_wls_ue(recs)
            if cent is not None:
                alt = f"{cent.alt_m:.1f}" if cent.alt_m is not None else "—"
                lines.append(f"  centroid: lat={cent.lat:.6f} lon={cent.lon:.6f} "
                             f"alt={alt}m cep95={cent.cep95_m:.1f}m")
            if wls is not None:
                alt = f"{wls.alt_m:.1f}" if wls.alt_m is not None else "—"
                lines.append(f"  WLS:      lat={wls.lat:.6f} lon={wls.lon:.6f} "
                             f"alt={alt}m cep95={wls.cep95_m:.1f}m")
            if rnti in truth_by_rnti and cent is not None:
                from pyproj import Geod
                _, _, err = Geod(ellps="WGS84").inv(
                    truth_by_rnti[rnti].lon, truth_by_rnti[rnti].lat,
                    cent.lon, cent.lat)
                tag = " (mobile UE — bias expected)" if truth_by_rnti[rnti].waypoints else ""
                lines.append(f"  error vs truth (centroid): {err:.1f} m{tag}")
        else:
            lines.append("  (no UL grants — DL-only grants cannot localize a UE)")
    return "\n".join(lines)


def make_plot(path: str, out_png: str, truth_ues=None,
              title: str | None = None) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    records = list(read_jsonl(path))
    summary = _summarize(records)
    fig, ax = plt.subplots(figsize=(9, 8))

    truth_by_rnti = {ue.c_rnti: ue for ue in (truth_ues or [])}
    traj_lats = [r["gps"]["lat"] for r in records
                 if r.get("kind") == "ue_sighting" and r.get("gps")]
    traj_lons = [r["gps"]["lon"] for r in records
                 if r.get("kind") == "ue_sighting" and r.get("gps")]
    if traj_lats:
        ax.plot(traj_lons, traj_lats, color="#999", linewidth=0.6,
                alpha=0.5, label="drone trajectory")

    colors = plt.cm.tab10.colors
    sc = None
    for i, ((pci, rnti), recs) in enumerate(sorted(summary["by_ue"].items())):
        color = colors[i % len(colors)]
        ul = [r for r in recs
              if (r.get("ue") or {}).get("direction", "").lower() == "ul"]
        if not ul:
            continue
        lats = np.array([r["gps"]["lat"] for r in ul])
        lons = np.array([r["gps"]["lon"] for r in ul])
        ul_rssi = np.array([r["ue"]["ul_rssi_dbm"] for r in ul])
        sc = ax.scatter(lons, lats, c=ul_rssi, cmap="viridis", s=22,
                        marker="o", edgecolors=[color], linewidths=1.2,
                        label=f"C-RNTI {rnti:#06x} (n={len(ul)} UL)",
                        alpha=0.85)
        cent = weighted_centroid_ue(recs)
        wls = path_loss_wls_ue(recs)
        if cent is not None:
            ax.plot(cent.lon, cent.lat, marker="x", color=color,
                    markersize=14, mew=2.4)
        if wls is not None:
            ax.plot(wls.lon, wls.lat, marker="+", color=color,
                    markersize=18, mew=2.4)
        if rnti in truth_by_rnti:
            t = truth_by_rnti[rnti]
            if t.waypoints:
                t_lats = [w.lat for w in t.waypoints]
                t_lons = [w.lon for w in t.waypoints]
                ax.plot(t_lons, t_lats, linestyle=":", color=color,
                        linewidth=1.4, alpha=0.7,
                        label=f"C-RNTI {rnti:#06x} truth path")
            else:
                ax.plot(t.lon, t.lat, marker="*", color=color, markersize=16,
                        markeredgecolor="black", markeredgewidth=0.7,
                        label=f"C-RNTI {rnti:#06x} truth")
    if sc is not None:
        cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("UL RSSI (dBm)")

    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(title or "Drone trajectory + per-UE positioning (UL only)")
    ax.grid(True, alpha=0.3)
    ax.set_aspect("equal", adjustable="datalim")
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
            make_plot(path, out)
            print(f"wrote plot: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
