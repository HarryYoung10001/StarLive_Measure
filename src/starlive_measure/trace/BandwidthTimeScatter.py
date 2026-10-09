#!/usr/bin/env python3
"""Plot per-millisecond UDP arrival bandwidth using a rolling 10ms window."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


ARRIVAL_FILENAME = "udp_arrival_time_ms.txt"
OUTPUT_FILENAME = "bandwidth_time_scatter.png"
DEFAULT_PAYLOAD_BYTES = 1472
WINDOW_MS = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot per-millisecond UDP receive bandwidth from arrival times."
    )
    parser.add_argument("input", type=Path, help="arrival TXT, a run directory, or a session directory.")
    parser.add_argument(
        "--payload-bytes",
        type=int,
        help="UDP payload bytes for a standalone TXT; run metadata is preferred when available.",
    )
    parser.add_argument("--output", "-o", type=Path, help="Output PNG for a single TXT/run input.")
    parser.add_argument(
        "--skip-empty-ms",
        action="store_true",
        help="Plot only milliseconds containing packets instead of filling gaps with zero.",
    )
    parser.add_argument(
        "--window-ms",
        type=int,
        default=WINDOW_MS,
        help="Trailing integer-millisecond window width (default: 10).",
    )
    parser.add_argument(
        "--duration-ms",
        type=int,
        help="Extend the plotted timeline through this millisecond offset.",
    )
    return parser.parse_args()


def read_arrival_times(path: Path) -> list[int]:
    values: list[int] = []
    previous: int | None = None
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            raise ValueError(f"blank arrival value at line {line_number}")
        try:
            value = int(text, 10)
        except ValueError as error:
            raise ValueError(f"invalid integer at line {line_number}: {text!r}") from error
        if value < 0:
            raise ValueError(f"negative arrival time at line {line_number}: {value}")
        if previous is not None and value < previous:
            raise ValueError(f"arrival times are not non-decreasing at line {line_number}")
        values.append(value)
        previous = value
    if not values:
        raise ValueError(f"no arrival values found in {path}")
    return values


def _read_key_value(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def infer_payload_bytes(path: Path, explicit: int | None = None) -> int:
    if explicit is not None:
        if explicit <= 0:
            raise ValueError("--payload-bytes must be positive")
        return explicit

    for directory in (path.parent, *path.parents):
        for filename, key in (
            ("metadata.txt", "udp_payload_len"),
            ("uplink_udp_sender_summary.txt", "payload_bytes"),
        ):
            raw = _read_key_value(directory / filename).get(key)
            if raw is None:
                continue
            try:
                payload = int(raw, 10)
            except ValueError:
                continue
            if payload > 0:
                return payload
    if path.name == ARRIVAL_FILENAME:
        raise ValueError(
            "payload bytes could not be inferred for a standalone TXT; provide --payload-bytes"
        )
    return DEFAULT_PAYLOAD_BYTES


def find_arrival_files(input_path: Path) -> list[tuple[Path, Path]]:
    """Return (arrival file, owning run directory) pairs."""

    if input_path.is_file():
        if input_path.name != ARRIVAL_FILENAME:
            raise ValueError(f"expected {ARRIVAL_FILENAME}, got {input_path.name}")
        owner = input_path.parent.parent if input_path.parent.name == "server" else input_path.parent
        return [(input_path, owner)]
    if not input_path.is_dir():
        return []
    if (input_path / ARRIVAL_FILENAME).is_file():
        owner = input_path.parent if input_path.name == "server" else input_path
        return [(input_path / ARRIVAL_FILENAME, owner)]
    pairs: list[tuple[Path, Path]] = []
    for run_dir in sorted(input_path.glob("dual_udp_latency_uplink_*")):
        if not run_dir.is_dir():
            continue
        candidates = [run_dir / ARRIVAL_FILENAME, run_dir / "server" / ARRIVAL_FILENAME]
        arrival = next((candidate for candidate in candidates if candidate.is_file()), None)
        if arrival is not None:
            pairs.append((arrival, run_dir))
    return pairs


def build_bandwidth_points(
    arrivals: list[int],
    payload_bytes: int,
    include_empty: bool = True,
    window_ms: int = WINDOW_MS,
    end_ms: int | None = None,
) -> tuple[list[int], list[float]]:
    if payload_bytes <= 0:
        raise ValueError("payload bytes must be positive")
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    if end_ms is not None and end_ms < 0:
        raise ValueError("end_ms must be non-negative")
    arrivals = sorted(arrivals)
    if not arrivals:
        raise ValueError("no arrival values found")

    # The integer-ms window is (t-window_ms, t], represented as the ten
    # buckets [t-window_ms+1, t]. Each right edge advances both pointers once.
    if include_empty:
        final_time = max(arrivals[-1], end_ms) if end_ms is not None else arrivals[-1]
        times = list(range(arrivals[0], final_time + 1))
    else:
        times = sorted(Counter(arrivals))
    left = 0
    right = 0
    counts: list[int] = []
    for time_ms in times:
        while right < len(arrivals) and arrivals[right] <= time_ms:
            right += 1
        window_start = time_ms - window_ms + 1
        while left < right and arrivals[left] < window_start:
            left += 1
        counts.append(right - left)
    per_packet_mbps = payload_bytes * 8 / (window_ms * 1000.0)
    return times, [count * per_packet_mbps for count in counts]


def load_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_run(
    run_dir: Path,
    arrival_path: Path,
    payload_bytes: int,
    output: Path | None = None,
    include_empty: bool = True,
    window_ms: int = WINDOW_MS,
    end_ms: int | None = None,
) -> Path:
    arrivals = read_arrival_times(arrival_path)
    times, bandwidth = build_bandwidth_points(
        arrivals, payload_bytes, include_empty, window_ms, end_ms
    )
    default_filename = (
        OUTPUT_FILENAME if window_ms == WINDOW_MS else f"bandwidth_time_scatter_{window_ms}ms.png"
    )
    output_path = output or run_dir / default_filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt = load_matplotlib()
    figure, axis = plt.subplots(figsize=(10, 5.5))
    axis.scatter(times, bandwidth, s=6, alpha=0.6, linewidths=0)
    axis.set_title(f"{run_dir.name} | bandwidth-time")
    axis.set_xlabel("Time since first packet (ms)")
    axis.set_ylabel("Received UDP bandwidth (Mbps)")
    axis.set_ylim(bottom=0)
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.65)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)
    return output_path


def main() -> int:
    args = parse_args()
    try:
        if args.window_ms <= 0:
            raise ValueError("--window-ms must be positive")
        pairs = find_arrival_files(args.input)
        if not pairs:
            raise ValueError(f"no {ARRIVAL_FILENAME} found at {args.input}")
        if args.output and len(pairs) != 1:
            raise ValueError("--output requires a single TXT or run input")
        for arrival_path, run_dir in pairs:
            payload_bytes = infer_payload_bytes(arrival_path, args.payload_bytes)
            output = plot_run(
                run_dir,
                arrival_path,
                payload_bytes,
                args.output,
                include_empty=not args.skip_empty_ms,
                window_ms=args.window_ms,
                end_ms=args.duration_ms,
            )
            print(f"Wrote {output} (payload_bytes={payload_bytes})")
    except (OSError, ValueError, ImportError) as error:
        print(f"ERROR: failed to plot bandwidth: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
