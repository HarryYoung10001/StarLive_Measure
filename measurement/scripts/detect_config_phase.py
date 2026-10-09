#!/usr/bin/env python3
"""Detect the repeating 15 s configuration phase ``15*k + b``."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Iterable
try:
    from common import find_runs, latency_rows, load_profile
    from loss_rate import process_run
except ImportError:  # package-style import
    from .common import find_runs, latency_rows, load_profile
    from .loss_rate import process_run
from starlive_measure.trace.Bandwidth100ms import build_100ms_points
try:
    from common import arrival_events
except ImportError:  # package-style import
    from .common import arrival_events


PERIOD_S = 15.0
SAMPLE_S = 0.1


def _median_smooth(values: list[float], radius: int = 1) -> list[float]:
    if not values:
        return []
    return [statistics.median(values[max(0, index - radius): index + radius + 1]) for index in range(len(values))]


def _robust_changes(values: list[float]) -> list[float]:
    smooth = _median_smooth(values)
    if len(smooth) < 2:
        return []
    differences = [abs(smooth[index] - smooth[index - 1]) for index in range(1, len(smooth))]
    center = statistics.median(differences)
    deviations = [abs(value - center) for value in differences]
    scale = max(1e-12, 1.4826 * statistics.median(deviations))
    # A zero-MAD series is common for synthetic step traces (and for a quiet
    # measurement).  Preserve the impulses instead of collapsing every value
    # to zero; the caller still rejects a genuinely featureless series.
    if max(differences, default=0.0) > 0 and scale <= 1e-12:
        scale = max(1e-12, max(differences) * 0.01)
    return [max(0.0, (value - center) / scale) for value in differences]


def _metric_series(run: Path, metric: str) -> list[float]:
    if metric == "rtt":
        path = run / "derived" / "rtt_10ms.tsv"
        if not path.is_file():
            return []
        with path.open(encoding="utf-8", newline="") as handle:
            return [float(row["rtt_ms"]) for row in csv.DictReader(handle, delimiter="\t") if row.get("rtt_ms")]
    if metric == "bandwidth":
        points = build_100ms_points(arrival_events(run))
        return [float(point["bandwidth_mbps"]) for point in points]
    if metric == "latency":
        rows = latency_rows(run)
        bins: dict[int, list[float]] = {}
        for time_ms, value in rows:
            bins.setdefault(int(time_ms // 100.0), []).append(value)
        return [statistics.fmean(bins[index]) if index in bins else 0.0 for index in range(max(bins, default=-1) + 1)]
    if metric == "loss":
        path = run / "derived" / "loss_100ms.tsv"
        if not path.is_file():
            process_run(run)
        with path.open(encoding="utf-8", newline="") as handle:
            return [float(row["loss_rate"]) for row in csv.DictReader(handle, delimiter="\t")]
    raise ValueError(f"unknown metric: {metric}")


def score_phase(changes: list[float], phase_s: float, period_s: float = PERIOD_S) -> float:
    if not changes:
        return 0.0
    event_indices = [int(round((phase_s + period_s * index) / SAMPLE_S)) for index in range(-1, int(len(changes) * SAMPLE_S / period_s) + 2)]
    selected: list[float] = []
    for event_index in event_indices:
        if event_index < 0 or event_index >= len(changes):
            continue
        selected.extend(changes[max(0, event_index - 1): min(len(changes), event_index + 2)])
    if not selected:
        return 0.0
    return statistics.fmean(selected)


def detect_metric_phase(values: list[float], period_s: float = PERIOD_S) -> dict[str, float | int | bool | list[float]]:
    changes = _robust_changes(values)
    if len(changes) < int(period_s / SAMPLE_S):
        return {"status": "insufficient_signal", "b_s": None, "score": 0.0, "confidence": 0.0, "candidates_s": []}
    candidates = [round(index * SAMPLE_S, 3) for index in range(int(period_s / SAMPLE_S))]
    scores = [(score_phase(changes, candidate, period_s), candidate) for candidate in candidates]
    # When a configuration step has both a rising and falling edge, equal
    # scores are resolved to the earliest edge (the configuration onset).
    scores.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, best_phase = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    baseline = statistics.fmean(changes) if changes else 0.0
    confidence = max(0.0, min(1.0, (best_score - second_score) / max(best_score, 1e-9)))
    if best_score <= max(1.0, baseline * 1.25):
        return {"status": "insufficient_signal", "b_s": None, "score": best_score, "confidence": confidence, "candidates_s": [phase for _score, phase in scores[:5]]}
    return {"status": "ok", "b_s": best_phase, "score": best_score, "confidence": confidence, "candidates_s": [phase for _score, phase in scores[:5]]}


def circular_mean(phases: Iterable[float], period_s: float) -> tuple[float | None, float]:
    values = list(phases)
    if not values:
        return None, 0.0
    x = sum(math.cos(2 * math.pi * value / period_s) for value in values) / len(values)
    y = sum(math.sin(2 * math.pi * value / period_s) for value in values) / len(values)
    phase = (math.atan2(y, x) * period_s / (2 * math.pi)) % period_s
    return phase, math.hypot(x, y)


def detect(input_path: Path, period_s: float = PERIOD_S) -> dict:
    runs = find_runs(input_path)
    run_results: dict[str, dict] = {}
    for run in runs:
        metrics = {}
        for metric in ("bandwidth", "latency", "loss", "rtt"):
            if metric == "rtt" and load_profile(run).get("workload", {}).get("kind") != "udp_echo_rtt":
                metrics[metric] = {"status": "not_available", "b_s": None, "score": 0.0, "confidence": 0.0, "candidates_s": []}
                continue
            try:
                result = detect_metric_phase(_metric_series(run, metric), period_s)
            except (OSError, ValueError, KeyError):
                result = {"status": "insufficient_signal", "b_s": None, "score": 0.0, "confidence": 0.0, "candidates_s": []}
            metrics[metric] = result
        run_results[run.name] = {"metrics": metrics}
    return {
        "schema_version": 2,
        "period_s": period_s,
        "runs": run_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--period-s", type=float, default=PERIOD_S)
    args = parser.parse_args(argv)
    result = detect(args.input, args.period_s)
    output = args.output or (args.input / "configuration_phase.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
