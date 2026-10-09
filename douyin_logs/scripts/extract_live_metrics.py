#!/usr/bin/env python3
"""Extract Douyin live logcat metrics into one sparse time-series CSV."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


LOG_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEF])\s+"
    r"(?P<tag>.*?):\s(?P<message>.*)$"
)
TS_RE = re.compile(r"\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3}")
NQ_RE = re.compile(r"current_network_quality_info=([^&\s\"'}]+)")
SENT_RE = re.compile(r"sent bytes\s*=\s*(\d+)", re.IGNORECASE)
RECEIVED_RE = re.compile(r"received bytes\s*=\s*(\d+)", re.IGNORECASE)

CSV_FIELDS = (
    "run_id",
    "event_type",
    "log_time",
    "time_s",
    "pid",
    "tid",
    "level",
    "tag",
    "source_line",
    "real_bitrate",
    "real_video_framerate",
    "stream_delay",
    "ntp_diff",
    "pts",
    "sei_index",
    "crop_w",
    "crop_h",
    "stride_w",
    "stride_h",
    "audio_loudness_integrated_source_loudness",
    "audio_loudness_integrated_source_peak",
    "audio_loudness_max_moment_loud",
    "audio_loudness_max_short_term_loud",
    "audio_loudness_momentaryLoudness",
    "audio_loudness_shorttermLoudness",
    "audio_loudness_windowLoudness",
    "audio_loudness_windowPeak",
    "http_rtt",
    "tcp_rtt",
    "quic_rtt",
    "downstream_throughput_kbps",
    "video_download_speed",
    "quic_receive_loss_rate",
    "quic_send_loss_rate",
    "net_effective_connection_type",
    "timing_dns",
    "timing_connect",
    "timing_ssl",
    "timing_send",
    "timing_waiting",
    "timing_receive",
    "timing_total",
    "totalSendBytes",
    "totalReceivedBytes",
    "remote_ip",
    "status",
    "net_error",
    "cronet_error_code",
    "cronet_internal_error_code",
    "room_id",
    "rid",
    "anchor_id",
    "definition",
    "play_mode",
    "net_quality",
    "live_type",
    "room_type",
    "sent_bytes",
    "received_bytes",
    "url_host",
    "url_path",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Douyin live metrics from captured logcat text into live_metrics_timeseries.csv."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=None,
        help="Run directory or logcat file. Default: douyin_logs/logs/latest_douyin_logcat/logcat_relevant.txt",
    )
    return parser.parse_args()


def default_input() -> Path:
    script_dir = Path(__file__).resolve().parent
    return script_dir.parent / "logs" / "latest_douyin_logcat" / "logcat_relevant.txt"


def resolve_input(path: Path | None) -> tuple[Path, Path]:
    input_path = path or default_input()
    if input_path.is_dir():
        relevant = input_path / "logcat_relevant.txt"
        raw = input_path / "logcat.txt"
        log_path = relevant if relevant.exists() else raw
        return log_path, input_path.resolve()
    return input_path, input_path.parent.resolve()


def parse_logcat_line(raw_line: str) -> dict[str, str] | None:
    ts_match = TS_RE.search(raw_line)
    if not ts_match:
        return None
    source_line = ""
    prefix = raw_line[: ts_match.start()]
    prefix_numbers = [part for part in prefix.split(":") if part.isdigit()]
    if prefix_numbers:
        source_line = prefix_numbers[-1]

    line = raw_line[ts_match.start() :].rstrip("\n")
    match = LOG_RE.match(line)
    if not match:
        return None

    data = match.groupdict()
    data["source_line"] = source_line
    return data


def parse_log_time(value: str) -> datetime:
    parsed = datetime.strptime(value, "%m-%d %H:%M:%S.%f")
    return parsed.replace(year=date.today().year)


def empty_row(run_id: str, event_type: str, common: dict[str, str], first_time: datetime) -> dict[str, Any]:
    log_dt = parse_log_time(common["ts"])
    row = {field: "" for field in CSV_FIELDS}
    row.update(
        {
            "run_id": run_id,
            "event_type": event_type,
            "log_time": common["ts"],
            "time_s": f"{(log_dt - first_time).total_seconds():.3f}",
            "pid": common["pid"],
            "tid": common["tid"],
            "level": common["level"],
            "tag": common["tag"].strip(),
            "source_line": common.get("source_line", ""),
        }
    )
    return row


def json_objects(text: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    idx = 0
    while True:
        start = text.find("{", idx)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict):
            objects.append(obj)
        idx = start + max(end, 1)
    return objects


def csv_value(value: Any) -> Any:
    return "" if value is None else value


def update_sei(row: dict[str, Any], obj: dict[str, Any]) -> None:
    for key in ("real_bitrate", "real_video_framerate", "stream_delay", "ntp_diff", "pts", "sei_index"):
        row[key] = csv_value(obj.get(key))

    crop = obj.get("live_crop")
    if isinstance(crop, dict):
        for key in ("crop_w", "crop_h", "stride_w", "stride_h"):
            row[key] = csv_value(crop.get(key))

    loudness = obj.get("audio_loudness")
    if isinstance(loudness, dict):
        for key, value in loudness.items():
            column = f"audio_loudness_{key}"
            if column in row:
                row[column] = csv_value(value)


def parse_network_quality_values(text: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for match in NQ_RE.finditer(text):
        encoded = match.group(1).replace("\\/", "/")
        decoded = unquote(unquote(encoded))
        try:
            obj = json.loads(decoded)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            values.append(obj)
    return values


def update_network_quality(row: dict[str, Any], obj: dict[str, Any]) -> None:
    for key in (
        "http_rtt",
        "tcp_rtt",
        "quic_rtt",
        "downstream_throughput_kbps",
        "video_download_speed",
        "quic_receive_loss_rate",
        "quic_send_loss_rate",
        "net_effective_connection_type",
    ):
        row[key] = csv_value(obj.get(key))


def update_api_timing(row: dict[str, Any], obj: dict[str, Any]) -> None:
    for key in ("timing_dns", "timing_connect", "timing_ssl", "timing_send", "timing_waiting", "timing_receive", "timing_total"):
        row[key] = csv_value(obj.get(key))
    row["totalSendBytes"] = csv_value(obj.get("timing_totalSendBytes") or obj.get("totalSendBytes"))
    row["totalReceivedBytes"] = csv_value(obj.get("timing_totalReceivedBytes") or obj.get("totalReceivedBytes"))
    row["remote_ip"] = csv_value(obj.get("timing_remoteIP") or obj.get("ip"))
    row["status"] = csv_value(obj.get("status"))
    row["net_error"] = csv_value(obj.get("net_error"))
    row["cronet_error_code"] = csv_value(obj.get("cronet_error_code"))
    row["cronet_internal_error_code"] = csv_value(obj.get("cronet_internal_error_code"))

    uri = obj.get("uri")
    if isinstance(uri, str):
        update_url_fields(row, uri)


def update_url_fields(row: dict[str, Any], url: str) -> None:
    cleaned = url.replace("\\/", "/")
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return
    if parsed.netloc:
        row["url_host"] = parsed.netloc
    if parsed.path:
        row["url_path"] = parsed.path
    params = parse_qs(parsed.query)
    for key in ("room_id", "rid", "anchor_id", "definition", "play_mode", "net_quality", "live_type", "room_type"):
        if key in params and params[key]:
            row[key] = params[key][0]


def first_regex_value(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def update_room_context(row: dict[str, Any], text: str) -> bool:
    found = False
    patterns = {
        "room_id": (r"[?&]room_id=([^&\s\"'}]+)", r'\\"room_id\\":\\"([^"\\]+)\\"', r'"room_id":"([^"]+)"'),
        "rid": (r"[?&]rid=([^&\s\"'}]+)",),
        "anchor_id": (r"[?&]anchor_id=([^&\s\"'}]+)", r'\\"anchor_id\\":\\"([^"\\]+)\\"', r'"anchor_id":"([^"]+)"'),
        "definition": (r"[?&]definition=([^&\s\"'}]+)", r'\\"definition\\":\\"([^"\\]+)\\"', r'"definition":"([^"]+)"'),
        "play_mode": (r"[?&]play_mode=([^&\s\"'}]+)", r'\\"play_mode\\":\\"([^"\\]+)\\"', r'"play_mode":"([^"]+)"'),
        "net_quality": (r"[?&]net_quality=([^&\s\"'}]+)", r'\\"net_quality\\":\\"?([^",\\]+)', r'"net_quality":"?([^",}]+)'),
        "live_type": (r"[?&]live_type=([^&\s\"'}]+)", r'\\"live_type\\":\\"([^"\\]+)\\"', r'"live_type":"([^"]+)"'),
        "room_type": (r"[?&]room_type=([^&\s\"'}]+)", r'\\"room_type\\":\\"([^"\\]+)\\"', r'"room_type":"([^"]+)"'),
    }
    for key, key_patterns in patterns.items():
        value = first_regex_value(key_patterns, text)
        if value:
            row[key] = unquote(value)
            found = True

    url_match = re.search(r"(https?://[^\s\"'}]+|wss://[^\s\"'}]+|sslocal://[^\s\"'}]+)", text)
    if found and url_match:
        update_url_fields(row, url_match.group(1))
    return found


def update_websocket(row: dict[str, Any], text: str) -> bool:
    if "wss://webcast" not in text:
        return False
    sent = SENT_RE.search(text)
    received = RECEIVED_RE.search(text)
    if sent:
        row["sent_bytes"] = sent.group(1)
    if received:
        row["received_bytes"] = received.group(1)
    url_match = re.search(r"(wss://[^\s\"'}]+)", text)
    if url_match:
        update_url_fields(row, url_match.group(1))
    return True


def extract_rows(log_path: Path, run_dir: Path) -> list[dict[str, Any]]:
    run_id = run_dir.name
    rows: list[dict[str, Any]] = []
    first_time: datetime | None = None
    seen_sei: set[tuple[Any, Any, Any]] = set()
    seen_network: set[tuple[str, str]] = set()

    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            common = parse_logcat_line(raw_line)
            if not common:
                continue
            if first_time is None:
                first_time = parse_log_time(common["ts"])
            message = common["message"]
            objects = json_objects(message)

            for obj in objects:
                if "real_bitrate" in obj or "sei_index" in obj:
                    key = (obj.get("ts"), obj.get("sei_index"), obj.get("pts"))
                    if key not in seen_sei:
                        row = empty_row(run_id, "sei", common, first_time)
                        update_sei(row, obj)
                        rows.append(row)
                        seen_sei.add(key)

                if any(key in obj for key in ("timing_total", "timing_waiting", "timing_totalSendBytes", "cronet_error_code")):
                    row = empty_row(run_id, "api_timing", common, first_time)
                    update_api_timing(row, obj)
                    rows.append(row)

            for nq_obj in parse_network_quality_values(message):
                key = (common["ts"], json.dumps(nq_obj, sort_keys=True))
                if key in seen_network:
                    continue
                row = empty_row(run_id, "network_quality", common, first_time)
                update_network_quality(row, nq_obj)
                rows.append(row)
                seen_network.add(key)

            ws_row = empty_row(run_id, "websocket_traffic", common, first_time)
            if update_websocket(ws_row, message):
                rows.append(ws_row)

            room_row = empty_row(run_id, "room_context", common, first_time)
            if update_room_context(room_row, message):
                rows.append(room_row)

    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    log_path, run_dir = resolve_input(args.input)
    if not log_path.exists() or not log_path.is_file():
        print(f"ERROR: log file does not exist: {log_path}")
        return 2

    rows = extract_rows(log_path, run_dir)
    output_path = run_dir / "live_metrics_timeseries.csv"
    output_gz_path = run_dir / "live_metrics_timeseries.csv.gz"
    write_csv(output_path, rows)
    write_csv_gz(output_gz_path, rows)

    counts: dict[str, int] = {}
    for row in rows:
        event_type = str(row["event_type"])
        counts[event_type] = counts.get(event_type, 0) + 1

    print(f"Wrote {len(rows)} rows: {output_path}")
    print(f"Wrote {len(rows)} rows: {output_gz_path}")
    for event_type in sorted(counts):
        print(f"  {event_type}: {counts[event_type]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
