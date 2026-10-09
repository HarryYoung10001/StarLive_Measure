#!/usr/bin/env python3
"""Plot fine RTT samples with the measurement phase-alignment convention."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from starlive_measure.measurement import aligned, aligned_ticks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--b", type=float, help="source event time in seconds (default: offset.txt + 390)")
    parser.add_argument("--end-s", type=float, default=102.0, help="aligned output endpoint in seconds")
    parser.add_argument("--output-dir", type=Path, help="directory for the aligned figure files")
    args = parser.parse_args()
    if args.end_s <= 0:
        parser.error("--end-s must be positive")
    if args.b is None:
        try:
            args.b = float((args.run_dir / "offset.txt").read_text(encoding="utf-8").strip()) + 390.0
        except (OSError, TypeError, ValueError):
            parser.error("--b is required when offset.txt is missing or invalid")

    source = args.run_dir / "fine_rtt_samples.csv"
    rows: list[tuple[float, float]] = []
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("lost", "").strip().lower() in {"1", "true", "yes", "y"}:
                continue
            if row.get("late", "").strip().lower() in {"1", "true", "yes", "y"}:
                continue
            try:
                rows.append((float(row["send_rel_s"]), float(row["rtt_ms"])))
            except (KeyError, TypeError, ValueError):
                continue
    if not rows:
        raise SystemExit(f"no valid RTT samples in {source}")

    shifted = aligned(rows, args.b, end_s=args.end_s)
    output = args.output_dir or args.run_dir / "aligned"
    output.mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    plt.rcParams.update(
        {
            "figure.figsize": (8.0, 3.0),
            "font.size": 18,
            "font.family": "Arial",
            "xtick.direction": "in",
            "ytick.direction": "in",
            "savefig.dpi": 300,
        }
    )
    fig, ax = plt.subplots(constrained_layout=True)
    if shifted:
        ax.scatter(
            [x for x, _ in shifted],
            [y for _, y in shifted],
            color="#4477AA",
            s=5,
            alpha=0.60,
            linewidths=0,
        )
    ticks = aligned_ticks(args.end_s)
    ax.set_xlim(0, args.end_s)
    ax.set_xticks(ticks)
    for tick in ticks[1:-1]:
        ax.axvline(tick, color="#9ca3af", linestyle="--", linewidth=0.45, alpha=0.65)
    ax.set_xlabel("Time (s)", fontsize=18)
    ax.set_ylabel("RTT (ms)", fontsize=18)
    ax.tick_params(axis="both", labelsize=13, width=0.8, length=4)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
    ax.grid(True, axis="y", linestyle="--", linewidth=0.45, color="#9ca3af", alpha=0.55)
    if shifted:
        values = [y for _, y in shifted]
        lower, upper = min(values), max(values)
        padding = max(2.0, (upper - lower) * 0.04)
        ax.set_ylim(lower - padding, upper + padding)
    tsv = output / "fine_rtt_raw_scatter_aligned.tsv"
    with tsv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["time_s", "rtt_ms"])
        writer.writerows((f"{x:.6f}", f"{y:.9f}") for x, y in shifted)
    print(f"Wrote {tsv} using {len(shifted)} samples")
    pdf = output / "fine_rtt_raw_scatter_aligned.pdf"
    fig.savefig(pdf)
    print(f"Wrote {pdf} using {len(shifted)} samples")
    png = output / "fine_rtt_raw_scatter_aligned.png"
    fig.savefig(png, dpi=300)
    print(f"Wrote {png} using {len(shifted)} samples")
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
