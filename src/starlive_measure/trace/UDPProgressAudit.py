#!/usr/bin/env python3
"""Build and classify compact UDP receive-gap reports."""

from __future__ import annotations

import argparse
import csv
import gzip
from pathlib import Path


GAP_FIELDS = [
    "gap_index",
    "start_recv_unix_ns",
    "end_recv_unix_ns",
    "start_recv_epoch_s",
    "end_recv_epoch_s",
    "gap_ms",
    "prev_seq",
    "next_seq",
    "seq_jump",
    "prev_send_rel_s",
    "next_send_rel_s",
    "send_advance_s",
    "socket_drop_delta",
]

CLASSIFIED_FIELDS = GAP_FIELDS + [
    "sender_telemetry",
    "receiver_telemetry",
    "sender_attempted_delta",
    "sender_sent_delta",
    "sender_failed_delta",
    "sender_last_seq_delta",
    "receiver_packet_delta",
    "classification",
]


def _open_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8", newline="") if path.suffix == ".gz" else path.open(
        "r", encoding="utf-8", newline=""
    )


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def read_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def build_gap_report(receiver_csv: Path, output: Path, gap_ms: float = 100.0) -> int:
    if gap_ms <= 0:
        raise ValueError("gap_ms must be positive")
    rows: list[dict[str, object]] = []
    with _open_text(receiver_csv) as handle:
        for row in csv.DictReader(handle):
            recv_ns = _int(row.get("recv_unix_ns"))
            seq = _int(row.get("seq"))
            send_rel = _float(row.get("send_rel_s"))
            if recv_ns is None or seq is None or send_rel is None:
                continue
            rows.append({"recv_ns": recv_ns, "seq": seq, "send_rel": send_rel})

    output.parent.mkdir(parents=True, exist_ok=True)
    gap_rows: list[dict[str, object]] = []
    for previous, current in zip(rows, rows[1:]):
        previous_recv = int(previous["recv_ns"])
        current_recv = int(current["recv_ns"])
        duration_ms = (current_recv - previous_recv) / 1e6
        if duration_ms < gap_ms:
            continue
        previous_send = float(previous["send_rel"])
        current_send = float(current["send_rel"])
        gap_rows.append(
            {
                "gap_index": len(gap_rows) + 1,
                "start_recv_unix_ns": previous_recv,
                "end_recv_unix_ns": current_recv,
                "start_recv_epoch_s": f"{previous_recv / 1e9:.9f}",
                "end_recv_epoch_s": f"{current_recv / 1e9:.9f}",
                "gap_ms": f"{duration_ms:.3f}",
                "prev_seq": previous["seq"],
                "next_seq": current["seq"],
                "seq_jump": int(current["seq"]) - int(previous["seq"]),
                "prev_send_rel_s": f"{previous_send:.9f}",
                "next_send_rel_s": f"{current_send:.9f}",
                "send_advance_s": f"{current_send - previous_send:.9f}",
            }
        )

    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=GAP_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(gap_rows)
    return len(gap_rows)


def _read_progress(path: Path, timestamp_key: str) -> list[dict[str, float | int | str]]:
    rows: list[dict[str, float | int | str]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle, delimiter="\t"):
            timestamp = _int(raw.get(timestamp_key))
            if timestamp is None:
                continue
            parsed: dict[str, float | int | str] = dict(raw)
            parsed[timestamp_key] = timestamp
            for key in (
                "elapsed_s",
                "attempted_packets",
                "sent_packets",
                "send_failed_packets",
                "last_seq",
                "last_send_rel_s",
                "send_lag_ms",
                "received_packets",
                "last_packet_recv_unix_ns",
                "socket_timeout_count",
            ):
                if key in raw:
                    number = _float(raw.get(key))
                    if number is not None:
                        parsed[key] = number
            rows.append(parsed)
    return rows


def _numeric(row: dict[str, float | int | str], key: str) -> float:
    value = row.get(key, 0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def classify_gap_report(
    gap_report: Path,
    sender_progress: Path,
    receiver_progress: Path,
    sender_summary: Path,
    output: Path,
    gap_ms: float = 100.0,
    direction: str = "uplink",
) -> int:
    if gap_ms <= 0:
        raise ValueError("gap_ms must be positive")
    sender_values = read_key_values(sender_summary)
    if direction not in {"uplink", "downlink"}:
        raise ValueError("direction must be uplink or downlink")
    server_minus_phone_ms = _float(sender_values.get("clock_server_minus_phone_ms"))
    clock_offset_ms = _float(sender_values.get("clock_offset_ms"))
    if direction == "uplink":
        offset_ms = server_minus_phone_ms
        if offset_ms is None and clock_offset_ms is not None:
            # Uplink sender summaries expose phone-minus-server as clock_offset_ms.
            offset_ms = -clock_offset_ms
    else:
        # Downlink sender progress is in the server clock domain while receive
        # gaps are in the phone domain. Receiver summaries expose server-minus-phone.
        source_offset = server_minus_phone_ms if server_minus_phone_ms is not None else clock_offset_ms
        offset_ms = -source_offset if source_offset is not None else None
    sender_rows = _read_progress(sender_progress, "timestamp_unix_ns") if offset_ms is not None else []
    if offset_ms is not None:
        for row in sender_rows:
            row["timestamp_unix_ns"] = int(row["timestamp_unix_ns"]) + int(offset_ms * 1e6)
    receiver_rows = _read_progress(receiver_progress, "timestamp_unix_ns")
    with gap_report.open("r", encoding="utf-8", newline="") as handle:
        gaps = list(csv.DictReader(handle, delimiter="\t"))

    final_sender_rows = [row for row in sender_rows if row.get("event") == "final"]
    last_recv_ns = max(
        (int(_numeric(row, "last_packet_recv_unix_ns")) for row in receiver_rows),
        default=0,
    )
    if final_sender_rows and last_recv_ns > 0:
        sender_end_ns = int(final_sender_rows[-1]["timestamp_unix_ns"])
        trailing_ms = (sender_end_ns - last_recv_ns) / 1e6
        if trailing_ms >= gap_ms:
            final_receiver = receiver_rows[-1] if receiver_rows else {}
            gaps.append(
                {
                    "gap_index": "trailing",
                    "start_recv_unix_ns": str(last_recv_ns),
                    "end_recv_unix_ns": str(sender_end_ns),
                    "start_recv_epoch_s": f"{last_recv_ns / 1e9:.9f}",
                    "end_recv_epoch_s": f"{sender_end_ns / 1e9:.9f}",
                    "gap_ms": f"{trailing_ms:.3f}",
                    "prev_seq": f"{_numeric(final_receiver, 'last_seq'):.0f}",
                    "next_seq": "",
                    "seq_jump": "",
                    "prev_send_rel_s": "",
                    "next_send_rel_s": "",
                    "send_advance_s": "",
                }
            )

    classified: list[dict[str, object]] = []
    for gap in gaps:
        start_ns = int(gap["start_recv_unix_ns"])
        end_ns = int(gap["end_recv_unix_ns"])
        sender_before = [row for row in sender_rows if int(row["timestamp_unix_ns"]) <= start_ns]
        sender_after = [row for row in sender_rows if int(row["timestamp_unix_ns"]) >= end_ns]
        receiver_before = [row for row in receiver_rows if int(row["timestamp_unix_ns"]) <= start_ns]
        receiver_after = [row for row in receiver_rows if int(row["timestamp_unix_ns"]) >= end_ns]

        sender_telemetry = "available" if sender_before and sender_after else "unavailable"
        receiver_telemetry = "alive" if receiver_before and receiver_after else "unavailable"
        attempted_delta = sent_delta = failed_delta = seq_delta = 0.0
        sender_active = False
        sender_failed = False
        if sender_before and sender_after:
            before = sender_before[-1]
            after = sender_after[0]
            attempted_delta = _numeric(after, "attempted_packets") - _numeric(before, "attempted_packets")
            sent_delta = _numeric(after, "sent_packets") - _numeric(before, "sent_packets")
            failed_delta = _numeric(after, "send_failed_packets") - _numeric(before, "send_failed_packets")
            seq_delta = _numeric(after, "last_seq") - _numeric(before, "last_seq")
            sender_active = sent_delta > 0 or seq_delta > 0
            sender_failed = failed_delta > 0
        socket_drop_delta = _numeric(gap, "socket_drop_delta")
        if socket_drop_delta > 0:
            classification = "receiver_socket_overflow"
        elif sender_telemetry == "unavailable":
            classification = "telemetry_unavailable"
        elif sender_failed:
            classification = "sender_write_failed"
        elif not sender_active:
            classification = "sender_paused"
        elif receiver_telemetry == "alive":
            classification = "udp_path_or_receiver_drop"
        else:
            classification = "receiver_heartbeat_missing"

        classified.append(
            {
                **gap,
                "sender_telemetry": sender_telemetry,
                "receiver_telemetry": receiver_telemetry,
                "sender_attempted_delta": f"{attempted_delta:.0f}",
                "sender_sent_delta": f"{sent_delta:.0f}",
                "sender_failed_delta": f"{failed_delta:.0f}",
                "sender_last_seq_delta": f"{seq_delta:.0f}",
                "receiver_packet_delta": "0",
                "classification": classification,
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CLASSIFIED_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(classified)
    return len(classified)


def mark_report_unavailable(gap_report: Path, output: Path) -> int:
    with gap_report.open("r", encoding="utf-8", newline="") as handle:
        gaps = list(csv.DictReader(handle, delimiter="\t"))
    rows = [
        {
            **gap,
            "sender_telemetry": "unavailable",
            "receiver_telemetry": "unavailable",
            "sender_attempted_delta": "",
            "sender_sent_delta": "",
            "sender_failed_delta": "",
            "sender_last_seq_delta": "",
            "receiver_packet_delta": "0",
            "classification": "telemetry_unavailable",
        }
        for gap in gaps
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CLASSIFIED_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    receiver = subparsers.add_parser("receiver", help="build a compact report from receiver CSV")
    receiver.add_argument("receiver_csv", type=Path)
    receiver.add_argument("--output", required=True, type=Path)
    receiver.add_argument("--gap-ms", type=float, default=100.0)
    classify = subparsers.add_parser("classify", help="classify gaps using sender and receiver heartbeats")
    classify.add_argument("gap_report", type=Path)
    classify.add_argument("--sender-progress", required=True, type=Path)
    classify.add_argument("--receiver-progress", required=True, type=Path)
    classify.add_argument("--sender-summary", type=Path)
    classify.add_argument("--clock-summary", type=Path)
    classify.add_argument("--direction", choices=("uplink", "downlink"), default="uplink")
    classify.add_argument("--output", required=True, type=Path)
    classify.add_argument("--gap-ms", type=float, default=100.0)
    unavailable = subparsers.add_parser(
        "unavailable", help="mark a gap report as lacking heartbeat telemetry"
    )
    unavailable.add_argument("gap_report", type=Path)
    unavailable.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "receiver":
        count = build_gap_report(args.receiver_csv, args.output, args.gap_ms)
    elif args.command == "classify":
        clock_summary = args.clock_summary or args.sender_summary
        if clock_summary is None:
            raise SystemExit("ERROR: classify requires --sender-summary or --clock-summary")
        count = classify_gap_report(
            args.gap_report,
            args.sender_progress,
            args.receiver_progress,
            clock_summary,
            args.output,
            args.gap_ms,
            args.direction,
        )
    else:
        count = mark_report_unavailable(args.gap_report, args.output)
    print(f"gap_rows={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
