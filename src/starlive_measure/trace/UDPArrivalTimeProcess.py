#!/usr/bin/env python3
"""Export custom UDP packet arrivals as a one-column millisecond sequence."""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path


RECEIVER_FILENAMES = (
    "downlink_udp_receiver.csv",
    "downlink_udp_receiver.csv.gz",
    "uplink_udp_receiver.csv",
    "uplink_udp_receiver.csv.gz",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export receiver arrival times as integer milliseconds, one packet per line."
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Receiver CSV/.gz file or a trace run directory.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Output path. Default: udp_arrival_time_ms.txt in the run/input directory.",
    )
    return parser.parse_args()


def find_receiver_csv(input_path: Path) -> tuple[Path, Path] | None:
    """Return the receiver CSV and directory that should own default output."""

    if input_path.is_file():
        return input_path, input_path.parent
    if not input_path.is_dir():
        return None

    preferred_dirs = (
        input_path / "udp_phone",
        input_path / "udp_phone" / "remote",
        input_path,
    )
    for directory in preferred_dirs:
        for filename in RECEIVER_FILENAMES:
            candidate = directory / filename
            if candidate.is_file():
                return candidate, input_path

    for filename in RECEIVER_FILENAMES:
        candidates = sorted(path for path in input_path.rglob(filename) if path.is_file())
        if candidates:
            return candidates[0], input_path
    return None


def open_csv_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def read_arrival_ms(path: Path) -> tuple[list[int], int]:
    """Read all valid receiver arrivals and return millisecond values plus skips."""

    values: list[int] = []
    skipped = 0
    with open_csv_text(path) as handle:
        for row in csv.DictReader(handle):
            raw_time = row.get("recv_rel_s")
            try:
                recv_time = Decimal(raw_time) if raw_time is not None else None
            except InvalidOperation:
                recv_time = None
            if recv_time is None or not recv_time.is_finite() or recv_time < 0:
                skipped += 1
                continue
            milliseconds = (recv_time * Decimal("1000")).to_integral_value(
                rounding=ROUND_FLOOR
            )
            values.append(int(milliseconds))
    values.sort()
    return values, skipped


def write_values(path: Path, values: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def main() -> int:
    args = parse_args()
    found = find_receiver_csv(args.input)
    if found is None:
        print(f"ERROR: no custom UDP receiver CSV (.csv or .gz) found at {args.input}", file=sys.stderr)
        return 2
    source, output_dir = found
    output = args.output or output_dir / "udp_arrival_time_ms.txt"
    if source.resolve() == output.resolve():
        print("ERROR: output path must not overwrite the input CSV", file=sys.stderr)
        return 2

    try:
        values, skipped = read_arrival_ms(source)
    except (OSError, csv.Error) as error:
        print(f"ERROR: failed to read {source}: {error}", file=sys.stderr)
        return 2
    if not values:
        print(f"ERROR: no valid recv_rel_s values found in {source}", file=sys.stderr)
        return 1

    write_values(output, values)
    if skipped:
        print(f"WARNING: skipped {skipped} invalid receiver rows", file=sys.stderr)
    print(f"Wrote {len(values)} arrival rows: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
