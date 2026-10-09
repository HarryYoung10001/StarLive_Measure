#!/usr/bin/env python3
"""Plot fixed-window UDP delay values as a continuous time-latency line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


WINDOW_MS = 10
TIME_FILENAME = "delay_window_time_ms.txt"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot delay.txt as a time-latency line."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="A run directory containing delay.txt or a session directory.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output PNG for a single run. Session inputs write one PNG per run.",
    )
    return parser.parse_args()


def read_delay_values(path: Path) -> list[int]:
    values: list[int] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        value = line.strip()
        if not value:
            raise ValueError(f"blank delay value at line {line_number}")
        try:
            parsed = int(value, 10)
        except ValueError as error:
            raise ValueError(f"invalid integer at line {line_number}: {value!r}") from error
        values.append(parsed)
    if not values:
        raise ValueError(f"no delay values found in {path}")
    return values


def read_window_times(path: Path) -> list[float]:
    values: list[float] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        value = line.strip()
        if not value:
            raise ValueError(f"blank window time at line {line_number}")
        try:
            values.append(float(value))
        except ValueError as error:
            raise ValueError(f"invalid window time at line {line_number}: {value!r}") from error
    if not values:
        raise ValueError(f"no window times found in {path}")
    return values


def find_run_dirs(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path.parent]
    if not input_path.is_dir():
        return []
    if (input_path / "delay.txt").is_file():
        return [input_path]
    return sorted(
        path
        for path in input_path.glob("dual_udp_latency_uplink_*")
        if path.is_dir()
        and (path / "delay.txt").is_file()
        and (path / TIME_FILENAME).is_file()
    )


def load_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_run(run_dir: Path, output: Path | None = None) -> Path:
    delay_path = run_dir / "delay.txt"
    time_path = run_dir / TIME_FILENAME
    values = read_delay_values(delay_path)
    times = read_window_times(time_path)
    if len(values) != len(times):
        raise ValueError(
            f"delay/time sidecar length mismatch: {len(values)} != {len(times)}"
        )
    output_path = output or run_dir / "time_delay_scatter.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    plt = load_matplotlib()
    xs = times
    fig, axis = plt.subplots(figsize=(10, 5.5))
    axis.plot(xs, values, color="tab:blue", linewidth=0.8)
    axis.set_title(f"{run_dir.name} | time-delay")
    axis.set_xlabel("Window start time (ms)")
    axis.set_ylabel("UDP one-way delay (ms)")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    fig.tight_layout()
    fig.savefig(output_path, dpi=160)
    plt.close(fig)
    return output_path


def main() -> int:
    args = parse_args()
    run_dirs = find_run_dirs(args.input)
    if not run_dirs:
        print(f"ERROR: no run with delay.txt found at {args.input}", file=sys.stderr)
        return 2
    if args.output and len(run_dirs) != 1:
        print("ERROR: --output requires a single run input", file=sys.stderr)
        return 2
    try:
        for run_dir in run_dirs:
            output = plot_run(run_dir, args.output)
            print(f"Wrote {output}")
    except (OSError, ValueError, ImportError) as error:
        print(f"ERROR: failed to plot delay: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
