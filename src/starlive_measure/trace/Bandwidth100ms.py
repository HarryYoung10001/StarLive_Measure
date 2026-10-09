#!/usr/bin/env python3
"""Build non-overlapping 100 ms UDP bandwidth traces and PDF line plots."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable


BIN_MS = 100.0
OUTPUT_TSV = "bandwidth_100ms.tsv"
OUTPUT_PDF = "bandwidth_100ms_line.pdf"


def _key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def payload_from_run(run_dir: Path, explicit: int | None = None) -> int:
    if explicit is not None:
        if explicit <= 0:
            raise ValueError("payload bytes must be positive")
        return explicit
    for name in ("profile.json",):
        try:
            profile = json.loads((run_dir / name).read_text(encoding="utf-8"))
            value = int(profile.get("workload", {}).get("payload_bytes", 0) or 0)
            if value > 0:
                return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    for directory in (run_dir, run_dir / "roles" / "server", run_dir / "roles" / "udp_sender", run_dir / "roles" / "udp_receiver"):
        for name, key in (("metadata.txt", "udp_payload_len"), ("uplink_udp_sender_summary.txt", "payload_bytes")):
            raw = _key_values(directory / name).get(key)
            try:
                value = int(raw or 0)
            except ValueError:
                value = 0
            if value > 0:
                return value
    raise ValueError(f"payload bytes could not be inferred for {run_dir}")


def packet_events(run_dir: Path, payload_bytes: int | None = None) -> list[tuple[float, int]]:
    precise = run_dir / "derived" / "udp_arrival_time_precise.tsv"
    if precise.is_file():
        rows: list[tuple[float, int]] = []
        with precise.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                try:
                    rows.append((float(row["receive_rel_s"]) * 1000.0, int(row["payload_bytes"])))
                except (KeyError, TypeError, ValueError):
                    continue
        if rows:
            return rows
    arrival = run_dir / "derived" / "udp_arrival_time_ms.txt"
    if not arrival.is_file():
        raise FileNotFoundError(f"missing arrival trace in {run_dir}")
    payload = payload_from_run(run_dir, payload_bytes)
    return [(float(line.strip()), payload) for line in arrival.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_100ms_points(
    events: Iterable[tuple[float, int]],
    *,
    duration_ms: float | None = None,
    bin_ms: float = BIN_MS,
) -> list[dict[str, float | int]]:
    if bin_ms <= 0:
        raise ValueError("bin_ms must be positive")
    buckets: dict[int, tuple[int, int]] = {}
    last_ms = 0.0
    for time_ms, payload in events:
        if time_ms < 0 or payload < 0:
            raise ValueError("packet event values must be non-negative")
        index = int(time_ms // bin_ms)
        packets, total_bytes = buckets.get(index, (0, 0))
        buckets[index] = packets + 1, total_bytes + int(payload)
        last_ms = max(last_ms, time_ms)
    if not buckets:
        raise ValueError("no packet events found")
    if duration_ms is not None:
        if duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        # A duration is an exclusive endpoint: [0, duration_ms).
        last_index = max(0, int(math.ceil(duration_ms / bin_ms) - 1))
    else:
        last_index = max(0, int(last_ms // bin_ms))
    points: list[dict[str, float | int]] = []
    for index in range(last_index + 1):
        packets, total_bytes = buckets.get(index, (0, 0))
        points.append({
            "time_ms": index * bin_ms,
            "interval_end_ms": (index + 1) * bin_ms,
            "packet_count": packets,
            "payload_bytes": total_bytes,
            "bandwidth_mbps": total_bytes * 8.0 / (bin_ms * 1000.0),
        })
    return points


def write_tsv(points: list[dict[str, float | int]], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["time_ms", "interval_end_ms", "packet_count", "payload_bytes", "bandwidth_mbps"],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for point in points:
            writer.writerow({**point, "time_ms": f"{float(point['time_ms']):.6f}", "interval_end_ms": f"{float(point['interval_end_ms']):.6f}", "bandwidth_mbps": f"{float(point['bandwidth_mbps']):.9f}"})
    return output


def _load_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot_pdf(points: list[dict[str, float | int]], output: Path, *, x_end_s: float | None = None) -> Path:
    plt = _load_matplotlib()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure, axis = plt.subplots(figsize=(8, 6))
    xs = [float(item["time_ms"]) / 1000.0 for item in points]
    ys = [float(item["bandwidth_mbps"]) for item in points]
    axis.plot(xs, ys, color="#2563eb", linewidth=1.15)
    axis.set_xlabel("Time (s)", fontsize=16)
    axis.set_ylabel("Bandwidth (Mbps)", fontsize=16)
    axis.tick_params(axis="both", labelsize=13)
    axis.grid(True, linestyle="--", linewidth=0.65, color="#9ca3af", alpha=0.7)
    axis.set_xlim(0, max(x_end_s or 0, xs[-1] if xs else 0, 1))
    axis.set_ylim(bottom=0)
    figure.tight_layout()
    figure.savefig(output, format="pdf")
    plt.close(figure)
    return output


def find_run_dirs(input_path: Path) -> list[Path]:
    if (input_path / "manifest.json").is_file():
        return [input_path]
    return sorted(path.parent for path in input_path.rglob("manifest.json"))


def process_run(run_dir: Path, *, payload_bytes: int | None = None, duration_ms: float | None = None) -> tuple[Path, Path]:
    events = packet_events(run_dir, payload_bytes)
    points = build_100ms_points(events, duration_ms=duration_ms)
    tsv = write_tsv(points, run_dir / "derived" / OUTPUT_TSV)
    pdf = plot_pdf(points, run_dir / "derived" / OUTPUT_PDF, x_end_s=(duration_ms / 1000.0 if duration_ms else None))
    return tsv, pdf


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="run or session directory")
    parser.add_argument("--payload-bytes", type=int)
    parser.add_argument("--duration-ms", type=float)
    args = parser.parse_args(argv)
    runs = find_run_dirs(args.input)
    if not runs:
        parser.error(f"no run manifests found below {args.input}")
    failures = 0
    for run in runs:
        try:
            tsv, pdf = process_run(run, payload_bytes=args.payload_bytes, duration_ms=args.duration_ms)
            print(f"Wrote {tsv} and {pdf}")
        except (OSError, ValueError, ImportError) as error:
            print(f"ERROR: {run}: {error}")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
