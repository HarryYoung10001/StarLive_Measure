#!/usr/bin/env python3
"""Fold all timestamp/RTT samples into a Unix-aligned 15-second cycle."""
from __future__ import annotations
import argparse
import csv
import math
from collections import Counter
from pathlib import Path
import matplotlib.pyplot as plt

SOURCE = Path("online/data/281475060019287-temp_page-push_stream-查询24.csv")
OUT_PNG = Path("online/analysis/time_rtt_folded_15s_top_stream.png")
OUT_CSV = Path("online/analysis/time_rtt_folded_15s_top_stream.csv")

def stream_counts(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            stream = (row.get("stream_name") or "").strip()
            if stream:
                counts[stream] += 1
    return counts

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUT_PNG)
    parser.add_argument("--points-output", type=Path, default=OUT_CSV)
    parser.add_argument("--stream", help="stream_name to plot; defaults to the stream with most records")
    args = parser.parse_args()
    counts = stream_counts(args.source)
    if not counts:
        raise ValueError("source contains no stream_name values")
    stream = args.stream or counts.most_common(1)[0][0]
    if stream not in counts:
        raise ValueError(f"stream not found: {stream}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.points_output.parent.mkdir(parents=True, exist_ok=True)
    xs, ys = [], []
    valid = zero = invalid = 0
    with args.source.open(encoding="utf-8-sig", newline="") as source_handle, args.points_output.open("w", encoding="utf-8", newline="") as points_handle:
        reader = csv.DictReader(source_handle)
        writer = csv.DictWriter(points_handle, fieldnames=["stream_name", "timestamp_ms", "folded_time_s", "rtt_ms"])
        writer.writeheader()
        for row in reader:
            if row.get("stream_name") != stream:
                continue
            try:
                timestamp = int(float(row.get("timestamp", "")))
                rtt = float(row.get("rtt", ""))
                if not math.isfinite(rtt):
                    raise ValueError
            except (TypeError, ValueError):
                invalid += 1
                continue
            folded = (timestamp % 15000) / 1000.0
            writer.writerow({"stream_name": stream, "timestamp_ms": timestamp, "folded_time_s": f"{folded:.3f}", "rtt_ms": f"{rtt:g}"})
            xs.append(folded)
            ys.append(rtt)
            valid += 1
            zero += rtt == 0
    figure, axis = plt.subplots(figsize=(14, 7), facecolor="white")
    axis.scatter(xs, ys, s=2, alpha=0.16, linewidths=0, rasterized=True, color="#1769aa")
    axis.set_xlim(0, 15)
    axis.set_xlabel("Position within 15-second cycle (s; Unix/UTC aligned)")
    axis.set_ylabel("RTT (ms)")
    axis.set_title(f"Folded time-RTT scatter: {stream} ({counts[stream]:,} records)", loc="left", fontweight="bold")
    axis.grid(True, alpha=0.2)
    axis.text(0.01, 0.98, f"{valid:,} valid pairs; {zero:,} zero RTT; {invalid:,} invalid/missing", transform=axis.transAxes, va="top", color="#52606d")
    figure.tight_layout()
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    print(f"Wrote {args.output} and {args.points_output} ({stream}; {valid} valid pairs)")

if __name__ == "__main__":
    main()
