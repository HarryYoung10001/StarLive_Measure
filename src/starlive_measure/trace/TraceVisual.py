#!/usr/bin/env python3
"""Plot RTT-time, capacity-time, custom UDP latency, and cross-rate summaries."""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

from .trace_discovery import dual_trace_run_dirs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot RTT-time, capacity-time, and custom UDP latency charts from trace CSV files."
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=Path("traces/logs"),
        help="Directory containing dual traces and generated CSV files.",
    )
    parser.add_argument(
        "--complete-only", action="store_true",
        help="Plot only traces whose trace_summary.csv status is complete.",
    )
    return parser.parse_args()


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def select_summary_rows(
    summary_rows: list[dict[str, str]], complete_only: bool
) -> list[dict[str, str]]:
    """Return the rows eligible for per-trace and aggregate plotting."""

    if not complete_only:
        return summary_rows
    return [row for row in summary_rows if row.get("status") == "complete"]


def dedupe_summary_rows(summary_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Choose one preferred trace per offered rate for aggregate plots.

    Resume sessions can retain failed or superseded attempts for audit. A
    complete trace wins over an incomplete attempt; among equal-status traces,
    the lexicographically latest run directory is the newest timestamped run.
    """

    selected: dict[str, dict[str, str]] = {}
    for row in summary_rows:
        rate_key = row.get("offered_rate_mbps") or row.get("offered_rate") or row.get("run_id", "")
        previous = selected.get(rate_key)
        if previous is None:
            selected[rate_key] = row
            continue
        previous_rank = (
            previous.get("status") == "complete",
            previous.get("trace_dir") or previous.get("run_id", ""),
        )
        current_rank = (
            row.get("status") == "complete",
            row.get("trace_dir") or row.get("run_id", ""),
        )
        if current_rank > previous_rank:
            selected[rate_key] = row
    return list(selected.values())


def save_figure(fig, output_base: Path, *, pdf: bool = False) -> None:
    png_path = output_base.with_suffix(".png")
    fig.tight_layout()
    fig.savefig(png_path, dpi=160)
    print(f"Wrote {png_path}")
    if pdf:
        pdf_path = output_base.with_suffix(".pdf")
        fig.savefig(pdf_path)
        print(f"Wrote {pdf_path}")


def load_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_rtt_time(run_dir: Path, summary: dict[str, str], plt) -> bool:
    rows = read_rows(run_dir / "rtt_timeseries.csv")
    points = []
    for row in rows:
        x_value = parse_float(row.get("time_rel_s"))
        y_value = parse_float(row.get("rtt_ms"))
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value))
    points.sort(key=lambda point: point[0])
    if not points:
        print(f"WARNING: no RTT rows found in {run_dir / 'rtt_timeseries.csv'}")
        return False

    xs, ys = zip(*points)
    mean_rtt = statistics.mean(ys)
    run_id = summary.get("run_id") or run_dir.name
    offered_rate = summary.get("offered_rate") or "unknown rate"
    loss_pct = parse_float(summary.get("ping_loss_pct"))
    loss_label = f"{loss_pct:.2f}%" if loss_pct is not None else "n/a"

    fig, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(xs, ys, color="tab:blue", linewidth=0.8, label="RTT line")
    axis.scatter(xs, ys, color="tab:blue", s=7, alpha=0.55, linewidths=0, label="RTT samples")
    axis.axhline(
        mean_rtt, color="tab:red", linestyle="--", linewidth=1.1,
        label=f"mean RTT = {mean_rtt:.2f} ms",
    )
    axis.text(
        0.99, 0.97,
        f"mean RTT = {mean_rtt:.2f} ms\nsamples = {len(ys)}\nloss = {loss_label}",
        transform=axis.transAxes, ha="right", va="top", fontsize=9,
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.8},
    )
    axis.set_title(f"{run_id} | RTT-time | offered {offered_rate}")
    axis.set_xlabel("Time since first successful ping (s)")
    axis.set_ylabel("RTT (ms)")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="lower right")
    save_figure(fig, run_dir / "rtt_time")
    plt.close(fig)
    return True


def plot_capacity_time(run_dir: Path, summary: dict[str, str], plt) -> bool:
    rows = read_rows(run_dir / "capacity_timeseries.csv")
    points = []
    for row in rows:
        if row.get("omitted", "").lower() == "true":
            continue
        x_value = parse_float(row.get("time_mid_s"))
        y_value = parse_float(row.get("capacity_mbps"))
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value))
    points.sort(key=lambda point: point[0])
    if not points:
        print(f"WARNING: no capacity rows found in {run_dir / 'capacity_timeseries.csv'}")
        return False

    xs, ys = zip(*points)
    run_id = summary.get("run_id") or run_dir.name
    offered_rate = summary.get("offered_rate") or "unknown rate"
    offered_mbps = parse_float(summary.get("offered_rate_mbps"))
    weighted = parse_float(summary.get("weighted_capacity_mbps"))
    fig, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(xs, ys, color="tab:blue", linewidth=1.0, label="receiver goodput line")
    axis.scatter(xs, ys, color="tab:blue", s=7, alpha=0.55, linewidths=0, label="receiver intervals")
    if offered_mbps is not None:
        axis.axhline(
            offered_mbps, color="tab:orange", linestyle="--", linewidth=1.0,
            label=f"offered rate = {offered_mbps:.2f} Mbps",
        )
    if weighted is not None:
        axis.text(
            0.99, 0.97, f"weighted goodput = {weighted:.2f} Mbps",
            transform=axis.transAxes, ha="right", va="top", fontsize=9,
            bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.8},
        )
    axis.set_title(f"{run_id} | capacity-time | offered {offered_rate}")
    axis.set_xlabel("Time midpoint (s)")
    axis.set_ylabel("Receiver goodput (Mbps)")
    axis.set_ylim(bottom=0)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="lower right")
    save_figure(fig, run_dir / "capacity_time")
    plt.close(fig)
    return True


def plot_udp_send_time_delay(run_dir: Path, summary: dict[str, str], plt) -> bool:
    rows = read_rows(run_dir / "udp_flow_latency.csv")
    points = []
    for row in rows:
        x_value = parse_float(row.get("send_rel_s"))
        y_value = parse_float(row.get("one_way_delay_ms"))
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value))
    points.sort(key=lambda point: point[0])
    if not points:
        print(f"WARNING: no UDP flow latency samples found in {run_dir / 'udp_flow_latency.csv'}")
        return False

    xs, ys = zip(*points)
    mean_ms = statistics.mean(ys)
    run_id = summary.get("run_id") or run_dir.name
    fig, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(xs, ys, color="tab:blue", linewidth=0.8, label="one-way delay line")
    axis.scatter(xs, ys, color="tab:blue", s=7, alpha=0.55, linewidths=0, label="packet samples")
    axis.axhline(mean_ms, color="tab:red", linestyle="--", linewidth=1.0, label=f"mean = {mean_ms:.2f} ms")
    axis.set_title(f"{run_id} | send time vs one-way delay")
    axis.set_xlabel("Server send time since flow start (s)")
    axis.set_ylabel("One-way delay (ms)")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, run_dir / "send_time_one_way_delay")
    plt.close(fig)
    return True


def plot_udp_received_time_seq(run_dir: Path, summary: dict[str, str], plt) -> bool:
    rows = read_rows(run_dir / "udp_flow_latency.csv")
    points = []
    for row in rows:
        x_value = parse_float(row.get("recv_rel_s"))
        y_value = parse_float(row.get("seq"))
        if x_value is not None and y_value is not None and row.get("received") == "true":
            points.append((x_value, y_value))
    points.sort(key=lambda point: point[0])
    if not points:
        print(f"WARNING: no received UDP flow packets found in {run_dir / 'udp_flow_latency.csv'}")
        return False

    xs, ys = zip(*points)
    run_id = summary.get("run_id") or run_dir.name
    fig, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(xs, ys, color="tab:green", linewidth=0.8, label="received sequence line")
    axis.scatter(xs, ys, color="tab:green", s=7, alpha=0.55, linewidths=0, label="received packets")
    axis.set_title(f"{run_id} | received time vs send sequence")
    axis.set_xlabel("Phone receive time since receiver start (s)")
    axis.set_ylabel("Server send sequence")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, run_dir / "received_time_send_seq")
    plt.close(fig)
    return True


def plot_rtt_vs_rate(logs_dir: Path, summaries: list[dict[str, str]], plt, *, output_base: Path | None = None,
                     title: str = "RTT versus offered UDP rate", pdf: bool = False) -> bool:
    series = {
        "mean RTT": ("mean_rtt_ms", "tab:red"),
        "median RTT": ("median_rtt_ms", "tab:blue"),
        "p95 RTT": ("p95_rtt_ms", "tab:green"),
        "p99 RTT": ("p99_rtt_ms", "tab:orange"),
        "max RTT": ("max_rtt_ms", "tab:purple"),
    }
    points_by_series = {label: [] for label in series}
    for row in summaries:
        x_value = parse_float(row.get("offered_rate_mbps"))
        if x_value is None:
            continue
        for label, (field, _color) in series.items():
            y_value = parse_float(row.get(field))
            if y_value is not None:
                points_by_series[label].append((x_value, y_value, row.get("offered_rate", "")))
    if not any(points_by_series.values()):
        print("WARNING: no RTT percentile values available for aggregate plot")
        return False
    fig, axis = plt.subplots(figsize=(9, 5.5))
    for label, (field, color) in series.items():
        points = sorted(points_by_series[label], key=lambda point: point[0])
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        axis.plot(xs, ys, color=color, marker="o", linewidth=1.1, label=label)
    annotations = points_by_series["mean RTT"]
    for x_value, y_value, label in annotations:
        axis.annotate(label or f"{x_value:g}", (x_value, y_value), xytext=(4, 4), textcoords="offset points", fontsize=8)
    axis.set_title(title)
    axis.set_xlabel("Offered rate (Mbps)")
    axis.set_ylabel("RTT (ms)")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, output_base or (logs_dir / "rtt_vs_rate"), pdf=pdf)
    plt.close(fig)
    return True


def plot_capacity_mean_vs_rate(logs_dir: Path, summaries: list[dict[str, str]], plt, *, output_base: Path | None = None,
                               title: str = "Weighted receiver goodput versus offered UDP rate", pdf: bool = False) -> bool:
    points = []
    for row in summaries:
        x_value = parse_float(row.get("offered_rate_mbps"))
        y_value = parse_float(row.get("weighted_capacity_mbps"))
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value, row.get("offered_rate", "")))
    if not points:
        print("WARNING: no weighted capacity values available for aggregate plot")
        return False
    points.sort(key=lambda point: point[0])
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    fig, axis = plt.subplots(figsize=(9, 5.5))
    axis.plot(xs, ys, color="tab:blue", marker="o", linewidth=1.1, label="weighted receiver goodput")
    max_axis = max(max(xs), max(ys), 1.0)
    axis.plot([0.0, max_axis], [0.0, max_axis], color="tab:orange", linestyle="--", linewidth=1.0, label="ideal y=x")
    for x_value, y_value, label in points:
        axis.annotate(label or f"{x_value:g}", (x_value, y_value), xytext=(4, 4), textcoords="offset points", fontsize=8)
    axis.set_title(title)
    axis.set_xlabel("Offered rate (Mbps)")
    axis.set_ylabel("Weighted receiver goodput (Mbps)")
    axis.set_xlim(left=0)
    axis.set_ylim(bottom=0)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="lower right")
    save_figure(fig, output_base or (logs_dir / "capacity_mean_vs_rate"), pdf=pdf)
    plt.close(fig)
    return True


def plot_mean_capacity_vs_rate(logs_dir: Path, summaries: list[dict[str, str]], plt, *, output_base: Path | None = None,
                               title: str = "Mean receiver capacity versus offered UDP rate",
                               series_label: str = "mean 10ms receiver capacity", pdf: bool = False) -> bool:
    points = []
    for row in summaries:
        offered_mbps = parse_float(row.get("offered_rate_mbps"))
        mean_capacity_mbps = parse_float(row.get("mean_capacity_mbps"))
        if offered_mbps is not None and mean_capacity_mbps is not None:
            points.append((offered_mbps, mean_capacity_mbps, row.get("offered_rate", "")))
    if not points:
        print("WARNING: no mean capacity values available for aggregate plot")
        return False

    points.sort(key=lambda point: point[0])
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    fig, axis = plt.subplots(figsize=(9, 5.5))
    axis.plot(xs, ys, color="teal", marker="o", linewidth=1.1, label=series_label)
    for offered_mbps, mean_capacity_mbps, label in points:
        axis.annotate(label or f"{offered_mbps:g}", (offered_mbps, mean_capacity_mbps), xytext=(4, 4), textcoords="offset points", fontsize=8)
    axis.set_title(title)
    axis.set_xlabel("Offered rate (Mbps)")
    axis.set_ylabel("Goodput (Mbps)" if "goodput" in series_label.lower() else "Mean receiver capacity (Mbps)")
    axis.set_xlim(left=0)
    axis.set_ylim(bottom=0)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, output_base or (logs_dir / "mean_capacity_vs_rate"), pdf=pdf)
    plt.close(fig)
    return True


def plot_loss_rate_vs_rate(logs_dir: Path, summaries: list[dict[str, str]], plt, *, output_base: Path | None = None,
                           title: str = "Loss rate versus offered UDP rate", pdf: bool = False) -> bool:
    series = {
        "ping loss": ("ping_loss_pct", "tab:red"),
        "capacity loss": ("capacity_loss_pct", "tab:blue"),
    }
    points_by_series = {label: [] for label in series}
    for row in summaries:
        x_value = parse_float(row.get("offered_rate_mbps"))
        if x_value is None:
            continue
        for label, (field, _color) in series.items():
            y_value = parse_float(row.get(field))
            if y_value is not None:
                points_by_series[label].append((x_value, y_value, row.get("offered_rate", "")))
    if not any(points_by_series.values()):
        print("WARNING: no loss-rate values available for aggregate plot")
        return False

    fig, axis = plt.subplots(figsize=(9, 5.5))
    for label, (field, color) in series.items():
        points = sorted(points_by_series[label], key=lambda point: point[0])
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        axis.plot(xs, ys, color=color, marker="o", linewidth=1.1, label=label)
    for x_value, y_value, label in points_by_series["ping loss"]:
        axis.annotate(label or f"{x_value:g}", (x_value, y_value), xytext=(4, 4), textcoords="offset points", fontsize=8)
    axis.set_title(title)
    axis.set_xlabel("Offered rate (Mbps)")
    axis.set_ylabel("Loss rate (%)")
    axis.set_ylim(bottom=0)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, output_base or (logs_dir / "loss_rate_vs_rate"), pdf=pdf)
    plt.close(fig)
    return True


def plot_udp_latency_vs_rate(logs_dir: Path, summaries: list[dict[str, str]], plt, *, output_base: Path | None = None,
                             title: str = "UDP one-way latency versus offered rate",
                             y_label: str = "One-way latency (ms)", pdf: bool = False) -> bool:
    series = {
        "mean": ("udp_flow_latency_mean_ms", "tab:green"),
        "p99": ("udp_flow_latency_p99_ms", "tab:red"),
        "p95": ("udp_flow_latency_p95_ms", "tab:orange"),
        "p75": ("udp_flow_latency_p75_ms", "tab:purple"),
        "p50 (median)": ("udp_flow_latency_median_ms", "tab:blue"),
    }
    points_by_series = {label: [] for label in series}
    for row in summaries:
        x_value = parse_float(row.get("offered_rate_mbps"))
        if x_value is None:
            continue
        for label, (field, _color) in series.items():
            y_value = parse_float(row.get(field))
            if y_value is not None:
                points_by_series[label].append((x_value, y_value, row.get("offered_rate", "")))
    if not any(points_by_series.values()):
        print("WARNING: no UDP one-way latency percentile values available for aggregate plot")
        return False

    fig, axis = plt.subplots(figsize=(9, 5.5))
    for label, (_field, color) in series.items():
        points = sorted(points_by_series[label], key=lambda point: point[0])
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        axis.plot(xs, ys, color=color, marker="o", linewidth=1.1, label=label)
    for x_value, y_value, label in points_by_series["mean"]:
        axis.annotate(label or f"{x_value:g}", (x_value, y_value), xytext=(4, 4), textcoords="offset points", fontsize=8)
    axis.set_title(title)
    axis.set_xlabel("Offered rate (Mbps)")
    axis.set_ylabel(y_label)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    axis.legend(loc="best")
    save_figure(fig, output_base or (logs_dir / "one_way_latency_vs_rate"), pdf=pdf)
    plt.close(fig)
    return True


def main() -> int:
    args = parse_args()
    logs_dir = args.logs_dir
    if not logs_dir.exists() or not logs_dir.is_dir():
        print(f"ERROR: logs directory does not exist: {logs_dir}")
        return 2

    try:
        plt = load_matplotlib()
    except ImportError as exc:
        print(f"ERROR: matplotlib is required for plotting: {exc}")
        return 2

    summary_rows = read_rows(logs_dir / "trace_summary.csv")
    selected_rows = select_summary_rows(summary_rows, args.complete_only)
    aggregate_rows = dedupe_summary_rows(selected_rows)
    if args.complete_only:
        skipped = len(summary_rows) - len(selected_rows)
        if skipped:
            print(f"WARNING: skipping {skipped} partial or failed traces")
    if not selected_rows:
        print("WARNING: no eligible traces available for plotting")
        return 1

    summary_by_run = {row.get("run_id", ""): row for row in selected_rows}
    plotted = 0
    for run_dir in dual_trace_run_dirs(logs_dir):
        summary = summary_by_run.get(run_dir.name)
        if summary is None:
            if args.complete_only:
                print(f"WARNING: skipping incomplete trace {run_dir.name}")
                continue
            summary = {"run_id": run_dir.name}
        icmp_enabled = summary.get("icmp_enabled", "true").strip().lower() not in {
            "false", "0", "no", "disabled"
        }
        if icmp_enabled and plot_rtt_time(run_dir, summary, plt):
            plotted += 1
        if summary.get("capacity_source"):
            if plot_capacity_time(run_dir, summary, plt):
                plotted += 1
        if summary.get("udp_latency_source") and (run_dir / "udp_flow_latency.csv").exists():
            if plot_udp_send_time_delay(run_dir, summary, plt):
                plotted += 1
            if plot_udp_received_time_seq(run_dir, summary, plt):
                plotted += 1

    icmp_rows = [
        row for row in aggregate_rows
        if row.get("icmp_enabled", "true").strip().lower() not in {"false", "0", "no", "disabled"}
    ]
    if icmp_rows:
        plot_rtt_vs_rate(logs_dir, icmp_rows, plt)
    plot_capacity_mean_vs_rate(logs_dir, aggregate_rows, plt)
    plot_mean_capacity_vs_rate(logs_dir, aggregate_rows, plt)
    plot_loss_rate_vs_rate(logs_dir, aggregate_rows, plt)
    if any(parse_float(row.get("udp_flow_latency_mean_ms")) is not None for row in aggregate_rows):
        plot_udp_latency_vs_rate(logs_dir, aggregate_rows, plt)
    print(f"Done. Plotted {plotted} per-trace figures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
