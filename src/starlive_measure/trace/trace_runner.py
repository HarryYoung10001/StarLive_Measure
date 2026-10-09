#!/usr/bin/env python3
"""Canonical CLI for all new traces experiments."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
import sys
import time
from typing import Any

from .trace_runtime import (
    ProfileError,
    apply_overrides,
    load_profile,
    make_run_paths,
    network_guard_config,
    network_guard_snapshot,
    parse_roles,
    parse_rate_mbps,
    parse_rate_pps,
    validate_custom_run_success,
    run_once,
    safe_name,
    validate_roles,
    write_json,
)
from starlive_measure.paths import MEASUREMENT_ROOT, REPO_ROOT, remote_server_repo


ROOT = REPO_ROOT
DEFAULT_SERVER_SSH = "sgp-server"
DEFAULT_SERVER_REPO = remote_server_repo()


def common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode", dest="collection_mode", choices=("trace", "measurement"),
        default="trace", help="timestamp/output mode (default: trace)",
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--server-ip", required=True)
    parser.add_argument("--server-ssh", default=DEFAULT_SERVER_SSH)
    parser.add_argument("--server-repo", default=DEFAULT_SERVER_REPO)
    parser.add_argument("--role", action="append", default=[], metavar="ROLE=SERIAL")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--logs-dir", type=Path)
    parser.add_argument("--rate")
    parser.add_argument("--duration", type=float)
    parser.add_argument("--interval", type=float)
    parser.add_argument("--port", type=int)
    parser.add_argument("--payload", type=int)
    parser.add_argument("--window-ms", type=float)
    parser.add_argument("--no-icmp", action="store_true")
    parser.add_argument("--expected-device-model")
    parser.add_argument("--expected-wifi-ssid")
    parser.add_argument("--expected-wifi-bssid")
    parser.add_argument("--expected-route-interface")
    parser.add_argument("--expected-route-gateway")
    parser.add_argument("--network-wait-s", type=float, default=0)
    parser.add_argument("--network-poll-s", type=float, default=5)
    parser.add_argument("--min-span-s", type=float)
    parser.add_argument("--max-span-s", type=float)
    parser.add_argument("--dry-run", action="store_true")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="execute one profile")
    common_arguments(run)
    sweep = sub.add_parser("sweep", help="run one profile at multiple rates")
    common_arguments(sweep)
    sweep.add_argument("--rates", required=True, help="comma-separated offered rates")
    sweep.add_argument("--continue-on-failure", action=argparse.BooleanOptionalAction, default=True)
    sweep.add_argument("--max-attempts-per-rate", type=int, default=1)
    sweep.add_argument("--cooldown-s", type=float, default=0)
    repeat = sub.add_parser("repeat", help="run one profile until enough complete runs exist")
    common_arguments(repeat)
    repeat.add_argument("--successes", type=int)
    repeat.add_argument("--max-attempts", type=int)
    repeat.add_argument("--segment-successes", type=int)
    repeat.add_argument("--segment-max-attempts", type=int)
    repeat.add_argument("--cooldown-s", type=float, default=0)
    repeat.add_argument("--resume", action="store_true")
    repeat.add_argument("--resume-environment-change", action="store_true")
    repeat.add_argument("--environment-change-reason")
    return parser


def profile_and_roles(args: argparse.Namespace) -> tuple[dict, dict[str, str]]:
    profile = load_profile(args.profile)
    profile = apply_overrides(
        profile,
        rate=args.rate,
        duration=args.duration,
        interval=args.interval,
        port=args.port,
        payload=args.payload,
        window_ms=args.window_ms,
        no_icmp=args.no_icmp,
    )
    roles = parse_roles(args.role)
    validate_roles(profile, roles)
    return profile, roles


def session_directory(args: argparse.Namespace, profile: dict) -> Path:
    if args.session_dir:
        session = args.session_dir
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        logs_dir = args.logs_dir or (
            MEASUREMENT_ROOT / "logs" if args.collection_mode == "measurement" else ROOT / "traces" / "logs"
        )
        session = logs_dir / f"{safe_name(profile['name'])}_{stamp}"
    session.mkdir(parents=True, exist_ok=True)
    return session


def _guard_and_span(args: argparse.Namespace) -> tuple[dict[str, str], float | None, float | None]:
    guard = network_guard_config(args)
    if args.min_span_s is not None and args.max_span_s is not None and args.min_span_s > args.max_span_s:
        raise ProfileError("--min-span-s must be <= --max-span-s")
    return guard, args.min_span_s, args.max_span_s


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def _environment(roles: dict[str, str], guard: dict[str, str]) -> dict[str, Any]:
    return {"roles": roles, "network_guard": guard}


def _active_environment(metadata: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    active = metadata.get("active_environment")
    if isinstance(active, dict):
        generation = int(active.get("generation", 1) or 1)
        return generation, {
            "roles": active.get("roles", {}),
            "network_guard": active.get("network_guard", {}),
        }
    return 1, _environment(metadata.get("roles", {}), metadata.get("network_guard", {}))


def _environment_change_allowed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    old_roles = previous.get("roles", {})
    new_roles = current.get("roles", {})
    old_guard = previous.get("network_guard", {})
    new_guard = current.get("network_guard", {})
    if set(old_roles) != set(new_roles):
        return False
    if set(old_guard) != set(new_guard):
        return False
    return all(
        old_guard.get(key) == new_guard.get(key)
        for key in old_guard
        if key != "device_model"
    )


def write_session_metadata(
    session: Path,
    args: argparse.Namespace,
    profile: dict,
    roles: dict[str, str],
    mode: str,
    *,
    resume: bool = False,
    network_guard: dict[str, str] | None = None,
) -> int:
    metadata = {
        "schema_version": 1,
        "collection_mode": getattr(args, "collection_mode", "trace"),
        "time_precision": "nanosecond" if getattr(args, "collection_mode", "trace") == "measurement" else "integer_ms",
        "rounding": "none" if getattr(args, "collection_mode", "trace") == "measurement" else "half_away_for_delay_floor_for_arrival",
        "mode": mode,
        "profile": profile["name"],
        "profile_hash": profile.get("_hash"),
        "server_ip": args.server_ip,
        "server_ssh": args.server_ssh,
        "server_repo": args.server_repo,
        "roles": roles,
        "workload": profile["workload"],
        "network_guard": network_guard or {},
        "min_span_s": args.min_span_s,
        "max_span_s": args.max_span_s,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    session_json = session / "session.json"
    if resume:
        try:
            existing = json.loads(session_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ProfileError(f"cannot resume session metadata: {error}") from error
        for key in ("profile_hash", "server_ip", "server_ssh", "server_repo", "workload", "min_span_s", "max_span_s"):
            if existing.get(key) != metadata.get(key):
                raise ProfileError(f"--resume metadata mismatch for {key}")
        generation, active = _active_environment(existing)
        current = _environment(roles, network_guard or {})
        if active == current:
            return generation
        if not args.resume_environment_change:
            raise ProfileError("--resume environment mismatch; use --resume-environment-change with a reason")
        if not args.environment_change_reason:
            raise ProfileError("--environment-change-reason is required with --resume-environment-change")
        if not _environment_change_allowed(active, current):
            raise ProfileError("--resume environment change may only change ADB serials and device_model")
        if "active_environment" not in existing:
            _append_jsonl(session / "environment_changes.jsonl", {
                "event": "environment_seed", "generation": generation,
                "environment": active, "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
        generation += 1
        record = {
            "event": "environment_change",
            "from_generation": generation - 1,
            "to_generation": generation,
            "from": active,
            "to": current,
            "reason": args.environment_change_reason,
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _append_jsonl(session / "environment_changes.jsonl", record)
        existing["active_environment"] = {"generation": generation, **current}
        existing["environment_generation"] = generation
        existing.pop("active_segment", None)
        write_json(session_json, existing)
        return generation
    write_json(session_json, metadata)
    (session / "profile.json").write_text(json.dumps({key: value for key, value in profile.items() if not key.startswith("_")}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 1


def run_one(
    args: argparse.Namespace,
    profile: dict,
    roles: dict[str, str],
    session: Path,
    index: int | None = None,
    environment_generation: int = 1,
) -> tuple[int, Path]:
    run_dir = make_run_paths(session, profile, index)
    guard, min_span_s, max_span_s = _guard_and_span(args)
    result = run_once(
        profile, run_dir, args.server_ip, args.server_ssh, args.server_repo, roles,
        dry_run=args.dry_run, network_guard=guard or None,
        min_span_s=min_span_s, max_span_s=max_span_s,
        environment={"generation": environment_generation, "roles": roles, "network_guard": guard},
        collection_mode=getattr(args, "collection_mode", "trace"),
    )
    with (session / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"run_id": run_dir.name, "path": str(run_dir), "exit_code": result}, sort_keys=True) + "\n")
    return result, run_dir


def run_command(args: argparse.Namespace) -> int:
    profile, roles = profile_and_roles(args)
    session = session_directory(args, profile)
    guard, _, _ = _guard_and_span(args)
    write_session_metadata(session, args, profile, roles, "run", network_guard=guard)
    result, _ = run_one(args, profile, roles, session)
    return result


def sweep_command(args: argparse.Namespace) -> int:
    profile, roles = profile_and_roles(args)
    rates = [rate.strip() for rate in args.rates.split(",") if rate.strip()]
    if not rates:
        raise ProfileError("--rates must contain at least one rate")
    if args.max_attempts_per_rate <= 0:
        raise ProfileError("--max-attempts-per-rate must be positive")
    session = session_directory(args, profile)
    guard, _, _ = _guard_and_span(args)
    write_session_metadata(session, args, profile, roles, "sweep", network_guard=guard)
    plan_rows = [[index, rate, parse_rate_mbps(rate, int(profile["workload"].get("payload_bytes", 0) or 0)), parse_rate_pps(rate, int(profile["workload"].get("payload_bytes", 0) or 0))] for index, rate in enumerate(rates, 1)]
    with (session / "rate_sweep_plan.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["cell_index", "requested_rate", "requested_mbps", "requested_pps"])
        writer.writerows(plan_rows)
    summary_rows: list[list[Any]] = []
    failures = 0
    total_attempts = 0
    completed_rates = 0
    for cell_index, rate in enumerate(rates, 1):
        child = apply_overrides(
            profile,
            rate=rate,
            duration=args.duration,
            interval=args.interval,
            port=args.port,
            payload=args.payload,
            window_ms=args.window_ms,
            no_icmp=args.no_icmp,
        )
        cell_attempts = 0
        selected_run = ""
        cell_status = "failed"
        last_run: Path | None = None
        while cell_attempts < args.max_attempts_per_rate:
            if not args.dry_run and not wait_for_network_guard(args, roles, session, guard):
                cell_status = "network_timeout"
                break
            cell_attempts += 1
            total_attempts += 1
            result, last_run = run_one(args, child, roles, session, total_attempts)
            valid = result == 0 and (args.dry_run or child["workload"].get("kind") != "udp_flow" or child["workload"].get("postprocess") != "both" or validate_custom_run_success(last_run, min_span_s=args.min_span_s, max_span_s=args.max_span_s))
            if valid:
                selected_run = last_run.name
                cell_status = "planned" if args.dry_run else "complete"
                completed_rates += 1
                break
            failures += 1
            if not args.dry_run and cell_attempts < args.max_attempts_per_rate and args.cooldown_s > 0:
                time.sleep(args.cooldown_s)
        if not selected_run and last_run is not None:
            selected_run = last_run.name
        selected_manifest: dict[str, Any] = {}
        selected_summary: dict[str, Any] = {}
        if selected_run:
            selected_path = session / selected_run
            try:
                selected_manifest = json.loads((selected_path / "manifest.json").read_text(encoding="utf-8"))
                selected_summary = json.loads((selected_path / "derived" / "custom_udp_trace_summary.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                selected_manifest = {}
                selected_summary = {}
        custom = selected_manifest.get("custom_udp", {})
        sender = custom.get("sender", {})
        receiver = custom.get("receiver", {})
        latency = selected_summary.get("latency", {})
        requested_mbps = parse_rate_mbps(rate, int(child["workload"].get("payload_bytes", 0) or 0))
        sender_mbps = sender.get("actual_payload_mbps", "")
        receiver_mbps = receiver.get("payload_mbps", "")
        delivery = ""
        if sender.get("sent_packets"):
            delivery = float(receiver.get("unique_packet_count", 0) or 0) / float(sender["sent_packets"])
        summary_rows.append([
            cell_index, rate, requested_mbps, parse_rate_pps(rate, int(child["workload"].get("payload_bytes", 0) or 0)),
            cell_status, cell_attempts, selected_run, sender_mbps, receiver_mbps,
            receiver.get("unique_packet_count", ""), receiver.get("payload_bytes", ""),
            selected_summary.get("capture_duration_ms", 0) / 1000 if selected_summary else "",
            delivery, latency.get("mean_ms", ""), latency.get("p50_ms", ""), latency.get("p95_ms", ""),
            latency.get("max_ms", ""), latency.get("interpolated_delay_windows", ""),
        ])
        if cell_status not in {"complete", "planned"} and not args.continue_on_failure:
            break
        if not args.dry_run and args.cooldown_s > 0 and cell_index < len(rates) and cell_status in {"complete", "planned"}:
            time.sleep(args.cooldown_s)
    with (session / "rate_sweep_summary.tsv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["cell_index", "requested_rate", "requested_mbps", "requested_pps", "status", "attempts", "selected_run_id", "sender_actual_mbps", "receiver_payload_mbps", "receiver_packets", "receiver_payload_bytes", "capture_span_s", "delivery_ratio", "latency_mean_ms", "latency_p50_ms", "latency_p95_ms", "latency_max_ms", "interpolated_delay_windows"])
        writer.writerows(summary_rows)
    write_json(session / "sweep_summary.json", {
        "status": "complete" if completed_rates == len(rates) else "incomplete",
        "rates": rates,
        "rate_count": len(rates),
        "completed_rates": completed_rates,
        "failed_rates": len(rates) - completed_rates,
        "attempts": total_attempts,
        "max_attempts_per_rate": args.max_attempts_per_rate,
        "cooldown_s": args.cooldown_s,
    })
    write_session_summaries(session)
    return 0 if completed_rates == len(rates) else 1


def _child_manifests(session: Path) -> list[tuple[Path, dict]]:
    children: list[tuple[Path, dict]] = []
    for manifest_path in sorted(session.glob("*/manifest.json")):
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            children.append((manifest_path.parent, value))
    return children


def _append_network_guard(session: Path, snapshot: dict, event: str) -> None:
    row = {"event": event, **snapshot}
    with (session / "network_guard.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def wait_for_network_guard(
    args: argparse.Namespace,
    roles: dict[str, str],
    session: Path,
    guard: dict[str, str],
) -> bool:
    if not guard:
        return True
    serial = roles.get("udp_sender") or roles.get("udp_receiver")
    deadline = time.monotonic() + max(0.0, args.network_wait_s)
    while True:
        snapshot = network_guard_snapshot(serial or "", args.server_ip, args.server_ssh, args.server_repo, guard)
        _append_network_guard(session, snapshot, "pre_run_check")
        if snapshot.get("status") == "ok":
            return True
        if time.monotonic() >= deadline:
            _append_network_guard(session, {"status": "timeout", "reasons": snapshot.get("reasons", [])}, "wait_timeout")
            return False
        print(
            "network guard waiting: " + "; ".join(snapshot.get("reasons", [])),
            file=sys.stderr,
            flush=True,
        )
        time.sleep(max(0.1, args.network_poll_s))


def _custom_success_count(session: Path, min_span_s: float | None, max_span_s: float | None) -> int:
    return sum(
        validate_custom_run_success(path, min_span_s=min_span_s, max_span_s=max_span_s)
        for path, manifest in _child_manifests(session)
        if manifest.get("workload", {}).get("kind") == "udp_flow"
        and manifest.get("workload", {}).get("postprocess") == "both"
    )


def _custom_success_count_for_generation(
    session: Path,
    min_span_s: float | None,
    max_span_s: float | None,
    generation: int,
) -> int:
    return sum(
        validate_custom_run_success(path, min_span_s=min_span_s, max_span_s=max_span_s)
        for path, manifest in _child_manifests(session)
        if manifest.get("workload", {}).get("kind") == "udp_flow"
        and manifest.get("workload", {}).get("postprocess") == "both"
        and int(manifest.get("environment", {}).get("generation", 1) or 1) == generation
    )


def _read_attempts(session: Path) -> int:
    try:
        return len((session / "runs.jsonl").read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def prepare_segment(
    session: Path,
    args: argparse.Namespace,
    generation: int,
    successes: int,
    attempts: int,
) -> dict[str, int] | None:
    if args.segment_successes is None and args.segment_max_attempts is None:
        return None
    if args.segment_successes is None or args.segment_max_attempts is None:
        raise ProfileError("--segment-successes and --segment-max-attempts must be used together")
    if args.segment_successes <= 0 or args.segment_max_attempts < args.segment_successes:
        raise ProfileError("segment attempts must be >= positive segment successes")
    session_json = session / "session.json"
    metadata = json.loads(session_json.read_text(encoding="utf-8"))
    active = metadata.get("active_segment")
    if isinstance(active, dict) and int(active.get("environment_generation", 0) or 0) == generation:
        if int(active.get("target_successes", 0) or 0) != args.segment_successes or int(active.get("max_attempts", 0) or 0) != args.segment_max_attempts:
            raise ProfileError("--segment-successes or --segment-max-attempts does not match the active segment")
        return {key: int(active[key]) for key in ("environment_generation", "baseline_successes", "baseline_attempts", "target_successes", "max_attempts")}
    segment = {
        "environment_generation": generation,
        "baseline_successes": successes,
        "baseline_attempts": attempts,
        "target_successes": args.segment_successes,
        "max_attempts": args.segment_max_attempts,
    }
    metadata["active_segment"] = segment
    write_json(session_json, metadata)
    _append_jsonl(session / "environment_changes.jsonl", {
        "event": "segment_started", **segment,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    return segment


def _read_trace_rows(path: Path) -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                rows.append((int(float(row["time_ms"])), int(float(row["payload_bytes"]))))
    except (OSError, KeyError, TypeError, ValueError):
        return []
    return rows


def _read_number_lines(path: Path) -> list[float]:
    try:
        return [float(line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def write_session_summaries(session: Path) -> None:
    """Write compact per-run tables and optional 200ms comparison plots."""

    bandwidth_lines: list[list[object]] = []
    latency_lines: list[list[object]] = []
    bandwidth_series: dict[str, list[tuple[int, float]]] = {}
    latency_series: dict[str, list[tuple[int, float]]] = {}
    for path, manifest in _child_manifests(session):
        run_id = path.name
        environment = manifest.get("environment", {})
        generation = int(environment.get("generation", 1) or 1) if isinstance(environment, dict) else 1
        roles = manifest.get("roles", {})
        serial = roles.get("udp_sender") or roles.get("udp_receiver") or ""
        guard = environment.get("network_guard", {}) if isinstance(environment, dict) else {}
        model = guard.get("device_model", "") if isinstance(guard, dict) else ""
        if not model:
            model = manifest.get("preflight", {}).get("network_guard", {}).get("model", "")
        label = f"g{generation}:{serial}:{run_id}"
        summary_path = path / "derived" / "custom_udp_trace_summary.json"
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = {}
        sender = manifest.get("custom_udp", {}).get("sender", {})
        receiver = manifest.get("custom_udp", {}).get("receiver", {})
        span_ms = float(summary.get("capture_duration_ms", 0.0) or 0.0)
        bandwidth_lines.append([
            run_id, generation, serial, model, manifest.get("status", ""), manifest.get("workload", {}).get("rate", ""),
            manifest.get("workload", {}).get("payload_bytes", ""), sender.get("actual_payload_mbps", ""),
            receiver.get("payload_mbps", ""), receiver.get("payload_bytes", ""), span_ms / 1000.0,
            manifest.get("custom_udp", {}).get("latency_stats", {}).get("window_count", ""),
            sender.get("sender_limited", ""),
        ])
        latency_stats = manifest.get("custom_udp", {}).get("latency_stats", {})
        latency_lines.append([
            run_id, generation, serial, model, manifest.get("status", ""), latency_stats.get("valid_rows", ""),
            latency_stats.get("window_count", ""), latency_stats.get("observed_window_count", ""),
            latency_stats.get("interpolated_window_count", ""), latency_stats.get("mean_ms", ""),
            latency_stats.get("p50_ms", ""), latency_stats.get("p95_ms", ""),
            latency_stats.get("max_ms", ""), latency_stats.get("window_start_ms", ""),
            latency_stats.get("window_end_ms", ""),
        ])
        aggregate: dict[int, int] = {}
        for time_ms, payload in _read_trace_rows(path / "derived" / "bandwidth_trace.tsv"):
            aggregate[time_ms // 200] = aggregate.get(time_ms // 200, 0) + payload
        if aggregate:
            bandwidth_series[label] = sorted((bucket * 200, value * 8 / 0.2 / 1e6) for bucket, value in aggregate.items())
        delays = _read_number_lines(path / "derived" / "delay.txt")
        times = _read_number_lines(path / "derived" / "delay_window_time_ms.txt")
        latency_buckets: dict[int, list[float]] = {}
        for time_ms, delay in zip(times, delays):
            latency_buckets.setdefault(int(time_ms) // 200, []).append(delay)
        if latency_buckets:
            latency_series[label] = sorted((bucket * 200, statistics.fmean(values)) for bucket, values in latency_buckets.items())
    def write_table(name: str, header: list[str], rows: list[list[object]]) -> None:
        with (session / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
    write_table(
        "bandwidth_summary.tsv",
        ["run_id", "environment_generation", "device_serial", "device_model", "status", "requested_rate", "payload_bytes", "sender_actual_mbps", "receiver_payload_mbps", "receiver_payload_bytes", "capture_span_s", "latency_window_count", "sender_limited"],
        bandwidth_lines,
    )
    write_table(
        "latency_summary.tsv",
        ["run_id", "environment_generation", "device_serial", "device_model", "status", "valid_rows", "window_count", "observed_window_count", "interpolated_window_count", "mean_ms", "p50_ms", "p95_ms", "max_ms", "window_start_ms", "window_end_ms"],
        latency_lines,
    )
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        if bandwidth_series:
            figure, axis = plt.subplots(figsize=(12, 5))
            for run_id, values in bandwidth_series.items():
                axis.plot([item[0] / 1000 for item in values], [item[1] for item in values], label=run_id, linewidth=0.8)
            axis.set(xlabel="time (s)", ylabel="payload Mbps", title="Custom UDP bandwidth, 200ms aggregation")
            axis.legend(fontsize=6, loc="upper right")
            figure.tight_layout()
            figure.savefig(session / "bandwidth_200ms_overlay.png", dpi=140)
            plt.close(figure)
        if latency_series:
            figure, axis = plt.subplots(figsize=(12, 5))
            for run_id, values in latency_series.items():
                axis.plot([item[0] / 1000 for item in values], [item[1] for item in values], label=run_id, linewidth=0.8)
            axis.set(xlabel="time (s)", ylabel="one-way latency (ms)", title="Custom UDP latency, 200ms aggregation")
            axis.legend(fontsize=6, loc="upper right")
            figure.tight_layout()
            figure.savefig(session / "latency_200ms_overlay.png", dpi=140)
            plt.close(figure)
    except Exception as error:  # Plotting is optional.
        (session / "summary_plot_error.txt").write_text(str(error) + "\n", encoding="utf-8")


def write_repeat_summary(
    session: Path,
    *,
    successes: int,
    failures: int,
    attempts: int,
    target: int,
    max_attempts: int,
    network_invalid: int = 0,
    status: str | None = None,
    segment: dict[str, int] | None = None,
) -> None:
    path = session / "repeat_summary.json"
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict):
            _append_jsonl(session / "repeat_summary_history.jsonl", {
                "event": "summary_snapshot", "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "summary": previous,
            })
    summary: dict[str, Any] = {
            "successes": successes,
            "failures": failures,
            "network_invalid": network_invalid,
            "attempts": attempts,
            "measurement_attempts": attempts,
            "target": target,
            "max_attempts": max_attempts,
            "status": status or ("complete" if successes >= target else "failed"),
    }
    if segment:
        segment_successes = max(0, successes - segment["baseline_successes"])
        segment_attempts = max(0, attempts - segment["baseline_attempts"])
        summary.update({
            "segment_environment_generation": segment["environment_generation"],
            "new_segment_successes": segment_successes,
            "new_segment_target": segment["target_successes"],
            "new_segment_attempts": segment_attempts,
            "new_segment_max_attempts": segment["max_attempts"],
        })
    write_json(path, summary)


def repeat_command(args: argparse.Namespace) -> int:
    profile, roles = profile_and_roles(args)
    if args.successes is None and args.segment_successes is None:
        raise ProfileError("repeat requires --successes or --segment-successes")
    if args.successes is not None and args.successes <= 0:
        raise ProfileError("--successes must be positive")
    if args.segment_successes is not None and args.successes is not None:
        raise ProfileError("use either --successes or --segment-successes, not both")
    guard, min_span_s, max_span_s = _guard_and_span(args)
    if args.resume and not args.session_dir:
        raise ProfileError("--resume requires --session-dir")
    session = session_directory(args, profile)
    generation = write_session_metadata(session, args, profile, roles, "repeat", resume=args.resume, network_guard=guard)
    is_custom = profile["workload"].get("kind") == "udp_flow" and profile["workload"].get("postprocess") == "both"
    successes = _custom_success_count(session, min_span_s, max_span_s) if is_custom else sum(
        manifest.get("status") == "complete" for _, manifest in _child_manifests(session)
    )
    attempts = _read_attempts(session)
    segment = prepare_segment(session, args, generation, successes, attempts)
    if segment:
        target = segment["baseline_successes"] + segment["target_successes"]
        max_attempts = segment["baseline_attempts"] + segment["max_attempts"]
    else:
        target = args.successes or 0
        max_attempts = args.max_attempts if args.max_attempts is not None else target
        if max_attempts < target:
            raise ProfileError("--max-attempts must be >= --successes")
    failures = max(0, attempts - successes)
    network_invalid = sum(manifest.get("status") == "network_invalid" for _, manifest in _child_manifests(session))
    timed_out = False
    while successes < target and attempts < max_attempts:
        if not args.dry_run and not wait_for_network_guard(args, roles, session, guard):
            timed_out = True
            break
        attempts += 1
        result, run_dir = run_one(args, profile, roles, session, attempts, generation)
        manifest = {}
        try:
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        postflight = manifest.get("postflight", {}).get("network_guard")
        if isinstance(postflight, dict):
            _append_network_guard(session, postflight, "post_run_check")
        if result == 0 and (args.dry_run or not is_custom or validate_custom_run_success(run_dir, min_span_s=min_span_s, max_span_s=max_span_s)):
            successes += 1
        else:
            failures += 1
        if manifest.get("status") == "network_invalid":
            network_invalid += 1
        write_repeat_summary(
            session, successes=successes, failures=failures, attempts=attempts,
            target=target, max_attempts=max_attempts, network_invalid=network_invalid,
            status="complete" if successes >= target else "running", segment=segment,
        )
        if successes >= target:
            break
        if not args.dry_run and args.cooldown_s > 0 and attempts < max_attempts:
            time.sleep(args.cooldown_s)
    status = "complete" if successes >= target else "failed"
    if timed_out:
        status = "network_timeout"
    write_repeat_summary(session, successes=successes, failures=failures, attempts=attempts, target=target, max_attempts=max_attempts, network_invalid=network_invalid, status=status, segment=segment)
    write_session_summaries(session)
    return 0 if status == "complete" else 1


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "run":
            return run_command(args)
        if args.command == "sweep":
            return sweep_command(args)
        return repeat_command(args)
    except ProfileError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
