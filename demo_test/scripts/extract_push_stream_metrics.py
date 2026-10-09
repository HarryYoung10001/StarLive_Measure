#!/usr/bin/env python3
"""Extract demo push-stream metrics from logcat into CSV and CSV.GZ files."""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOG_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEF])\s+"
    r"(?P<tag>.*?):\s(?P<message>.*)$"
)
TS_RE = re.compile(r"\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}")

SOURCE_FIELDS = (
    "run_id", "log_time", "timestamp", "timestamp_iso_utc", "time_s",
    "pid", "tid", "level", "tag", "source_line",
)
METRIC_FIELDS = (
    "mode", "project_key", "live_sdk_version", "report_version", "cdn_ip",
    "video_codec", "audio_codec", "video_profile", "audio_profile", "hardware",
    "real_bitrate", "video_enc_bitrate", "audio_enc_bitrate", "meta_video_bitrate",
    "meta_audio_bitrate", "max_video_bitrate", "min_video_bitrate", "max_bitrate",
    "min_bitrate", "encode_fps", "real_video_framerate", "meta_video_framerate",
    "preview_fps", "render_fps", "in_cap_fps", "out_cap_fps", "drop_source_fps",
    "width", "height", "video_capture_width", "video_capture_height",
    "video_capture_fps", "real_gop_ms", "video_qp", "iframe_psnr", "pframe_psnr",
    "rtt", "bwe", "loss_rate", "network_quality", "nqe_bwe", "package_delay",
    "rtmp_buffer_time", "drop_count", "transport_drop_count", "transport_layer_status",
    "push_stream_stall_status", "transport_send_audio_stall_time",
    "transport_send_audio_stall_count", "transport_send_video_stall_time",
    "transport_send_video_stall_count", "transport_pts_audio_stall_time",
    "transport_pts_audio_stall_count", "transport_pts_video_stall_time",
    "transport_pts_video_stall_count", "transport_reconnect_count",
    "transport_reconnect_time", "cpu", "memory", "camera_capture_time",
    "finish_process_to_next_capture", "update_tex_image_time", "oes_to_2d_time",
    "effect_process_time_v2", "post_process_algorithm_time", "after_effect_gl_finish_time",
    "process_to_render_latency", "render_time", "process_to_encoder_latency",
    "encoder_draw_surface_time", "start_to_send_time", "event_index",
)
MAIN_FIELDS = SOURCE_FIELDS + METRIC_FIELDS
DIAGNOSTIC_BASE_FIELDS = (
    "run_id", "log_time", "pid", "tid", "level", "tag", "source_line",
)
SENSITIVE_PARTS = ("url", "secret", "sign", "token", "session")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract push_stream metrics. With no input, process every demo log run."
    )
    parser.add_argument("input", nargs="?", type=Path, help="Run directory or logcat file")
    return parser.parse_args()


def demo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_offset_file(run_dir: Path) -> None:
    offset_path = run_dir / "offset.txt"
    if offset_path.exists() and offset_path.read_text(encoding="utf-8", errors="replace").strip():
        return
    offset_path.write_text("12\n", encoding="utf-8")
    print(f"Initialized offset: {offset_path}")


def resolve_latest_pointer(path: Path) -> Path | None:
    if path.name != "latest_demo_logcat" or not path.is_file():
        return None
    target_text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not target_text:
        return None
    target = Path(target_text)
    candidates = [target]
    if target.name:
        candidates.append(path.parent / target.name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def resolve_log(run_or_log: Path) -> tuple[Path, Path]:
    pointer_target = resolve_latest_pointer(run_or_log)
    if pointer_target is not None:
        return resolve_log(pointer_target)
    if run_or_log.is_dir():
        relevant = run_or_log / "logcat_relevant.txt"
        raw = run_or_log / "logcat.txt"
        if relevant.is_file():
            return relevant, run_or_log.resolve()
        if raw.is_file():
            return raw, run_or_log.resolve()
        raise FileNotFoundError(f"no logcat_relevant.txt or logcat.txt under {run_or_log}")
    if not run_or_log.is_file():
        raise FileNotFoundError(f"logcat file not found: {run_or_log}")
    return run_or_log, run_or_log.parent.resolve()


def discover_inputs(value: Path | None) -> list[tuple[Path, Path]]:
    if value is not None:
        return [resolve_log(value)]
    runs = []
    for path in sorted((demo_root() / "logs").glob("demo_logcat_*")):
        if path.is_symlink() or not path.is_dir():
            continue
        try:
            runs.append(resolve_log(path))
        except FileNotFoundError:
            print(f"Warning: skipping run without logcat input: {path}")
    return runs


def parse_logcat_line(raw_line: str) -> dict[str, str] | None:
    ts_match = TS_RE.search(raw_line)
    if not ts_match:
        return None
    prefix = raw_line[: ts_match.start()]
    source_line = ""
    match_number = re.search(r"(\d+):\s*$", prefix)
    if match_number:
        source_line = match_number.group(1)
    match = LOG_RE.match(raw_line[ts_match.start():].rstrip("\n"))
    if not match:
        return None
    result = match.groupdict()
    result["source_line"] = source_line
    return result


def timestamp_iso(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_PARTS)


def flatten_diagnostic(value: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, item in value.items():
        column = f"{prefix}.{key}" if prefix else key
        if sensitive_key(column):
            continue
        if isinstance(item, dict):
            flattened.update(flatten_diagnostic(item, column))
        elif isinstance(item, list):
            flattened[column] = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        else:
            flattened[column] = csv_value(item)
    return flattened


def encode_csv(rows: list[dict[str, Any]], fields: tuple[str, ...] | list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def write_csv_pair(run_dir: Path, stem: str, rows: list[dict[str, Any]], fields: tuple[str, ...] | list[str]) -> tuple[Path, Path]:
    csv_path = run_dir / f"{stem}.csv"
    gzip_path = run_dir / f"{stem}.csv.gz"
    data = encode_csv(rows, fields)
    csv_path.write_bytes(data)
    gzip_path.write_bytes(gzip.compress(data, mtime=0))
    return csv_path, gzip_path


def process_run(log_path: Path, run_dir: Path) -> None:
    run_id = run_dir.name
    ensure_offset_file(run_dir)
    main_rows: list[dict[str, Any]] = []
    diagnostic_rows: list[dict[str, Any]] = []
    monitor_total = monitor_failed = monitor_non_push = 0
    diagnostic_total = diagnostic_failed = 0

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            common = parse_logcat_line(raw_line)
            if common is None:
                continue
            tag = common["tag"].strip()
            message = common["message"]
            if tag == "live_client_monitor_log":
                monitor_total += 1
                try:
                    obj = json.loads(message)
                except json.JSONDecodeError:
                    monitor_failed += 1
                    continue
                if not isinstance(obj, dict) or obj.get("event_key") != "push_stream":
                    monitor_non_push += 1
                    continue
                row = {field: "" for field in MAIN_FIELDS}
                row.update({
                    "run_id": run_id,
                    "log_time": common["ts"],
                    "timestamp": csv_value(obj.get("timestamp")),
                    "timestamp_iso_utc": timestamp_iso(obj.get("timestamp")),
                    "pid": common["pid"], "tid": common["tid"],
                    "level": common["level"], "tag": tag,
                    "source_line": common["source_line"],
                })
                for field in METRIC_FIELDS:
                    row[field] = csv_value(obj.get(field))
                main_rows.append(row)
            elif tag == "push_stream.message":
                diagnostic_total += 1
                try:
                    outer = json.loads(message)
                    inner_text = outer.get("message") if isinstance(outer, dict) else None
                    if not isinstance(inner_text, str):
                        raise ValueError("missing inner message")
                    inner = json.loads(inner_text)
                    if not isinstance(inner, dict):
                        raise ValueError("inner message is not an object")
                except (json.JSONDecodeError, ValueError):
                    diagnostic_failed += 1
                    continue
                row = {
                    "run_id": run_id, "log_time": common["ts"],
                    "pid": common["pid"], "tid": common["tid"],
                    "level": common["level"], "tag": tag,
                    "source_line": common["source_line"],
                }
                row.update(flatten_diagnostic(inner))
                diagnostic_rows.append(row)

    main_rows.sort(key=lambda row: (float(row["timestamp"]) if str(row["timestamp"]).replace(".", "", 1).isdigit() else float("inf"), row["log_time"]))
    first_timestamp = next((float(row["timestamp"]) for row in main_rows if isinstance(row["timestamp"], (int, float))), None)
    for row in main_rows:
        value = row["timestamp"]
        if first_timestamp is not None and isinstance(value, (int, float)):
            row["time_s"] = f"{(value - first_timestamp) / 1000:.3f}"

    diagnostic_extra = sorted({key for row in diagnostic_rows for key in row if key not in DIAGNOSTIC_BASE_FIELDS})
    diagnostic_fields = list(DIAGNOSTIC_BASE_FIELDS) + diagnostic_extra
    main_csv, main_gz = write_csv_pair(run_dir, "push_stream_metrics", main_rows, MAIN_FIELDS)
    diagnostic_csv, diagnostic_gz = write_csv_pair(run_dir, "push_stream_diagnostics", diagnostic_rows, diagnostic_fields)
    metadata_path = run_dir / "push_stream_metrics_metadata.txt"
    metadata_path.write_text(
        "\n".join((
            f"run_id={run_id}", f"input={log_path}",
            f"monitor_total={monitor_total}", f"monitor_success={len(main_rows)}",
            f"monitor_parse_failed={monitor_failed}", f"monitor_non_push={monitor_non_push}",
            f"diagnostic_total={diagnostic_total}", f"diagnostic_success={len(diagnostic_rows)}",
            f"diagnostic_truncated_or_failed={diagnostic_failed}",
            f"main_csv={main_csv}", f"main_csv_gz={main_gz}",
            f"diagnostic_csv={diagnostic_csv}", f"diagnostic_csv_gz={diagnostic_gz}",
            f"generated_utc={datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
            "sensitive_fields_excluded=true", "diagnostic_partial_recovery=false", "",
        )),
        encoding="utf-8",
    )
    print(f"{run_id}: metrics={len(main_rows)}, diagnostics={len(diagnostic_rows)}, input={log_path.name}")


def main() -> int:
    args = parse_args()
    try:
        inputs = discover_inputs(args.input)
    except FileNotFoundError as error:
        print(f"Error: {error}")
        return 1
    if not inputs:
        print("Error: no demo log runs found")
        return 1
    for log_path, run_dir in inputs:
        process_run(log_path, run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
