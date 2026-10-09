#!/usr/bin/env python3
"""Plot demo push-stream time-series metrics from CSV or CSV.GZ."""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import tempfile
from pathlib import Path
from typing import Callable


STALL_COLUMNS = (
    "transport_send_audio_stall_count",
    "transport_send_video_stall_count",
    "transport_pts_audio_stall_count",
    "transport_pts_video_stall_count",
)
OFFSET_FILE = "offset.txt"
MARK_INTERVAL_S = 15.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot push_stream metrics. With no input, process every demo log run."
    )
    parser.add_argument("input", nargs="?", type=Path, help="Run directory or metrics CSV/CSV.GZ")
    return parser.parse_args()


def demo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_latest_pointer(path: Path) -> Path | None:
    if path.name != "latest_demo_logcat" or not path.is_file():
        return None
    target_text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not target_text:
        return None
    target = Path(target_text)
    candidates = [target]
    if target.name:
        candidates.append(path.parent / target.name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_input(path: Path) -> tuple[Path, Path]:
    pointer_target = resolve_latest_pointer(path)
    if pointer_target is not None:
        return resolve_input(pointer_target)
    if path.is_dir():
        gzip_path = path / "push_stream_metrics.csv.gz"
        csv_path = path / "push_stream_metrics.csv"
        if gzip_path.is_file():
            return gzip_path, path.resolve()
        if csv_path.is_file():
            return csv_path, path.resolve()
        raise FileNotFoundError(f"no push_stream_metrics.csv.gz or .csv under {path}")
    if not path.is_file():
        raise FileNotFoundError(f"metrics file not found: {path}")
    if path.name.endswith(".csv.gz") or path.suffix == ".csv":
        return path, path.parent.resolve()
    raise ValueError(f"input must be a run directory, .csv, or .csv.gz file: {path}")


def discover_inputs(value: Path | None) -> list[tuple[Path, Path]]:
    if value is not None:
        return [resolve_input(value)]
    inputs = []
    for run_dir in sorted((demo_root() / "logs").glob("demo_logcat_*")):
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        try:
            inputs.append(resolve_input(run_dir))
        except FileNotFoundError:
            print(f"Warning: skipping run without metrics CSV: {run_dir}")
    return inputs


def read_rows(path: Path) -> list[dict[str, str]]:
    if path.name.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_offset(run_dir: Path) -> float | None:
    offset_path = run_dir / OFFSET_FILE
    if not offset_path.exists():
        return None
    text = offset_path.read_text(encoding="utf-8", errors="replace").strip()
    raw_value = text.split()[0] if text else ""
    value = parse_float(raw_value)
    if value is None:
        print(f"Warning: invalid offset in {offset_path}: {text!r}; skipping markers")
    return value


def column_value(row: dict[str, str], column: str) -> float | None:
    return parse_float(row.get(column))


def stall_total(row: dict[str, str]) -> float | None:
    return parse_float(row.get("stall_total_count"))


def add_run_cumulative_counts(rows: list[dict[str, str]]) -> None:
    """Make source counters cumulative across stream-session counter resets."""
    ordered_rows = sorted(rows, key=lambda row: parse_float(row.get("time_s")) or 0.0)
    columns = STALL_COLUMNS + ("transport_reconnect_count",)
    previous: dict[str, float | None] = {column: None for column in columns}
    totals: dict[str, float] = {column: 0.0 for column in columns}
    for row in ordered_rows:
        for column in columns:
            current = parse_float(row.get(column))
            if current is None:
                continue
            old = previous[column]
            if old is None:
                totals[column] = current
            elif current >= old:
                totals[column] += current - old
            else:
                totals[column] += current
            previous[column] = current
        row["stall_total_count"] = str(sum(totals[column] for column in STALL_COLUMNS))
        row["reconnect_total_count"] = str(totals["transport_reconnect_count"])


def series(
    rows: list[dict[str, str]],
    value_getter: Callable[[dict[str, str]], float | None],
) -> tuple[list[float], list[float]]:
    points = []
    for row in rows:
        x_value = parse_float(row.get("time_s"))
        y_value = value_getter(row)
        if x_value is not None and y_value is not None:
            points.append((x_value, y_value))
    points.sort(key=lambda point: point[0])
    if not points:
        return [], []
    x_values, y_values = zip(*points)
    return list(x_values), list(y_values)


def add_offset_markers(ax, offset_s: float | None, min_time_s: float, max_time_s: float) -> bool:
    if offset_s is None:
        return False

    mark_s = offset_s
    while mark_s < min_time_s:
        mark_s += MARK_INTERVAL_S

    added = False
    while mark_s <= max_time_s:
        ax.axvline(mark_s, color="#f2c200", linestyle="--", linewidth=0.9, alpha=0.9)
        mark_s += MARK_INTERVAL_S
        added = True
    return added


def save_figure(fig, output_base: Path) -> None:
    png_path = output_base.with_suffix(".png")
    pdf_path = output_base.with_suffix(".pdf")
    fig.savefig(png_path, dpi=160)
    fig.savefig(pdf_path)
    print(f"  Wrote {png_path.name} and {pdf_path.name}")


def plot_run(csv_path: Path, run_dir: Path) -> int:
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "starlink_matplotlib"))
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        print(f"Error: matplotlib is required for plotting: {error}")
        return 2

    rows = read_rows(csv_path)
    if not rows:
        print(f"Warning: no metric rows in {csv_path}")
        return 0
    add_run_cumulative_counts(rows)

    run_id = rows[0].get("run_id") or run_dir.name
    offset_s = read_offset(run_dir)
    specs = (
        (
            "push_stream_bitrate_time", "Push-stream bitrate", "Bitrate (kbps)",
            (
                (lambda row: column_value(row, "real_bitrate"), "real_bitrate", "tab:blue"),
                (lambda row: column_value(row, "video_enc_bitrate"), "video_enc_bitrate", "tab:orange"),
            ),
        ),
        (
            "push_stream_bwe_time", "Bandwidth estimate", "BWE (Mbps)",
            ((lambda row: column_value(row, "bwe"), "bwe", "tab:green"),),
        ),
        (
            "push_stream_rtt_time", "Network RTT", "RTT (ms)",
            ((lambda row: column_value(row, "rtt"), "rtt", "tab:red"),),
        ),
        (
            "push_stream_loss_rate_time", "Network loss rate", "Loss rate (ratio)",
            ((lambda row: column_value(row, "loss_rate"), "loss_rate", "tab:purple"),),
        ),
        (
            "push_stream_framerate_time", "Actual video framerate", "FPS",
            ((lambda row: column_value(row, "real_video_framerate"), "real_video_framerate", "tab:brown"),),
        ),
        (
            "push_stream_stall_reconnect_time", "Stall and reconnect totals", "Cumulative count",
            (
                (stall_total, "stall_total_count", "tab:pink"),
                (lambda row: column_value(row, "reconnect_total_count"), "reconnect_total_count", "tab:gray"),
            ),
        ),
    )

    plotted = 0
    print(f"{run_id}: input={csv_path.name}")
    for output_name, title, y_label, definitions in specs:
        fig, axis = plt.subplots(figsize=(10, 5.5))
        used = 0
        min_time_s = float("inf")
        max_time_s = float("-inf")
        for getter, label, color in definitions:
            x_values, y_values = series(rows, getter)
            if not x_values:
                continue
            min_time_s = min(min_time_s, min(x_values))
            max_time_s = max(max_time_s, max(x_values))
            axis.plot(x_values, y_values, linewidth=1.15, alpha=0.9, label=label, color=color)
            used += 1
        if used == 0:
            plt.close(fig)
            print(f"  Warning: no plottable data for {output_name}")
            continue
        axis.set_title(f"{run_id} | {title}")
        axis.set_xlabel("Time (s)")
        axis.set_ylabel(y_label)
        axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
        add_offset_markers(axis, offset_s, min_time_s, max_time_s)
        if len(definitions) > 1:
            axis.legend(loc="best", fontsize=8, frameon=True)
        fig.tight_layout()
        save_figure(fig, run_dir / output_name)
        plt.close(fig)
        plotted += 1
    print(f"  Plotted {plotted} figures")
    return 0


def main() -> int:
    args = parse_args()
    try:
        inputs = discover_inputs(args.input)
    except (FileNotFoundError, ValueError) as error:
        print(f"Error: {error}")
        return 2
    if not inputs:
        print("Error: no demo metric CSV files found")
        return 2
    result = 0
    for csv_path, run_dir in inputs:
        result = max(result, plot_run(csv_path, run_dir))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
