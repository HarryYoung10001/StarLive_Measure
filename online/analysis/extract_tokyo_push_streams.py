#!/usr/bin/env python3
"""Extract raw push-stream rows whose client IP geolocates to Tokyo."""
from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Mapping
from urllib.request import Request, urlopen

URL = "http://ip-api.com/batch?fields=status,country,countryCode,regionName,city,query"
BATCH = 100
SOURCE = Path("online/data/281475060019287-temp_page-push_stream-查询24.csv")
OUTPUT = Path("online/data/tokyo_push_streams.csv")
CACHE = Path("online/analysis/.ip_geolocation_cache.json")

def ips_from_csv(path: Path) -> list[str]:
    result: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = (row.get("client_ip") or "").strip()
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_global:
                result.add(value)
    return sorted(result)

def load_cache(path: Path) -> dict[str, Mapping[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    return {str(k): v for k, v in value.items() if isinstance(v, dict)}

def save_cache(path: Path, cache: Mapping[str, Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".geo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, sort_keys=True)
        os.replace(temp, path)
    except BaseException:
        os.unlink(temp)
        raise

def geocode_batch(batch: list[str]) -> list[dict[str, str]]:
    request = Request(URL, data=json.dumps(batch).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=45) as response:
        values = json.load(response)
    if not isinstance(values, list) or len(values) != len(batch):
        raise RuntimeError("unexpected geolocation response")
    return [value for value in values if isinstance(value, dict)]

def geocode(ips: list[str], path: Path, pause: float) -> dict[str, Mapping[str, str]]:
    cache = load_cache(path)
    pending = [ip for ip in ips if ip not in cache]
    for start in range(0, len(pending), BATCH):
        batch = pending[start:start + BATCH]
        for attempt in range(4):
            try:
                values = geocode_batch(batch)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        for value in values:
            if value.get("query"):
                cache[value["query"]] = value
        save_cache(path, cache)
        print(f"geolocated {min(start + BATCH, len(pending))}/{len(pending)}")
        if pause and start + BATCH < len(pending):
            time.sleep(pause)
    return cache

def extract(source: Path, output: Path, cache_path: Path, pause: float) -> tuple[int, int, int]:
    ips = ips_from_csv(source)
    cache = geocode(ips, cache_path, pause)
    tokyo = {ip for ip in ips if cache.get(ip, {}).get("status") == "success"
             and cache[ip].get("countryCode") == "JP"
             and cache[ip].get("regionName") == "Tokyo"}
    temp = output.with_suffix(output.suffix + ".tmp")
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with source.open(encoding="utf-8-sig", newline="") as source_handle, temp.open("w", encoding="utf-8", newline="") as output_handle:
        reader = csv.DictReader(source_handle)
        fields = [*reader.fieldnames, "geo_country", "geo_country_code", "geo_region", "geo_city"]
        writer = csv.DictWriter(output_handle, fieldnames=fields)
        writer.writeheader()
        for row in reader:
            ip = (row.get("client_ip") or "").strip()
            if ip not in tokyo:
                continue
            location = cache[ip]
            writer.writerow({**row, "geo_country": location.get("country", ""),
                             "geo_country_code": location.get("countryCode", ""),
                             "geo_region": location.get("regionName", ""),
                             "geo_city": location.get("city", "")})
            rows += 1
    os.replace(temp, output)
    failures = sum(cache.get(ip, {}).get("status") != "success" for ip in ips)
    return rows, len(tokyo), failures

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--pause", type=float, default=1.0)
    args = parser.parse_args()
    rows, ips, failures = extract(args.source, args.output, args.cache, args.pause)
    print(f"Wrote {args.output}: {rows} rows, {ips} Tokyo IPs, {failures} geolocation failures")

if __name__ == "__main__":
    main()
