"""Shared phase-alignment helpers for measurement post-processing."""

from __future__ import annotations

from typing import Iterable


def aligned_ticks(end_s: float, *, period_s: float = 15.0, anchor_s: float = 12.0) -> list[float]:
    """Return phase-event ticks plus the requested aligned endpoint."""
    end_s = float(end_s)
    if end_s < 0:
        raise ValueError("aligned end must be non-negative")
    ticks = [0.0]
    tick = float(anchor_s)
    while tick < end_s - 1e-9:
        ticks.append(round(tick, 9))
        tick += period_s
    if abs(ticks[-1] - end_s) > 1e-9:
        ticks.append(end_s)
    return ticks


def aligned(rows: Iterable[tuple[float, float]], b: float, end_s: float = 102.0) -> list[tuple[float, float]]:
    """Shift samples so the first complete event (``b + 15``) is at 12 s."""
    shift = 12.0 - (float(b) + 15.0)
    end_s = float(end_s)
    if end_s < 0:
        raise ValueError("aligned end must be non-negative")
    return [(x + shift, y) for x, y in rows if 0 <= x + shift <= end_s]
