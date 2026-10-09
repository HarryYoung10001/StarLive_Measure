#!/usr/bin/env python3
"""Small, dependency-free readers shared by the measurement tools."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def find_runs(path: Path) -> list[Path]:
    if (path / "manifest.json").is_file():
        return [path]
    return sorted(item.parent for item in path.rglob("manifest.json"))


def read_kv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def load_manifest(run: Path) -> dict[str, Any]:
    value = load_json(run / "manifest.json", {})
    return value if isinstance(value, dict) else {}


def load_profile(run: Path) -> dict[str, Any]:
    value = load_json(run / "profile.json", {})
    return value if isinstance(value, dict) else {}


def duration_ms(run: Path) -> float | None:
    profile = load_profile(run)
    try:
        value = float(profile.get("workload", {}).get("duration_s")) * 1000.0
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def payload_bytes(run: Path) -> int | None:
    profile = load_profile(run)
    try:
        value = int(profile.get("workload", {}).get("payload_bytes", 0) or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    for candidate in run.rglob("*summary.txt"):
        values = read_kv(candidate)
        try:
            value = int(values.get("payload_bytes", values.get("udp_payload_len", 0)) or 0)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return None


def precise_rows(run: Path) -> list[dict[str, str]]:
    path = run / "derived" / "udp_arrival_time_precise.tsv"
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def latency_rows(run: Path) -> list[tuple[float, float]]:
    precise = run / "derived" / "delay_precise.tsv"
    if precise.is_file():
        with precise.open(encoding="utf-8", newline="") as handle:
            return [
                (float(row["window_start_ms"]), float(row["mean_delay_ms"]))
                for row in csv.DictReader(handle, delimiter="\t")
            ]
    values_path = run / "derived" / "delay.txt"
    times_path = run / "derived" / "delay_window_time_ms.txt"
    if not values_path.is_file() or not times_path.is_file():
        return []
    values = [float(line) for line in values_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    times = [float(line) for line in times_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return list(zip(times, values))


def arrival_events(run: Path) -> list[tuple[float, int]]:
    rows = precise_rows(run)
    if rows:
        return [(float(row["receive_rel_s"]) * 1000.0, int(row["payload_bytes"])) for row in rows]
    path = run / "derived" / "udp_arrival_time_ms.txt"
    payload = payload_bytes(run)
    if not path.is_file() or not payload:
        return []
    return [(float(line), payload) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

