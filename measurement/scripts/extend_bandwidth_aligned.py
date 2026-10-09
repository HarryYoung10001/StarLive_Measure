#!/usr/bin/env python3
"""Extend an aligned measurement bandwidth series with complete trace cycles."""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path

from starlive_measure.measurement import aligned, aligned_ticks
from starlive_measure.phase import detect_metric_phase
from starlive_measure.trace.Bandwidth100ms import build_100ms_points, packet_events


def read_measurement_bandwidth(path: Path) -> list[tuple[float, float]]:
    rows: list[tuple[float, float]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            try:
                rows.append((float(row["time_s"]), float(row["bandwidth_mbps"])))
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(rows)


def trace_bandwidth(trace_run: Path) -> list[tuple[float, float]]:
    points = build_100ms_points(packet_events(trace_run))
    return [(float(point["time_ms"]) / 1000.0, float(point["bandwidth_mbps"])) for point in points]


def detect_trace_phase(trace_rows: list[tuple[float, float]]) -> float:
    result = detect_metric_phase([value for _time_s, value in trace_rows])
    if result.get("status") != "ok" or result.get("b_s") is None:
        raise ValueError(f"trace bandwidth phase detection failed: {result.get('status', 'unknown')}")
    return float(result["b_s"])


def append_complete_cycles(
    base_rows: list[tuple[float, float]],
    trace_rows: list[tuple[float, float]],
    *,
    trace_b_s: float,
    base_end_s: float = 102.0,
    target_end_s: float = 192.0,
    period_s: float = 15.0,
    trace_start_s: float = 12.0,
) -> list[tuple[float, float]]:
    if target_end_s <= base_end_s:
        return [(x, y) for x, y in base_rows if 0.0 <= x <= target_end_s]
    aligned_trace = aligned(trace_rows, trace_b_s, end_s=max(target_end_s, 102.0))
    if not aligned_trace:
        return [(x, y) for x, y in base_rows if 0.0 <= x <= base_end_s]

    # A cycle is complete only when its aligned endpoint is present.  This
    # prevents a short final trace from creating a partial period with blanks.
    trace_times = {round(x, 6) for x, _ in aligned_trace}
    max_trace_s = max(x for x, _ in aligned_trace)
    available_cycles = int(math.floor((max_trace_s - trace_start_s + 1e-6) / period_s))
    requested_cycles = int(math.floor((target_end_s - base_end_s + 1e-6) / period_s))
    cycles = max(0, min(available_cycles, requested_cycles))
    while cycles > 0 and round(trace_start_s + cycles * period_s, 6) not in trace_times:
        cycles -= 1
    trace_limit_s = trace_start_s + cycles * period_s

    result = [(x, y) for x, y in base_rows if 0.0 <= x <= base_end_s]
    existing = {round(x, 6) for x, _ in result}
    for x, y in aligned_trace:
        if trace_start_s - 1e-6 <= x <= trace_limit_s + 1e-6:
            shifted_x = round(x + base_end_s - trace_start_s, 6)
            if shifted_x > base_end_s + 1e-6 and shifted_x <= base_end_s + cycles * period_s + 1e-6 and shifted_x not in existing:
                result.append((shifted_x, y))
                existing.add(shifted_x)
    return sorted(result)


def write_bandwidth_tsv(path: Path, rows: list[tuple[float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["time_s", "bandwidth_mbps"])
        writer.writerows((f"{x:.6f}", f"{y:.9f}") for x, y in rows)


def plot_mobicom(rows: list[tuple[float, float]], pdf: Path, png: Path, *, end_s: float) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    plt.rcParams.update(
        {
            "figure.figsize": (8.0, 3.0),
            "figure.dpi": 80,
            "savefig.dpi": 300,
            "font.size": 18,
            "font.family": "Arial",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "xtick.direction": "in",
            "ytick.direction": "in",
        }
    )
    fig, ax = plt.subplots(constrained_layout=True)
    if rows:
        ax.plot([x for x, _ in rows], [y for _, y in rows], color="#4477AA", linewidth=1.35)
    ticks = aligned_ticks(end_s)
    ax.set_xlim(0, end_s)
    ax.set_xticks(ticks)
    for tick in ticks[1:-1]:
        ax.axvline(tick, color="#9ca3af", linestyle="--", linewidth=0.45, alpha=0.65, zorder=0)
    ax.set_xlabel("Time (s)", fontsize=18)
    ax.set_ylabel("Bandwidth (Mbps)", fontsize=18)
    ax.tick_params(axis="both", labelsize=13, width=0.8, length=4)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 5, 10]))
    ax.grid(True, axis="y", linestyle="--", linewidth=0.45, color="#9ca3af", alpha=0.55)
    if rows:
        upper = max(y for _, y in rows)
        ax.set_ylim(0, math.ceil(upper / 10.0) * 10.0)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(pdf)
    fig.savefig(png, dpi=300)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("measurement_run", type=Path)
    parser.add_argument("trace_run", type=Path)
    parser.add_argument("--trace-b", type=float, help="override independently detected trace phase in seconds")
    parser.add_argument("--end-s", type=float, default=192.0)
    parser.add_argument("--output-dir", type=Path, default=Path("mobicom"))
    args = parser.parse_args(argv)
    if args.end_s <= 0:
        parser.error("--end-s must be positive")

    source = args.measurement_run / "derived" / "aligned" / "bandwidth_100ms.tsv"
    base = read_measurement_bandwidth(source)
    if not base:
        raise SystemExit(f"no measurement bandwidth rows in {source}")
    trace_rows = trace_bandwidth(args.trace_run)
    trace_b_s = args.trace_b if args.trace_b is not None else detect_trace_phase(trace_rows)
    extended = append_complete_cycles(base, trace_rows, trace_b_s=trace_b_s, target_end_s=args.end_s)
    aligned_path = args.measurement_run / "derived" / "aligned" / "bandwidth_100ms.tsv"
    write_bandwidth_tsv(aligned_path, extended)
    output_tsv = args.output_dir / "bandwidth_100ms_aligned_192s.tsv"
    write_bandwidth_tsv(output_tsv, extended)
    pdf = args.output_dir / "bandwidth_100ms_mobicom.pdf"
    png = args.output_dir / "bandwidth_100ms_mobicom.png"
    plot_mobicom(extended, pdf, png, end_s=args.end_s)
    print(f"measurement_tsv={aligned_path}")
    print(f"mobicom_tsv={output_tsv}")
    print(f"mobicom_pdf={pdf}")
    print(f"mobicom_png={png}")
    print(f"trace_b_s={trace_b_s:.3f}")
    print(f"rows={len(extended)} end_s={max(x for x, _ in extended):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
