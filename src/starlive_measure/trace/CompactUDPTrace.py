#!/usr/bin/env python3
"""Convert UTRC packet captures into compact latency/bandwidth artifacts."""

from __future__ import annotations

import argparse
import json
import math
import struct
from collections import defaultdict
from pathlib import Path
from typing import Any

from .BandwidthTrace import build_bandwidth_trace


HEADER_SIZE = 64
RECORD = struct.Struct("!QQQQqIHH")


def round_half_away(value: float) -> int:
    return math.floor(value + 0.5) if value >= 0 else math.ceil(value - 0.5)


def records(path: Path):
    with path.open("rb") as handle:
        header = handle.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE or header[:4] != b"UTRC":
            raise ValueError("invalid UTRC header")
        while True:
            data = handle.read(RECORD.size)
            if not data:
                return
            if len(data) != RECORD.size:
                raise ValueError("truncated UTRC record")
            yield RECORD.unpack(data)


def _sender_summary(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        values: dict[str, Any] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            try:
                values[key.strip()] = float(value) if "." in value else int(value)
            except ValueError:
                values[key.strip()] = value
        return values
    except (OSError, json.JSONDecodeError):
        return {}


def _write_gap_report(rows: list[tuple[int, ...]], output_dir: Path, gap_ms: float) -> int:
    path = output_dir / "udp_receive_gap_report.tsv"
    gap_index = 0
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            "gap_index\tstart_recv_unix_ns\tend_recv_unix_ns\tstart_recv_epoch_s\t"
            "end_recv_epoch_s\tgap_ms\tprev_seq\tnext_seq\tseq_jump\t"
            "prev_send_rel_s\tnext_send_rel_s\tsend_advance_s\tsocket_drop_delta\n"
        )
        for previous, current in zip(rows, rows[1:]):
            gap = (current[4] - previous[4]) / 1_000_000
            if gap < gap_ms:
                continue
            gap_index += 1
            handle.write(
                f"{gap_index}\t{previous[4]}\t{current[4]}\t{previous[4]/1e9:.9f}\t"
                f"{current[4]/1e9:.9f}\t{gap:.3f}\t{previous[0]}\t{current[0]}\t"
                f"{current[0]-previous[0]}\t{previous[2]/1e9:.9f}\t"
                f"{current[2]/1e9:.9f}\t{(current[2]-previous[2])/1e9:.9f}\t"
                f"{current[5]-previous[5]}\n"
            )
    return gap_index


def _write_arrivals(rows: list[tuple[int, ...]], output_dir: Path, first_recv: int) -> Path:
    values: list[int] = []
    previous = 0
    for row in rows:
        value = max(previous, (row[4] - first_recv) // 1_000_000)
        values.append(value)
        previous = value
    path = output_dir / "udp_arrival_time_ms.txt"
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")
    return path


def _write_precise_rows(
    rows: list[tuple[int, ...]],
    output_dir: Path,
    first_recv: int,
    window_ms: float,
    sender: dict[str, Any],
) -> dict[str, Path]:
    """Write unrounded packet and latency-window sidecars.

    UTRC stores nanoseconds.  Keeping the integer nanosecond values in the
    sidecar avoids a second rounding step while still making the files easy to
    consume from Python, R, or a spreadsheet.
    """
    arrival_path = output_dir / "udp_arrival_time_precise.tsv"
    with arrival_path.open("w", encoding="utf-8") as handle:
        handle.write(
            "seq\tsend_time_s\treceive_time_s\tpayload_bytes\t"
            "scheduled_send_rel_s\tsend_rel_s\tsend_unix_ns\t"
            "receive_unix_ns\treceive_rel_s\tsocket_drops\n"
        )
        for row in rows:
            handle.write(
                f"{row[0]}\t{row[2] / 1e9:.9f}\t{(row[4] - first_recv) / 1e9:.9f}\t"
                f"{row[6]}\t{row[1] / 1e9:.9f}\t{row[2] / 1e9:.9f}\t"
                f"{row[3]}\t{row[4]}\t{(row[4] - first_recv) / 1e9:.9f}\t{row[5]}\n"
            )

    offset_ms = float(sender.get("clock_offset_ms", 0.0) or 0.0)
    latency_path = output_dir / "udp_latency_precise.tsv"
    window_ns = max(1, int(window_ms * 1_000_000))
    windows: dict[int, list[tuple[float, int]]] = defaultdict(list)
    with latency_path.open("w", encoding="utf-8") as handle:
        handle.write(
            "seq\tsend_time_s\treceive_time_s\tdelay_ms\tpayload_bytes\t"
            "send_unix_ns\treceive_unix_ns\treceive_rel_s\n"
        )
        for row in rows:
            if not row[3] or not row[4]:
                continue
            delay_ms = (row[4] - row[3]) / 1e6 + offset_ms
            receive_rel_s = (row[4] - first_recv) / 1e9
            handle.write(
                f"{row[0]}\t{row[3] / 1e9:.9f}\t{receive_rel_s:.9f}\t"
                f"{delay_ms:.9f}\t{row[6]}\t{row[3]}\t{row[4]}\t{receive_rel_s:.9f}\n"
            )
            windows[(row[4] - first_recv) // window_ns].append((delay_ms, row[0]))

    window_path = output_dir / "delay_precise.tsv"
    with window_path.open("w", encoding="utf-8") as handle:
        handle.write("window_start_ms\tmean_delay_ms\tsample_count\n")
        for index in sorted(windows):
            values = [value for value, _seq in windows[index]]
            handle.write(
                f"{index * window_ms:.9f}\t{sum(values) / len(values):.9f}\t{len(values)}\n"
            )
    return {"arrival_path": arrival_path, "latency_path": latency_path, "window_path": window_path}


def _write_latency(
    rows: list[tuple[int, ...]],
    output_dir: Path,
    first_recv: int,
    window_ms: float,
    sender: dict[str, Any],
    *,
    interpolate_empty: bool = False,
) -> dict[str, Any]:
    window_ns = max(1, int(window_ms * 1_000_000))
    offset_ms = float(sender.get("clock_offset_ms", 0.0) or 0.0)
    windows: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        if row[3] and row[4]:
            index = (row[4] - first_recv) // window_ns
            windows[index].append((row[4] - row[3]) / 1_000_000 + offset_ms)
    observed_indices = sorted(windows)
    observed_values = {
        index: sum(windows[index]) / len(windows[index]) for index in observed_indices
    }
    completed_values = dict(observed_values)
    if interpolate_empty and len(observed_indices) >= 2:
        for left, right in zip(observed_indices, observed_indices[1:]):
            if right == left + 1:
                continue
            left_value = observed_values[left]
            slope = (observed_values[right] - left_value) / (right - left)
            for index in range(left + 1, right):
                completed_values[index] = left_value + slope * (index - left)
    indices = sorted(completed_values)
    raw_empty = (
        observed_indices[-1] - observed_indices[0] + 1 - len(observed_indices)
        if observed_indices else 0
    )
    interpolated = len(indices) - len(observed_indices)
    (output_dir / "delay.txt").write_text(
        "".join(
            f"{round_half_away(completed_values[index])}\n"
            for index in indices
        ),
        encoding="utf-8",
    )
    (output_dir / "delay_window_time_ms.txt").write_text(
        "".join(f"{index * window_ms:g}\n" for index in indices), encoding="utf-8"
    )
    valid_rows = sum(len(windows[index]) for index in observed_indices)
    (output_dir / "delay_summary.txt").write_text(
        "".join(
            [
                "source=utrc_postprocess\n",
                f"valid_delay_rows={valid_rows}\n",
                f"delay_windows={len(indices)}\n",
                f"observed_delay_windows={len(observed_indices)}\n",
                f"interpolated_delay_windows={interpolated}\n",
                f"raw_empty_windows={raw_empty}\n",
                f"completed_delay_windows={len(indices)}\n",
                f"delay_interpolation={'linear' if interpolate_empty else 'none'}\n",
                f"first_window_index={indices[0] if indices else -1}\n",
                f"last_window_index={indices[-1] if indices else -1}\n",
                f"empty_windows={raw_empty}\n",
                "first_recv_rel_ms=0\n",
                f"last_recv_rel_ms={(rows[-1][4] - first_recv) / 1_000_000:.6f}\n",
                f"clock_offset_ms={offset_ms:.6f}\n",
                f"clock_sync_samples={sender.get('clock_sync_samples', 0)}\n",
                "delay_formula=recv_unix_ns-send_unix_ns+clock_offset_ms\n",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "valid_delay_rows": valid_rows,
        "delay_windows": len(indices),
        "observed_delay_windows": len(observed_indices),
        "interpolated_delay_windows": interpolated,
        "raw_empty_windows": raw_empty,
        "completed_delay_windows": len(indices),
        "delay_interpolation": "linear" if interpolate_empty else "none",
        "window_ms": window_ms,
        "clock_offset_ms": offset_ms,
        "clock_sync_samples": sender.get("clock_sync_samples", 0),
    }


def _write_bandwidth(
    rows: list[tuple[int, ...]],
    output_dir: Path,
    first_recv: int,
    window_ms: int,
    duration_ms: int | None,
) -> tuple[Path, dict[str, Any]]:
    events = (((row[4] - first_recv) // 1_000_000, row[6]) for row in rows)
    _arrivals, trace_rows = build_bandwidth_trace(
        events, duration_ms=duration_ms, window_ms=window_ms
    )
    path = output_dir / "bandwidth_trace.tsv"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(
            "time_ms\tpacket_count\tpayload_bytes\twindow_packet_count\t"
            "window_payload_bytes\tbandwidth_mbps\n"
        )
        for item in trace_rows:
            handle.write(
                f"{item['time_ms']}\t{item['packet_count']}\t{item['payload_bytes']}\t"
                f"{item['window_packet_count']}\t{item['window_payload_bytes']}\t"
                f"{item['bandwidth_mbps']:.9f}\n"
            )
    return path, {
        "trace_rows": len(trace_rows),
        "window_ms": window_ms,
        "duration_ms": duration_ms,
    }


def convert(
    path: Path,
    output_dir: Path,
    mode: str,
    window_ms: float = 10.0,
    gap_ms: float = 100.0,
    *,
    sender_summary: Path | None = None,
    duration_ms: int | None = None,
    payload_bytes: int | None = None,
    precision_mode: str = "trace",
) -> dict[str, Any]:
    if mode not in {"arrival", "latency", "both"}:
        raise ValueError("mode must be arrival, latency, or both")
    if precision_mode not in {"trace", "measurement"}:
        raise ValueError("precision_mode must be trace or measurement")
    output_dir.mkdir(parents=True, exist_ok=True)
    parsed_rows = list(records(path))
    if not parsed_rows:
        raise ValueError("empty UTRC capture")
    rows = [row for row in parsed_rows if row[6] > 0]
    invalid_records = len(parsed_rows) - len(rows)
    if not rows:
        raise ValueError("UTRC contains no valid UDP payload bytes")
    first_recv = rows[0][4]
    gap_count = _write_gap_report(rows, output_dir, gap_ms)
    arrival_path = _write_arrivals(rows, output_dir, first_recv)
    sender = _sender_summary(sender_summary)
    precise_paths: dict[str, Path] = {}
    if precision_mode == "measurement":
        precise_paths = _write_precise_rows(rows, output_dir, first_recv, window_ms, sender)
    if mode == "arrival":
        return {
            "arrival_path": arrival_path,
            "packet_count": len(rows),
            "precision_mode": precision_mode,
            "precise_files": {key: str(value) for key, value in precise_paths.items()},
        }

    latency = _write_latency(
        rows,
        output_dir,
        first_recv,
        window_ms,
        sender,
        interpolate_empty=mode == "both",
    )
    if mode == "latency":
        return {
            "packet_count": len(rows),
            "precision_mode": precision_mode,
            "precise_files": {key: str(value) for key, value in precise_paths.items()},
            **latency,
        }

    integer_window = max(1, int(round(window_ms)))
    trace_path, bandwidth = _write_bandwidth(
        rows, output_dir, first_recv, integer_window, duration_ms
    )
    plot_path = output_dir / "bandwidth_time_scatter.png"
    plot_error = ""
    try:
        from .BandwidthTimeScatter import plot_run
        inferred_payload = payload_bytes or int(round(sum(row[6] for row in rows) / len(rows)))
        plot_run(
            output_dir,
            arrival_path,
            inferred_payload,
            output=plot_path,
            window_ms=integer_window,
            end_ms=duration_ms,
        )
    except Exception as error:  # Plotting is optional.
        plot_error = str(error)

    sequence_values = [row[0] for row in rows]
    sequence_gaps = sum(
        max(0, current - previous - 1)
        for previous, current in zip(sequence_values, sequence_values[1:])
    )
    summary = {
        "source_utrc": str(path),
        "source_utrc_exists": path.is_file(),
        "source_utrc_size_bytes": path.stat().st_size if path.is_file() else 0,
        "server_utrc_transfer": "remote_only",
        "records_seen": len(parsed_rows),
        "invalid_records": invalid_records,
        "packet_count": len(rows),
        "unique_packet_count": len(set(sequence_values)),
        "duplicate_packet_count": len(rows) - len(set(sequence_values)),
        "sequence_gap_packets": sequence_gaps,
        "payload_bytes": sum(row[6] for row in rows),
        "payload_bytes_min": min(row[6] for row in rows),
        "payload_bytes_max": max(row[6] for row in rows),
        "socket_drop_max": max(row[5] for row in rows),
        "socket_drop_last": rows[-1][5],
        "first_recv_unix_ns": rows[0][4],
        "last_recv_unix_ns": rows[-1][4],
        "capture_duration_ms": (rows[-1][4] - rows[0][4]) / 1_000_000,
        "records_truncated": False,
        "gap_report_rows": gap_count,
        "clock_sync_samples": sender.get("clock_sync_samples", 0),
        "clock_offset_ms": float(sender.get("clock_offset_ms", 0.0) or 0.0),
        "sender_summary": str(sender_summary) if sender_summary else "",
        "latency": latency,
        "bandwidth": bandwidth,
        "arrival_file": str(arrival_path),
        "bandwidth_trace_file": str(trace_path),
        "plot_file": str(plot_path) if plot_path.is_file() else "",
        "plot_error": plot_error,
        "precision_mode": precision_mode,
        "precise_files": {key: str(value) for key, value in precise_paths.items()},
    }
    summary_path = output_dir / "custom_udp_trace_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "packet_count": len(rows),
        "arrival_path": arrival_path,
        "trace_path": trace_path,
        "summary_path": summary_path,
        "plot_path": plot_path if plot_path.is_file() else None,
        "plot_error": plot_error,
        **summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("arrival", "latency", "both"), required=True)
    parser.add_argument("--window-ms", type=float, default=10.0)
    parser.add_argument("--gap-ms", type=float, default=100.0)
    parser.add_argument("--sender-summary", type=Path)
    parser.add_argument("--duration-ms", type=int)
    parser.add_argument("--payload-bytes", type=int)
    parser.add_argument(
        "--precision-mode", choices=("trace", "measurement"), default="trace",
        help="trace keeps legacy integer-ms files; measurement writes unrounded TSV sidecars",
    )
    args = parser.parse_args(argv)
    result = convert(
        args.capture,
        args.output_dir,
        args.mode,
        args.window_ms,
        args.gap_ms,
        sender_summary=args.sender_summary,
        duration_ms=args.duration_ms,
        payload_bytes=args.payload_bytes,
        precision_mode=args.precision_mode,
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if not isinstance(value, Path)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
