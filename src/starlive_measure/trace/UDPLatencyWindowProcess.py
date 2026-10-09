#!/usr/bin/env python3
"""Export fixed-window custom UDP one-way-delay averages as integer milliseconds."""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path


LATENCY_FILENAMES = ("udp_flow_latency.csv", "udp_flow_latency.csv.gz")
RECEIVER_FILENAMES = ("uplink_udp_receiver.csv", "uplink_udp_receiver.csv.gz")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Average UDP one-way delay in fixed receive-time windows."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="udp_flow_latency CSV/.gz file or a trace run directory.",
    )
    parser.add_argument(
        "--window-ms",
        default="10",
        help="Fixed, non-overlapping receive-time window in milliseconds. Default: 10.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output path. Default: delay.txt in the run/input directory.",
    )
    parser.add_argument(
        "--sender-summary",
        type=Path,
        help="Sender summary for raw uplink receiver CSV input.",
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        help="Optional key=value summary output for validation.",
    )
    parser.add_argument(
        "--time-output",
        type=Path,
        help="Optional one-column window start times in milliseconds.",
    )
    return parser.parse_args()


def parse_positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("must be a positive number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("must be a positive number")
    return parsed


def find_latency_csv(input_path: Path) -> tuple[Path, Path] | None:
    """Return a latency or raw uplink receiver CSV and its owning directory."""

    if input_path.is_file():
        return input_path, input_path.parent
    if not input_path.is_dir():
        return None

    for filename in LATENCY_FILENAMES + RECEIVER_FILENAMES:
        candidate = input_path / filename
        if candidate.is_file():
            return candidate, input_path
    for filename in LATENCY_FILENAMES + RECEIVER_FILENAMES:
        candidates = sorted(path for path in input_path.rglob(filename) if path.is_file())
        if candidates:
            return candidates[0], input_path
    return None


def open_csv_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def parse_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def is_received(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _window_result(
    windows: defaultdict[int, list[Decimal]],
    skipped: int,
    first_recv_s: Decimal | None = None,
    last_recv_s: Decimal | None = None,
    clock_offset_ms: Decimal | None = None,
    clock_sync_samples: int | None = None,
) -> tuple[list[int], int, dict[str, object]]:
    """Round windows and collect validation metadata."""

    values: list[int] = []
    for index in sorted(windows):
        delays = windows[index]
        mean_delay = sum(delays, Decimal("0")) / len(delays)
        values.append(int(mean_delay.to_integral_value(rounding=ROUND_HALF_UP)))

    indices = sorted(windows)
    empty_windows = 0
    if indices:
        empty_windows = indices[-1] - indices[0] + 1 - len(indices)
    stats: dict[str, object] = {
        "valid_delay_rows": sum(len(rows) for rows in windows.values()),
        "delay_windows": len(values),
        "first_window_index": indices[0] if indices else "",
        "last_window_index": indices[-1] if indices else "",
        "window_indices": indices,
        "empty_windows": empty_windows,
        "first_recv_rel_s": first_recv_s,
        "last_recv_rel_s": last_recv_s,
        "clock_offset_ms": clock_offset_ms,
        "clock_sync_samples": clock_sync_samples,
    }
    return values, skipped, stats


def read_window_values(path: Path, window_ms: Decimal) -> tuple[list[int], int]:
    """Read udp_flow_latency.csv and return rounded window values."""

    windows: defaultdict[int, list[Decimal]] = defaultdict(list)
    skipped = 0
    first_recv_s: Decimal | None = None
    last_recv_s: Decimal | None = None
    with open_csv_text(path) as handle:
        for row in csv.DictReader(handle):
            if not is_received(row.get("received")):
                skipped += 1
                continue
            recv_time_s = parse_decimal(row.get("recv_rel_s"))
            delay_ms = parse_decimal(row.get("one_way_delay_ms"))
            if recv_time_s is None or delay_ms is None or recv_time_s < 0:
                skipped += 1
                continue
            window_index = int(
                (recv_time_s * Decimal("1000") / window_ms).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            windows[window_index].append(delay_ms)
            first_recv_s = recv_time_s if first_recv_s is None else min(first_recv_s, recv_time_s)
            last_recv_s = recv_time_s if last_recv_s is None else max(last_recv_s, recv_time_s)
    values, skipped, _ = _window_result(windows, skipped, first_recv_s, last_recv_s)
    return values, skipped


def read_receiver_window_values(
    path: Path,
    window_ms: Decimal,
    sender_summary: Path,
) -> tuple[list[int], int, dict[str, object]]:
    """Compute corrected one-way delay directly from a raw uplink receiver CSV."""

    summary: dict[str, str] = {}
    for line in sender_summary.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        summary[key.strip()] = value.strip()
    offset = parse_decimal(summary.get("clock_offset_ms"))
    if offset is None:
        raise ValueError("sender summary has no finite clock_offset_ms")
    try:
        clock_samples = int(summary.get("clock_sync_samples", ""))
    except ValueError as error:
        raise ValueError("sender summary has invalid clock_sync_samples") from error
    if clock_samples <= 0:
        raise ValueError("sender summary clock_sync_samples must be positive")

    windows: defaultdict[int, list[Decimal]] = defaultdict(list)
    skipped = 0
    first_recv_s: Decimal | None = None
    last_recv_s: Decimal | None = None
    with open_csv_text(path) as handle:
        for row in csv.DictReader(handle):
            recv_time_s = parse_decimal(row.get("recv_rel_s"))
            send_unix_ns = parse_decimal(row.get("send_unix_ns"))
            recv_unix_ns = parse_decimal(row.get("recv_unix_ns"))
            if (
                recv_time_s is None
                or send_unix_ns is None
                or recv_unix_ns is None
                or recv_time_s < 0
            ):
                skipped += 1
                continue
            delay_ms = (recv_unix_ns - send_unix_ns) / Decimal("1000000") + offset
            window_index = int(
                (recv_time_s * Decimal("1000") / window_ms).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            )
            windows[window_index].append(delay_ms)
            first_recv_s = recv_time_s if first_recv_s is None else min(first_recv_s, recv_time_s)
            last_recv_s = recv_time_s if last_recv_s is None else max(last_recv_s, recv_time_s)

    return _window_result(
        windows,
        skipped,
        first_recv_s,
        last_recv_s,
        offset,
        clock_samples,
    )


def _summary_text(source: Path, skipped: int, stats: dict[str, object]) -> str:
    def value(key: str) -> object:
        item = stats.get(key, "")
        return "" if item is None else item

    first_s = stats.get("first_recv_rel_s")
    last_s = stats.get("last_recv_rel_s")
    first_ms = "" if first_s in (None, "") else Decimal(str(first_s)) * Decimal("1000")
    last_ms = "" if last_s in (None, "") else Decimal(str(last_s)) * Decimal("1000")
    return "\n".join(
        [
            f"source={source}",
            f"valid_delay_rows={value('valid_delay_rows')}",
            f"delay_windows={value('delay_windows')}",
            f"first_window_index={value('first_window_index')}",
            f"last_window_index={value('last_window_index')}",
            f"empty_windows={value('empty_windows')}",
            f"skipped_rows={skipped}",
            f"first_recv_rel_ms={first_ms}",
            f"last_recv_rel_ms={last_ms}",
            f"clock_offset_ms={value('clock_offset_ms')}",
            f"clock_sync_samples={value('clock_sync_samples')}",
            "",
        ]
    )


def write_window_times(path: Path, stats: dict[str, object], window_ms: Decimal) -> None:
    indices = stats.get("window_indices", [])
    if not isinstance(indices, list):
        raise ValueError("window indices are unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{Decimal(index) * window_ms}\n" for index in indices),
        encoding="utf-8",
    )


def write_values(path: Path, values: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        window_ms = parse_positive_decimal(args.window_ms)
    except ValueError as error:
        print(f"ERROR: --window-ms {error}", file=sys.stderr)
        return 2

    found = find_latency_csv(args.input)
    if found is None:
        print(
            f"ERROR: no udp_flow_latency.csv(.gz) or uplink_udp_receiver.csv(.gz) found at {args.input}",
            file=sys.stderr,
        )
        return 2
    source, output_dir = found
    output = args.output or output_dir / "delay.txt"
    if source.resolve() == output.resolve():
        print("ERROR: output path must not overwrite the input CSV", file=sys.stderr)
        return 2

    try:
        if source.name in RECEIVER_FILENAMES:
            sender_summary = args.sender_summary or source.parent / "uplink_udp_sender_summary.txt"
            if not sender_summary.is_file():
                raise ValueError(f"sender summary not found: {sender_summary}")
            values, skipped, stats = read_receiver_window_values(
                source, window_ms, sender_summary
            )
        else:
            values, skipped = read_window_values(source, window_ms)
            stats = {
                "valid_delay_rows": sum(1 for _ in values),
                "delay_windows": len(values),
                "first_window_index": "",
                "last_window_index": "",
                "window_indices": list(range(len(values))),
                "empty_windows": "",
                "first_recv_rel_s": None,
                "last_recv_rel_s": None,
                "clock_offset_ms": None,
                "clock_sync_samples": None,
            }
    except (OSError, csv.Error, ValueError) as error:
        print(f"ERROR: failed to read {source}: {error}", file=sys.stderr)
        return 2
    if not values:
        print(f"ERROR: no valid received delay rows found in {source}", file=sys.stderr)
        return 1

    write_values(output, values)
    if args.time_output:
        write_window_times(args.time_output, stats, window_ms)
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(
            _summary_text(source, skipped, stats), encoding="utf-8"
        )
    if skipped:
        print(f"WARNING: skipped {skipped} non-received or invalid latency rows", file=sys.stderr)
    print(f"Wrote {len(values)} window averages ({window_ms} ms): {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
