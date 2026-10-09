#!/usr/bin/env python3
"""Align traces so the first detected ``15*k+b`` event is at 12 seconds."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

try:
    from common import find_runs, latency_rows  # noqa: E402
    from loss_rate import process_run  # noqa: E402
except ImportError:  # package-style import
    from .common import find_runs, latency_rows  # noqa: E402
    from .loss_rate import process_run  # noqa: E402
from starlive_measure.trace.Bandwidth100ms import build_100ms_points, packet_events


TICKS = [0, 12, 27, 42, 57, 72, 87, 102]


def _ensure_bandwidth(run: Path) -> list[dict[str, float | int]]:
    path = run / "derived" / "bandwidth_100ms.tsv"
    if not path.is_file():
        points = build_100ms_points(packet_events(run))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["time_ms", "interval_end_ms", "packet_count", "payload_bytes", "bandwidth_mbps"], delimiter="\t", lineterminator="\n")
            writer.writeheader()
            for point in points:
                writer.writerow(point)
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _ensure_loss(run: Path) -> list[dict[str, str]]:
    path = run / "derived" / "loss_100ms.tsv"
    if not path.is_file():
        process_run(run)
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _aligned(rows: Iterable[tuple[float, float]], shift_s: float, end_s: float = 100.0) -> list[tuple[float, float]]:
    result = []
    for time_ms, value in rows:
        time_s = time_ms / 1000.0 + shift_s
        if 0.0 <= time_s <= end_s:
            result.append((time_s, value))
    return result


def write_aligned(run: Path, phase: dict, *, target_s: float = 12.0) -> dict[str, Path]:
    if phase.get("status") != "ok" or phase.get("b_s") is None:
        raise ValueError("configuration phase is unavailable or low confidence")
    b = float(phase["b_s"])
    shift_s = target_s - (b + 15.0)
    output_dir = run / "derived" / "aligned"
    output_dir.mkdir(parents=True, exist_ok=True)
    bandwidth_rows = _aligned(
        [(float(row["time_ms"]), float(row["bandwidth_mbps"])) for row in _ensure_bandwidth(run)],
        shift_s,
        102.0,
    )
    latency = _aligned(latency_rows(run), shift_s, 102.0)
    loss_rows = _aligned(
        [(float(row["time_ms"]), float(row["loss_rate"])) for row in _ensure_loss(run)],
        shift_s,
        102.0,
    )
    outputs: dict[str, Path] = {}
    for name, rows, field in (
        ("bandwidth_100ms.tsv", bandwidth_rows, "bandwidth_mbps"),
        ("latency_10ms.tsv", latency, "delay_ms"),
        ("loss_100ms.tsv", loss_rows, "loss_rate"),
    ):
        path = output_dir / name
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["time_s", field])
            writer.writerows((f"{time_s:.6f}", f"{value:.9f}") for time_s, value in rows)
        outputs[name] = path
    return outputs


def _load_matplotlib():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def _plot(rows: list[tuple[float, float]], output: Path, ylabel: str, phase: dict) -> Path:
    plt = _load_matplotlib()
    figure, axis = plt.subplots(figsize=(8, 6))
    if rows:
        axis.plot([row[0] for row in rows], [row[1] for row in rows], color="#2563eb", linewidth=1.15)
    for tick in TICKS[1:-1]:
        axis.axvline(tick, color="#9ca3af", linestyle="--", linewidth=0.55, alpha=0.65)
    axis.set_xlim(0, 102)
    axis.set_xticks(TICKS)
    axis.set_xlabel("Time (s)", fontsize=16)
    axis.set_ylabel(ylabel, fontsize=16)
    axis.tick_params(axis="both", labelsize=13)
    axis.grid(True, linestyle="--", linewidth=0.65, color="#9ca3af", alpha=0.7)
    axis.set_ylim(bottom=0)
    figure.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, format="pdf")
    plt.close(figure)
    return output


def plot_run(run: Path, phase: dict) -> list[Path]:
    aligned = write_aligned(run, phase)
    outputs = [
        _plot(_read_pairs(aligned["bandwidth_100ms.tsv"], "bandwidth_mbps"), run / "derived" / "aligned" / "bandwidth_100ms.pdf", "Bandwidth (Mbps)", phase),
        _plot(_read_pairs(aligned["latency_10ms.tsv"], "delay_ms"), run / "derived" / "aligned" / "latency_10ms.pdf", "Latency (ms)", phase),
        _plot(_read_pairs(aligned["loss_100ms.tsv"], "loss_rate"), run / "derived" / "aligned" / "loss_100ms.pdf", "Loss rate", phase),
    ]
    return outputs


def _read_pairs(path: Path, value_field: str) -> list[tuple[float, float]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [(float(row["time_s"]), float(row[value_field])) for row in csv.DictReader(handle, delimiter="\t")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="run or session directory")
    parser.add_argument("--phase", type=Path, required=True, help="configuration_phase.json")
    args = parser.parse_args(argv)
    try:
        phase = json.loads(args.phase.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: invalid phase file: {error}", file=sys.stderr)
        return 2
    runs = find_runs(args.input)
    if not runs:
        print(f"ERROR: no run manifests found below {args.input}", file=sys.stderr)
        return 2
    if not isinstance(phase.get("runs"), dict):
        print("ERROR: phase file must use schema v2 with runs mapping", file=sys.stderr)
        return 2
    failures = 0
    for run in runs:
        try:
            entry = phase["runs"].get(run.name)
            if not entry:
                print(f"WARNING: {run}: no phase entry; skipped", file=sys.stderr)
                continue
            try:
                from . import plot_measurement_series as series
            except ImportError:
                import plot_measurement_series as series
            series.plot_raw(run)
            result = series.plot_aligned(run, entry)
            for metric, info in result.items():
                if info.get("status") == "ok": print(f"Wrote aligned {metric} for {run}")
                else: print(f"WARNING: {run} {metric}: {info.get('reason')}", file=sys.stderr)
        except (OSError, ValueError, ImportError) as error:
            print(f"ERROR: {run}: {error}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
