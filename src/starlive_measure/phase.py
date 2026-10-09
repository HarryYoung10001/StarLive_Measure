"""Pure helpers for detecting periodic measurement phase offsets."""

from __future__ import annotations

import math
import statistics


PERIOD_S = 15.0
SAMPLE_S = 0.1


def _robust_changes(values: list[float]) -> list[float]:
    if not values:
        return []
    smooth = [statistics.median(values[max(0, i - 1): i + 2]) for i in range(len(values))]
    if len(smooth) < 2:
        return []
    differences = [abs(smooth[i] - smooth[i - 1]) for i in range(1, len(smooth))]
    center = statistics.median(differences)
    deviations = [abs(value - center) for value in differences]
    scale = max(1e-12, 1.4826 * statistics.median(deviations))
    if max(differences, default=0.0) > 0 and scale <= 1e-12:
        scale = max(1e-12, max(differences) * 0.01)
    return [max(0.0, (value - center) / scale) for value in differences]


def _score_phase(changes: list[float], phase_s: float, period_s: float) -> float:
    if not changes:
        return 0.0
    event_indices = [int(round((phase_s + period_s * index) / SAMPLE_S)) for index in range(-1, int(len(changes) * SAMPLE_S / period_s) + 2)]
    selected: list[float] = []
    for event_index in event_indices:
        if 0 <= event_index < len(changes):
            selected.extend(changes[max(0, event_index - 1): min(len(changes), event_index + 2)])
    return statistics.fmean(selected) if selected else 0.0


def detect_metric_phase(values: list[float], period_s: float = PERIOD_S) -> dict[str, float | list[float] | None | str]:
    changes = _robust_changes(values)
    if len(changes) < int(period_s / SAMPLE_S):
        return {"status": "insufficient_signal", "b_s": None, "score": 0.0, "confidence": 0.0, "candidates_s": []}
    candidates = [round(index * SAMPLE_S, 3) for index in range(int(period_s / SAMPLE_S))]
    scores = [(_score_phase(changes, candidate, period_s), candidate) for candidate in candidates]
    scores.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    best_score, best_phase = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    baseline = statistics.fmean(changes) if changes else 0.0
    confidence = max(0.0, min(1.0, (best_score - second_score) / max(best_score, 1e-9)))
    candidates_s = [phase for _score, phase in scores[:5]]
    if best_score <= max(1.0, baseline * 1.25):
        return {"status": "insufficient_signal", "b_s": None, "score": best_score, "confidence": confidence, "candidates_s": candidates_s}
    return {"status": "ok", "b_s": best_phase, "score": best_score, "confidence": confidence, "candidates_s": candidates_s}


def circular_mean(phases: list[float], period_s: float) -> tuple[float | None, float]:
    if not phases:
        return None, 0.0
    x = sum(math.cos(2 * math.pi * value / period_s) for value in phases) / len(phases)
    y = sum(math.sin(2 * math.pi * value / period_s) for value in phases) / len(phases)
    return (math.atan2(y, x) * period_s / (2 * math.pi)) % period_s, math.hypot(x, y)
