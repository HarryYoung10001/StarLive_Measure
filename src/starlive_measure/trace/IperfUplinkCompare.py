#!/usr/bin/env python3
"""Extract comparable uplink metrics from iperf3 client/server JSON."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return value


def receiver_rows(server: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    intervals = server.get("intervals")
    if not isinstance(intervals, list):
        return rows
    for index, interval in enumerate(intervals):
        if not isinstance(interval, dict):
            continue
        summary = interval.get("sum")
        if not isinstance(summary, dict):
            continue
        if summary.get("sender") is not False:
            continue
        rows.append(
            {
                "interval_index": index,
                "start_s": number(summary.get("start")),
                "end_s": number(summary.get("end")),
                "seconds": number(summary.get("seconds")),
                "bits_per_second": number(summary.get("bits_per_second")),
                "megabits_per_second": number(summary.get("bits_per_second")) / 1_000_000,
                "jitter_ms": number(summary.get("jitter_ms")),
                "lost_packets": integer(summary.get("lost_packets")),
                "packets": integer(summary.get("packets")),
                "lost_percent": number(summary.get("lost_percent")),
            }
        )
    return rows


def client_metrics(client: dict[str, Any]) -> dict[str, Any]:
    end = client.get("end")
    if not isinstance(end, dict):
        return {"client_elapsed_s": "", "client_sent_mbps": ""}
    summary = end.get("sum_sent")
    if not isinstance(summary, dict):
        summary = end.get("sum")
    if not isinstance(summary, dict):
        return {"client_elapsed_s": "", "client_sent_mbps": ""}
    elapsed = number(summary.get("seconds"))
    bps = number(summary.get("bits_per_second"))
    return {
        "client_elapsed_s": f"{elapsed:.9f}" if elapsed else "",
        "client_sent_mbps": f"{bps / 1_000_000:.9f}" if bps else "",
        "client_sent_packets": integer(summary.get("packets")),
    }


def write_metrics(path: Path, metrics: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for key, value in metrics.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-json", type=Path, required=True)
    parser.add_argument("--server-json", type=Path, required=True)
    parser.add_argument("--intervals-output", type=Path, required=True)
    parser.add_argument("--metrics-output", type=Path, required=True)
    args = parser.parse_args()

    try:
        client = load_json(args.client_json)
        server = load_json(args.server_json)
        rows = receiver_rows(server)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        write_metrics(
            args.metrics_output,
            {"status": "invalid_json", "reason": str(exc), "server_interval_rows": 0},
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    with args.intervals_output.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "interval_index", "start_s", "end_s", "seconds", "bits_per_second",
            "megabits_per_second", "jitter_ms", "lost_packets", "packets", "lost_percent",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)

    metrics = client_metrics(client)
    values = [float(row["megabits_per_second"]) for row in rows]
    total_packets = sum(int(row["packets"]) for row in rows)
    reported_lost = sum(int(row["lost_packets"]) for row in rows)
    sent_packets = integer(metrics.get("client_sent_packets"))
    inferred_lost = max(reported_lost, sent_packets - total_packets) if sent_packets else reported_lost
    metrics.update(
        {
            "status": "complete" if rows else "receiver_incomplete",
            "server_received_mbps_mean": f"{statistics.mean(values):.9f}" if values else "",
            "server_received_mbps_median": f"{statistics.median(values):.9f}" if values else "",
            "server_received_mbps_min": f"{min(values):.9f}" if values else "",
            "server_received_mbps_max": f"{max(values):.9f}" if values else "",
            "server_total_packets": total_packets,
            "server_reported_lost_packets": reported_lost,
            "server_lost_packets": inferred_lost,
            "server_loss_percent": f"{100.0 * inferred_lost / sent_packets:.9f}" if sent_packets else "",
            "server_interval_rows": len(rows),
            "server_last_interval_s": f"{max((float(row['end_s']) for row in rows), default=0.0):.9f}",
        }
    )
    write_metrics(args.metrics_output, metrics)
    return 0 if rows else 3


if __name__ == "__main__":
    raise SystemExit(main())
