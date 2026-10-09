#!/usr/bin/env python3
"""Shared time-bucket and rolling-window bandwidth calculations."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable


def build_bandwidth_trace(
    events: Iterable[tuple[int, int]],
    *,
    duration_ms: int | None = None,
    window_ms: int = 10,
) -> tuple[list[int], list[dict[str, int | float]]]:
    """Build arrival times and a millisecond rolling bandwidth trace."""

    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    if duration_ms is not None and duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    buckets: dict[int, dict[str, int]] = defaultdict(
        lambda: {"packet_count": 0, "payload_bytes": 0}
    )
    arrivals: list[int] = []
    previous = 0
    for relative_ms, payload_bytes in events:
        value = max(previous, int(relative_ms))
        payload = int(payload_bytes)
        if value < 0 or payload < 0:
            raise ValueError("arrival time and payload bytes must be non-negative")
        arrivals.append(value)
        previous = value
        buckets[value]["packet_count"] += 1
        buckets[value]["payload_bytes"] += payload
    if not arrivals:
        raise ValueError("bandwidth trace requires at least one event")

    last_ms = max(arrivals[-1], duration_ms) if duration_ms is not None else arrivals[-1]
    rows: list[dict[str, int | float]] = []
    rolling_bytes = 0
    rolling_packets = 0
    for time_ms in range(last_ms + 1):
        current = buckets.get(time_ms)
        if current:
            rolling_bytes += current["payload_bytes"]
            rolling_packets += current["packet_count"]
        expired = buckets.get(time_ms - window_ms)
        if expired:
            rolling_bytes -= expired["payload_bytes"]
            rolling_packets -= expired["packet_count"]
        rows.append(
            {
                "time_ms": time_ms,
                "packet_count": current["packet_count"] if current else 0,
                "payload_bytes": current["payload_bytes"] if current else 0,
                "window_packet_count": rolling_packets,
                "window_payload_bytes": rolling_bytes,
                "bandwidth_mbps": rolling_bytes * 8 / (window_ms * 1000.0),
            }
        )
    return arrivals, rows
