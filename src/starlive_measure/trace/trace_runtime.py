#!/usr/bin/env python3
"""Shared lifecycle, profile validation, and artifact handling for traces.

This module intentionally contains no experiment-specific shell snippets.  A
workload is described by a profile and is executed through direct ADB/SSH
commands or the stable server helper.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
from typing import Any, Iterable

from .PcapBandwidthTrace import process_run_pcap
from starlive_measure.paths import ANDROID_ROOT, BUILD_ROOT, REPO_ROOT, SERVER_ROOT, remote_server_repo


ROOT = REPO_ROOT
DEFAULT_SERVER_REPO = remote_server_repo()
SERVER_HELPER = SERVER_ROOT / "trace_server.py"
PCAP_PROCESSOR = Path(__file__).with_name("PcapBandwidthTrace.py")
COMPACT_PROCESSOR = Path(__file__).with_name("CompactUDPTrace.py")
BANDWIDTH_TRACE = Path(__file__).with_name("BandwidthTrace.py")
BANDWIDTH_PLOT = Path(__file__).with_name("BandwidthTimeScatter.py")
DELAY_PLOT = Path(__file__).with_name("DelayTimeScatter.py")
RATE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:[KMG](?:[bB][pP][sS])?|pps)?$")
ROLES = {"udp_sender", "udp_receiver", "udp_echo_phone", "icmp_phone"}
SSH_COMMAND_TIMEOUT_S = 60
SSH_UPLOAD_TIMEOUT_S = 180
SSH_ARTIFACT_TIMEOUT_S = 600


class ProfileError(ValueError):
    """Raised when a profile or override is invalid."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return cleaned.strip("._-") or "trace"


def sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_profile(path: Path) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"cannot read profile {path}: {error}") from error
    if not isinstance(profile, dict):
        raise ProfileError("profile root must be an object")
    validate_profile(profile)
    profile["_source"] = str(path.resolve())
    profile["_hash"] = sha256_json({key: value for key, value in profile.items() if not key.startswith("_")})
    return profile


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ProfileError(f"{label} must be numeric") from error
    if not math.isfinite(number) or number <= 0:
        raise ProfileError(f"{label} must be positive")
    return number


def validate_profile(profile: dict[str, Any]) -> None:
    if profile.get("schema_version") in {2, 3}:
        # Root profiles have a deliberately separate contract, but allowing
        # the shared loader to validate them keeps profile discovery tools
        # backwards compatible without changing v1 execution semantics.
        from starlive_measure.root.runtime import validate_root_profile

        validate_root_profile(profile)
        return
    if profile.get("schema_version") != 1:
        raise ProfileError("schema_version must be 1")
    name = profile.get("name")
    if not isinstance(name, str) or not name or not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ProfileError("name must contain only letters, digits, dot, underscore, and hyphen")
    workload = profile.get("workload")
    if not isinstance(workload, dict):
        raise ProfileError("workload must be an object")
    kind = workload.get("kind")
    direction = workload.get("direction")
    if kind not in {"iperf3", "udp_flow", "udp_echo_rtt", "icmp"}:
        raise ProfileError("workload.kind must be iperf3, udp_flow, udp_echo_rtt, or icmp")
    if direction not in {"uplink", "downlink", "none"}:
        raise ProfileError("workload.direction must be uplink, downlink, or none")
    if kind == "icmp" and direction != "none":
        raise ProfileError("icmp workload direction must be none")
    if kind != "icmp" and direction == "none":
        raise ProfileError("non-ICMP workload requires uplink or downlink direction")
    _positive_number(workload.get("duration_s", 0), "workload.duration_s")
    if kind == "udp_echo_rtt":
        _positive_number(workload.get("payload_bytes", 0), "workload.payload_bytes")
        _positive_number(workload.get("interval_s", 0), "workload.interval_s")
        _positive_number(workload.get("timeout_ms", 0), "workload.timeout_ms")
    elif kind != "icmp":
        rate = workload.get("rate")
        if not isinstance(rate, str) or not RATE_RE.fullmatch(rate):
            raise ProfileError("workload.rate has an invalid format")
        _positive_number(workload.get("payload_bytes", 0), "workload.payload_bytes")
        _positive_number(workload.get("interval_s", 0), "workload.interval_s")
        if workload.get("postprocess", "arrival") not in {"arrival", "latency", "both"}:
            raise ProfileError("workload.postprocess must be arrival, latency, or both")
    roles = profile.get("roles")
    if not isinstance(roles, dict):
        raise ProfileError("roles must be an object")
    for key in ("required", "optional"):
        values = roles.get(key, [])
        if not isinstance(values, list) or any(value not in ROLES for value in values):
            raise ProfileError(f"roles.{key} contains an unknown role")
    required = set(roles.get("required", []))
    optional = set(roles.get("optional", []))
    if required & optional:
        raise ProfileError("a role cannot be both required and optional")
    if kind == "icmp" and "icmp_phone" not in required:
        raise ProfileError("icmp profile must require icmp_phone")
    if kind == "udp_echo_rtt" and "udp_echo_phone" not in required:
        raise ProfileError("udp_echo_rtt profile must require udp_echo_phone")
    if kind not in {"icmp", "udp_echo_rtt"} and "udp_sender" not in required and "udp_receiver" not in required:
        raise ProfileError("non-ICMP profile must require udp_sender or udp_receiver")
    icmp = profile.get("icmp", {})
    if not isinstance(icmp, dict):
        raise ProfileError("icmp must be an object")
    if icmp.get("enabled", False):
        _positive_number(icmp.get("interval_s", workload.get("interval_s", 0)), "icmp.interval_s")
    server = profile.get("server", {})
    if not isinstance(server, dict):
        raise ProfileError("server must be an object")
    try:
        port = int(server.get("port", 0))
    except (TypeError, ValueError) as error:
        raise ProfileError("server.port must be an integer") from error
    if not (1 <= port <= 65535):
        raise ProfileError("server.port must be in 1..65535")
    tcpdump = server.get("tcpdump", {})
    if not isinstance(tcpdump, dict):
        raise ProfileError("server.tcpdump must be an object")
    if tcpdump.get("enabled", False) and direction != "uplink":
        raise ProfileError("tcpdump is only supported for uplink profiles")


def apply_overrides(
    profile: dict[str, Any],
    *,
    rate: str | None = None,
    duration: float | None = None,
    interval: float | None = None,
    port: int | None = None,
    payload: int | None = None,
    window_ms: float | None = None,
    no_icmp: bool = False,
) -> dict[str, Any]:
    result = copy.deepcopy({key: value for key, value in profile.items() if not key.startswith("_")})
    workload = result["workload"]
    if rate is not None:
        workload["rate"] = rate
    if duration is not None:
        workload["duration_s"] = duration
    if interval is not None:
        workload["interval_s"] = interval
    if payload is not None:
        workload["payload_bytes"] = payload
    if window_ms is not None:
        workload["window_ms"] = window_ms
    if port is not None:
        result.setdefault("server", {})["port"] = port
    if no_icmp:
        if result["workload"]["kind"] == "icmp":
            raise ProfileError("--no-icmp cannot disable an icmp-only profile")
        result.setdefault("icmp", {})["enabled"] = False
    validate_profile(result)
    result["_hash"] = sha256_json(result)
    return result


def parse_roles(values: Iterable[str]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ProfileError(f"--role must be ROLE=SERIAL: {value}")
        role, serial = value.split("=", 1)
        if role not in ROLES or not serial:
            raise ProfileError(f"invalid role mapping: {value}")
        if role in roles:
            raise ProfileError(f"duplicate role mapping: {role}")
        roles[role] = serial
    return roles


def validate_roles(profile: dict[str, Any], roles: dict[str, str]) -> None:
    required = profile["roles"].get("required", [])
    for role in required:
        if role not in roles:
            raise ProfileError(f"required role is missing: {role}")
    if profile.get("icmp", {}).get("enabled") and "icmp_phone" not in roles:
        raise ProfileError("icmp is enabled but --role icmp_phone=SERIAL is missing")
    device_values = list(roles.values())
    if len(device_values) != len(set(device_values)):
        raise ProfileError("ADB role serials must be distinct")


def make_run_paths(session_dir: Path, profile: dict[str, Any], index: int | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffix = f"_{index:03d}" if index is not None else ""
    base = f"{safe_name(profile['name'])}_{stamp}{suffix}"
    run_dir = session_dir / base
    collision = 1
    while run_dir.exists():
        run_dir = session_dir / f"{base}_{collision:02d}"
        collision += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    for path in (run_dir / "roles" / "server", run_dir / "roles" / "udp_sender", run_dir / "roles" / "udp_receiver", run_dir / "roles" / "udp_echo_phone", run_dir / "roles" / "icmp_phone", run_dir / "raw", run_dir / "derived"):
        path.mkdir(parents=True, exist_ok=True)
    return run_dir


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_event(run_dir: Path, event: str, **fields: object) -> None:
    row = {"timestamp_utc": utc_now(), "event": event, **fields}
    with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


class RunState:
    def __init__(
        self,
        run_dir: Path,
        profile: dict[str, Any],
        server_ip: str,
        server_ssh: str,
        roles: dict[str, str],
        environment: dict[str, Any] | None = None,
        collection_mode: str = "trace",
    ) -> None:
        self.run_dir = run_dir
        self.profile = profile
        self.server_ip = server_ip
        self.server_ssh = server_ssh
        self.roles = roles
        self.collection_mode = collection_mode
        self.manifest: dict[str, Any] = {
            "schema_version": 1,
            "collection_mode": collection_mode,
            "time_precision": "nanosecond" if collection_mode == "measurement" else "integer_ms",
            "rounding": "none" if collection_mode == "measurement" else "half_away_for_delay_floor_for_arrival",
            "run_id": run_dir.name,
            "profile": profile["name"],
            "profile_hash": profile.get("_hash", sha256_json(profile)),
            "server": {"ip": server_ip, "ssh": server_ssh, "repo": profile.get("_server_repo", DEFAULT_SERVER_REPO), "port": profile["server"]["port"]},
            "roles": roles,
            "environment": environment or {},
            "workload": profile["workload"],
            "icmp": {"enabled": bool(profile.get("icmp", {}).get("enabled", False)), "status": "pending"},
            "status": "preflight",
            "created_utc": utc_now(),
            "artifacts": {},
            "commands": [],
        }
        write_json(run_dir / "manifest.json", self.manifest)
        write_json(run_dir / "profile.json", {key: value for key, value in profile.items() if not key.startswith("_")})
        append_event(run_dir, "created", profile=profile["name"])

    def update(self, **values: object) -> None:
        self.manifest.update(values)
        write_json(self.run_dir / "manifest.json", self.manifest)

    def command(self, label: str, command: list[str]) -> None:
        self.manifest["commands"].append({"label": label, "argv": command})
        write_json(self.run_dir / "manifest.json", self.manifest)


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def adb_devices() -> set[str]:
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=False)
    return {line.split()[0] for line in result.stdout.splitlines() if len(line.split()) >= 2 and line.split()[1] == "device"}


def parse_wifi_info(text: str) -> dict[str, str]:
    """Extract the stable Android Wi-Fi identity fields from dumpsys output."""

    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        match = re.search(r"\bSSID:\s*([^,]+)", stripped, re.IGNORECASE)
        if match and "ssid" not in values:
            values["ssid"] = match.group(1).strip().strip('"')
        match = re.search(r"\bBSSID:\s*([^,]+)", stripped, re.IGNORECASE)
        if match and "bssid" not in values:
            values["bssid"] = match.group(1).strip().strip('"').lower()
        match = re.search(r"\b(?:RSSI|Rssi):\s*(-?\d+)", stripped)
        if match and "rssi" not in values:
            values["rssi"] = match.group(1)
        match = re.search(r"\b(?:Link speed|LinkSpeed):\s*([0-9.]+)", stripped, re.IGNORECASE)
        if match and "link_speed_mbps" not in values:
            values["link_speed_mbps"] = match.group(1)
    return values


def parse_route_info(text: str) -> dict[str, str]:
    """Parse the gateway and interface from `ip route get` output."""

    values: dict[str, str] = {}
    match = re.search(r"(?:^|\s)via\s+(\S+)", text)
    if match:
        values["gateway"] = match.group(1)
    match = re.search(r"(?:^|\s)dev\s+(\S+)", text)
    if match:
        values["interface"] = match.group(1)
    match = re.search(r"(?:^|\s)src\s+(\S+)", text)
    if match:
        values["source"] = match.group(1)
    return values


def _adb_shell(serial: str, command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", "-s", serial, "shell", *command],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def network_guard_snapshot(
    serial: str,
    server_ip: str,
    server_ssh: str,
    server_repo: str,
    expected: dict[str, str],
) -> dict[str, Any]:
    """Return a machine-readable pre/post measurement network identity check."""

    snapshot: dict[str, Any] = {
        "timestamp_utc": utc_now(),
        "serial": serial,
        "expected": expected,
        "reasons": [],
        "status": "failed",
    }
    try:
        model = _adb_shell(serial, ["getprop", "ro.product.model"])
        wifi = _adb_shell(serial, ["dumpsys", "wifi"])
        route = _adb_shell(serial, ["ip", "route", "get", server_ip])
    except (OSError, subprocess.SubprocessError) as error:
        snapshot["reasons"].append(f"adb query failed: {error}")
        return snapshot
    snapshot["model"] = model.stdout.strip()
    snapshot["wifi"] = parse_wifi_info(wifi.stdout)
    snapshot["route"] = parse_route_info(route.stdout)
    if model.returncode != 0:
        snapshot["reasons"].append("model query failed")
    if wifi.returncode != 0:
        snapshot["reasons"].append("wifi query failed")
    if route.returncode != 0:
        snapshot["reasons"].append("route query failed")
    checks = {
        "device_model": (snapshot.get("model"), expected.get("device_model")),
        "wifi_ssid": (snapshot.get("wifi", {}).get("ssid"), expected.get("wifi_ssid")),
        "wifi_bssid": (snapshot.get("wifi", {}).get("bssid"), expected.get("wifi_bssid", "").lower()),
        "route_interface": (snapshot.get("route", {}).get("interface"), expected.get("route_interface")),
        "route_gateway": (snapshot.get("route", {}).get("gateway"), expected.get("route_gateway")),
    }
    for name, (actual, wanted) in checks.items():
        if wanted and actual != wanted:
            snapshot["reasons"].append(f"{name}: expected {wanted!r}, got {actual!r}")
    try:
        server = subprocess.run(
            remote_command(server_ssh, ["sh", "-c", (
                "command -v python3 >/dev/null && test -d {repo} && "
                "! ss -Hln '( sport = :5201 )' | grep -q . && "
                "! ps -eo args= | grep -E '[t]race_server.py|[u]plink_udp_receiver|[u]plink_udp_sender' | grep -q ."
            ).format(repo=shlex.quote(server_repo))]),
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_COMMAND_TIMEOUT_S,
        )
        snapshot["server_check"] = {"returncode": server.returncode, "stderr": server.stderr.strip()}
        if server.returncode != 0:
            snapshot["reasons"].append("server dependency, port, or residual-process check failed")
    except (OSError, subprocess.SubprocessError) as error:
        snapshot["reasons"].append(f"server query failed: {error}")
    snapshot["status"] = "ok" if not snapshot["reasons"] else "mismatch"
    return snapshot


def network_guard_config(args: Any) -> dict[str, str]:
    values = {
        "device_model": getattr(args, "expected_device_model", None),
        "wifi_ssid": getattr(args, "expected_wifi_ssid", None),
        "wifi_bssid": getattr(args, "expected_wifi_bssid", None),
        "route_interface": getattr(args, "expected_route_interface", None),
        "route_gateway": getattr(args, "expected_route_gateway", None),
    }
    return {key: value for key, value in values.items() if value}


def latency_statistics(values: Iterable[float]) -> dict[str, float | int]:
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return {"valid_rows": 0, "mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    def percentile(fraction: float) -> float:
        position = (len(numbers) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return numbers[lower]
        return numbers[lower] + (numbers[upper] - numbers[lower]) * (position - lower)
    return {
        "valid_rows": len(numbers),
        "mean_ms": statistics.fmean(numbers),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": numbers[-1],
    }


def _read_kv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    return values


def augment_custom_udp_summary(state: RunState) -> None:
    derived = state.run_dir / "derived"
    summary_path = derived / "custom_udp_trace_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(summary, dict):
        return
    try:
        delay_values = [float(line.strip()) for line in (derived / "delay.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        delay_values = []
    try:
        time_values = [float(line.strip()) for line in (derived / "delay_window_time_ms.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        time_values = []
    stats = latency_statistics(delay_values)
    stats["valid_rows"] = int(summary.get("latency", {}).get("valid_delay_rows", stats["valid_rows"]) or stats["valid_rows"])
    stats["window_count"] = len(time_values)
    stats["window_start_ms"] = min(time_values) if time_values else 0.0
    stats["window_end_ms"] = max(time_values) if time_values else 0.0
    summary["latency_stats"] = stats
    sender = _read_kv(state.run_dir / "roles" / "udp_sender" / "uplink_udp_sender_summary.txt")
    requested_rate = str(state.profile["workload"].get("rate", ""))
    try:
        actual_mbps = float(sender.get("actual_payload_mbps", 0) or 0)
    except ValueError:
        actual_mbps = 0.0
    payload_bytes = int(state.profile["workload"].get("payload_bytes", 0) or 0)
    rate_mbps = parse_rate_mbps(requested_rate, payload_bytes)
    requested_pps = parse_rate_pps(requested_rate, payload_bytes)
    latency_summary = summary.get("latency", {})
    if isinstance(latency_summary, dict):
        stats["observed_window_count"] = int(latency_summary.get("observed_delay_windows", 0) or 0)
        stats["interpolated_window_count"] = int(latency_summary.get("interpolated_delay_windows", 0) or 0)
    sender_info = {
        "actual_payload_mbps": actual_mbps,
        "pps": float(sender.get("pps", 0) or 0),
        "requested_pps": requested_pps,
        "requested_payload_mbps": rate_mbps,
        "sent_packets": int(float(sender.get("sent_packets", 0) or 0)),
        "done_ack": sender.get("done_ack", "false").lower() == "true",
        "clock_sync_samples": int(float(sender.get("clock_sync_samples", 0) or 0)),
        "sender_limited": bool(rate_mbps and actual_mbps < rate_mbps * 0.9),
    }
    receiver_duration_s = max(float(summary.get("capture_duration_ms", 0.0) or 0.0) / 1000.0, 0.001)
    receiver_bytes = int(summary.get("payload_bytes", 0) or 0)
    summary["sender"] = sender_info
    summary["receiver"] = {
        "unique_packet_count": int(summary.get("unique_packet_count", 0) or 0),
        "payload_bytes": receiver_bytes,
        "payload_mbps": receiver_bytes * 8 / receiver_duration_s / 1e6,
        "capture_duration_ms": float(summary.get("capture_duration_ms", 0.0) or 0.0),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state.manifest.setdefault("custom_udp", {})["summary"] = summary
    state.manifest["custom_udp"]["latency_stats"] = stats
    state.manifest["custom_udp"]["sender"] = sender_info
    state.manifest["custom_udp"]["receiver"]["payload_bytes"] = receiver_bytes
    state.manifest["custom_udp"]["receiver"]["payload_mbps"] = summary["receiver"]["payload_mbps"]
    state.update()


def parse_rate_pps(rate: str, payload_bytes: int) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:(pps)|([KMG])?[bB]?(?:ps)?)", str(rate), re.IGNORECASE)
    if not match:
        return 0.0
    value = float(match.group(1))
    if match.group(2):
        return value
    if payload_bytes <= 0:
        return 0.0
    suffix = (match.group(3) or "").upper()
    bits_per_second = value * {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}[suffix]
    return bits_per_second / (payload_bytes * 8)


def parse_rate_mbps(rate: str, payload_bytes: int = 0) -> float:
    pps_match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)pps", str(rate), re.IGNORECASE)
    if pps_match:
        return float(pps_match.group(1)) * payload_bytes * 8 / 1e6
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMG])?[bB]?(?:ps)?", str(rate), re.IGNORECASE)
    if not match:
        return 0.0
    value = float(match.group(1))
    multiplier = {"K": 1e-3, "M": 1.0, "G": 1e3}.get((match.group(2) or "").upper(), 1e-6)
    return value * multiplier


def delay_trace_is_contiguous(run_dir: Path, window_ms: float) -> bool:
    try:
        delays = [line.strip() for line in (run_dir / "derived" / "delay.txt").read_text(encoding="utf-8").splitlines()]
        times = [float(line.strip()) for line in (run_dir / "derived" / "delay_window_time_ms.txt").read_text(encoding="utf-8").splitlines()]
    except (OSError, ValueError):
        return False
    if not delays or len(delays) != len(times) or any(not value for value in delays):
        return False
    return all(math.isclose(current - previous, window_ms, abs_tol=1e-6) for previous, current in zip(times, times[1:]))


def validate_custom_run_success(run_dir: Path, *, min_span_s: float | None = None, max_span_s: float | None = None) -> bool:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        summary = json.loads((run_dir / "derived" / "custom_udp_trace_summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("status") != "complete" or manifest.get("custom_udp", {}).get("status") != "complete":
        return False
    if manifest.get("artifacts", {}).get("server_pull_rc") != 0 or manifest.get("artifacts", {}).get("phone_pull_rc") != 0:
        return False
    required = ("udp_arrival_time_ms.txt", "delay.txt", "delay_window_time_ms.txt", "delay_summary.txt", "bandwidth_trace.tsv", "time_delay_scatter.png")
    if manifest.get("workload", {}).get("direction") == "downlink":
        required += ("bandwidth_time_scatter.png",)
    if any(not (run_dir / "derived" / name).is_file() or (run_dir / "derived" / name).stat().st_size == 0 for name in required):
        return False
    latency = summary.get("latency", {})
    window_ms = float(latency.get("window_ms", 0) or 0) if isinstance(latency, dict) else 0.0
    if window_ms <= 0 or not delay_trace_is_contiguous(run_dir, window_ms):
        return False
    custom = manifest.get("custom_udp", {})
    if manifest.get("workload", {}).get("direction") == "downlink":
        if custom.get("phone_utrc", {}).get("transfer") != "processed_on_device":
            return False
        receiver = custom.get("receiver", {})
        sender = custom.get("sender", {})
        if int(custom.get("clock_sync_samples", 0) or 0) <= 0 or int(receiver.get("unique_packet_count", 0) or 0) <= 0 or int(receiver.get("payload_bytes", 0) or 0) <= 0 or not sender.get("done_ack") or int(sender.get("sent_packets", 0) or 0) <= 0:
            return False
    else:
        utrc = custom.get("server_utrc", {})
        if utrc.get("transfer") != "remote_only" or not utrc.get("exists") or int(utrc.get("size_bytes", 0) or 0) <= 0:
            return False
        sender = custom.get("sender", {})
        if not sender.get("done_ack") or int(sender.get("clock_sync_samples", 0) or 0) <= 0 or int(sender.get("sent_packets", 0) or 0) <= 0:
            return False
    span = float(summary.get("capture_duration_ms", 0.0) or 0.0) / 1000.0
    if min_span_s is not None and span < min_span_s:
        return False
    if max_span_s is not None and span > max_span_s:
        return False
    return True


def preflight(state: RunState, dry_run: bool, network_guard: dict[str, str] | None = None) -> None:
    state.update(status="preflight")
    profile = state.profile
    if dry_run:
        state.manifest["preflight"] = {
            "mode": "dry-run",
            "status": "skipped",
            "network_guard": {"status": "skipped", "expected": network_guard or {}},
        }
        state.update()
        append_event(state.run_dir, "preflight_skipped", reason="dry-run")
        return
    if not command_available("adb"):
        raise ProfileError("adb is required")
    online = adb_devices()
    missing = sorted(serial for serial in state.roles.values() if serial not in online)
    if missing:
        raise ProfileError(f"ADB devices are not ready: {', '.join(missing)}")
    if network_guard:
        serial = state.roles.get("udp_sender") or state.roles.get("udp_receiver") or state.roles.get("udp_echo_phone")
        snapshot = network_guard_snapshot(serial or "", state.server_ip, state.server_ssh, state.profile.get("_server_repo", DEFAULT_SERVER_REPO), network_guard)
        state.manifest.setdefault("preflight", {})["network_guard"] = snapshot
        state.update()
        if snapshot.get("status") != "ok":
            raise ProfileError("network guard rejected run: " + "; ".join(snapshot.get("reasons", [])))
    if profile["workload"]["kind"] == "icmp":
        state.manifest["preflight"] = {
            "status": "ok", "adb_devices": sorted(online), "server_check": "not_required",
            "network_guard": state.manifest.get("preflight", {}).get("network_guard", {}),
        }
        state.update()
        append_event(state.run_dir, "preflight_ok")
        return
    for name in ("ssh", "scp"):
        if not command_available(name):
            raise ProfileError(f"{name} is required")
    requirements = ["command -v python3 >/dev/null"]
    if profile["workload"]["kind"] == "iperf3":
        requirements.append("command -v iperf3 >/dev/null")
    if profile["server"].get("tcpdump", {}).get("enabled", False):
        requirements.append("command -v tcpdump >/dev/null && tcpdump -D >/dev/null")
    try:
        server_check = subprocess.run(
            [
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                state.server_ssh, " && ".join(requirements),
            ],
            capture_output=True, text=True, check=False, timeout=SSH_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise ProfileError("remote server preflight timed out") from error
    if server_check.returncode != 0:
        raise ProfileError("remote server preflight failed: " + " && ".join(requirements))
    if profile["workload"]["kind"] == "iperf3":
        serial = state.roles["udp_sender" if profile["workload"]["direction"] == "uplink" else "udp_receiver"]
        binary_check = subprocess.run(["adb", "-s", serial, "shell", "test", "-x", "/data/local/tmp/iperf3"], check=False)
        if binary_check.returncode != 0:
            raise ProfileError(f"Android iperf3 is not executable on {serial}")
    state.manifest["preflight"] = {
        "status": "ok", "adb_devices": sorted(online), "server_check": server_check.returncode,
        "network_guard": state.manifest.get("preflight", {}).get("network_guard", {}),
    }
    state.update()
    append_event(state.run_dir, "preflight_ok")


def remote_command(server_ssh: str, words: list[str]) -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
        server_ssh, " ".join(shlex.quote(word) for word in words),
    ]


def scp_remote(server_ssh: str, remote_path: str, local_path: Path) -> int:
    local_path.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [
                "scp", "-O", "-q", "-r", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                f"{server_ssh}:{remote_path}", str(local_path),
            ],
            capture_output=True, text=True, check=False, timeout=SSH_ARTIFACT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return 124
    return result.returncode


def scp_remote_files(server_ssh: str, remote_dir: str, names: list[str], local_dir: Path) -> int:
    """Copy a bounded set of small files without transferring the remote PCAP.

    A multi-source legacy SCP invocation can return success while silently
    omitting one source on this server.  A single tar stream keeps the transfer
    to one SSH session and makes missing required files a hard failure.
    """
    local_dir.mkdir(parents=True, exist_ok=True)
    remote_archive_command = ["tar", "-cf", "-", "-C", remote_dir, *names]
    ssh_command = remote_command(server_ssh, remote_archive_command)
    ssh_process = subprocess.Popen(
        ssh_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert ssh_process.stdout is not None
    tar_process = subprocess.Popen(
        ["tar", "-xf", "-", "-C", str(local_dir)],
        stdin=ssh_process.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ssh_process.stdout.close()
    try:
        tar_stdout, tar_stderr = tar_process.communicate(timeout=SSH_ARTIFACT_TIMEOUT_S)
        ssh_stdout, ssh_stderr = ssh_process.communicate(timeout=SSH_ARTIFACT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        tar_process.kill()
        ssh_process.kill()
        tar_process.wait()
        ssh_process.wait()
        return 124
    if ssh_process.returncode != 0 or tar_process.returncode != 0:
        return ssh_process.returncode or tar_process.returncode or 1
    return 0


def start_process(command: list[str], stdout_path: Path, stderr_path: Path) -> subprocess.Popen[bytes]:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    return subprocess.Popen(command, stdout=stdout, stderr=stderr)


def normalize_iperf_json(path: Path) -> dict[str, Any]:
    """Parse iperf3 JSON even when adb prefixes a transport warning."""
    raw = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    start = raw.find("{")
    if start < 0:
        raise json.JSONDecodeError("iperf3 JSON object not found", raw, 0)
    value, _end = decoder.raw_decode(raw, start)
    if not isinstance(value, dict):
        raise json.JSONDecodeError("iperf3 JSON root is not an object", raw, start)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def wait_remote_ready(server_ssh: str, path: str, timeout_s: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                remote_command(server_ssh, ["test", "-f", path]),
                capture_output=True, check=False, timeout=SSH_COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            return True
        time.sleep(0.5)
    return False


def build_binary(source: Path, goos: str, goarch: str, goarm: str | None = None) -> Path:
    if not command_available("go"):
        raise ProfileError("go is required for UDP flow profiles")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    tag = f"{source.stem}_{goos}_{goarch}{goarm or ''}_{digest}"
    output = BUILD_ROOT / tag
    if output.exists() and os.access(output, os.X_OK):
        return output
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"})
    env["GOCACHE"] = str(BUILD_ROOT / "go-cache")
    if goarm:
        env["GOARM"] = goarm
    command = ["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(output)]
    if source.name == "udp_echo_receiver.go":
        command.extend(["-tags", "udpecho"])
    command.append(str(source))
    result = subprocess.run(command, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ProfileError(f"failed to build {source.name}: {result.stderr.strip()}")
    output.chmod(0o755)
    return output


def android_binary(source: Path, serial: str) -> Path:
    abi = subprocess.run(["adb", "-s", serial, "shell", "getprop", "ro.product.cpu.abilist"], capture_output=True, text=True, check=False).stdout.strip()
    if "arm64-v8a" in abi:
        return build_binary(source, "android", "arm64")
    if "armeabi-v7a" in abi:
        return build_binary(source, "linux", "arm", "7")
    raise ProfileError(f"unsupported Android ABI for {serial}: {abi or 'unknown'}")


def start_icmp(state: RunState) -> subprocess.Popen[bytes] | None:
    if not state.manifest["icmp"]["enabled"]:
        state.manifest["icmp"]["status"] = "disabled"
        state.update()
        return None
    serial = state.roles["icmp_phone"]
    workload = state.profile["workload"]
    duration = float(workload["duration_s"])
    interval = float(state.profile.get("icmp", {}).get("interval_s", workload.get("interval_s", 0.2)))
    count = max(1, math.ceil(duration / interval))
    directory = state.run_dir / "roles" / "icmp_phone"
    command = ["adb", "-s", serial, "shell", "ping", "-D", "-i", str(interval), "-c", str(count), state.server_ip]
    state.command("icmp", command)
    state.manifest["icmp"].update({"status": "running", "count": count, "serial": serial, "interval_s": interval})
    state.update()
    directory.mkdir(parents=True, exist_ok=True)
    return start_process(command, directory / "ping.log", directory / "ping.stderr")


def stop_process(process: subprocess.Popen[bytes] | None, timeout_s: float = 10.0) -> int:
    if process is None:
        return 0
    try:
        return process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            return process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            return process.wait()


def stop_remote_run(server_ssh: str, remote_dir: str) -> None:
    if not remote_dir:
        return
    command = remote_command(
        server_ssh,
        [
            "sh", "-c",
            f"for pid in $(ps -eo pid=,args= | awk -v d={shlex.quote(remote_dir)} '$0 ~ /trace_server.py/ && index($0,d) {{print $1}}'); do kill -TERM $pid 2>/dev/null || true; done",
        ],
    )
    try:
        subprocess.run(command, capture_output=True, check=False, timeout=SSH_COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


def start_server(state: RunState, *, tcpdump: bool, direction: str, command_kind: str = "iperf", extra: list[str] | None = None) -> tuple[subprocess.Popen[bytes], str, Path]:
    server_dir = state.run_dir / "roles" / "server"
    # server_repo is supplied by CLI via the private runtime key.
    remote_repo = state.profile.get("_server_repo", DEFAULT_SERVER_REPO)
    remote_dir = f"{remote_repo}/traces/logs/.trace_runs/{state.run_dir.name}"
    server_dir.mkdir(parents=True, exist_ok=True)
    try:
        mkdir_remote = subprocess.run(
            remote_command(state.server_ssh, ["mkdir", "-p", remote_dir]),
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise ProfileError("timed out creating remote run directory") from error
    if mkdir_remote.returncode != 0:
        raise ProfileError(f"failed to create remote run directory: {mkdir_remote.stderr.strip()}")
    try:
        upload = subprocess.run(
            [
                "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                str(SERVER_HELPER), f"{state.server_ssh}:{remote_dir}/trace_server.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_UPLOAD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as error:
        raise ProfileError("timed out uploading trace_server.py") from error
    if upload.returncode != 0:
        raise ProfileError(f"failed to upload trace_server.py: {upload.stderr.strip()}")
    if command_kind == "iperf":
        words = ["python3", f"{remote_dir}/trace_server.py", "iperf-receiver", "--run-dir", remote_dir, "--port", str(state.profile["server"]["port"]), "--interval", str(state.profile["workload"].get("interval_s", 0.2)), "--direction", direction, "--interface", str(state.profile["server"].get("tcpdump", {}).get("interface", "auto")), "--snaplen", str(state.profile["server"].get("tcpdump", {}).get("snaplen", 256))]
        if tcpdump:
            try:
                upload_processor = subprocess.run(
                    [
                        "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                        "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                        str(PCAP_PROCESSOR), f"{state.server_ssh}:{remote_dir}/PcapBandwidthTrace.py",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=SSH_UPLOAD_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as error:
                raise ProfileError("timed out uploading PcapBandwidthTrace.py") from error
            if upload_processor.returncode != 0:
                raise ProfileError(f"failed to upload PcapBandwidthTrace.py: {upload_processor.stderr.strip()}")
            for helper in (BANDWIDTH_TRACE, BANDWIDTH_PLOT):
                try:
                    upload_helper = subprocess.run(
                        [
                            "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                            str(helper), f"{state.server_ssh}:{remote_dir}/{helper.name}",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=SSH_UPLOAD_TIMEOUT_S,
                    )
                except subprocess.TimeoutExpired as error:
                    raise ProfileError(f"timed out uploading {helper.name}") from error
                if upload_helper.returncode != 0:
                    raise ProfileError(
                        f"failed to upload {helper.name}: {upload_helper.stderr.strip()}"
                    )
            words.append("--tcpdump")
            words.extend([
                "--pcap-processor", f"{remote_dir}/PcapBandwidthTrace.py",
                "--derived-dir", f"{remote_dir}/derived",
                "--duration-ms", str(round(float(state.profile["workload"]["duration_s"]) * 1000)),
                "--payload-bytes", str(state.profile["workload"].get("payload_bytes", 0)),
            ])
    elif command_kind == "udp-echo":
        receiver = build_binary(SERVER_ROOT / "udp_echo_receiver.go", "linux", "amd64")
        upload_receiver = subprocess.run(
            ["scp", "-O", "-q", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", str(receiver), f"{state.server_ssh}:{remote_dir}/udp_echo_receiver"],
            capture_output=True, text=True, check=False, timeout=SSH_UPLOAD_TIMEOUT_S,
        )
        if upload_receiver.returncode != 0:
            raise ProfileError(f"failed to upload UDP echo receiver: {upload_receiver.stderr.strip()}")
        words = ["python3", f"{remote_dir}/trace_server.py", "udp-echo-receiver", "--run-dir", remote_dir, "--receiver-bin", f"{remote_dir}/udp_echo_receiver", "--port", str(state.profile["server"]["port"]), "--run-id", state.run_dir.name, "--ready-file", f"{remote_dir}/ready"]
    elif command_kind == "udp-downlink":
        sender = SERVER_ROOT / "downlink_udp_sender.py"
        upload_sender = subprocess.run(
            [
                "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                str(sender), f"{state.server_ssh}:{remote_dir}/downlink_udp_sender.py",
            ],
            capture_output=True, text=True, check=False, timeout=SSH_UPLOAD_TIMEOUT_S,
        )
        if upload_sender.returncode != 0:
            raise ProfileError(f"failed to upload downlink UDP sender: {upload_sender.stderr.strip()}")
        words = ["python3", f"{remote_dir}/trace_server.py", "udp-downlink-sender", "--server-repo", remote_repo, "--sender-script", f"{remote_dir}/downlink_udp_sender.py", "--run-dir", remote_dir, "--port", str(state.profile["server"]["port"]), "--duration-s", str(state.profile["workload"]["duration_s"]), "--rate", str(state.profile["workload"]["rate"]), "--payload-bytes", str(state.profile["workload"].get("payload_bytes", 1200)), "--run-id", state.run_dir.name, "--duration-mode", "wall-clock", "--ready-file", f"{remote_dir}/ready"]
        if state.collection_mode == "trace":
            words.append("--no-csv")
    else:
        receiver = build_binary(SERVER_ROOT / "uplink_udp_receiver.go", "linux", "amd64")
        compact = COMPACT_PROCESSOR
        upload_receiver = subprocess.run(
            [
                "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                str(receiver), f"{state.server_ssh}:{remote_dir}/uplink_udp_receiver",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_UPLOAD_TIMEOUT_S,
        )
        if upload_receiver.returncode != 0:
            raise ProfileError(f"failed to upload uplink receiver: {upload_receiver.stderr.strip()}")
        chmod_receiver = subprocess.run(
            remote_command(state.server_ssh, ["chmod", "755", f"{remote_dir}/uplink_udp_receiver"]),
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_COMMAND_TIMEOUT_S,
        )
        if chmod_receiver.returncode != 0:
            raise ProfileError(f"failed to chmod uplink receiver: {chmod_receiver.stderr.strip()}")
        upload_compact = subprocess.run(
            [
                "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                str(compact), f"{state.server_ssh}:{remote_dir}/CompactUDPTrace.py",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_UPLOAD_TIMEOUT_S,
        )
        if upload_compact.returncode != 0:
            raise ProfileError(f"failed to upload UDP postprocessor: {upload_compact.stderr.strip()}")
        for helper in (BANDWIDTH_TRACE, BANDWIDTH_PLOT):
            upload_helper = subprocess.run(
                [
                    "scp", "-O", "-q", "-o", "ConnectTimeout=30",
                    "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                    str(helper), f"{state.server_ssh}:{remote_dir}/{helper.name}",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=SSH_UPLOAD_TIMEOUT_S,
            )
            if upload_helper.returncode != 0:
                raise ProfileError(f"failed to upload UDP helper {helper.name}: {upload_helper.stderr.strip()}")
        postprocess = str(state.profile["workload"].get("postprocess", "arrival"))
        receiver_postprocess = "none" if postprocess == "both" else postprocess
        words = ["python3", f"{remote_dir}/trace_server.py", "udp-uplink-receiver", "--server-repo", remote_repo, "--run-dir", remote_dir, "--receiver-bin", f"{remote_dir}/uplink_udp_receiver", "--compact-script", f"{remote_dir}/CompactUDPTrace.py", "--port", str(state.profile["server"]["port"]), "--duration-s", str(state.profile["workload"]["duration_s"]), "--run-id", state.run_dir.name, "--postprocess", receiver_postprocess, "--window-ms", str(state.profile["workload"].get("window_ms", 100)), "--payload-bytes", str(state.profile["workload"].get("payload_bytes", 1472)), "--precision-mode", state.collection_mode, "--ready-file", f"{remote_dir}/ready"]
        if postprocess == "both":
            words.extend(["--receive-api", "readmsg"])
    if extra:
        words.extend(extra)
    command = remote_command(state.server_ssh, words)
    state.command("server", command)
    process = start_process(command, server_dir / "ssh.stdout", server_dir / "ssh.stderr")
    return process, remote_dir, server_dir


def _local_artifact(run_dir: Path, path: Path) -> dict[str, object]:
    exists = path.is_file()
    return {
        "path": str(path.relative_to(run_dir)),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else 0,
    }


def is_custom_udp_both(state: RunState) -> bool:
    workload = state.profile["workload"]
    return (
        workload.get("kind") == "udp_flow"
        and workload.get("postprocess") == "both"
    )


def is_custom_udp_uplink_both(state: RunState) -> bool:
    return is_custom_udp_both(state) and state.profile["workload"].get("direction") == "uplink"


def is_custom_udp_downlink_both(state: RunState) -> bool:
    return is_custom_udp_both(state) and state.profile["workload"].get("direction") == "downlink"


def postprocess_custom_udp(state: RunState, remote_dir: str, sender_summary: Path) -> int:
    """Upload the small sender clock summary and process UTRC on the server."""

    if not sender_summary.is_file() or sender_summary.stat().st_size == 0:
        state.manifest["custom_udp_postprocess"] = {
            "status": "failed",
            "exit_code": 1,
            "error": "missing or empty sender summary",
        }
        state.update()
        return 1
    upload = subprocess.run(
        [
            "scp", "-O", "-q", "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            str(sender_summary), f"{state.server_ssh}:{remote_dir}/uplink_udp_sender_summary.txt",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=SSH_UPLOAD_TIMEOUT_S,
    )
    if upload.returncode != 0:
        state.manifest["custom_udp_postprocess"] = {
            "status": "failed",
            "exit_code": upload.returncode,
            "error": f"sender summary upload failed: {upload.stderr.strip()}",
        }
        state.update()
        return upload.returncode

    duration_ms = round(float(state.profile["workload"]["duration_s"]) * 1000)
    window_ms = max(1, round(float(state.profile["workload"].get("window_ms", 10))))
    command = remote_command(
        state.server_ssh,
        [
            "sh", "-c",
            "python3 {script} {capture} --output-dir {derived} --mode both "
            "--window-ms {window} --gap-ms 100 --sender-summary {summary} "
            "--duration-ms {duration} --payload-bytes {payload} --precision-mode {precision} "
            "> {stdout} 2> {stderr}; rc=$?; printf '%s\\n' \"$rc\" > {exit_code}; exit $rc".format(
                script=shlex.quote(f"{remote_dir}/CompactUDPTrace.py"),
                capture=shlex.quote(f"{remote_dir}/udp_trace.utrc"),
                derived=shlex.quote(f"{remote_dir}/derived"),
                window=window_ms,
                summary=shlex.quote(f"{remote_dir}/uplink_udp_sender_summary.txt"),
                duration=duration_ms,
                payload=int(state.profile["workload"].get("payload_bytes", 1472)),
                precision=shlex.quote(state.collection_mode),
                stdout=shlex.quote(f"{remote_dir}/custom_udp_postprocess.stdout"),
                stderr=shlex.quote(f"{remote_dir}/custom_udp_postprocess.stderr"),
                exit_code=shlex.quote(f"{remote_dir}/custom_udp_postprocess.exit_code"),
            ),
        ],
    )
    state.command("custom_udp_postprocess", command)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SSH_ARTIFACT_TIMEOUT_S,
        )
        rc = result.returncode
        error = result.stderr.strip()
    except subprocess.TimeoutExpired:
        rc = 124
        error = "custom UDP remote postprocess timed out"
    state.manifest["custom_udp_postprocess"] = {
        "status": "complete" if rc == 0 else "failed",
        "exit_code": rc,
        "remote_utrc": f"{remote_dir}/udp_trace.utrc",
        "error": error,
    }
    state.update()
    return rc


def copy_remote_artifacts(state: RunState, remote_dir: str, server_dir: Path) -> None:
    workload = state.profile["workload"]
    server_side_pcap = (
        workload.get("kind") == "iperf3"
        and workload.get("direction") == "uplink"
        and bool(state.profile["server"].get("tcpdump", {}).get("enabled", False))
    )
    custom_udp = is_custom_udp_uplink_both(state)
    downlink_custom_udp = is_custom_udp_downlink_both(state)
    transfer_mode = "full"
    if server_side_pcap:
        transfer_mode = "derived_only"
        top_names = [
            "iperf3_server.json",
            "iperf3_server.stderr",
            "tcpdump.stderr",
            "tcpdump.stdout",
            "exit_code.txt",
            "metadata.txt",
            "server_end_time_utc.txt",
            "pcap_postprocess.json",
            "pcap_postprocess.stdout",
            "pcap_postprocess.stderr",
        ]
        derived_names = [
            "udp_arrival_time_ms.txt",
            "bandwidth_trace.tsv",
            "pcap_bandwidth_summary.json",
        ]
        top_result = scp_remote_files(state.server_ssh, remote_dir, top_names, server_dir)
        derived_dir = state.run_dir / "derived"
        derived_result = scp_remote_files(state.server_ssh, f"{remote_dir}/derived", derived_names, derived_dir)
        result = top_result if top_result != 0 else derived_result
        if result == 0:
            # Plotting is optional; a missing matplotlib installation must not
            # invalidate the parsed server-side trace.
            scp_remote_files(state.server_ssh, f"{remote_dir}/derived", ["bandwidth_time_scatter.png"], derived_dir)
    elif custom_udp:
        transfer_mode = "derived_only"
        top_names = [
            "exit_code.txt",
            "uplink_udp_receiver_summary.txt",
            "uplink_udp_receiver_progress.tsv",
            "uplink_udp_sender_summary.txt",
            "custom_udp_postprocess.exit_code",
            "custom_udp_postprocess.stdout",
            "custom_udp_postprocess.stderr",
        ]
        derived_names = [
            "udp_arrival_time_ms.txt",
            "udp_arrival_time_precise.tsv",
            "udp_latency_precise.tsv",
            "delay_precise.tsv",
            "delay.txt",
            "delay_window_time_ms.txt",
            "delay_summary.txt",
            "udp_receive_gap_report.tsv",
            "bandwidth_trace.tsv",
            "custom_udp_trace_summary.json",
        ]
        top_result = scp_remote_files(state.server_ssh, remote_dir, top_names, server_dir)
        postprocess_ok = state.manifest.get("custom_udp_postprocess", {}).get("exit_code") == 0
        derived_result = (
            scp_remote_files(state.server_ssh, f"{remote_dir}/derived", derived_names, state.run_dir / "derived")
            if postprocess_ok else 1
        )
        result = top_result if top_result != 0 else derived_result
        if result == 0:
            scp_remote_files(
                state.server_ssh,
                f"{remote_dir}/derived",
                ["bandwidth_time_scatter.png"],
                state.run_dir / "derived",
            )
    elif downlink_custom_udp:
        transfer_mode = "summary_only"
        result = scp_remote_files(
            state.server_ssh,
            remote_dir,
            [
                "exit_code.txt",
                "metadata.txt",
                "downlink_udp_sender_summary.txt",
                "downlink_udp_sender_progress.tsv",
                "downlink_udp_control.csv",
                "downlink_udp_sender.csv.gz",
            ],
            server_dir,
        )
    else:
        result = scp_remote(state.server_ssh, remote_dir, server_dir / "remote")
    state.manifest["artifacts"]["server_pull_rc"] = result
    state.manifest["artifacts"]["server_pull"] = {
        "status": "complete" if result == 0 else "failed",
        "exit_code": result,
        "source": remote_dir,
        "transfer_mode": transfer_mode,
    }
    if result == 0 and not server_side_pcap:
        copied = server_dir / "remote" / Path(remote_dir).name
        if copied.exists():
            for item in copied.iterdir():
                target = server_dir / item.name
                if target.exists() and target.is_dir() and item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, target)
    if server_side_pcap:
        metadata_path = server_dir / "pcap_postprocess.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            state.manifest["pcap_postprocess"] = {
                "status": metadata.get("status", "failed"),
                "server_side": True,
                "exit_code": metadata.get("exit_code", 1),
                "summary": metadata.get("summary", {}),
            }
            pcap_info = metadata.get("server_pcap")
            if isinstance(pcap_info, dict):
                state.manifest["artifacts"]["server_pcap"] = pcap_info
            summary = metadata.get("summary", {})
            pcap_artifact = {
                "status": metadata.get("status", "failed"),
                "exit_code": metadata.get("exit_code", 1),
                "server_side": True,
            }
            if isinstance(summary, dict):
                for key in ("packet_count", "payload_bytes", "capture_duration_ms", "plot_error"):
                    if key in summary:
                        pcap_artifact[key] = summary[key]
            state.manifest["artifacts"]["pcap_bandwidth"] = pcap_artifact
            for name, artifact_name in {
                "udp_arrival_time_ms.txt": "udp_arrival_time_ms",
                "bandwidth_trace.tsv": "bandwidth_trace",
                "pcap_bandwidth_summary.json": "pcap_bandwidth_summary",
                "bandwidth_time_scatter.png": "bandwidth_time_scatter",
            }.items():
                path = state.run_dir / "derived" / name
                if path.exists():
                    state.manifest["artifacts"][artifact_name] = _local_artifact(state.run_dir, path)
    if custom_udp:
        summary_path = state.run_dir / "derived" / "custom_udp_trace_summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        if isinstance(summary, dict):
            receiver_summary: dict[str, object] = {}
            receiver_summary_path = server_dir / "uplink_udp_receiver_summary.txt"
            if receiver_summary_path.is_file():
                for line in receiver_summary_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    try:
                        receiver_summary[key] = int(value)
                    except ValueError:
                        receiver_summary[key] = value
            state.manifest["custom_udp"] = {
                "status": "complete" if result == 0 else "failed",
                "server_utrc": {
                    "path": summary.get("source_utrc", f"{remote_dir}/udp_trace.utrc"),
                    "exists": bool(summary.get("source_utrc_exists", False)),
                    "size_bytes": int(summary.get("source_utrc_size_bytes", 0) or 0),
                    "transfer": "remote_only",
                },
                "clock_sync_samples": summary.get("clock_sync_samples", 0),
                "clock_offset_ms": summary.get("clock_offset_ms", 0.0),
                "receiver": {
                    "packet_count": summary.get("packet_count", 0),
                    "unique_packet_count": summary.get("unique_packet_count", 0),
                    "sequence_gap_packets": summary.get("sequence_gap_packets", 0),
                    "socket_drop_max": summary.get("socket_drop_max", 0),
                },
                "receiver_summary": receiver_summary,
                "summary": summary,
            }
        for name, artifact_name in {
            "udp_arrival_time_ms.txt": "udp_arrival_time_ms",
            "delay.txt": "delay",
            "delay_window_time_ms.txt": "delay_window_time_ms",
            "delay_summary.txt": "delay_summary",
            "udp_receive_gap_report.tsv": "udp_receive_gap_report",
            "bandwidth_trace.tsv": "bandwidth_trace",
            "custom_udp_trace_summary.json": "custom_udp_trace_summary",
            "bandwidth_time_scatter.png": "bandwidth_time_scatter",
            "time_delay_scatter.png": "time_delay_scatter",
        }.items():
            path = state.run_dir / "derived" / name
            if path.exists():
                state.manifest["artifacts"][artifact_name] = _local_artifact(state.run_dir, path)
    exit_codes: dict[str, object] = {}
    exit_path = server_dir / "exit_code.txt"
    if exit_path.is_file():
        for line in exit_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            try:
                exit_codes[key] = int(value)
            except ValueError:
                exit_codes[key] = value
    state.manifest["artifacts"]["server_exit_codes"] = exit_codes
    state.update()


def launch_iperf(state: RunState) -> tuple[subprocess.Popen[bytes], Path]:
    workload = state.profile["workload"]
    serial = state.roles["udp_sender" if workload["direction"] == "uplink" else "udp_receiver"]
    command = ["adb", "-s", serial, "shell", "/data/local/tmp/iperf3", "-c", state.server_ip, "-p", str(state.profile["server"]["port"]), "-u"]
    if workload["direction"] == "downlink":
        command.append("-R")
    command += ["-b", str(workload["rate"]), "-l", str(workload["payload_bytes"]), "-t", str(workload["duration_s"]), "-i", str(workload.get("interval_s", 0.2)), "-J", "--get-server-output"]
    output = state.run_dir / "roles" / ("udp_sender" if workload["direction"] == "uplink" else "udp_receiver")
    state.command("iperf", command)
    return start_process(command, output / "iperf3_client.json", output / "iperf3_client.stderr"), output


def launch_udp(state: RunState) -> tuple[subprocess.Popen[bytes], Path, str, Path]:
    workload = state.profile["workload"]
    direction = workload["direction"]
    role = "udp_sender" if direction == "uplink" else "udp_receiver"
    serial = state.roles[role]
    source = ANDROID_ROOT / "probes" / ("uplink_udp_sender.go" if direction == "uplink" else "downlink_udp_receiver.go")
    binary = android_binary(source, serial)
    remote_path = "/data/local/tmp/trace_udp_workload"
    push = subprocess.run(["adb", "-s", serial, "push", str(binary), remote_path], capture_output=True, text=True, check=False)
    if push.returncode != 0:
        raise ProfileError(f"failed to push UDP binary: {push.stderr.strip()}")
    subprocess.run(["adb", "-s", serial, "shell", "chmod", "755", remote_path], check=False)
    directory = state.run_dir / "roles" / role
    if direction == "uplink":
        command = ["adb", "-s", serial, "shell", remote_path, "--run-id", state.run_dir.name, "--target", state.server_ip, "--port", str(state.profile["server"]["port"]), "--duration-s", str(workload["duration_s"]), "--rate", str(workload["rate"]), "--payload-bytes", str(workload["payload_bytes"]), "--out-dir", "/data/local/tmp/trace_udp_workload_out", "--duration-mode", "wall-clock", "--progress-interval", "1s"]
        if workload.get("postprocess") == "both" and state.collection_mode == "trace":
            command.append("--no-csv")
    else:
        command = ["adb", "-s", serial, "shell", remote_path, "--run-id", state.run_dir.name, "--server", state.server_ip, "--port", str(state.profile["server"]["port"]), "--duration-s", str(workload["duration_s"]), "--timeout-s", "5", "--wait-timeout-s", "120", "--payload-bytes", str(workload["payload_bytes"]), "--out-dir", "/data/local/tmp/trace_udp_workload_out", "--output-mode", str(workload.get("postprocess", "arrival")), "--window-ms", str(workload.get("window_ms", 100)), "--precision-mode", state.collection_mode, "--progress-interval", "1s"]
    state.command("udp", command)
    return start_process(command, directory / "udp.stdout", directory / "udp.stderr"), directory, remote_path, source


def launch_udp_echo(state: RunState) -> tuple[subprocess.Popen[bytes], Path, str, Path]:
    workload = state.profile["workload"]
    serial = state.roles["udp_echo_phone"]
    source = ANDROID_ROOT / "probes" / "fine_rtt_probe.go"
    binary = android_binary(source, serial)
    remote_path = "/data/local/tmp/measurement_fine_rtt_probe"
    push = subprocess.run(["adb", "-s", serial, "push", str(binary), remote_path], capture_output=True, text=True, check=False)
    if push.returncode != 0:
        raise ProfileError(f"failed to push UDP echo probe: {push.stderr.strip()}")
    subprocess.run(["adb", "-s", serial, "shell", "chmod", "755", remote_path], check=False)
    directory = state.run_dir / "roles" / "udp_echo_phone"
    command = ["adb", "-s", serial, "shell", remote_path, "--run-id", state.run_dir.name, "--target", state.server_ip, "--port", str(state.profile["server"]["port"]), "--duration-s", str(workload["duration_s"]), "--interval-ms", str(round(float(workload.get("interval_s", 0.01)) * 1000)), "--payload-bytes", str(workload["payload_bytes"]), "--timeout-ms", str(workload.get("timeout_ms", 2000)), "--out-dir", f"{remote_path}_out"]
    state.command("udp_echo", command)
    return start_process(command, directory / "fine_rtt_probe.stdout", directory / "fine_rtt_probe.stderr"), directory, remote_path, source


def pull_phone_udp_echo(state: RunState, remote_path: str, role_dir: Path) -> None:
    remote_dir = f"{remote_path}_out"
    local_root = role_dir / "remote"
    local_root.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["adb", "-s", state.roles["udp_echo_phone"], "pull", remote_dir, str(local_root)], capture_output=True, text=True, check=False)
    state.manifest["artifacts"]["phone_pull_rc"] = result.returncode
    if result.returncode != 0:
        return
    nested = local_root / Path(remote_dir).name
    if nested.is_dir():
        for item in nested.iterdir():
            target = role_dir / item.name
            if item.is_file():
                shutil.copy2(item, target)


def pull_phone_udp(state: RunState, remote_path: str, role_dir: Path) -> None:
    remote_dir = f"{remote_path}_out"
    local_root = role_dir / "remote"
    local_root.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["adb", "-s", state.roles["udp_sender" if state.profile["workload"]["direction"] == "uplink" else "udp_receiver"], "pull", remote_dir, str(local_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    state.manifest["artifacts"]["phone_pull_rc"] = result.returncode
    if result.returncode != 0:
        return
    # adb pull commonly creates <local_root>/<basename(remote_dir)>/. Keep the
    # role directory stable regardless of that adb version-specific behavior.
    nested = local_root / Path(remote_dir).name
    if nested.is_dir():
        for item in nested.iterdir():
            target = role_dir / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)


def pull_phone_udp_summary(state: RunState, remote_path: str, role_dir: Path) -> Path | None:
    """Pull only the sender summary for server-side UTRC post-processing."""

    serial = state.roles["udp_sender" if state.profile["workload"]["direction"] == "uplink" else "udp_receiver"]
    output = role_dir / "uplink_udp_sender_summary.txt"
    result = subprocess.run(
        [
            "adb", "-s", serial, "exec-out", "cat",
            f"{remote_path}_out/uplink_udp_sender_summary.txt",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout:
        output.write_bytes(result.stdout)
        state.manifest["artifacts"]["phone_sender_summary"] = _local_artifact(state.run_dir, output)
        state.manifest["artifacts"]["phone_pull_rc"] = 0
        state.manifest["artifacts"]["phone_pull_mode"] = "summary_only"
        state.update()
        return output
    state.manifest["artifacts"]["phone_pull_rc"] = result.returncode or 1
    state.manifest["artifacts"]["phone_pull_mode"] = "summary_only"
    state.manifest["artifacts"]["phone_pull_error"] = result.stderr.decode("utf-8", errors="replace").strip()
    state.update()
    return None


def postprocess(state: RunState, *, min_span_s: float | None = None, max_span_s: float | None = None) -> None:
    derived = state.run_dir / "derived"
    workload = state.profile["workload"]
    if workload["kind"] == "udp_echo_rtt":
        role_dir = state.run_dir / "roles" / "udp_echo_phone"
        source = role_dir / "fine_rtt_samples.csv"
        if not source.is_file() or source.stat().st_size == 0:
            raise RuntimeError("missing or empty fine_rtt_samples.csv")
        import csv as _csv
        rows = []
        with source.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(_csv.DictReader(handle))
        duration = float(workload["duration_s"])
        rtt_path = derived / "rtt_10ms.tsv"
        loss_path = derived / "loss_100ms.tsv"
        # Aggregate valid phone-local RTTs by the exact 10 ms send-time
        # bucket.  Empty buckets are retained with blank RTT/sample_count so
        # downstream plots preserve the complete requested time axis.
        rtt_bins: dict[int, list[float]] = {}
        for row in rows:
            try:
                t = float(row.get("send_rel_s", "")); value = float(row.get("rtt_ms", ""))
            except (TypeError, ValueError):
                continue
            if row.get("lost", "false").lower() == "true" or row.get("late", "false").lower() == "true":
                continue
            rtt_bins.setdefault(int(t // 0.01), []).append(value)
        with rtt_path.open("w", encoding="utf-8", newline="") as handle:
            writer = _csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["time_s", "rtt_ms", "sample_count"])
            for index in range(max(0, int(math.ceil(duration / 0.01)))):
                values = rtt_bins.get(index, [])
                writer.writerow([f"{index * 0.01:.6f}", f"{sum(values) / len(values):.9f}" if values else "", len(values) if values else ""])
        bins = {}
        for row in rows:
            try:
                t = float(row.get("send_rel_s", "")); index = int(t // 0.1)
            except (TypeError, ValueError):
                continue
            bucket = bins.setdefault(index, [0, 0])
            bucket[0] += 1
            if row.get("lost", "false").lower() == "true" or row.get("late", "false").lower() == "true":
                bucket[1] += 1
        with loss_path.open("w", encoding="utf-8", newline="") as handle:
            writer = _csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["time_ms", "sent_packets", "received_packets", "lost_packets", "loss_rate", "source"])
            for index in range(max(0, int(math.ceil(duration / 0.1)))):
                sent, lost = bins.get(index, [0, 0]); received = sent - lost
                rate = "" if not sent else f"{lost / sent:.9f}"
                writer.writerow([f"{index * 100:.6f}", sent, received, lost, rate, "phone_echo"])
        summary = _read_kv(role_dir / "fine_rtt_probe_summary.txt")
        summary.update({"source": "phone_udp_echo", "processed_on_device": "true", "kernel_payload_processing": "header_only"})
        write_json(derived / "rtt_summary.json", summary)
        write_json(derived / "loss_100ms_metadata.json", {"source": "phone_echo", "loss_definition": "per_probe_send_time", "kernel_socket_overflow": summary.get("socket_rxq_overflow_packets", "0")})
        state.manifest["udp_echo_rtt"] = {"status": "complete", "processed_on_device": True, "payload_processing": "header_only", "artifacts": {"rtt": _local_artifact(state.run_dir, rtt_path), "loss": _local_artifact(state.run_dir, loss_path)}}
        state.update()
        return
    if workload["kind"] == "iperf3":
        client_json = state.run_dir / "roles" / ("udp_sender" if workload["direction"] == "uplink" else "udp_receiver") / "iperf3_client.json"
        try:
            client_data = normalize_iperf_json(client_json)
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid or missing iperf3 client JSON: {error}") from error
        if client_data.get("error"):
            raise RuntimeError(f"iperf3 client reported error: {client_data['error']}")
        if not isinstance(client_data.get("end"), dict) or not isinstance(client_data["end"].get("sum"), dict):
            raise RuntimeError("iperf3 client JSON has no completed end summary")
        source = state.run_dir / "roles" / ("server" if workload["direction"] == "uplink" else "udp_receiver") / "iperf3_server.json"
        if source.exists() and workload["direction"] == "uplink":
            output = derived / "iperf_intervals.tsv"
            command = [sys.executable, str(ROOT / "traces" / "scripts" / "IperfUplinkCompare.py"), "--client-json", str(state.run_dir / "roles" / "udp_sender" / "iperf3_client.json"), "--server-json", str(source), "--intervals-output", str(output), "--metrics-output", str(derived / "iperf_metrics.txt")]
            subprocess.run(command, check=False)
        if workload["direction"] == "uplink" and state.profile["server"].get("tcpdump", {}).get("enabled", False):
            server_side = state.manifest.get("pcap_postprocess", {}).get("server_side", False)
            server_pull_rc = state.manifest["artifacts"].get("server_pull_rc")
            if server_side:
                required = (
                    state.run_dir / "derived" / "udp_arrival_time_ms.txt",
                    state.run_dir / "derived" / "bandwidth_trace.tsv",
                    state.run_dir / "derived" / "pcap_bandwidth_summary.json",
                )
                pcap_result = {
                    "status": "complete"
                    if server_pull_rc == 0
                    and state.manifest.get("pcap_postprocess", {}).get("status") == "complete"
                    and all(path.is_file() and path.stat().st_size > 0 for path in required)
                    else "failed",
                    "error": "server-side PCAP artifacts are incomplete",
                }
            elif server_pull_rc not in (None, 0):
                pcap_result = {
                    "status": "failed",
                    "error": f"server artifact pull failed with exit code {server_pull_rc}",
                    "artifacts": {
                        "pcap_bandwidth": {
                            "status": "failed",
                            "exit_code": 1,
                            "error": "server artifact pull failed",
                        }
                    },
                }
            else:
                pcap_result = process_run_pcap(state.run_dir, state.profile)
            state.manifest["pcap_postprocess"] = {
                **state.manifest.get("pcap_postprocess", {}),
                "status": pcap_result.get("status"),
                "error": pcap_result.get("error", ""),
            }
            state.manifest["artifacts"].update(pcap_result.get("artifacts", {}))
            state.update()
            if pcap_result.get("status") != "complete":
                raise RuntimeError(f"PCAP post-processing failed: {pcap_result.get('error', 'unknown error')}")
    if workload["kind"] == "udp_flow":
        if is_custom_udp_downlink_both(state):
            role_dir = state.run_dir / "roles" / "udp_receiver"
            for filename in (
                "udp_arrival_time_ms.txt", "udp_arrival_time_precise.tsv", "udp_latency_precise.tsv", "delay_precise.tsv", "delay.txt", "delay_window_time_ms.txt",
                "delay_summary.txt", "udp_receive_gap_report.tsv", "bandwidth_trace.tsv",
                "custom_udp_trace_summary.json", "downlink_udp_receiver_summary.txt",
            ):
                source = role_dir / filename
                if source.is_file():
                    shutil.copy2(source, derived / filename)
            required = (
                derived / "udp_arrival_time_ms.txt",
                derived / "delay.txt",
                derived / "delay_window_time_ms.txt",
                derived / "delay_summary.txt",
                derived / "bandwidth_trace.tsv",
                derived / "custom_udp_trace_summary.json",
            )
            if state.manifest.get("artifacts", {}).get("server_pull_rc") != 0 or state.manifest.get("artifacts", {}).get("phone_pull_rc") != 0 or not all(path.is_file() and path.stat().st_size > 0 for path in required):
                raise RuntimeError("custom UDP downlink latency/bandwidth artifacts are incomplete")
            receiver_path = state.run_dir / "roles" / "udp_receiver" / "downlink_udp_receiver_summary.txt"
            receiver = _read_kv(receiver_path)
            try:
                summary = json.loads((derived / "custom_udp_trace_summary.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"invalid custom UDP downlink trace summary: {error}") from error
            delays = [float(line) for line in (derived / "delay.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
            times = [float(line) for line in (derived / "delay_window_time_ms.txt").read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(delays) != len(times) or not delays:
                raise RuntimeError("custom UDP downlink delay trace is incomplete")
            capture_span_ms = float(summary.get("capture_duration_ms", 0.0) or 0.0)
            requested_ms = float(workload["duration_s"]) * 1000
            lower_ms = requested_ms * 0.833 if min_span_s is None else min_span_s * 1000
            upper_ms = requested_ms * 1.167 if max_span_s is None else max_span_s * 1000
            if not lower_ms <= capture_span_ms <= upper_ms:
                raise RuntimeError(
                    f"custom UDP downlink receive span {capture_span_ms:.3f} ms is outside "
                    f"the expected {lower_ms:.0f}..{upper_ms:.0f} ms"
                )
            latency = summary.setdefault("latency", {})
            latency.update({"valid_delay_rows": len(delays), "window_count": len(times), **latency_statistics(delays)})
            summary["receiver"] = {
                "received_packets": int(receiver.get("received_packets", 0) or 0),
                "unique_packet_count": int(receiver.get("unique_received_packets", 0) or 0),
                "payload_bytes": int(receiver.get("payload_bytes_total", receiver.get("payload_bytes", 0)) or 0),
                "payload_mbps": int(receiver.get("payload_bytes_total", receiver.get("payload_bytes", 0)) or 0) * 8 / max(float(summary.get("capture_duration_ms", 1) or 1), 1) / 1000,
                "socket_drops": int(receiver.get("socket_rxq_overflow_packets", 0) or 0),
            }
            (derived / "custom_udp_trace_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            state.manifest["custom_udp"] = {
                "status": "complete",
                "direction": "downlink",
                "phone_utrc": {"transfer": "processed_on_device", "retained": False},
                "clock_sync_samples": int(receiver.get("clock_sync_samples", 0) or 0),
                "clock_offset_ms": float(receiver.get("clock_offset_ms", 0) or 0),
                "receiver": summary["receiver"],
                "receiver_summary": receiver,
                "summary": summary,
                "latency_stats": latency,
            }
            sender = _read_kv(state.run_dir / "roles" / "server" / "downlink_udp_sender_summary.txt")
            requested_mbps = parse_rate_mbps(str(workload.get("rate", "")), int(workload.get("payload_bytes", 0)))
            actual_mbps = float(sender.get("actual_payload_mbps", 0) or 0)
            state.manifest["custom_udp"]["sender"] = {
                "done_ack": sender.get("done_ack", "false").lower() == "true",
                "clock_sync_samples": int(sender.get("clock_sync_samples", 0) or 0),
                "sent_packets": int(sender.get("sent_packets", 0) or 0),
                "send_failed_packets": int(sender.get("send_failed_packets", 0) or 0),
                "actual_payload_mbps": actual_mbps,
                "requested_payload_mbps": requested_mbps,
                "sender_limited": bool(requested_mbps and actual_mbps < requested_mbps * 0.9),
            }
            bandwidth_plot = derived / "bandwidth_time_scatter.png"
            plot_python = ROOT / ".venv" / "bin" / "python"
            if not plot_python.is_file():
                plot_python = Path(sys.executable)
            if not bandwidth_plot.is_file():
                subprocess.run([str(plot_python), str(BANDWIDTH_PLOT), str(derived / "udp_arrival_time_ms.txt"), "--payload-bytes", str(workload.get("payload_bytes", 1200)), "--window-ms", str(round(float(workload.get("window_ms", 10)))), "--duration-ms", str(round(float(workload["duration_s"]) * 1000)), "--output", str(bandwidth_plot)], check=False)
            delay_plot = derived / "time_delay_scatter.png"
            if not delay_plot.is_file():
                subprocess.run([str(plot_python), str(DELAY_PLOT), str(derived), "--output", str(delay_plot)], check=False)
            if not bandwidth_plot.is_file() or not delay_plot.is_file():
                raise RuntimeError("custom UDP downlink plot generation failed")
            for path, name in ((bandwidth_plot, "bandwidth_time_scatter"), (delay_plot, "time_delay_scatter")):
                if path.is_file(): state.manifest["artifacts"][name] = _local_artifact(state.run_dir, path)
            state.manifest["artifacts"]["custom_udp_trace_summary"] = _local_artifact(state.run_dir, derived / "custom_udp_trace_summary.json")
            state.update()
            return
        if is_custom_udp_both(state):
            custom = state.manifest.get("custom_udp", {})
            required = (
                state.run_dir / "derived" / "udp_arrival_time_ms.txt",
                state.run_dir / "derived" / "delay.txt",
                state.run_dir / "derived" / "delay_window_time_ms.txt",
                state.run_dir / "derived" / "delay_summary.txt",
                state.run_dir / "derived" / "bandwidth_trace.tsv",
                state.run_dir / "derived" / "custom_udp_trace_summary.json",
            )
            if (
                state.manifest.get("artifacts", {}).get("server_pull_rc") != 0
                or custom.get("status") != "complete"
                or not all(path.is_file() and path.stat().st_size > 0 for path in required)
            ):
                raise RuntimeError("custom UDP latency/bandwidth artifacts are incomplete")
            sender_path = state.run_dir / "roles" / "udp_sender" / "uplink_udp_sender_summary.txt"
            sender_values: dict[str, Any] = {}
            if sender_path.is_file():
                for line in sender_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    sender_values[key] = value
            state.manifest["custom_udp"]["sender"] = {
                "done_ack": sender_values.get("done_ack", "false").lower() == "true",
                "clock_sync_samples": int(sender_values.get("clock_sync_samples", "0") or 0),
                "sent_packets": int(sender_values.get("sent_packets", "0") or 0),
                "actual_payload_mbps": float(sender_values.get("actual_payload_mbps", "0") or 0),
            }
            if not state.manifest["custom_udp"]["sender"]["done_ack"]:
                raise RuntimeError("custom UDP sender did not receive done acknowledgement")
            if state.manifest["custom_udp"]["sender"]["clock_sync_samples"] <= 0:
                raise RuntimeError("custom UDP sender has no clock synchronization samples")
            try:
                summary_data = json.loads(
                    (derived / "custom_udp_trace_summary.json").read_text(encoding="utf-8")
                )
                capture_span_ms = float(summary_data.get("capture_duration_ms", 0.0))
                requested_ms = float(workload["duration_s"]) * 1000
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"invalid custom UDP trace summary: {error}") from error
            lower_ms = requested_ms * 0.833 if min_span_s is None else min_span_s * 1000
            upper_ms = requested_ms * 1.167 if max_span_s is None else max_span_s * 1000
            if not lower_ms <= capture_span_ms <= upper_ms:
                raise RuntimeError(
                    f"custom UDP receive span {capture_span_ms:.3f} ms is outside "
                    f"the expected {lower_ms:.0f}..{upper_ms:.0f} ms"
                )
            plot = derived / "bandwidth_time_scatter.png"
            if not plot.is_file():
                plot_python = ROOT / ".venv" / "bin" / "python"
                if not plot_python.is_file():
                    plot_python = Path(sys.executable)
                plot_result = subprocess.run(
                    [
                        str(plot_python), str(BANDWIDTH_PLOT),
                        str(derived / "udp_arrival_time_ms.txt"), "--payload-bytes",
                        str(workload.get("payload_bytes", 1472)), "--window-ms",
                        str(max(1, round(float(workload.get("window_ms", 10))))), "--duration-ms",
                        str(round(requested_ms)), "--output", str(plot),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if plot_result.returncode != 0:
                    state.manifest["custom_udp"]["plot_error"] = plot_result.stderr.strip()
            if plot.is_file():
                summary_data["plot_file"] = str(plot.relative_to(state.run_dir))
                summary_data["plot_error"] = ""
                (derived / "custom_udp_trace_summary.json").write_text(
                    json.dumps(summary_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                state.manifest["artifacts"]["bandwidth_time_scatter"] = _local_artifact(state.run_dir, plot)
            delay_plot = derived / "time_delay_scatter.png"
            delay_plot_python = ROOT / ".venv" / "bin" / "python"
            if not delay_plot_python.is_file():
                delay_plot_python = Path(sys.executable)
            delay_plot_result = subprocess.run(
                [
                    str(delay_plot_python), str(DELAY_PLOT), str(derived),
                    "--output", str(delay_plot),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if delay_plot_result.returncode != 0 or not delay_plot.is_file():
                state.manifest["custom_udp"]["delay_plot_error"] = (
                    delay_plot_result.stderr.strip()
                    or delay_plot_result.stdout.strip()
                    or "time-latency plot was not generated"
                )
            else:
                summary_data["delay_plot_file"] = str(delay_plot.relative_to(state.run_dir))
                summary_data["delay_plot_error"] = ""
                (derived / "custom_udp_trace_summary.json").write_text(
                    json.dumps(summary_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                state.manifest["artifacts"]["time_delay_scatter"] = _local_artifact(state.run_dir, delay_plot)
            augment_custom_udp_summary(state)
            state.update()
        role_dir = state.run_dir / "roles" / ("udp_sender" if workload["direction"] == "uplink" else "udp_receiver")
        for filename in ("udp_arrival_time_ms.txt", "udp_arrival_time_precise.tsv", "udp_latency_precise.tsv", "delay_precise.tsv", "delay.txt", "delay_window_time_ms.txt", "delay_summary.txt", "udp_receive_gap_report.tsv", "uplink_udp_receiver_summary.txt", "downlink_udp_receiver_summary.txt"):
            candidates = [role_dir / filename, role_dir / "remote" / filename, state.run_dir / "roles" / "server" / filename]
            for candidate in candidates:
                if candidate.exists():
                    shutil.copy2(candidate, derived / filename)
                    break
    if state.manifest["icmp"].get("status") in {"complete", "running"}:
        ping = state.run_dir / "roles" / "icmp_phone" / "ping.log"
        if ping.exists():
            summary = derived / "icmp_summary.txt"
            script = ROOT / "traces" / "scripts" / "ICMPPingSummaryProcess.py"
            count = int(state.manifest["icmp"].get("count", 0))
            subprocess.run([sys.executable, str(script), str(ping), "--expected-transmitted", str(count), "--output", str(summary)], check=False)


def run_once(
    profile: dict[str, Any],
    run_dir: Path,
    server_ip: str,
    server_ssh: str,
    server_repo: str,
    roles: dict[str, str],
    *,
    dry_run: bool = False,
    network_guard: dict[str, str] | None = None,
    min_span_s: float | None = None,
    max_span_s: float | None = None,
    environment: dict[str, Any] | None = None,
    collection_mode: str = "trace",
) -> int:
    profile = copy.deepcopy(profile)
    profile["_server_repo"] = server_repo
    state = RunState(run_dir, profile, server_ip, server_ssh, roles, environment, collection_mode)
    server_process: subprocess.Popen[bytes] | None = None
    workload_process: subprocess.Popen[bytes] | None = None
    icmp_process: subprocess.Popen[bytes] | None = None
    remote_dir = ""
    server_dir = state.run_dir / "roles" / "server"
    try:
        validate_roles(profile, roles)
        preflight(state, dry_run, network_guard)
        if dry_run:
            workload = profile["workload"]
            state.command("plan", ["trace_runner", "run", "--profile", profile["name"], "--server-ip", server_ip])
            state.update(status="planned", ended_utc=utc_now(), result="dry_run")
            append_event(run_dir, "dry_run_complete")
            return 0
        state.update(status="running", started_utc=utc_now())
        workload = profile["workload"]
        if workload["kind"] == "iperf3":
            tcpdump = bool(profile["server"].get("tcpdump", {}).get("enabled", False))
            server_process, remote_dir, server_dir = start_server(state, tcpdump=tcpdump, direction=workload["direction"])
            if not wait_remote_ready(server_ssh, f"{remote_dir}/ready"):
                raise RuntimeError("server helper did not become ready")
        elif workload["kind"] == "udp_flow" and workload["direction"] == "uplink":
            server_process, remote_dir, server_dir = start_server(state, tcpdump=False, direction="uplink", command_kind="udp-uplink")
            if not wait_remote_ready(server_ssh, f"{remote_dir}/ready"):
                raise RuntimeError("UDP server receiver did not become ready")
        elif workload["kind"] == "udp_flow" and workload["direction"] == "downlink":
            server_process, remote_dir, server_dir = start_server(state, tcpdump=False, direction="downlink", command_kind="udp-downlink")
            if not wait_remote_ready(server_ssh, f"{remote_dir}/ready"):
                raise RuntimeError("UDP server sender did not become ready")
        elif workload["kind"] == "udp_echo_rtt":
            server_process, remote_dir, server_dir = start_server(state, tcpdump=False, direction="uplink", command_kind="udp-echo")
            if not wait_remote_ready(server_ssh, f"{remote_dir}/ready"):
                raise RuntimeError("UDP echo receiver did not become ready")
        icmp_process = start_icmp(state) if profile.get("icmp", {}).get("enabled", False) and workload["kind"] != "icmp" else None
        workload_dir = state.run_dir / "roles"
        phone_remote = ""
        if workload["kind"] == "iperf3":
            workload_process, workload_dir = launch_iperf(state)
        elif workload["kind"] == "udp_flow":
            workload_process, workload_dir, phone_remote, _source = launch_udp(state)
        elif workload["kind"] == "udp_echo_rtt":
            workload_process, workload_dir, phone_remote, _source = launch_udp_echo(state)
        elif workload["kind"] == "icmp":
            icmp_process = start_icmp(state)
        workload_rc = stop_process(workload_process, float(workload["duration_s"]) + 90)
        icmp_rc = stop_process(icmp_process, 10)
        if icmp_process is not None:
            state.manifest["icmp"].update({"status": "complete" if icmp_rc == 0 else "failed", "exit_code": icmp_rc})
        sender_summary: Path | None = None
        if workload["kind"] == "udp_flow" and phone_remote:
            if is_custom_udp_uplink_both(state):
                sender_summary = pull_phone_udp_summary(state, phone_remote, workload_dir)
            else:
                pull_phone_udp(state, phone_remote, workload_dir)
        if workload["kind"] == "udp_echo_rtt" and phone_remote:
            pull_phone_udp_echo(state, phone_remote, workload_dir)

        # A custom sender can finish without receiving the receiver's final
        # ACK.  In that case the receiver intentionally waits for the control
        # packet, so terminate this exact remote run before waiting on the SSH
        # process.  The run will remain a failed, diagnosable attempt.
        if is_custom_udp_uplink_both(state):
            sender_values = _read_kv(sender_summary) if sender_summary else {}
            if sender_summary is None or sender_values.get("done_ack", "false").lower() != "true":
                stop_remote_run(server_ssh, remote_dir)
        # Server helpers are long-lived listeners and intentionally do not
        # exit when the phone workload finishes.  Stop the exact remote run
        # first, then reap the local SSH wrapper promptly; waiting for the
        # workload duration here would add another 150+ seconds every run.
        if remote_dir:
            stop_remote_run(server_ssh, remote_dir)
        server_rc = stop_process(server_process, 10)
        custom_postprocess_rc = None
        if is_custom_udp_uplink_both(state) and remote_dir:
            custom_postprocess_rc = postprocess_custom_udp(
                state,
                remote_dir,
                sender_summary or Path("/nonexistent/uplink_udp_sender_summary.txt"),
            )
        if state.collection_mode == "measurement" and workload["kind"] == "udp_flow" and phone_remote and is_custom_udp_uplink_both(state):
            pull_phone_udp(state, phone_remote, workload_dir)
        if remote_dir:
            copy_remote_artifacts(state, remote_dir, server_dir)
        state.manifest["workload_result"] = {"exit_code": workload_rc}
        state.manifest["server_result"] = {
            "exit_code": server_rc,
            "remote_exit_codes": state.manifest["artifacts"].get("server_exit_codes", {}),
        }
        postprocess(state, min_span_s=min_span_s, max_span_s=max_span_s)
        if network_guard:
            serial = roles.get("udp_sender") or roles.get("udp_receiver") or roles.get("udp_echo_phone")
            postflight = network_guard_snapshot(serial or "", server_ip, server_ssh, server_repo, network_guard)
            state.manifest["postflight"] = {"network_guard": postflight}
            if postflight.get("status") != "ok":
                state.manifest["failure_reason"] = "network identity changed during run: " + "; ".join(postflight.get("reasons", []))
                state.update()
                final = "network_invalid"
            else:
                final = None
        else:
            final = None
        if final is None and workload["kind"] == "icmp":
            final = "complete" if icmp_rc == 0 else "failed"
        elif final is None and (workload_rc != 0 or server_rc not in (0, None)):
            final = "failed"
        elif final is None and workload["kind"] == "iperf3":
            remote_codes = state.manifest["server_result"].get("remote_exit_codes", {})
            remote_iperf = remote_codes.get("iperf3")
            remote_tcpdump = remote_codes.get("tcpdump")
            tcpdump_required = bool(profile["server"].get("tcpdump", {}).get("enabled", False))
            tcpdump_ok = remote_tcpdump == 0 if tcpdump_required else remote_tcpdump in (0, "disabled")
            final = "complete" if remote_iperf == 0 and tcpdump_ok else "failed"
        elif final is None and is_custom_udp_both(state):
            remote_codes = state.manifest["server_result"].get("remote_exit_codes", {})
            receiver_key = "udp_uplink_receiver" if workload["direction"] == "uplink" else "udp_downlink_sender"
            receiver_ok = remote_codes.get(receiver_key) == 0
            custom_ok = (
                (custom_postprocess_rc == 0 if workload["direction"] == "uplink" else True)
                and state.manifest.get("artifacts", {}).get("phone_pull_rc") == 0
                and state.manifest.get("custom_udp", {}).get("status") == "complete"
            )
            final = "complete" if receiver_ok and custom_ok else "failed"
        elif final is None and workload["kind"] == "udp_echo_rtt":
            remote_codes = state.manifest["server_result"].get("remote_exit_codes", {})
            echo_ok = remote_codes.get("udp_echo_receiver") == 0
            phone_ok = state.manifest.get("artifacts", {}).get("phone_pull_rc") == 0
            # Socket queue overflow is a diagnostic failure, distinct from
            # network packet loss; retain all artifacts but mark the run
            # failed so callers cannot silently accept overloaded capture.
            phone_summary = _read_kv(state.run_dir / "roles" / "udp_echo_phone" / "fine_rtt_probe_summary.txt")
            server_summary = _read_kv(state.run_dir / "roles" / "server" / "udp_echo_receiver_summary.txt")
            overflow = int(phone_summary.get("socket_rxq_overflow_packets", "0") or 0) + int(server_summary.get("socket_rxq_overflow_packets", "0") or 0)
            final = "complete" if echo_ok and phone_ok and overflow == 0 else "failed"
            if overflow:
                state.manifest["failure_reason"] = f"socket receive queue overflow ({overflow} packets)"
        elif final is None and profile.get("icmp", {}).get("enabled", False) and icmp_rc != 0:
            final = "partial"
        elif final is None:
            final = "complete"
        state.update(status=final, ended_utc=utc_now(), exit_code=0 if final == "complete" else 1)
        append_event(run_dir, "finished", status=final, workload_exit_code=workload_rc, server_exit_code=server_rc, icmp_exit_code=icmp_rc)
        return 0 if final == "complete" else 1
    except KeyboardInterrupt:
        stop_process(workload_process, 3)
        stop_process(icmp_process, 3)
        stop_process(server_process, 3)
        stop_remote_run(server_ssh, remote_dir)
        state.manifest["failure_reason"] = "interrupted by user"
        state.update(status="failed", ended_utc=utc_now(), exit_code=130)
        append_event(run_dir, "failed", reason="interrupted by user")
        return 130
    except (ProfileError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        stop_process(workload_process, 3)
        stop_process(icmp_process, 3)
        stop_process(server_process, 3)
        stop_remote_run(server_ssh, remote_dir)
        state.manifest["failure_reason"] = str(error)
        if network_guard and not dry_run:
            serial = roles.get("udp_sender") or roles.get("udp_receiver") or roles.get("udp_echo_phone")
            try:
                state.manifest["postflight"] = {"network_guard": network_guard_snapshot(serial or "", server_ip, server_ssh, server_repo, network_guard)}
            except (OSError, subprocess.SubprocessError):
                pass
        state.update(status="failed", ended_utc=utc_now(), exit_code=2)
        append_event(run_dir, "failed", reason=str(error))
        return 2
