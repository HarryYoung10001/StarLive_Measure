#!/usr/bin/env python3
"""Summarize the online push-stream export without loading it into memory.

The input is the CSV export under ``online/data``.  An IP geolocation cache
produced separately (JSON object keyed by client IP) can be supplied with
``--geo-cache``.  The generated CSVs contain only aggregates, never client IPs.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Optional, Set, Tuple


def display(value: Optional[str]) -> str:
    return value if value and value != "NULL" else "Unknown"


def load_geo_cache(path: Optional[Path]) -> Mapping[str, Mapping[str, str]]:
    if path is None:
        return {}
    with path.open(encoding="utf-8") as handle:
        loaded = json.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"Geo cache must be a JSON object: {path}")
    return loaded


def write_distribution(
    path: Path,
    records: Iterable[Mapping[str, object]],
    fieldnames: Iterable[str],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(records)


def summarize(source: Path, output_dir: Path, geo_cache: Mapping[str, Mapping[str, str]]) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    counts = Counter()
    unique_ips: MutableMapping[str, Set[str]] = defaultdict(set)
    unique_streams: MutableMapping[str, Set[str]] = defaultdict(set)
    unique_cdn_ips: MutableMapping[str, Set[str]] = defaultdict(set)
    unique_cdn_streams: MutableMapping[str, Set[str]] = defaultdict(set)
    country_ips: MutableMapping[Tuple[str, str], Set[str]] = defaultdict(set)
    country_streams: MutableMapping[Tuple[str, str], Set[str]] = defaultdict(set)
    region_ips: MutableMapping[Tuple[str, str, str], Set[str]] = defaultdict(set)
    region_streams: MutableMapping[Tuple[str, str, str], Set[str]] = defaultdict(set)
    time_buckets: MutableMapping[str, Counter] = defaultdict(Counter)
    daily_cdn_counts = Counter()
    country_cdn_counts = Counter()
    all_ips: Set[str] = set()
    all_streams: Set[str] = set()
    min_timestamp: Optional[int] = None
    max_timestamp: Optional[int] = None
    total_rows = 0
    geo_rows = 0

    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            total_rows += 1
            ip = row.get("client_ip", "")
            stream = row.get("stream_name", "")
            cdn_name = display(row.get("cdn_name"))
            cdn_isp = display(row.get("cdn_isp"))
            country_key = ("Unknown", "", "Unknown")
            location = geo_cache.get(ip, {})
            if location.get("status") == "success":
                geo_rows += 1
                country_key = (
                    display(location.get("country")),
                    location.get("countryCode", ""),
                    display(location.get("regionName")),
                )

            counts[("cdn_name", cdn_name)] += 1
            counts[("cdn_isp", cdn_isp)] += 1
            counts[("country", country_key[0], country_key[1])] += 1
            counts[("region", *country_key)] += 1
            unique_ips[f"cdn_name::{cdn_name}"].add(ip)
            unique_streams[f"cdn_name::{cdn_name}"].add(stream)
            unique_cdn_ips[cdn_isp].add(ip)
            unique_cdn_streams[cdn_isp].add(stream)
            country_ips[(country_key[0], country_key[1])].add(ip)
            country_streams[(country_key[0], country_key[1])].add(stream)
            region_ips[country_key].add(ip)
            region_streams[country_key].add(stream)
            country_cdn_counts[(country_key[0], country_key[1], cdn_name)] += 1
            if ip:
                all_ips.add(ip)
            if stream:
                all_streams.add(stream)

            try:
                timestamp = int(row["timestamp"])
                min_timestamp = timestamp if min_timestamp is None else min(min_timestamp, timestamp)
                max_timestamp = timestamp if max_timestamp is None else max(max_timestamp, timestamp)
                bucket = datetime.fromtimestamp(timestamp / 1000, timezone.utc).strftime("%Y-%m-%d")
                time_buckets["daily"][bucket] += 1
                daily_cdn_counts[(bucket, cdn_name)] += 1
            except (KeyError, TypeError, ValueError):
                pass

    total = total_rows or 1
    cdn_name_rows = []
    for (kind, name), rows in sorted(
        ((key, value) for key, value in counts.items() if key[0] == "cdn_name"),
        key=lambda item: item[1],
        reverse=True,
    ):
        cdn_name_rows.append(
            {
                "cdn_name": name,
                "rows": rows,
                "share_pct": round(rows / total * 100, 4),
                "unique_client_ips": len(unique_ips[f"cdn_name::{name}"]),
                "unique_client_ip_share_pct": round(
                    len(unique_ips[f"cdn_name::{name}"]) / (len(all_ips) or 1) * 100, 4
                ),
                "unique_streams": len(unique_streams[f"cdn_name::{name}"]),
                "unique_stream_share_pct": round(
                    len(unique_streams[f"cdn_name::{name}"]) / (len(all_streams) or 1) * 100, 4
                ),
            }
        )

    cdn_isp_rows = []
    for (kind, name), rows in sorted(
        ((key, value) for key, value in counts.items() if key[0] == "cdn_isp"),
        key=lambda item: item[1],
        reverse=True,
    ):
        cdn_isp_rows.append(
            {
                "cdn_isp": name,
                "rows": rows,
                "share_pct": round(rows / total * 100, 4),
                "unique_client_ips": len(unique_cdn_ips[name]),
                "unique_client_ip_share_pct": round(
                    len(unique_cdn_ips[name]) / (len(all_ips) or 1) * 100, 4
                ),
                "unique_streams": len(unique_cdn_streams[name]),
                "unique_stream_share_pct": round(
                    len(unique_cdn_streams[name]) / (len(all_streams) or 1) * 100, 4
                ),
            }
        )

    country_rows = []
    country_items = sorted(
        ((key, value) for key, value in counts.items() if key[0] == "country"),
        key=lambda item: item[1],
        reverse=True,
    )
    for (_, country, code), rows in country_items:
        country_rows.append(
            {
                "country": country,
                "country_code": code,
                "rows": rows,
                "share_pct": round(rows / total * 100, 4),
                "unique_client_ips": len(country_ips[(country, code)]),
                "unique_client_ip_share_pct": round(
                    len(country_ips[(country, code)]) / (len(all_ips) or 1) * 100, 4
                ),
                "unique_streams": len(country_streams[(country, code)]),
            }
        )

    region_rows = []
    region_items = sorted(
        ((key, value) for key, value in counts.items() if key[0] == "region"),
        key=lambda item: item[1],
        reverse=True,
    )
    for (_, country, code, region), rows in region_items:
        region_rows.append(
            {
                "country": country,
                "country_code": code,
                "region": region,
                "rows": rows,
                "share_pct": round(rows / total * 100, 4),
                "unique_client_ips": len(region_ips[(country, code, region)]),
                "unique_client_ip_share_pct": round(
                    len(region_ips[(country, code, region)]) / (len(all_ips) or 1) * 100, 4
                ),
                "unique_streams": len(region_streams[(country, code, region)]),
            }
        )

    daily_rows = [
        {"date_utc": date, "rows": rows, "share_pct": round(rows / total * 100, 4)}
        for date, rows in sorted(time_buckets["daily"].items())
    ]
    daily_cdn_rows = []
    for (date, cdn_name), rows in sorted(daily_cdn_counts.items()):
        daily_total = time_buckets["daily"][date]
        daily_cdn_rows.append(
            {
                "date_utc": date,
                "cdn_name": cdn_name,
                "rows": rows,
                "daily_share_pct": round(rows / daily_total * 100, 4),
            }
        )
    country_cdn_rows = []
    for (country, code, cdn_name), rows in sorted(
        country_cdn_counts.items(), key=lambda item: item[1], reverse=True
    ):
        country_total = counts[("country", country, code)]
        country_cdn_rows.append(
            {
                "country": country,
                "country_code": code,
                "cdn_name": cdn_name,
                "rows": rows,
                "country_share_pct": round(rows / country_total * 100, 4),
                "global_share_pct": round(rows / total * 100, 4),
            }
        )
    common_fields = [
        "rows", "share_pct", "unique_client_ips", "unique_client_ip_share_pct",
        "unique_streams", "unique_stream_share_pct",
    ]
    write_distribution(
        output_dir / "cdn_name_distribution.csv",
        cdn_name_rows,
        ["cdn_name", *common_fields],
    )
    write_distribution(
        output_dir / "cdn_isp_distribution.csv",
        cdn_isp_rows,
        ["cdn_isp", *common_fields],
    )
    write_distribution(
        output_dir / "country_distribution.csv",
        country_rows,
        [
            "country", "country_code", "rows", "share_pct", "unique_client_ips",
            "unique_client_ip_share_pct", "unique_streams",
        ],
    )
    write_distribution(
        output_dir / "region_distribution.csv",
        region_rows,
        [
            "country", "country_code", "region", "rows", "share_pct",
            "unique_client_ips", "unique_client_ip_share_pct", "unique_streams",
        ],
    )
    write_distribution(output_dir / "daily_volume.csv", daily_rows, ["date_utc", "rows", "share_pct"])
    write_distribution(
        output_dir / "daily_cdn_distribution.csv",
        daily_cdn_rows,
        ["date_utc", "cdn_name", "rows", "daily_share_pct"],
    )
    write_distribution(
        output_dir / "country_cdn_distribution.csv",
        country_cdn_rows,
        [
            "country", "country_code", "cdn_name", "rows", "country_share_pct",
            "global_share_pct",
        ],
    )

    return {
        "source": str(source),
        "rows": total_rows,
        "unique_client_ips": len(all_ips),
        "unique_streams": len(all_streams),
        "geolocated_rows": geo_rows,
        "geolocated_row_share_pct": round(geo_rows / total * 100, 4),
        "unique_cdn_names": len(cdn_name_rows),
        "unique_cdn_isps": sum(row["cdn_isp"] != "Unknown" for row in cdn_isp_rows),
        "missing_cdn_isp_rows": counts[("cdn_isp", "Unknown")],
        "min_timestamp_utc": datetime.fromtimestamp(min_timestamp / 1000, timezone.utc).isoformat() if min_timestamp else None,
        "max_timestamp_utc": datetime.fromtimestamp(max_timestamp / 1000, timezone.utc).isoformat() if max_timestamp else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="online push-stream CSV export")
    parser.add_argument("--output-dir", type=Path, default=Path("online/analysis"))
    parser.add_argument("--geo-cache", type=Path, help="IP geolocation JSON cache")
    args = parser.parse_args()
    summary = summarize(args.source, args.output_dir, load_geo_cache(args.geo_cache))
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
