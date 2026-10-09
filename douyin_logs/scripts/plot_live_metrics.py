#!/usr/bin/env python3
"""Plot Douyin live metrics extracted from live_metrics_timeseries.csv."""

from __future__ import annotations

import argparse
import csv
import gzip
from pathlib import Path


OFFSET_FILE = "offset.txt"
OFFSET_PERIOD_S = 15.0

PLOT_SPECS = (
    ("live_bitrate_time", "Live bitrate", "Bitrate", (("real_bitrate", "real_bitrate"),)),
    ("live_framerate_time", "Live framerate", "FPS", (("real_video_framerate", "real_video_framerate"),)),
    ("live_ntp_diff_time", "Live NTP diff", "Milliseconds", (("ntp_diff", "ntp_diff"),)),
    (
        "live_network_rtt_time",
        "Network RTT",
        "RTT (ms)",
        (("http_rtt", "http_rtt"), ("tcp_rtt", "tcp_rtt"), ("quic_rtt", "quic_rtt")),
    ),
    (
        "live_downstream_throughput_time",
        "Downstream throughput",
        "Kbps",
        (("downstream_throughput_kbps", "downstream_throughput_kbps"),),
    ),
    (
        "live_video_download_speed_time",
        "Video download speed",
        "Speed",
        (("video_download_speed", "video_download_speed"),),
    ),
    (
        "live_quic_loss_rate_time",
        "QUIC loss rate",
        "Loss rate",
        (("quic_receive_loss_rate", "quic_receive_loss_rate"), ("quic_send_loss_rate", "quic_send_loss_rate")),
    ),
    (
        "live_api_timing_time",
        "API timing",
        "Milliseconds",
        (
            ("timing_total", "total"),
            ("timing_waiting", "waiting"),
            ("timing_receive", "receive"),
            ("timing_send", "send"),
        ),
    ),
    (
        "live_ws_bytes_time",
        "WebSocket traffic",
        "Bytes",
        (("sent_bytes", "sent"), ("received_bytes", "received")),
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create PNG time-series plots from Douyin live_metrics_timeseries.csv."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "Run directory or CSV/CSV.GZ file. Default: "
            "douyin_logs/logs/douyin_logcat_20260713T120238Z_300s_douyin_watch/live_metrics_timeseries.csv"
        ),
    )
    return parser.parse_args()


def default_input() -> Path:
    script_dir = Path(__file__).resolve().parent
    return (
        script_dir.parent
        / "logs"
        / "douyin_logcat_20260713T120238Z_300s_douyin_watch"
        / "live_metrics_timeseries.csv"
    )


def resolve_input(path: Path | None) -> tuple[Path, Path]:
    input_path = path or default_input()
    if input_path.is_dir():
        gz_path = input_path / "live_metrics_timeseries.csv.gz"
        csv_path = input_path / "live_metrics_timeseries.csv"
        return (gz_path if gz_path.exists() else csv_path), input_path.resolve()
    return input_path, input_path.parent.resolve()


def parse_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    if csv_path.suffix == ".gz":
        with gzip.open(csv_path, "rt", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_offset(run_dir: Path) -> float | None:
    offset_path = run_dir / OFFSET_FILE
    if not offset_path.exists():
        return None
    raw_value = offset_path.read_text(encoding="utf-8").strip()
    value = parse_float(raw_value)
    if value is None:
        print(f"WARNING: ignoring invalid offset in {offset_path}: {raw_value!r}")
    return value


def series(rows: list[dict[str, str]], column: str) -> tuple[list[float], list[float]]:
    points: list[tuple[float, float]] = []
    for row in rows:
        x = parse_float(row.get("time_s"))
        y = parse_float(row.get(column))
        if x is None or y is None:
            continue
        points.append((x, y))
    points.sort(key=lambda item: item[0])
    if not points:
        return [], []
    xs, ys = zip(*points)
    return list(xs), list(ys)


def add_offset_lines(ax, offset_s: float | None, max_time_s: float) -> bool:
    if offset_s is None:
        return False

    line_time = offset_s
    while line_time < 0:
        line_time += OFFSET_PERIOD_S

    added = False
    while line_time <= max_time_s:
        label = f"{OFFSET_FILE}: m + 15k" if not added else None
        ax.axvline(
            line_time,
            color="#ff7f7f",
            linestyle="--",
            linewidth=0.9,
            alpha=0.6,
            label=label,
        )
        line_time += OFFSET_PERIOD_S
        added = True

    return added


def save_figure(fig, output_base: Path) -> None:
    png_path = output_base.with_suffix(".png")
    fig.savefig(png_path, dpi=160)
    print(f"Wrote {png_path}")


def plot_specs(csv_path: Path, run_dir: Path) -> int:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        print(f"ERROR: matplotlib is required for plotting: {exc}")
        return 2

    rows = read_rows(csv_path)
    if not rows:
        print(f"WARNING: no rows found in {csv_path}")
        return 0

    run_id = rows[0].get("run_id") or run_dir.name
    offset_s = read_offset(run_dir)
    plotted = 0
    for output_name, title, ylabel, columns in PLOT_SPECS:
        fig, ax = plt.subplots(figsize=(10, 5.5))
        used = 0
        max_time_s = 0.0
        for column, label in columns:
            xs, ys = series(rows, column)
            if not xs:
                continue
            max_time_s = max(max_time_s, max(xs))
            ax.plot(xs, ys, linewidth=1.05, alpha=0.9, label=label)
            used += 1

        if not used:
            plt.close(fig)
            print(f"WARNING: no plottable data for {output_name}")
            continue

        ax.set_title(f"{run_id} | {title}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel(ylabel)
        ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
        offset_lines_added = add_offset_lines(ax, offset_s, max_time_s)
        if used > 1 or offset_lines_added:
            ax.legend(loc="best", fontsize=8, frameon=True)
        fig.tight_layout()
        save_figure(fig, run_dir / output_name)
        plt.close(fig)
        plotted += 1

    print(f"Done. Plotted {plotted} figures.")
    return 0


def main() -> int:
    args = parse_args()
    csv_path, run_dir = resolve_input(args.input)
    if not csv_path.exists() or not csv_path.is_file():
        print(f"ERROR: CSV file does not exist: {csv_path}")
        return 2
    return plot_specs(csv_path, run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
