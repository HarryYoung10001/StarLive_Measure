#!/usr/bin/env python3
"""Extract RTT, receiver capacity, and custom UDP flow latency from dual traces."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

from .trace_discovery import dual_trace_run_dirs


PING_REPLY_RE = re.compile(
    r"^\[(?P<ts>\d+(?:\.\d+)?)\].*?"
    r"icmp_seq=(?P<seq>\d+)"
    r"(?:.*?\bttl=(?P<ttl>\d+))?.*?"
    r"\btime=(?P<rtt>\d+(?:\.\d+)?)\s*ms"
)
PING_SUMMARY_RE = re.compile(
    r"(?P<sent>\d+)\s+packets transmitted,\s+"
    r"(?P<received>\d+)\s+received(?:,\s+\+?(?P<duplicates>\d+)\s+duplicates)?,\s+"
    r"(?P<loss>[\d.]+)%\s+packet loss"
)
RATE_RE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[KMG]?)\s*$", re.I)

RTT_FIELDS = (
    "trace_id", "run_id", "target", "offered_rate", "offered_rate_mbps",
    "time_epoch_s", "time_rel_s", "icmp_seq", "ttl", "rtt_ms",
    "seq_gap", "lost_before",
)
CAPACITY_FIELDS = (
    "trace_id", "run_id", "direction", "offered_rate", "offered_rate_mbps",
    "source_json", "time_start_s", "time_end_s", "time_mid_s", "seconds",
    "capacity_bps", "capacity_mbps", "bytes", "jitter_ms", "lost_packets",
    "packets", "lost_percent", "omitted",
)
SUMMARY_FIELDS = (
    "run_id", "trace_dir", "direction", "target", "offered_rate",
    "offered_rate_mbps", "duration_s", "icmp_enabled", "rtt_sample_count", "ping_transmitted",
    "ping_received", "ping_loss_pct", "mean_rtt_ms", "median_rtt_ms",
    "p95_rtt_ms", "p99_rtt_ms", "min_rtt_ms", "max_rtt_ms", "capacity_interval_count",
    "mean_capacity_mbps", "weighted_capacity_mbps", "capacity_loss_pct",
    "rtt_source", "capacity_source", "status",
    "udp_flow_latency_sample_count", "udp_sent_count", "udp_received_count",
    "udp_loss_pct", "udp_flow_latency_mean_ms", "udp_flow_latency_median_ms",
    "udp_flow_latency_p75_ms", "udp_flow_latency_p95_ms", "udp_flow_latency_p99_ms",
    "udp_flow_latency_min_ms", "udp_flow_latency_max_ms", "udp_latency_source",
    "udp_clock_offset_ms", "udp_clock_rtt_ms", "udp_clock_sync_samples",
    "udp_arrival_first_rel_s", "udp_arrival_last_rel_s", "udp_arrival_span_s",
    "udp_arrival_interval_mean_ms", "udp_arrival_interval_median_ms",
    "udp_arrival_interval_p95_ms",
)
UDP_LATENCY_FIELDS = (
    "trace_id", "run_id", "direction", "target", "offered_rate",
    "offered_rate_mbps", "payload_bytes", "seq", "scheduled_send_rel_s",
    "send_rel_s", "send_unix_ns", "send_epoch_s", "recv_rel_s",
    "recv_unix_ns", "recv_epoch_s", "raw_one_way_delay_ms", "clock_offset_ms",
    "one_way_delay_ms", "received",
    "duplicate", "lost",
)
CUSTOM_CAPACITY_WINDOW_S = 0.010


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract RTT, receiver-capacity, and custom UDP latency CSV files from dual traces."
    )
    parser.add_argument(
        "--logs-dir", type=Path, default=Path("traces/logs"),
        help="Directory containing dual_icmp_heavy_udp_* traces.",
    )
    return parser.parse_args()


def read_metadata(run_dir: Path) -> dict[str, str]:
    metadata: dict[str, str] = {}
    path = run_dir / "metadata.txt"
    if not path.exists():
        return metadata
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def parse_rate_mbps(value: str | None) -> float | None:
    if not value:
        return None
    match = RATE_RE.match(value)
    if not match:
        return None
    number = float(match.group("number"))
    factor = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1000.0}[match.group("unit").upper()]
    return number * factor


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def format_number(value: float | int | None) -> str:
    return "" if value is None else f"{value:.6f}"


def percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, int((percentile / 100.0) * len(ordered) + 0.999999) - 1)
    return ordered[min(index, len(ordered) - 1)]


def find_ping_log(run_dir: Path) -> Path | None:
    preferred = run_dir / "icmp_phone" / "remote" / "ping.log"
    if preferred.exists():
        return preferred
    candidates = sorted(run_dir.rglob("ping.log"))
    return candidates[0] if candidates else None


def find_udp_sender_csv(run_dir: Path) -> Path | None:
    direction = read_metadata(run_dir).get("udp_direction", "downlink")
    basename = "uplink_udp_sender" if direction == "uplink" else "downlink_udp_sender"
    preferred_dir = run_dir / ("udp_phone" if direction == "uplink" else "server")
    for suffix in (".csv.gz", ".csv"):
        preferred = preferred_dir / f"{basename}{suffix}"
        if preferred.exists():
            return preferred
    for suffix in (".csv.gz", ".csv"):
        candidates = sorted(run_dir.rglob(f"{basename}{suffix}"))
        if candidates:
            return candidates[0]
    return None


def find_udp_receiver_csv(run_dir: Path) -> Path | None:
    direction = read_metadata(run_dir).get("udp_direction", "downlink")
    basename = "uplink_udp_receiver" if direction == "uplink" else "downlink_udp_receiver"
    preferred_dir = run_dir / ("server" if direction == "uplink" else "udp_phone")
    for suffix in (".csv", ".csv.gz"):
        preferred = preferred_dir / f"{basename}{suffix}"
        if preferred.exists():
            return preferred
    for suffix in (".csv", ".csv.gz"):
        candidates = sorted(run_dir.rglob(f"{basename}{suffix}"))
        if candidates:
            return candidates[0]
    return None


def open_csv_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def read_udp_sender_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with open_csv_text(path) as handle:
        for row in csv.DictReader(handle):
            try:
                int(row.get("seq", ""))
            except (TypeError, ValueError):
                continue
            if as_float(row.get("send_rel_s")) is None:
                continue
            rows.append(row)
    rows.sort(key=lambda row: int(row["seq"]))
    return rows


def read_udp_receiver_rows(path: Path) -> dict[int, dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    with open_csv_text(path) as handle:
        for row in csv.DictReader(handle):
            try:
                seq = int(row.get("seq", ""))
            except (TypeError, ValueError):
                continue
            previous = rows.get(seq)
            if previous is None:
                rows[seq] = row
                continue
            previous["duplicate"] = "true"
            if as_int(previous.get("recv_unix_ns")) is None and as_int(row.get("recv_unix_ns")) is not None:
                rows[seq] = row
                rows[seq]["duplicate"] = "true"
    return rows


def read_key_value_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def find_udp_receiver_summary(run_dir: Path) -> Path | None:
    direction = read_metadata(run_dir).get("udp_direction", "downlink")
    filename = "uplink_udp_receiver_summary.txt" if direction == "uplink" else "downlink_udp_receiver_summary.txt"
    preferred = run_dir / ("server" if direction == "uplink" else "udp_phone") / filename
    if preferred.exists():
        return preferred
    candidates = sorted(run_dir.rglob(filename))
    return candidates[0] if candidates else None


def build_udp_capacity_rows(
    receiver_rows: dict[int, dict[str, str]],
    run_id: str,
    direction: str,
    target: str,
    offered_rate: str,
    offered_rate_mbps: float | None,
    source: str,
    loss_pct: float | None,
    window_s: float = CUSTOM_CAPACITY_WINDOW_S,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate receiver arrivals into trailing 10ms capacity windows."""

    packets: list[tuple[float, float]] = []
    for row in receiver_rows.values():
        recv_s = as_float(row.get("recv_rel_s"))
        payload = as_float(row.get("payload_bytes"))
        if recv_s is None or payload is None or payload <= 0:
            continue
        packets.append((recv_s, payload))
    packets.sort(key=lambda item: item[0])
    if not packets or window_s <= 0:
        return [], {
            "interval_count": 0,
            "mean_capacity_mbps": None,
            "weighted_capacity_mbps": None,
            "capacity_loss_pct": loss_pct,
            "source": source,
        }

    rows: list[dict[str, Any]] = []
    capacities: list[float] = []
    total_bytes = 0.0
    total_seconds = 0.0
    left = 0
    right = 0
    first_index = max(1, int(math.floor(packets[0][0] / window_s)) + 1)
    last_index = max(first_index, int(math.floor(packets[-1][0] / window_s)) + 1)
    for window_index in range(first_index, last_index + 1):
        end_s = window_index * window_s
        start_s = end_s - window_s
        while right < len(packets) and packets[right][0] < end_s:
            right += 1
        while left < right and packets[left][0] < start_s:
            left += 1
        if left >= right:
            continue
        window_bytes = sum(payload for _, payload in packets[left:right])
        window_packets = right - left
        capacity_bps = window_bytes * 8.0 / window_s
        capacity_mbps = capacity_bps / 1_000_000.0
        capacities.append(capacity_mbps)
        total_bytes += window_bytes
        total_seconds += window_s
        rows.append({
            "trace_id": run_id,
            "run_id": run_id,
            "direction": direction,
            "offered_rate": offered_rate,
            "offered_rate_mbps": csv_value(offered_rate_mbps),
            "source_json": source,
            "time_start_s": start_s,
            "time_end_s": end_s,
            "time_mid_s": (start_s + end_s) / 2.0,
            "seconds": window_s,
            "capacity_bps": capacity_bps,
            "capacity_mbps": capacity_mbps,
            "bytes": window_bytes,
            "jitter_ms": "",
            "lost_packets": "",
            "packets": window_packets,
            "lost_percent": "",
            "omitted": False,
        })
    return rows, {
        "interval_count": len(rows),
        "mean_capacity_mbps": statistics.mean(capacities) if capacities else None,
        "weighted_capacity_mbps": (
            total_bytes * 8.0 / total_seconds / 1_000_000.0
            if total_seconds > 0 else None
        ),
        "capacity_loss_pct": loss_pct,
        "source": source,
    }


def parse_udp_latency(
    run_dir: Path,
    metadata: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sender_path = find_udp_sender_csv(run_dir)
    receiver_path = find_udp_receiver_csv(run_dir)
    empty_stats = {
        "sample_count": 0,
        "sent_count": 0,
        "received_count": 0,
        "loss_pct": None,
        "mean_ms": None,
        "median_ms": None,
        "p95_ms": None,
        "p75_ms": None,
        "p99_ms": None,
        "min_ms": None,
        "max_ms": None,
        "arrival_first_s": None,
        "arrival_last_s": None,
        "arrival_span_s": None,
        "arrival_interval_mean_ms": None,
        "arrival_interval_median_ms": None,
        "arrival_interval_p95_ms": None,
        "clock_offset_ms": 0.0,
        "clock_rtt_ms": None,
        "clock_sync_samples": 0,
        "capacity_rows": [],
        "capacity_interval_count": 0,
        "mean_capacity_mbps": None,
        "weighted_capacity_mbps": None,
        "capacity_loss_pct": None,
        "capacity_source": "",
        "source": "",
    }
    if sender_path is None or receiver_path is None:
        missing = []
        if sender_path is None:
            missing.append("custom UDP sender CSV")
        if receiver_path is None:
            missing.append("custom UDP receiver CSV")
        print(f"WARNING: missing UDP latency input in {run_dir}: {', '.join(missing)}")
        return [], empty_stats

    sender_rows = read_udp_sender_rows(sender_path)
    receiver_rows = read_udp_receiver_rows(receiver_path)
    run_id = metadata.get("run_id", run_dir.name)
    direction = metadata.get("udp_direction", "downlink")
    target = metadata.get("server", "")
    offered_rate = metadata.get("udp_rate") or metadata.get("rate", "")
    offered_rate_mbps = parse_rate_mbps(offered_rate)
    receiver_summary_path = find_udp_receiver_summary(run_dir)
    receiver_summary = read_key_value_file(receiver_summary_path) if receiver_summary_path else {}
    sender_summary_name = sender_path.name.replace(".csv.gz", "_summary.txt").replace(".csv", "_summary.txt")
    sender_summary = read_key_value_file(sender_path.with_name(sender_summary_name))
    clock_values = sender_summary if direction == "uplink" else receiver_summary
    clock_offset_ms = as_float(clock_values.get("clock_offset_ms")) or 0.0
    clock_rtt_ms = as_float(clock_values.get("clock_rtt_ms"))
    clock_sync_samples = as_int(clock_values.get("clock_sync_samples")) or 0
    merged: list[dict[str, Any]] = []
    delays: list[float] = []
    arrival_times: list[float] = []
    received_count = 0
    for sender in sender_rows:
        seq = int(sender["seq"])
        receiver = receiver_rows.get(seq)
        received = receiver is not None
        if received:
            received_count += 1
            recv_rel_s = as_float(receiver.get("recv_rel_s"))
            if recv_rel_s is not None:
                arrival_times.append(recv_rel_s)
        else:
            recv_rel_s = None
        send_unix_ns = as_int(sender.get("send_unix_ns"))
        recv_unix_ns = as_int(receiver.get("recv_unix_ns")) if receiver else None
        raw_delay_ms = None
        delay_ms = None
        if send_unix_ns is not None and recv_unix_ns is not None:
            raw_delay_ms = (recv_unix_ns - send_unix_ns) / 1_000_000.0
            delay_ms = raw_delay_ms + clock_offset_ms
            delays.append(delay_ms)
        merged.append({
            "trace_id": run_id,
            "run_id": run_id,
            "direction": direction,
            "target": target,
            "offered_rate": offered_rate,
            "offered_rate_mbps": csv_value(offered_rate_mbps),
            "payload_bytes": sender.get("payload_bytes", metadata.get("udp_payload_len", "")),
            "seq": seq,
            "scheduled_send_rel_s": sender.get("scheduled_send_rel_s", ""),
            "send_rel_s": sender.get("send_rel_s", ""),
            "send_unix_ns": sender.get("send_unix_ns", ""),
            "send_epoch_s": sender.get("send_epoch_s", ""),
            "recv_rel_s": receiver.get("recv_rel_s", "") if receiver else "",
            "recv_unix_ns": receiver.get("recv_unix_ns", "") if receiver else "",
            "recv_epoch_s": receiver.get("recv_epoch_s", "") if receiver else "",
            "raw_one_way_delay_ms": format_number(raw_delay_ms),
            "clock_offset_ms": format_number(clock_offset_ms),
            "one_way_delay_ms": format_number(delay_ms),
            "received": str(received).lower(),
            "duplicate": (receiver.get("duplicate", "false") if receiver else "false"),
            "lost": str(not received).lower(),
        })

    sent_count = len(sender_rows)
    arrival_times.sort()
    arrival_intervals = [
        (right - left) * 1000.0
        for left, right in zip(arrival_times, arrival_times[1:])
    ]
    stats = {
        "sample_count": len(delays),
        "sent_count": sent_count,
        "received_count": received_count,
        "loss_pct": 100.0 * (sent_count - received_count) / sent_count if sent_count else None,
        "mean_ms": statistics.mean(delays) if delays else None,
        "median_ms": statistics.median(delays) if delays else None,
        "p95_ms": percentile_nearest_rank(delays, 95.0),
        "p75_ms": percentile_nearest_rank(delays, 75.0),
        "p99_ms": percentile_nearest_rank(delays, 99.0),
        "min_ms": min(delays) if delays else None,
        "max_ms": max(delays) if delays else None,
        "arrival_first_s": arrival_times[0] if arrival_times else None,
        "arrival_last_s": arrival_times[-1] if arrival_times else None,
        "arrival_span_s": (arrival_times[-1] - arrival_times[0]) if len(arrival_times) > 1 else 0.0 if arrival_times else None,
        "arrival_interval_mean_ms": statistics.mean(arrival_intervals) if arrival_intervals else None,
        "arrival_interval_median_ms": statistics.median(arrival_intervals) if arrival_intervals else None,
        "arrival_interval_p95_ms": percentile_nearest_rank(arrival_intervals, 95.0),
        "clock_offset_ms": clock_offset_ms,
        "clock_rtt_ms": clock_rtt_ms,
        "clock_sync_samples": clock_sync_samples,
        "source": f"{sender_path.relative_to(run_dir)} + {receiver_path.relative_to(run_dir)}",
    }
    capacity_source = str(receiver_path.relative_to(run_dir))
    capacity_rows, capacity_stats = build_udp_capacity_rows(
        receiver_rows,
        run_id,
        direction,
        target,
        offered_rate,
        offered_rate_mbps,
        capacity_source,
        stats["loss_pct"],
    )
    stats["capacity_rows"] = capacity_rows
    stats["capacity_interval_count"] = capacity_stats["interval_count"]
    stats["mean_capacity_mbps"] = capacity_stats["mean_capacity_mbps"]
    stats["weighted_capacity_mbps"] = capacity_stats["weighted_capacity_mbps"]
    stats["capacity_loss_pct"] = capacity_stats["capacity_loss_pct"]
    stats["capacity_source"] = capacity_stats["source"]
    return merged, stats


def parse_ping_log(
    ping_path: Path, run_dir: Path, metadata: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = metadata.get("run_id", run_dir.name)
    target = metadata.get("server") or metadata.get("target", "")
    offered_rate = metadata.get("udp_rate") or metadata.get("rate", "")
    offered_rate_mbps = parse_rate_mbps(offered_rate)
    rows: list[dict[str, Any]] = []
    first_time: float | None = None
    previous_seq: int | None = None
    transmitted: int | None = None
    received: int | None = None
    transmitted_total = received_total = 0
    summary_elapsed_s: list[float] = []
    worker_index = -1
    seen_replies: set[tuple[int, int]] = set()

    for line in ping_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("PING "):
            worker_index += 1
        summary_match = PING_SUMMARY_RE.search(line)
        if summary_match:
            transmitted_total += int(summary_match.group("sent"))
            received_total += int(summary_match.group("received")) - int(summary_match.group("duplicates") or 0)
            elapsed_match = re.search(r"\btime\s+(\d+(?:\.\d+)?)\s*ms", line)
            if elapsed_match:
                summary_elapsed_s.append(float(elapsed_match.group(1)) / 1000.0)
            continue

        match = PING_REPLY_RE.search(line)
        if not match:
            continue
        timestamp = float(match.group("ts"))
        sequence = int(match.group("seq"))
        reply_key = (worker_index, sequence)
        if reply_key in seen_replies:
            continue
        seen_replies.add(reply_key)
        if first_time is None:
            first_time = timestamp
        seq_gap: int | str = ""
        lost_before = 0
        if previous_seq is not None:
            seq_gap = sequence - previous_seq
            if seq_gap > 1:
                lost_before = seq_gap - 1
        rows.append({
            "trace_id": run_id,
            "run_id": run_id,
            "target": target,
            "offered_rate": offered_rate,
            "offered_rate_mbps": csv_value(offered_rate_mbps),
            "time_epoch_s": timestamp,
            "time_rel_s": timestamp - first_time,
            "icmp_seq": sequence,
            "ttl": match.group("ttl") or "",
            "rtt_ms": float(match.group("rtt")),
            "seq_gap": seq_gap,
            "lost_before": lost_before,
        })
        previous_seq = sequence

    if transmitted_total or received_total:
        transmitted, received = transmitted_total, len(rows)
    rows.sort(key=lambda row: float(row["time_epoch_s"]))

    if transmitted is None:
        raw_count = metadata.get("ping_count", "")
        transmitted = int(raw_count) if raw_count.isdigit() else len(rows)
    if received is None:
        received = len(rows)

    rtts = [float(row["rtt_ms"]) for row in rows]
    loss_pct = 100.0 * max(0, transmitted - received) / transmitted if transmitted else None
    stats = {
        "sample_count": len(rows),
        "transmitted": transmitted,
        "received": received,
        "loss_pct": loss_pct,
        "mean_rtt_ms": statistics.mean(rtts) if rtts else None,
        "median_rtt_ms": statistics.median(rtts) if rtts else None,
        "p95_rtt_ms": percentile_nearest_rank(rtts, 95.0),
        "p99_rtt_ms": percentile_nearest_rank(rtts, 99.0),
        "min_rtt_ms": min(rtts) if rtts else None,
        "max_rtt_ms": max(rtts) if rtts else None,
        "source": str(ping_path.relative_to(run_dir)),
        "elapsed_s": max(summary_elapsed_s) if summary_elapsed_s else (
            max((float(row["time_epoch_s"]) for row in rows), default=0.0)
            - min((float(row["time_epoch_s"]) for row in rows), default=0.0)
        ),
    }
    return rows, stats


def capacity_candidates(run_dir: Path, direction: str) -> list[Path]:
    if direction == "downlink":
        preferred = [
            run_dir / "heavy_udp_phone" / "remote" / "iperf3_client.json",
            run_dir / "heavy_udp_phone" / "iperf3_client.json",
            run_dir / "server" / "iperf3_server.json",
        ]
    else:
        preferred = [
            run_dir / "server" / "iperf3_server.json",
            run_dir / "server" / "remote" / "iperf3_server.json",
            run_dir / "heavy_udp_phone" / "remote" / "iperf3_client.json",
        ]
    discovered = sorted(
        path for path in run_dir.rglob("*.json")
        if path.name in {"iperf3_client.json", "iperf3_server.json"}
    )
    return list(dict.fromkeys([path for path in preferred if path.exists()] + discovered))


def read_capacity_rows(
    run_dir: Path, metadata: dict[str, str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    run_id = metadata.get("run_id", run_dir.name)
    direction = metadata.get("udp_direction") or metadata.get("direction", "downlink")
    offered_rate = metadata.get("udp_rate") or metadata.get("rate", "")
    offered_rate_mbps = parse_rate_mbps(offered_rate)
    expected_sender = False

    for json_path in capacity_candidates(run_dir, direction):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"WARNING: failed to read capacity JSON {json_path}: {exc}")
            continue
        except json.JSONDecodeError as exc:
            print(f"WARNING: invalid capacity JSON {json_path}: {exc}")
            continue
        intervals = data.get("intervals")
        if not isinstance(intervals, list):
            continue

        rows: list[dict[str, Any]] = []
        for interval in intervals:
            if not isinstance(interval, dict):
                continue
            summary = interval.get("sum")
            if not isinstance(summary, dict) or summary.get("sender") is not expected_sender:
                continue
            start = as_float(summary.get("start"))
            end = as_float(summary.get("end"))
            seconds = as_float(summary.get("seconds"))
            bps = as_float(summary.get("bits_per_second"))
            if start is None or end is None or bps is None:
                continue
            rows.append({
                "trace_id": run_id,
                "run_id": run_id,
                "direction": direction,
                "offered_rate": offered_rate,
                "offered_rate_mbps": csv_value(offered_rate_mbps),
                "source_json": str(json_path.relative_to(run_dir)),
                "time_start_s": start,
                "time_end_s": end,
                "time_mid_s": (start + end) / 2.0,
                "seconds": csv_value(seconds),
                "capacity_bps": bps,
                "capacity_mbps": bps / 1_000_000.0,
                "bytes": csv_value(summary.get("bytes")),
                "jitter_ms": csv_value(summary.get("jitter_ms")),
                "lost_packets": csv_value(summary.get("lost_packets")),
                "packets": csv_value(summary.get("packets")),
                "lost_percent": csv_value(summary.get("lost_percent")),
                "omitted": csv_value(summary.get("omitted", False)),
            })
        if rows:
            active = [row for row in rows if row["omitted"] != "true"]
            capacities = [float(row["capacity_mbps"]) for row in active]
            total_bytes = sum(as_float(row["bytes"]) or 0.0 for row in active)
            total_seconds = sum(as_float(row["seconds"]) or 0.0 for row in active)
            total_lost = sum(as_float(row["lost_packets"]) or 0.0 for row in active)
            total_packets = sum(as_float(row["packets"]) or 0.0 for row in active)
            stats = {
                "interval_count": len(rows),
                "mean_capacity_mbps": statistics.mean(capacities) if capacities else None,
                "weighted_capacity_mbps": (
                    total_bytes * 8.0 / total_seconds / 1_000_000.0
                    if total_seconds > 0 else None
                ),
                "capacity_loss_pct": (
                    100.0 * total_lost / total_packets if total_packets > 0 else None
                ),
                "source": str(json_path.relative_to(run_dir)),
            }
            return rows, stats

    print(f"WARNING: no receiver interval data found in {run_dir}")
    return [], {
        "interval_count": 0, "mean_capacity_mbps": None,
        "weighted_capacity_mbps": None, "capacity_loss_pct": None, "source": "",
    }


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def process_session(run_dir: Path) -> tuple[dict[str, Any], bool]:
    metadata = read_metadata(run_dir)
    run_id = metadata.get("run_id", run_dir.name)
    is_udp_latency = (
        metadata.get("type") in {"dual_udp_latency_downlink", "dual_udp_latency_uplink"}
        or run_dir.name.startswith(("dual_udp_latency_downlink_", "dual_udp_latency_uplink_"))
    )
    direction = metadata.get("udp_direction") or metadata.get("direction", "downlink")
    target = metadata.get("server") or metadata.get("target", "")
    offered_rate = metadata.get("udp_rate") or metadata.get("rate", "")
    offered_rate_mbps = parse_rate_mbps(offered_rate)
    icmp_enabled = metadata.get("icmp_enabled", "true").strip().lower() not in {
        "false", "0", "no", "disabled"
    }

    ping_path = find_ping_log(run_dir)
    if ping_path is None:
        ping_rows, ping_stats = [], {
            "sample_count": 0, "transmitted": 0, "received": 0, "loss_pct": None,
            "mean_rtt_ms": None, "median_rtt_ms": None, "p95_rtt_ms": None,
            "p99_rtt_ms": None,
            "min_rtt_ms": None, "max_rtt_ms": None, "source": "",
        }
        if icmp_enabled:
            print(f"WARNING: no ping.log found in {run_dir}")
    else:
        ping_rows, ping_stats = parse_ping_log(ping_path, run_dir, metadata)
        if icmp_enabled and not ping_rows:
            print(f"WARNING: no RTT replies found in {ping_path}")

    if is_udp_latency:
        udp_latency_rows, udp_latency_stats = parse_udp_latency(run_dir, metadata)
        capacity_rows = udp_latency_stats["capacity_rows"]
        capacity_stats = {
            "interval_count": udp_latency_stats["capacity_interval_count"],
            "mean_capacity_mbps": udp_latency_stats["mean_capacity_mbps"],
            "weighted_capacity_mbps": udp_latency_stats["weighted_capacity_mbps"],
            "capacity_loss_pct": udp_latency_stats["capacity_loss_pct"],
            "source": udp_latency_stats["capacity_source"],
        }
    else:
        capacity_rows, capacity_stats = read_capacity_rows(run_dir, metadata)
        udp_latency_rows, udp_latency_stats = [], {
            "sample_count": 0,
            "sent_count": 0,
            "received_count": 0,
            "loss_pct": None,
            "mean_ms": None,
            "median_ms": None,
            "p75_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "min_ms": None,
            "max_ms": None,
            "arrival_first_s": None,
            "arrival_last_s": None,
            "arrival_span_s": None,
            "arrival_interval_mean_ms": None,
            "arrival_interval_median_ms": None,
            "arrival_interval_p95_ms": None,
            "clock_offset_ms": 0.0,
            "clock_rtt_ms": None,
            "clock_sync_samples": 0,
            "source": "",
        }
    rtt_path = run_dir / "rtt_timeseries.csv"
    capacity_path = run_dir / "capacity_timeseries.csv"
    udp_latency_path = run_dir / "udp_flow_latency.csv"
    write_csv(rtt_path, RTT_FIELDS, ping_rows)
    write_csv(capacity_path, CAPACITY_FIELDS, capacity_rows)
    if is_udp_latency:
        write_csv(udp_latency_path, UDP_LATENCY_FIELDS, udp_latency_rows)

    if is_udp_latency:
        status = "complete" if (
            udp_latency_rows and udp_latency_stats["sample_count"] and capacity_rows
            and (not icmp_enabled or ping_rows)
        ) else "partial"
    else:
        status = "complete" if ping_rows and capacity_rows else "partial"
    if not ping_rows and not capacity_rows:
        if not is_udp_latency or not udp_latency_rows:
            status = "failed"
    duration = as_float(metadata.get("duration_s"))
    if duration is None and capacity_rows:
        duration = max(float(row["time_end_s"]) for row in capacity_rows)

    summary = {
        "run_id": run_id,
        "trace_dir": run_dir.name,
        "direction": direction,
        "target": target,
        "offered_rate": offered_rate,
        "offered_rate_mbps": csv_value(offered_rate_mbps),
        "duration_s": csv_value(duration),
        "icmp_enabled": "true" if icmp_enabled else "false",
        "rtt_sample_count": ping_stats["sample_count"],
        "ping_transmitted": ping_stats["transmitted"],
        "ping_received": ping_stats["received"],
        "ping_loss_pct": format_number(ping_stats["loss_pct"]),
        "mean_rtt_ms": format_number(ping_stats["mean_rtt_ms"]),
        "median_rtt_ms": format_number(ping_stats["median_rtt_ms"]),
        "p95_rtt_ms": format_number(ping_stats["p95_rtt_ms"]),
        "p99_rtt_ms": format_number(ping_stats["p99_rtt_ms"]),
        "min_rtt_ms": format_number(ping_stats["min_rtt_ms"]),
        "max_rtt_ms": format_number(ping_stats["max_rtt_ms"]),
        "capacity_interval_count": capacity_stats["interval_count"],
        "mean_capacity_mbps": format_number(capacity_stats["mean_capacity_mbps"]),
        "weighted_capacity_mbps": format_number(capacity_stats["weighted_capacity_mbps"]),
        "capacity_loss_pct": format_number(capacity_stats["capacity_loss_pct"]),
        "rtt_source": ping_stats["source"],
        "capacity_source": capacity_stats["source"],
        "status": status,
        "udp_flow_latency_sample_count": udp_latency_stats["sample_count"],
        "udp_sent_count": udp_latency_stats["sent_count"],
        "udp_received_count": udp_latency_stats["received_count"],
        "udp_loss_pct": format_number(udp_latency_stats["loss_pct"]),
        "udp_flow_latency_mean_ms": format_number(udp_latency_stats["mean_ms"]),
        "udp_flow_latency_median_ms": format_number(udp_latency_stats["median_ms"]),
        "udp_flow_latency_p75_ms": format_number(udp_latency_stats["p75_ms"]),
        "udp_flow_latency_p95_ms": format_number(udp_latency_stats["p95_ms"]),
        "udp_flow_latency_p99_ms": format_number(udp_latency_stats["p99_ms"]),
        "udp_flow_latency_min_ms": format_number(udp_latency_stats["min_ms"]),
        "udp_flow_latency_max_ms": format_number(udp_latency_stats["max_ms"]),
        "udp_latency_source": udp_latency_stats["source"],
        "udp_clock_offset_ms": format_number(udp_latency_stats["clock_offset_ms"]),
        "udp_clock_rtt_ms": format_number(udp_latency_stats["clock_rtt_ms"]),
        "udp_clock_sync_samples": udp_latency_stats["clock_sync_samples"],
        "udp_arrival_first_rel_s": format_number(udp_latency_stats["arrival_first_s"]),
        "udp_arrival_last_rel_s": format_number(udp_latency_stats["arrival_last_s"]),
        "udp_arrival_span_s": format_number(udp_latency_stats["arrival_span_s"]),
        "udp_arrival_interval_mean_ms": format_number(udp_latency_stats["arrival_interval_mean_ms"]),
        "udp_arrival_interval_median_ms": format_number(udp_latency_stats["arrival_interval_median_ms"]),
        "udp_arrival_interval_p95_ms": format_number(udp_latency_stats["arrival_interval_p95_ms"]),
    }
    print(
        f"{run_id}: RTT rows={len(ping_rows)}, capacity rows={len(capacity_rows)}, "
        f"UDP latency rows={len(udp_latency_rows)}, status={status}"
    )
    return summary, status == "complete"


def main() -> int:
    args = parse_args()
    logs_dir = args.logs_dir
    if not logs_dir.exists() or not logs_dir.is_dir():
        print(f"ERROR: logs directory does not exist: {logs_dir}")
        return 2

    run_dirs = dual_trace_run_dirs(logs_dir)
    if not run_dirs:
        print(f"WARNING: no dual traces found under {logs_dir}")
        return 1

    summaries: list[dict[str, Any]] = []
    all_complete = True
    for run_dir in run_dirs:
        summary, complete = process_session(run_dir)
        summaries.append(summary)
        all_complete = all_complete and complete

    write_csv(logs_dir / "trace_summary.csv", SUMMARY_FIELDS, summaries)
    print(f"Wrote {logs_dir / 'trace_summary.csv'} ({len(summaries)} traces)")
    return 0 if all_complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
