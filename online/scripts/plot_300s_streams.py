#!/usr/bin/env python3
"""Extract ten geographically diverse streams from a 300-second window.

The input export is sampled at roughly five-second intervals.  This script
selects streams that cover the requested window, writes the extracted RTT,
bandwidth-estimate, and real-bitrate samples, and creates three overlay plots.
Geographical labels are only used when ``--geo-cache`` is provided; the cache is expected to
be a JSON object keyed by ``client_ip`` (for example, an ip-api batch result).
The output files never contain client IP addresses.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


DEFAULT_DURATION_S = 300
DEFAULT_MIN_SAMPLES = 30
REQUIRED_NUMERIC_FIELDS = ("timestamp", "rtt", "bwe", "real_bitrate")


def default_source() -> Path:
    data_files = sorted(Path(__file__).resolve().parents[1].joinpath("data").glob("*.csv"))
    if not data_files:
        raise FileNotFoundError("no CSV file found under online/data")
    return data_files[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=default_source())
    parser.add_argument(
        "--start",
        help="window start in UTC, e.g. 2026-08-17T13:25:00Z; default: choose the busiest 300s window",
    )
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION_S)
    parser.add_argument("--streams", type=int, default=10, help="number of streams to plot")
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help="minimum samples a stream must have in the window",
    )
    parser.add_argument(
        "--geo-cache",
        type=Path,
        help="optional JSON map: client_ip -> {country, countryCode, regionName, city, status}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("online/analysis/plots_300s"),
    )
    return parser.parse_args()


def parse_start(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def iso_timestamp(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def parse_number(value: str) -> Optional[float]:
    if value is None or value == "" or value == "NULL":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def label_value(value: Any, fallback: str = "Unknown") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text and text != "NULL" else fallback


def load_geo_cache(path: Optional[Path]) -> Mapping[str, Mapping[str, Any]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("--geo-cache must contain a JSON object keyed by client_ip")
    return value


def scan_stream_bounds(source: Path) -> Tuple[Dict[str, Tuple[int, int, int]], int, int]:
    """Return stream -> (first timestamp, last timestamp, row count), and global bounds."""
    bounds: Dict[str, List[int]] = {}
    global_first: Optional[int] = None
    global_last: Optional[int] = None
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            stream = row.get("stream_name", "")
            timestamp = parse_number(row.get("timestamp", ""))
            if not stream or timestamp is None:
                continue
            timestamp_i = int(timestamp)
            entry = bounds.get(stream)
            if entry is None:
                bounds[stream] = [timestamp_i, timestamp_i, 1]
            else:
                entry[0] = min(entry[0], timestamp_i)
                entry[1] = max(entry[1], timestamp_i)
                entry[2] += 1
            global_first = timestamp_i if global_first is None else min(global_first, timestamp_i)
            global_last = timestamp_i if global_last is None else max(global_last, timestamp_i)
    if global_first is None or global_last is None:
        raise ValueError("source contains no valid timestamp/stream_name rows")
    return {key: tuple(value) for key, value in bounds.items()}, global_first, global_last


def choose_window(
    bounds: Mapping[str, Tuple[int, int, int]],
    global_first: int,
    global_last: int,
    duration_ms: int,
) -> Tuple[int, int]:
    if global_last - global_first < duration_ms:
        raise ValueError("source is shorter than the requested window")
    step_ms = duration_ms
    first_grid = (global_first // step_ms) * step_ms
    last_grid = ((global_last - duration_ms) // step_ms) * step_ms
    best: Optional[Tuple[int, int]] = None
    for start in range(first_grid, last_grid + 1, step_ms):
        end = start + duration_ms
        count = sum(first <= start and last >= end for first, last, _ in bounds.values())
        candidate = (count, start)
        if best is None or candidate > best:
            best = candidate
    if best is None or best[0] == 0:
        raise ValueError("could not find a window covered by any stream")
    return best[1], best[1] + duration_ms


def collect_window_rows(source: Path, start_ms: int, end_ms: int) -> Dict[str, List[Dict[str, str]]]:
    streams: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            timestamp = parse_number(row.get("timestamp", ""))
            stream = row.get("stream_name", "")
            if timestamp is None or not stream:
                continue
            timestamp_i = int(timestamp)
            if start_ms <= timestamp_i < end_ms:
                streams[stream].append(row)
    for rows in streams.values():
        rows.sort(key=lambda row: int(float(row["timestamp"])))
    return streams


def stream_geo(rows: Sequence[Mapping[str, str]], geo_cache: Mapping[str, Mapping[str, Any]]) -> Dict[str, str]:
    ip_counts = Counter(row.get("client_ip", "") for row in rows if row.get("client_ip"))
    if not ip_counts:
        return {"country": "Unknown", "country_code": "", "region": "Unknown", "city": "Unknown"}
    ip = ip_counts.most_common(1)[0][0]
    location = geo_cache.get(ip, {})
    if location.get("status") not in (None, "success"):
        return {"country": "Unknown", "country_code": "", "region": "Unknown", "city": "Unknown"}
    return {
        "country": label_value(location.get("country")),
        "country_code": label_value(location.get("countryCode"), ""),
        "region": label_value(location.get("regionName")),
        "city": label_value(location.get("city")),
    }


def stream_metadata(
    stream: str,
    rows: Sequence[Dict[str, str]],
    geo_cache: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    geo = stream_geo(rows, geo_cache)
    first = int(float(rows[0]["timestamp"]))
    last = int(float(rows[-1]["timestamp"]))
    return {
        "stream_name": stream,
        **geo,
        "cdn_name": label_value(rows[0].get("cdn_name")),
        "os": label_value(rows[0].get("os")),
        "app": label_value(rows[0].get("app")),
        "samples": len(rows),
        "nonzero_rtt_samples": sum(
            (parse_number(row.get("rtt", "")) or 0) > 0 for row in rows
        ),
        "nonzero_bwe_samples": sum(
            (parse_number(row.get("bwe", "")) or 0) > 0 for row in rows
        ),
        "nonzero_real_bitrate_samples": sum(
            (parse_number(row.get("real_bitrate", "")) or 0) > 0 for row in rows
        ),
        "first_timestamp_utc": iso_timestamp(first),
        "last_timestamp_utc": iso_timestamp(last),
    }


def select_streams(
    streams: Mapping[str, Sequence[Dict[str, str]]],
    count: int,
    min_samples: int,
    geo_cache: Mapping[str, Mapping[str, Any]],
) -> List[Tuple[str, List[Dict[str, str]], Dict[str, Any]]]:
    candidates = [
        (stream, list(rows), stream_metadata(stream, rows, geo_cache))
        for stream, rows in streams.items()
        if len(rows) >= min_samples
    ]
    candidates = [
        candidate
        for candidate in candidates
        if candidate[2]["nonzero_rtt_samples"] >= min_samples
        and candidate[2]["nonzero_bwe_samples"] >= min_samples
        and candidate[2]["nonzero_real_bitrate_samples"] >= min_samples
    ]
    if len(candidates) < count:
        raise ValueError(
            f"only {len(candidates)} streams have at least {min_samples} nonzero RTT, BWE, "
            "and real-bitrate samples; "
            f"cannot select {count}"
        )
    candidates.sort(key=lambda item: (-item[2]["samples"], item[0]))

    selected: List[Tuple[str, List[Dict[str, str]], Dict[str, Any]]] = []
    used_countries: Set[str] = set()
    for candidate in candidates:
        country_key = candidate[2]["country_code"] or candidate[2]["country"]
        if country_key in used_countries:
            continue
        selected.append(candidate)
        used_countries.add(country_key)
        if len(selected) == count:
            return selected

    # If fewer than ``count`` countries are available, fill with the best
    # remaining streams while retaining deterministic ordering.
    selected_names = {item[0] for item in selected}
    for candidate in candidates:
        if candidate[0] in selected_names:
            continue
        selected.append(candidate)
        if len(selected) == count:
            break
    return selected


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def plot_series(
    path: Path,
    selected: Sequence[Tuple[str, List[Dict[str, str]], Dict[str, Any]]],
    start_ms: int,
    duration_s: int,
    field: str,
    ylabel: str,
    title: str,
) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("matplotlib is required; run with `uv run --no-project --with matplotlib ...`") from error

    fig, axis = plt.subplots(figsize=(13, 7))
    colors = plt.get_cmap("tab10").colors
    for index, (stream, rows, metadata) in enumerate(selected):
        points_x: List[float] = []
        points_y: List[float] = []
        for row in rows:
            timestamp = parse_number(row.get("timestamp", ""))
            value = parse_number(row.get(field, ""))
            if timestamp is None or value is None:
                continue
            # Zero is retained in the extracted CSV but is usually a missing
            # metric for these exports; omit it from the visual line.
            if value == 0:
                continue
            points_x.append((timestamp - start_ms) / 1000.0)
            points_y.append(value)
        if not points_x:
            continue
        country = metadata["country"]
        region = metadata["region"]
        label = f"S{index + 1} {country} / {region} [{metadata['cdn_name']}]"
        axis.plot(points_x, points_y, linewidth=1.25, marker=".", markersize=2.0, alpha=0.85, color=colors[index % len(colors)], label=label)

    axis.set_xlim(0, duration_s)
    axis.set_xlabel("Time from window start (s)")
    axis.set_ylabel(ylabel)
    axis.set_title(f"{title}\n{iso_timestamp(start_ms)} to {iso_timestamp(start_ms + duration_s * 1000)}")
    axis.grid(True, linestyle="--", linewidth=0.5, alpha=0.55)
    axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=8, frameon=True)
    fig.tight_layout()
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def run(args: argparse.Namespace) -> Dict[str, Any]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source not found: {source}")
    if args.duration <= 0 or args.streams <= 0 or args.min_samples <= 0:
        raise ValueError("duration, streams, and min-samples must be positive")

    geo_cache = load_geo_cache(args.geo_cache)
    bounds, global_first, global_last = scan_stream_bounds(source)
    duration_ms = args.duration * 1000
    if args.start:
        start_ms = parse_start(args.start)
        end_ms = start_ms + duration_ms
    else:
        start_ms, end_ms = choose_window(bounds, global_first, global_last, duration_ms)
    window_streams = collect_window_rows(source, start_ms, end_ms)
    selected = select_streams(window_streams, args.streams, args.min_samples, geo_cache)

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata_rows = [
        {"series_id": f"S{index}", **metadata}
        for index, (_, _, metadata) in enumerate(selected, start=1)
    ]
    write_csv(
        output_dir / "selected_streams_300s.csv",
        [
            "series_id", "stream_name", "country", "country_code", "region", "city", "cdn_name", "os", "app",
            "samples", "nonzero_rtt_samples", "nonzero_bwe_samples", "nonzero_real_bitrate_samples",
            "first_timestamp_utc", "last_timestamp_utc",
        ],
        metadata_rows,
    )

    metric_rows: List[Dict[str, Any]] = []
    for index, (stream, rows, metadata) in enumerate(selected, start=1):
        for row in rows:
            timestamp = int(float(row["timestamp"]))
            metric_rows.append(
                {
                    "time_s": round((timestamp - start_ms) / 1000.0, 3),
                    "timestamp_ms": timestamp,
                    "series_id": f"S{index}",
                    "stream_name": stream,
                    "country": metadata["country"],
                    "country_code": metadata["country_code"],
                    "region": metadata["region"],
                    "city": metadata["city"],
                    "cdn_name": metadata["cdn_name"],
                    "rtt_ms": row.get("rtt", ""),
                    "bwe_raw": row.get("bwe", ""),
                    "real_bitrate": row.get("real_bitrate", ""),
                }
            )
    metric_rows.sort(key=lambda row: (row["stream_name"], row["timestamp_ms"]))
    write_csv(
        output_dir / "stream_metrics_300s.csv",
        [
            "time_s", "timestamp_ms", "series_id", "stream_name", "country", "country_code", "region", "city",
            "cdn_name", "rtt_ms", "bwe_raw", "real_bitrate",
        ],
        metric_rows,
    )

    plot_series(
        output_dir / "time_rtt.png",
        selected,
        start_ms,
        args.duration,
        "rtt",
        "RTT (ms; zero values omitted)",
        "300-second stream RTT comparison",
    )
    plot_series(
        output_dir / "time_bandwidth.png",
        selected,
        start_ms,
        args.duration,
        "bwe",
        "Bandwidth estimate (raw export units; zero values omitted)",
        "300-second stream bandwidth-estimate comparison",
    )
    plot_series(
        output_dir / "time_realbitrate.png",
        selected,
        start_ms,
        args.duration,
        "real_bitrate",
        "Real bitrate (raw export units; zero values omitted)",
        "300-second stream real-bitrate comparison",
    )

    summary = {
        "source": str(source),
        "window_start_utc": iso_timestamp(start_ms),
        "window_end_utc_exclusive": iso_timestamp(end_ms),
        "duration_s": args.duration,
        "selected_streams": len(selected),
        "available_streams_in_window": len(window_streams),
        "min_samples": args.min_samples,
        "geo_cache_entries": len(geo_cache),
        "outputs": [
            "selected_streams_300s.csv",
            "stream_metrics_300s.csv",
            "time_rtt.png",
            "time_bandwidth.png",
            "time_realbitrate.png",
        ],
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    try:
        summary = run(args)
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
