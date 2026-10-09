#!/usr/bin/env python3
"""Fold all timestamp/loss-rate samples into a Unix-aligned 15-second cycle."""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

import matplotlib.pyplot as plt


SOURCE = Path("online/data/281475060019287-temp_page-push_stream-查询24.csv")
OUT_PNG = Path("online/analysis/time_lossrate_folded_15s.png")
OUT_CSV = Path("online/analysis/time_lossrate_folded_15s.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUT_PNG)
    parser.add_argument("--points-output", type=Path, default=OUT_CSV)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.points_output.parent.mkdir(parents=True, exist_ok=True)

    xs: list[float] = []
    ys: list[float] = []
    valid = zero = invalid = 0
    with args.source.open(encoding="utf-8-sig", newline="") as source_handle, args.points_output.open(
        "w", encoding="utf-8", newline=""
    ) as points_handle:
        reader = csv.DictReader(source_handle)
        writer = csv.DictWriter(points_handle, fieldnames=["timestamp_ms", "folded_time_s", "loss_rate"])
        writer.writeheader()
        for row in reader:
            try:
                timestamp = int(float(row.get("timestamp", "")))
                loss_rate = float(row.get("loss_rate", ""))
                if not math.isfinite(loss_rate):
                    raise ValueError
            except (TypeError, ValueError):
                invalid += 1
                continue

            folded = (timestamp % 15000) / 1000.0
            writer.writerow(
                {
                    "timestamp_ms": timestamp,
                    "folded_time_s": f"{folded:.3f}",
                    "loss_rate": f"{loss_rate:g}",
                }
            )
            xs.append(folded)
            ys.append(loss_rate)
            valid += 1
            zero += loss_rate == 0

    figure, axis = plt.subplots(figsize=(14, 7), facecolor="white")
    axis.scatter(xs, ys, s=2, alpha=0.16, linewidths=0, rasterized=True, color="#1769aa")
    axis.set_xlim(0, 15)
    axis.set_xlabel("Position within 15-second cycle (s; Unix/UTC aligned)")
    axis.set_ylabel("Loss rate (ratio)")
    axis.set_title("Folded time-loss-rate scatter over the full observation period", loc="left", fontweight="bold")
    axis.grid(True, alpha=0.2)
    axis.text(
        0.01,
        0.98,
        f"{valid:,} valid pairs; {zero:,} zero loss rate; {invalid:,} invalid/missing",
        transform=axis.transAxes,
        va="top",
        color="#52606d",
    )
    figure.tight_layout()
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    print(f"Wrote {args.output} and {args.points_output} ({valid} valid pairs)")


if __name__ == "__main__":
    main()
