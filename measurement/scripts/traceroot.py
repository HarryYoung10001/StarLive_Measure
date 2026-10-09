#!/usr/bin/env python3
"""Root-phone measurement runner.

This is intentionally a parallel entry point to ``runner.py``.  Existing
measurement and trace runs keep their v1 lifecycle; root runs use v2/v3 profiles,
``adb shell su -c``, and packet captures while emitting the same derived file
names consumed by the analysis scripts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import shlex
import subprocess
import sys
import time
import shutil
from typing import Any

from starlive_measure.paths import ANDROID_ROOT, MEASUREMENT_ROOT, REPO_ROOT, SERVER_ROOT, remote_server_repo
from starlive_measure.root.runtime import (
    RootProfileError,
    build_su_command,
    build_su_script_command,
    collect_clock_sync,
    load_root_profile,
    process_root_pcap,
    root_command_text,
    root_preflight_commands,
    root_metadata,
    run_root_checked,
    start_phone_capture,
    stop_phone_capture,
    write_root_manifest,
)
from starlive_measure.trace.trace_runtime import (
    append_event,
    make_run_paths,
    parse_roles,
    remote_command,
    scp_remote_files,
    start_process,
    wait_remote_ready,
    write_json,
)
from starlive_measure.root.packet_ledger import (
    LedgerError, delete_phone_pcap, extract_on_phone, install_extractor,
    process_ledger, read_manifest as read_ledger_manifest, transfer_from_phone, transfer_from_server,
)
from starlive_measure.root.reference_sweep import aggregate_sweep, derive_reference_run
from starlive_measure.trace.PcapBandwidthTrace import read_tcpdump_stats


REPO = REPO_ROOT
DEFAULT_SERVER_REPO = remote_server_repo()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("run", "sweep", "repeat"):
        command = sub.add_parser(name)
        command.add_argument("--profile", type=Path, required=True)
        command.add_argument("--server-ip", required=True)
        command.add_argument("--server-ssh", default="sgp-server")
        command.add_argument("--server-repo", default=DEFAULT_SERVER_REPO)
        command.add_argument("--role", action="append", default=[], metavar="ROLE=SERIAL")
        command.add_argument("--session-dir", type=Path)
        command.add_argument("--logs-dir", type=Path)
        command.add_argument("--rate")
        command.add_argument("--duration", type=float)
        command.add_argument("--interval", type=float)
        command.add_argument("--port", type=int)
        command.add_argument("--payload", type=int)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--phone-tcpdump", default="/data/local/tmp/tcpdump")
        command.add_argument("--phone-iperf3", default="/data/local/tmp/iperf3")
        command.add_argument("--clock-offset-ms", type=float)
        command.add_argument("--icmp-workers", type=int, default=1)
        command.add_argument("--icmp-pps", type=float, default=0.0,
                             help="run a parallel root ping on the workload phone at this target packet rate")
        command.add_argument("--server-ledger", action="store_true",
                             help="extract and transfer the server pcap as a verified packet ledger after measurement")
        if name == "sweep":
            command.add_argument("--rates", required=True)
            command.add_argument("--resume", action="store_true",
                                 help="resume a sweep in the existing --session-dir")
            command.add_argument("--retry-rate", action="append", default=[], metavar="RATE",
                                 help="after inspecting a failed/invalid attempt, retry this rate once")
        if name == "repeat":
            command.add_argument("--successes", type=int, required=True)
            command.add_argument("--max-attempts", type=int)
            command.add_argument("--cooldown-s", type=float, default=0)
    recover = sub.add_parser("recover", help="finish a v3 phone ledger transfer without rerunning traffic")
    recover.add_argument("--run-dir", type=Path, required=True)
    recover.add_argument("--role", action="append", default=[], metavar="ROLE=SERIAL")
    recover.add_argument("--server-ssh")
    recover.add_argument("--server-repo")
    return root


def apply_overrides(profile: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result = copy.deepcopy({key: value for key, value in profile.items() if not key.startswith("_")})
    workload = result["workload"]
    if args.rate is not None:
        workload["rate"] = args.rate
    if args.duration is not None:
        workload["duration_s"] = args.duration
    if args.interval is not None:
        workload["interval_s"] = args.interval
    if args.port is not None:
        result["server"]["port"] = args.port
    if args.payload is not None:
        workload["payload_bytes"] = args.payload
    if args.icmp_workers != 1:
        workload["icmp_workers"] = args.icmp_workers
    from starlive_measure.root.runtime import validate_root_profile

    validate_root_profile(result)
    result["_hash"] = __import__("hashlib").sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def session_dir(args: argparse.Namespace, profile: dict[str, Any]) -> Path:
    if args.session_dir:
        path = args.session_dir
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = args.logs_dir or (MEASUREMENT_ROOT / "logs")
        path = path / f"{profile['name']}_{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_session(path: Path, args: argparse.Namespace, profile: dict[str, Any], roles: dict[str, str], mode: str) -> None:
    write_json(path / "session.json", {
        "schema_version": profile["schema_version"],
        "collection_mode": "root",
        "mode": mode,
        "profile": profile["name"],
        "profile_hash": profile.get("_hash"),
        "server_ip": args.server_ip,
        "server_ssh": args.server_ssh,
        "server_repo": args.server_repo,
        "roles": roles,
        "icmp_companion_pps": getattr(args, "icmp_pps", 0.0),
        "server_packet_ledger": getattr(args, "server_ledger", False),
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def workload_command(profile: dict[str, Any], args: argparse.Namespace, serial: str) -> list[str]:
    workload = profile["workload"]
    kind = workload["kind"]
    if kind == "icmp":
        workers = int(workload.get("icmp_workers", args.icmp_workers))
        if workers < 1:
            raise RootProfileError("--icmp-workers must be >= 1")
        ping = "ping -i {interval} -w {duration} {target}".format(
            interval=shlex.quote(str(workload.get("interval_s", 0.2))),
            duration=shlex.quote(str(workload["duration_s"])),
            target=shlex.quote(args.server_ip),
        )
        if workers == 1:
            return build_su_command(serial, ["ping", "-i", str(workload.get("interval_s", 0.2)), "-w", str(workload["duration_s"]), args.server_ip])
        # Android ping throttles each process independently. Run multiple
        # root workers and concatenate their summaries for aggregate pps.
        # Use a shell loop for portable PID collection; Android sh does not
        # guarantee wait-by-job syntax across builds.
        launch = [
            'set -u',
            'dir="/data/local/tmp/traceroot_icmp_$$"',
            'mkdir -p "$dir"',
            'pids=""',
        ]
        pids = []
        for index in range(1, workers + 1):
            launch.append(f'{ping} > "$dir/worker_{index}.log" 2>&1 & pids="$pids $!"')
        launch.extend([
            'rc=0',
            'for pid in $pids; do wait "$pid" || rc=1; done',
            'cat "$dir"/worker_*.log',
            'rm -rf "$dir"',
            'exit "$rc"',
        ])
        return build_su_command(serial, ["sh", "-c", "; ".join(launch)])
    if kind == "udp_echo_rtt":
        return build_su_command(serial, [
            "/data/local/tmp/measurement_fine_rtt_probe", "--run-id", "root", "--target", args.server_ip,
            "--port", str(profile["server"]["port"]), "--duration-s", str(workload["duration_s"]),
            "--interval-ms", str(round(float(workload["interval_s"]) * 1000)), "--payload-bytes", str(workload["payload_bytes"]),
            "--timeout-ms", "2000", "--out-dir", "/data/local/tmp/traceroot_echo_out",
        ])
    rate = str(workload["rate"])
    if rate.lower().endswith("pps"):
        # iperf3 accepts bitrates, while the profile also permits an explicit
        # packet-rate target for sparse latency probes.
        packets_per_second = float(rate[:-3])
        rate = f"{packets_per_second * int(workload['payload_bytes']) * 8 / 1_000_000:g}M"
    command = [args.phone_iperf3, "-c", args.server_ip, "-p", str(profile["server"]["port"]), "-u", "-b", rate, "-l", str(workload["payload_bytes"]), "-t", str(workload["duration_s"]), "-i", str(workload.get("interval_s", 0.2)), "-J"]
    if workload["direction"] == "downlink":
        command.append("-R")
    if profile["schema_version"] == 3:
        command.append("--udp-counters-64bit")
    return build_su_command(serial, command)


def companion_ping_command(serial: str, target: str, duration_s: float, target_pps: float) -> tuple[list[str], int, float]:
    """Use parallel low-rate toybox ping workers when one process is throttled."""
    workers = max(1, int(math.ceil(target_pps / 70.0)))
    interval = workers / target_pps
    ping = "ping -D -i {interval} -w {duration} {target}".format(
        interval=shlex.quote(f"{interval:.9f}"), duration=shlex.quote(str(duration_s)), target=shlex.quote(target))
    if workers == 1:
        return build_su_command(serial, ["ping", "-D", "-i", f"{interval:.9f}", "-w", str(duration_s), target]), workers, interval
    launch = ['set -u', 'dir="/data/local/tmp/traceroot_icmp_$$"', 'mkdir -p "$dir"', 'pids=""']
    for index in range(1, workers + 1):
        launch.append(f'{ping} > "$dir/worker_{index}.log" 2>&1 & pids="$pids $!"')
    launch.extend(['rc=0', 'for pid in $pids; do wait "$pid" || rc=1; done',
                   'cat "$dir"/worker_*.log', 'rm -rf "$dir"', 'exit "$rc"'])
    command = build_su_command(serial, ["sh", "-c", "; ".join(launch)])
    return command, workers, interval


def remote_server_command(profile: dict[str, Any], args: argparse.Namespace, remote_dir: str) -> list[str]:
    workload = profile["workload"]
    tool_dir = f"{args.server_repo}/measurement/logs/.traceroot_tools"
    if workload["kind"] == "udp_echo_rtt":
        command = ["python3", f"{tool_dir}/trace_server.py", "udp-echo-receiver", "--run-dir", remote_dir, "--receiver-bin", f"{tool_dir}/udp_echo_receiver", "--port", str(profile["server"]["port"]), "--run-id", "root", "--ready-file", f"{remote_dir}/ready"]
        if ledger_enabled(profile):
            command.extend(["--tcpdump", "--interface", str(profile["collection"].get("server_interface", "auto")),
                            "--snaplen", str(profile["collection"].get("pcap_snaplen", 256)), "--pcap-processor", f"{tool_dir}/PcapBandwidthTrace.py",
                            "--derived-dir", f"{remote_dir}/derived", "--duration-ms", str(round(float(workload["duration_s"]) * 1000)),
                            "--payload-bytes", str(workload["payload_bytes"])])
    else:
        command = ["python3", f"{tool_dir}/trace_server.py", "iperf-receiver", "--run-dir", remote_dir, "--port", str(profile["server"]["port"]), "--interval", str(workload.get("interval_s", 0.2)), "--direction", workload.get("direction", "uplink"), "--interface", str(profile.get("collection", {}).get("server_interface", "auto")), "--snaplen", str(profile.get("collection", {}).get("pcap_snaplen", 256)), "--tcpdump", "--pcap-processor", f"{tool_dir}/PcapBandwidthTrace.py", "--derived-dir", f"{remote_dir}/derived", "--duration-ms", str(round(float(workload.get("duration_s", 0)) * 1000)), "--payload-bytes", str(workload.get("payload_bytes", 0))]
        if ledger_enabled(profile):
            command.extend(["--timeout-s", str(float(workload["duration_s"]) + 180)])
    if ledger_enabled(profile):
        script = f"{shlex.join(command)} & pid=$!; echo $pid > {shlex.quote(remote_dir + '/helper.pid')}; wait $pid; rc=$?; echo $rc > {shlex.quote(remote_dir + '/helper.exit_code')}; exit $rc"
        command = ["sh", "-c", script]
    return remote_command(args.server_ssh, command)


def ledger_enabled(profile: dict[str, Any]) -> bool:
    return profile["schema_version"] == 3 and profile["collection"].get("phone_capture", True) and profile["workload"]["kind"] != "icmp"


def checkpoint(run_dir: Path, manifest: dict[str, Any], event: str | None = None, **fields: Any) -> None:
    temporary = run_dir / "manifest.json.part"
    write_json(temporary, manifest)
    temporary.replace(run_dir / "manifest.json")
    if event:
        append_event(run_dir, event, **fields)


def pull_server_artifacts(run_dir: Path, manifest: dict[str, Any], profile: dict[str, Any]) -> int:
    if profile["workload"]["kind"] == "icmp":
        return 0
    names = ["exit_code.txt", "helper.exit_code"] if profile["workload"]["kind"] == "udp_echo_rtt" else ["iperf3_server.json", "tcpdump.stderr", "metadata.txt", "exit_code.txt", "pcap_postprocess.json", "derived"]
    if ledger_enabled(profile):
        # The phone ledger supplies local per-packet data. Keep the server's
        # much larger arrival text remote; transfer only bounded server
        # evidence needed to verify its capture and bandwidth derivation.
        if profile["workload"]["kind"] == "udp_echo_rtt":
            names.extend(["tcpdump.stderr", "pcap_postprocess.json"])
        else:
            names.remove("derived")
        names.append("derived/pcap_bandwidth_summary.json")
    if ledger_enabled(profile) and "helper.exit_code" not in names:
        names.append("helper.exit_code")
    # v2 helpers do not create the helper completion marker.
    if not ledger_enabled(profile) and "helper.exit_code" in names:
        names.remove("helper.exit_code")
    target = run_dir / "roles" / "server" / "remote"
    rc = scp_remote_files(manifest["server"]["ssh"], manifest["server"]["run_dir"], names, target)
    if ledger_enabled(profile) and (not (target / "helper.exit_code").is_file() or (target / "helper.exit_code").read_text().strip() != "0"):
        rc = rc or 1
    if ledger_enabled(profile) and (target / "exit_code.txt").is_file():
        codes = dict(line.split("=", 1) for line in (target / "exit_code.txt").read_text().splitlines() if "=" in line)
        for name in (("udp_echo_receiver", "tcpdump", "pcap_postprocess") if profile["workload"]["kind"] == "udp_echo_rtt" else ("iperf3", "tcpdump", "pcap_postprocess")):
            if codes.get(name) != "0":
                rc = rc or 1
    manifest["artifacts"]["server_pull_rc"] = rc
    manifest["artifacts"]["server_pcap_postprocess_rc"] = 0
    postprocess = target / "pcap_postprocess.json"
    if profile["workload"]["kind"] != "udp_echo_rtt" or ledger_enabled(profile):
        if not postprocess.is_file():
            manifest["artifacts"]["server_pcap_postprocess_rc"] = 1
        else:
            report = json.loads(postprocess.read_text(encoding="utf-8"))
            manifest["artifacts"]["server_pcap_postprocess_rc"] = int(report.get("exit_code", 1 if report.get("status") != "complete" else 0))
    # The phone ledger always owns local bandwidth outputs for v3.
    if not ledger_enabled(profile) and rc == 0 and (target / "derived").is_dir():
        for item in (target / "derived").iterdir():
            if item.is_file():
                shutil.copy2(item, run_dir / "derived" / item.name)
    checkpoint(run_dir, manifest, "server_artifacts_collected", exit_code=rc)
    return rc


def finish_server_ledger(run_dir: Path, manifest: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Extract only after server helper/tcpdump exit, then pull verified chunks."""
    state = manifest["server_transfer"]
    remote_dir = manifest["server"]["run_dir"]
    local_dir = run_dir / state["local_dir"]
    stderr = run_dir / "roles" / "server" / "remote" / "tcpdump.stderr"
    if any(manifest.get("lifecycle", {}).get(key) != 0 for key in
           ("workload_exit_code", "phone_capture_exit_code", "server_helper_exit_code")):
        raise LedgerError("server extraction is deferred until workload and both capture processes exit")
    if state.get("extraction") != "complete":
        state["extraction"] = "running"
        checkpoint(run_dir, manifest, "server_ledger_extraction_started")
        manifest_path = f"{remote_dir}/packet_ledger/ledger_manifest.json"
        exists = subprocess.run(remote_command(manifest["server"]["ssh"], ["test", "-f", manifest_path]),
                                capture_output=True, text=True, timeout=60)
        if exists.returncode:
            stats = read_tcpdump_stats(stderr)
            if any(stats.get(key) is None for key in
                   ("packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel")):
                raise LedgerError("server tcpdump final counters are unavailable; keeping remote pcap")
            pcap = f"{remote_dir}/iperf3_{profile['workload']['direction']}_udp_{profile['server']['port']}.pcap"
            command = [state["remote_binary"], "--input", pcap, "--out-dir", f"{remote_dir}/packet_ledger",
                       "--port", str(profile["server"]["port"]), "--direction", "source",
                       "--tcpdump-captured", str(stats["packets_captured"]),
                       "--tcpdump-received-by-filter", str(stats["packets_received_by_filter"]),
                       "--tcpdump-dropped-by-kernel", str(stats["packets_dropped_by_kernel"]), "--decode-iperf"]
            result = subprocess.run(remote_command(manifest["server"]["ssh"], command), capture_output=True,
                                    text=True, timeout=1800)
            if result.returncode:
                raise LedgerError(result.stderr.strip() or result.stdout.strip() or "server packet-ledger extraction failed")
        state["extraction"] = "complete"
        checkpoint(run_dir, manifest, "server_ledger_extraction_finished")
    state["transfer"] = "running"
    checkpoint(run_dir, manifest, "server_ledger_transfer_started")
    ledger, fetched = transfer_from_server(manifest["server"]["ssh"], remote_dir, local_dir)
    expected_direction = "source" if profile["workload"]["direction"] == "downlink" else "destination"
    if ledger["port"] != profile["server"]["port"] or ledger["direction"] != expected_direction:
        raise LedgerError("server packet ledger does not match this run's port/direction")
    stats = read_tcpdump_stats(stderr)
    if any(stats.get(key) != ledger["tcpdump"][key] for key in
           ("packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel")):
        raise LedgerError("server ledger counters differ from tcpdump's final output")
    state.update({"transfer": "complete", "verification": "complete", "chunks": len(ledger["chunks"]),
                  "last_fetched_chunks": fetched, "pcap_sha256": ledger["pcap_sha256"],
                  "ledger_sha256": ledger["ledger_sha256"], "pcap_size_bytes": ledger["pcap_size_bytes"]})
    checkpoint(run_dir, manifest, "server_ledger_transfer_finished", fetched_chunks=fetched)
    return ledger


def finish_ledger(run_dir: Path, manifest: dict[str, Any], profile: dict[str, Any], serial: str) -> int:
    """Only callable after the durable measurement completion barriers."""
    state = manifest["phone_transfer"]
    lifecycle = manifest.get("lifecycle", {})
    if any(lifecycle.get(key) != 0 for key in ("workload_exit_code", "phone_capture_exit_code", "server_helper_exit_code")):
        raise LedgerError("workload and both capture/helper exits must be confirmed before extraction")
    local = run_dir / state["local_dir"]
    try:
        if state.get("extraction") != "complete":
            state["extraction"] = "running"
            checkpoint(run_dir, manifest, "phone_ledger_extraction_started")
            existing = run_root_checked(serial, ["test", "-f", f"{state['remote_dir']}/packet_ledger/ledger_manifest.json"], timeout=30)
            if existing.returncode:
                extract_on_phone(serial, state["remote_dir"], run_dir / "roles" / manifest["root_execution"]["role"] / "tcpdump.stderr",
                                 port=profile["server"]["port"], direction="source" if profile["workload"]["direction"] == "downlink" else "destination",
                                 decode_iperf=profile["workload"]["kind"] in {"iperf3", "udp_flow"})
            state["extraction"] = "complete"
            checkpoint(run_dir, manifest, "phone_ledger_extraction_finished")
        if state.get("verification") != "complete" or state.get("derivation") != "complete":
            state["transfer"] = "running"
            checkpoint(run_dir, manifest, "phone_ledger_transfer_started")
            ledger, fetched = transfer_from_phone(serial, state["remote_dir"], local)
            expected_direction = "source" if profile["workload"]["direction"] == "downlink" else "destination"
            if ledger["port"] != profile["server"]["port"] or ledger["direction"] != expected_direction:
                raise LedgerError("phone ledger does not match this run's port/direction")
            stats = read_tcpdump_stats(run_dir / "roles" / manifest["root_execution"]["role"] / "tcpdump.stderr")
            if any(stats[key] != ledger["tcpdump"][key] for key in ("packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel")):
                raise LedgerError("phone manifest counters differ from tcpdump's final output")
            state.update({"transfer": "complete", "chunks": len(ledger["chunks"]), "last_fetched_chunks": fetched,
                          "pcap_sha256": ledger["pcap_sha256"], "ledger_sha256": ledger["ledger_sha256"], "pcap_size_bytes": ledger["pcap_size_bytes"]})
            checkpoint(run_dir, manifest, "phone_ledger_transfer_finished", fetched_chunks=fetched)
            state.update({"verification": "running", "derivation": "running"})
            checkpoint(run_dir, manifest, "phone_ledger_verification_started")
            summary = process_ledger(local, run_dir, duration_ms=round(profile["workload"]["duration_s"] * 1000), clock_sync=manifest["clock_sync"])
            state.update({"verification": "complete", "derivation": "complete"})
            manifest["root_transport"] = summary
            checkpoint(run_dir, manifest, "phone_ledger_verified")
        else:
            # A recovery attempt verifies local data again before retrying deletion.
            manifest["root_transport"] = process_ledger(local, run_dir, duration_ms=round(profile["workload"]["duration_s"] * 1000), clock_sync=manifest["clock_sync"])
        if manifest["artifacts"].get("server_pull_rc", 1) != 0 or manifest["artifacts"].get("server_pcap_postprocess_rc", 1) != 0:
            raise LedgerError("server artifacts or derivation are incomplete; phone pcap retained")
        if "server_transfer" in manifest:
            finish_server_ledger(run_dir, manifest, profile)
            run_summary = derive_reference_run(run_dir, offered_rate=str(profile["workload"]["rate"]),
                                               duration_s=float(profile["workload"]["duration_s"]),
                                               server_ip=manifest["server"]["ip"],
                                               uncertainty_limit_ms=float(profile["clock_sync"].get("max_uncertainty_ms", 5.0)))
            manifest["reference_sweep"] = run_summary
            checkpoint(run_dir, manifest, "reference_flow_derived", status=run_summary["status"])
            if run_summary["status"] != "complete":
                raise LedgerError("invalid rate capture: " + ", ".join(run_summary.get("invalid_reasons", [])))
        if state.get("pcap_cleanup") != "complete":
            state["pcap_cleanup"] = "running"
            checkpoint(run_dir, manifest, "phone_pcap_cleanup_started")
            delete_phone_pcap(serial, state["remote_dir"])
            state["pcap_cleanup"] = "complete"
            checkpoint(run_dir, manifest, "phone_pcap_cleanup_finished")
        state.pop("error", None)
        manifest.pop("failure_reason", None)
        manifest.update({"status": "complete", "exit_code": 0})
    except (LedgerError, OSError, ValueError, subprocess.SubprocessError) as error:
        for phase in ("extraction", "transfer", "verification", "derivation", "pcap_cleanup"):
            if state.get(phase) == "running":
                state[phase] = "failed"
        server_state = manifest.get("server_transfer", {})
        for phase in ("extraction", "transfer", "verification"):
            if server_state.get(phase) == "running":
                server_state[phase] = "failed"
        state["error"] = str(error)
        manifest.update({"status": "recoverable", "exit_code": 1, "failure_reason": str(error)})
    checkpoint(run_dir, manifest, "finished", status=manifest["status"])
    return manifest["exit_code"]


def recover_run(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    profile = load_root_profile(run_dir / "profile.json")
    if not ledger_enabled(profile) or "phone_transfer" not in manifest:
        raise LedgerError("recover requires a schema v3 phone packet-ledger run")
    role = manifest["root_execution"]["role"]
    overrides = parse_roles(args.role)
    if set(overrides) - {role}:
        raise LedgerError(f"recover accepts only the original phone role {role}")
    serial = overrides.get(role, manifest.get("recovery", {}).get("serial", manifest["roles"][role]))
    manifest["recovery"] = {"serial": serial, "role": role, "attempt_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.server_ssh:
        manifest["server"]["ssh"] = args.server_ssh
    if args.server_repo:
        manifest["server"]["repo"] = args.server_repo
        manifest["server"]["run_dir"] = f"{args.server_repo}/measurement/logs/.traceroot_runs/{run_dir.name}"
    checkpoint(run_dir, manifest, "recovery_started", serial=serial)
    state = manifest["phone_transfer"]
    remote = state["remote_dir"]
    try:
        lifecycle = manifest.setdefault("lifecycle", {})
        if lifecycle.get("workload_exit_code") != 0:
            result = run_root_checked(serial, ["cat", f"{remote}/workload.exit_code"], timeout=30)
            if result.returncode or result.stdout.strip() != "0":
                raise LedgerError("successful workload exit is not confirmed; recover never reruns traffic")
            lifecycle["workload_exit_code"] = 0
            checkpoint(run_dir, manifest, "workload_finished", exit_code=0, recovered=True)
        if lifecycle.get("phone_capture_exit_code") != 0:
            # The phone shell writes its marker only after tcpdump has flushed and exited.
            script = f"if test ! -f {shlex.quote(remote + '/capture.exit_code')}; then kill -INT $(cat {shlex.quote(remote + '/tcpdump.pid')}); fi"
            subprocess.run(build_su_script_command(serial, script), capture_output=True, timeout=30)
            result = run_root_checked(serial, ["cat", f"{remote}/capture.exit_code"], timeout=30)
            if result.returncode or result.stdout.strip() != "0":
                raise LedgerError("phone capture has not confirmed a clean exit; retry recover after it stops")
            lifecycle["phone_capture_exit_code"] = 0
            checkpoint(run_dir, manifest, "phone_capture_finished", exit_code=0, recovered=True)
        if lifecycle.get("server_helper_exit_code") != 0:
            if profile["workload"]["kind"] == "udp_echo_rtt":
                remote_server = manifest["server"]["run_dir"]
                script = f"test -f {shlex.quote(remote_server + '/helper.exit_code')} || kill -TERM $(cat {shlex.quote(remote_server + '/helper.pid')})"
                subprocess.run(remote_command(manifest["server"]["ssh"], ["sh", "-c", script]), capture_output=True, timeout=60)
            result = subprocess.run(remote_command(manifest["server"]["ssh"], ["cat", f"{manifest['server']['run_dir']}/helper.exit_code"]), capture_output=True, text=True, timeout=60)
            if result.returncode or result.stdout.strip() != "0":
                raise LedgerError("server helper has not confirmed a clean exit; phone extraction remains deferred")
            lifecycle["server_helper_exit_code"] = 0
            checkpoint(run_dir, manifest, "server_helper_finished", exit_code=0, recovered=True)
        if state.get("extraction") != "complete":
            from starlive_measure.trace.trace_runtime import android_binary

            install_extractor(serial, android_binary(ANDROID_ROOT / "probes" / "pcap_packet_ledger.go", serial))
            stderr = run_dir / "roles" / role / "tcpdump.stderr"
            temporary = stderr.with_suffix(".stderr.part")
            pull = subprocess.run(["adb", "-s", serial, "pull", f"{remote}/tcpdump.stderr", str(temporary)], capture_output=True, text=True, timeout=120)
            if pull.returncode:
                raise LedgerError("cannot recover phone tcpdump counters")
            temporary.replace(stderr)
        else:
            existing = run_root_checked(serial, ["test", "-f", f"{remote}/packet_ledger/ledger_manifest.json"], timeout=30)
            if existing.returncode and state.get("pcap_cleanup") != "complete":
                state["extraction"] = "pending"
                checkpoint(run_dir, manifest, "phone_ledger_manifest_missing")
        if manifest["artifacts"].get("server_pull_rc", 1) != 0:
            pull_server_artifacts(run_dir, manifest, profile)
        # Corrupt local data is fetched again, even when a prior attempt reached verification.
        if state.get("verification") == "complete":
            try:
                process_ledger(run_dir / state["local_dir"], run_dir, duration_ms=round(profile["workload"]["duration_s"] * 1000), clock_sync=manifest["clock_sync"])
            except (OSError, ValueError):
                state.update({"verification": "pending", "derivation": "pending"})
        return finish_ledger(run_dir, manifest, profile, serial)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        manifest.update({"status": "recoverable", "exit_code": 1, "failure_reason": str(error)})
        state["error"] = str(error)
        checkpoint(run_dir, manifest, "recovery_deferred", reason=str(error))
        return 1


def run_once(args: argparse.Namespace, profile: dict[str, Any], roles: dict[str, str], session: Path,
             index: int | None = None, on_created: Any | None = None) -> tuple[int, Path]:
    run_dir = make_run_paths(session, profile, index)
    if on_created is not None:
        on_created(run_dir)
    role = next((name for name in ("udp_sender", "udp_receiver", "udp_echo_phone", "icmp_phone") if name in roles), "")
    serial = roles[role]
    remote_dir = f"{args.server_repo}/measurement/logs/.traceroot_runs/{run_dir.name}"
    phone_commands = root_preflight_commands(profile, serial, tcpdump=args.phone_tcpdump, iperf3=args.phone_iperf3)
    if profile["schema_version"] == 3 and profile["workload"]["kind"] in {"iperf3", "udp_flow"}:
        phone_commands.append(build_su_script_command(serial, f"{shlex.quote(args.phone_iperf3)} --help 2>&1 | grep -q -- udp-counters-64bit"))
    workload = workload_command(profile, args, serial)
    use_ledger = ledger_enabled(profile)
    icmp_pps = float(getattr(args, "icmp_pps", 0.0))
    use_server_ledger = bool(getattr(args, "server_ledger", False))
    if icmp_pps < 0:
        raise RootProfileError("--icmp-pps cannot be negative")
    if use_server_ledger and (not use_ledger or profile["workload"]["kind"] != "iperf3" or profile["workload"]["direction"] != "downlink"):
        raise RootProfileError("--server-ledger requires a schema v3 root iperf3 downlink profile")
    if use_server_ledger and icmp_pps <= 0:
        raise RootProfileError("--server-ledger reference sweeps require --icmp-pps")
    ping_command = None
    ping_workers = 0
    ping_interval_s = 0.0
    if icmp_pps > 0:
        ping_command, ping_workers, ping_interval_s = companion_ping_command(
            serial, args.server_ip, float(profile["workload"]["duration_s"]), icmp_pps)
    phone_remote = f"/data/local/tmp/traceroot_{run_dir.name}"
    if use_ledger:
        script = f"{workload[-1]}; rc=$?; echo $rc > {shlex.quote(phone_remote + '/workload.exit_code')}; exit $rc"
        workload = build_su_script_command(serial, script)
    commands = [*phone_commands, workload, *([ping_command] if ping_command else []), remote_server_command(profile, args, remote_dir)]
    manifest = write_root_manifest(run_dir, profile, args.server_ip, args.server_ssh, roles, commands, status="planned" if args.dry_run else "preflight")
    manifest["server"]["repo"] = args.server_repo
    manifest["server"]["run_dir"] = remote_dir
    manifest["collection"]["phone_tcpdump"] = args.phone_tcpdump
    manifest["collection"]["phone_iperf3"] = args.phone_iperf3
    manifest["lifecycle"] = {}
    if icmp_pps > 0:
        manifest["icmp_companion"] = {"target_pps": icmp_pps, "worker_count": ping_workers,
                                       "per_worker_interval_s": ping_interval_s, "command": ping_command, "status": "pending"}
    if use_ledger:
        manifest["phone_transfer"] = {"strategy": "packet_ledger_v1", "remote_dir": phone_remote,
                                      "local_dir": f"roles/{role}/packet_ledger", "extraction": "pending", "transfer": "pending",
                                      "verification": "pending", "derivation": "pending", "pcap_cleanup": "pending",
                                      "chunk_timeout_s": 120, "chunk_attempts": 3}
    if use_server_ledger:
        manifest["server_transfer"] = {"strategy": "packet_ledger_v1", "remote_binary": f"{args.server_repo}/measurement/logs/.traceroot_tools/pcap_packet_ledger",
                                        "local_dir": "roles/server/packet_ledger", "remote_dir": f"{remote_dir}/packet_ledger",
                                        "extraction": "pending", "transfer": "pending", "verification": "pending",
                                        "chunk_timeout_s": 120, "chunk_attempts": 3}
    write_json(run_dir / "manifest.json", manifest)
    if args.dry_run:
        manifest["preflight"] = {"status": "skipped", "mode": "dry-run", "commands": [{"argv": cmd} for cmd in phone_commands]}
        manifest["status"] = "planned"
        write_json(run_dir / "manifest.json", manifest)
        append_event(run_dir, "preflight_skipped", reason="dry-run")
        return 0, run_dir

    for command in phone_commands:
        check = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
        if check.returncode != 0 or (command[-1] == "id -u" and check.stdout.strip() != "0"):
            manifest["status"] = "failed"
            manifest["failure_reason"] = check.stderr.strip() or "root preflight failed"
            write_json(run_dir / "manifest.json", manifest)
            return 2, run_dir
    manifest["preflight"] = {"status": "ok", "phone_uid": 0, "executor": "su"}
    write_json(run_dir / "manifest.json", manifest)
    clock = {"status": "unavailable", "required": profile["clock_sync"].get("required", True), "reason": "clock sync not requested for ICMP"}
    if profile["workload"]["kind"] != "icmp":
        try:
            clock = collect_clock_sync(serial, args.server_ssh, samples=5)
            clock["required"] = profile["clock_sync"].get("required", True)
            clock["max_uncertainty_ms"] = profile["clock_sync"].get("max_uncertainty_ms", 5.0)
        except (OSError, subprocess.SubprocessError) as error:
            clock = {"status": "unavailable", "required": profile["clock_sync"].get("required", True), "reason": str(error)}
    manifest["clock_sync"] = clock
    checkpoint(run_dir, manifest)
    write_json(run_dir / "derived" / "clock_sync.json", clock)
    if use_ledger:
        from starlive_measure.trace.trace_runtime import android_binary

        try:
            binary = android_binary(ANDROID_ROOT / "probes" / "pcap_packet_ledger.go", serial)
            extractor_digest = hashlib.sha256(binary.read_bytes()).hexdigest()
            if getattr(args, "_phone_extractor_digest", None) != extractor_digest:
                install_extractor(serial, binary)
                args._phone_extractor_digest = extractor_digest
                checkpoint(run_dir, manifest, "phone_ledger_extractor_installed", sha256=extractor_digest)
        except (LedgerError, ValueError, OSError, subprocess.SubprocessError) as error:
            manifest.update({"status": "failed", "failure_reason": str(error), "exit_code": 2})
            checkpoint(run_dir, manifest)
            return 2, run_dir
    if profile["workload"]["kind"] == "udp_echo_rtt":
        from starlive_measure.trace.trace_runtime import android_binary

        echo_binary = android_binary(ANDROID_ROOT / "probes" / "fine_rtt_probe.go", serial)
        push = subprocess.run(["adb", "-s", serial, "push", str(echo_binary), "/data/local/tmp/measurement_fine_rtt_probe"], capture_output=True, text=True, check=False, timeout=180)
        if push.returncode != 0:
            manifest["status"] = "failed"
            manifest["failure_reason"] = push.stderr.strip() or "failed to install root echo probe"
            write_json(run_dir / "manifest.json", manifest)
            return 2, run_dir
        subprocess.run(["adb", "-s", serial, "shell", "chmod", "755", "/data/local/tmp/measurement_fine_rtt_probe"], check=False)
    if profile["workload"]["kind"] != "icmp":
        tool_dir = f"{args.server_repo}/measurement/logs/.traceroot_tools"
        remote_mkdir = subprocess.run(remote_command(args.server_ssh, ["mkdir", "-p", remote_dir, tool_dir]), capture_output=True, text=True, check=False, timeout=60)
        if remote_mkdir.returncode != 0:
            manifest["status"] = "failed"
            manifest["failure_reason"] = remote_mkdir.stderr.strip() or "remote run directory failed"
            write_json(run_dir / "manifest.json", manifest)
            return 2, run_dir
        uploads = [
            (SERVER_ROOT / "trace_server.py", f"{tool_dir}/trace_server.py"),
            (REPO / "src" / "starlive_measure" / "trace" / "PcapBandwidthTrace.py", f"{tool_dir}/PcapBandwidthTrace.py"),
            (REPO / "src" / "starlive_measure" / "trace" / "BandwidthTrace.py", f"{tool_dir}/BandwidthTrace.py"),
        ]
        if use_server_ledger:
            from starlive_measure.trace.trace_runtime import build_binary

            extractor = build_binary(ANDROID_ROOT / "probes" / "pcap_packet_ledger.go", "linux", "amd64")
            uploads.append((extractor, f"{tool_dir}/pcap_packet_ledger"))
        if profile["workload"]["kind"] == "udp_echo_rtt":
            from starlive_measure.trace.trace_runtime import build_binary

            uploads.append((build_binary(SERVER_ROOT / "udp_echo_receiver.go", "linux", "amd64"), f"{tool_dir}/udp_echo_receiver"))
        for local, remote in uploads:
            local_digest = hashlib.sha256(local.read_bytes()).hexdigest()
            cached_tools = getattr(args, "_server_tool_digests", {})
            if cached_tools.get(remote) == local_digest:
                remote_digest = local_digest
            else:
                remote_hash = subprocess.run(remote_command(args.server_ssh, ["sha256sum", remote]), capture_output=True,
                                             text=True, check=False, timeout=60)
                remote_parts = remote_hash.stdout.split()
                remote_digest = remote_parts[0] if remote_hash.returncode == 0 and remote_parts else ""
            upload = None
            if remote_digest != local_digest:
                upload = subprocess.run(["scp", "-O", "-q", "-o", "ConnectTimeout=30", str(local), f"{args.server_ssh}:{remote}"], capture_output=True, text=True, check=False, timeout=180)
            if upload is not None and upload.returncode != 0:
                manifest["status"] = "failed"
                manifest["failure_reason"] = upload.stderr.strip() or f"failed to upload {local.name}"
                write_json(run_dir / "manifest.json", manifest)
                return 2, run_dir
            cached_tools = getattr(args, "_server_tool_digests", {})
            cached_tools[remote] = local_digest
            args._server_tool_digests = cached_tools
        if use_server_ledger:
            manifest["server_transfer"]["remote_binary"] = f"{tool_dir}/pcap_packet_ledger"
            chmod = subprocess.run(remote_command(args.server_ssh, ["chmod", "755", f"{tool_dir}/pcap_packet_ledger"]),
                                   capture_output=True, text=True, check=False, timeout=60)
            if chmod.returncode:
                manifest["status"] = "failed"
                manifest["failure_reason"] = chmod.stderr.strip() or "failed to install server packet-ledger extractor"
                write_json(run_dir / "manifest.json", manifest)
                return 2, run_dir
    phone_dir = run_dir / "roles" / role
    phone_dir.mkdir(parents=True, exist_ok=True)
    server_process = None
    capture_process = None
    phone_remote_dir = ""
    if profile["workload"]["kind"] != "icmp":
        server_process = start_process(remote_server_command(profile, args, remote_dir), run_dir / "roles" / "server" / "ssh.stdout", run_dir / "roles" / "server" / "ssh.stderr")
        if not wait_remote_ready(args.server_ssh, f"{remote_dir}/ready", timeout_s=60):
            server_process.terminate()
            server_process.wait(timeout=10)
            manifest["status"] = "failed"
            manifest["failure_reason"] = "remote server did not become ready"
            write_json(run_dir / "manifest.json", manifest)
            return 2, run_dir
    if profile.get("collection", {}).get("phone_capture", True):
        capture_process, phone_remote_dir, _phone_pcap = start_phone_capture(profile, serial, run_dir.name, int(profile["server"]["port"]), phone_dir)
        time.sleep(0.5)
    ping_process = None
    ping_started = None
    if ping_command is not None:
        ping_started = time.monotonic()
        ping_process = start_process(ping_command, phone_dir / "icmp_ping.log", phone_dir / "icmp_ping.stderr")
        manifest["icmp_companion"]["status"] = "running"
        checkpoint(run_dir, manifest, "icmp_companion_started", target_pps=icmp_pps)
    manifest["status"] = "measuring"
    checkpoint(run_dir, manifest, "workload_started")
    workload_process = start_process(workload, phone_dir / "workload.stdout", phone_dir / "workload.stderr")
    try:
        workload_rc = workload_process.wait(timeout=float(profile["workload"]["duration_s"]) + 90)
    except subprocess.TimeoutExpired:
        workload_process.terminate()
        workload_process.wait(timeout=10)
        workload_rc = 124
    manifest["lifecycle"]["workload_exit_code"] = workload_rc
    checkpoint(run_dir, manifest, "workload_finished", exit_code=workload_rc)
    if ping_process is not None:
        try:
            ping_rc = ping_process.wait(timeout=max(60.0, float(profile["workload"]["duration_s"]) + 60.0))
        except subprocess.TimeoutExpired:
            ping_process.terminate()
            try:
                ping_rc = ping_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ping_process.kill()
                ping_process.wait()
                ping_rc = 124
        ping_elapsed = max(0.0, time.monotonic() - float(ping_started))
        manifest["icmp_companion"].update({"status": "finished", "exit_code": ping_rc, "elapsed_s": ping_elapsed})
        manifest["lifecycle"]["ping_exit_code"] = ping_rc
        checkpoint(run_dir, manifest, "icmp_companion_finished", exit_code=ping_rc, elapsed_s=ping_elapsed)
    capture_rc = 0
    if capture_process is not None:
        try:
            capture_rc = stop_phone_capture(serial, phone_remote_dir, capture_process, phone_dir, pull=not use_ledger)
        except (OSError, subprocess.SubprocessError) as error:
            capture_rc = 124
            manifest["failure_reason"] = str(error)
    manifest["lifecycle"]["phone_capture_exit_code"] = capture_rc
    checkpoint(run_dir, manifest, "phone_capture_finished", exit_code=capture_rc)
    if profile["workload"]["kind"] == "udp_echo_rtt":
        echo_pull = subprocess.run(["adb", "-s", serial, "pull", "/data/local/tmp/traceroot_echo_out", str(phone_dir / "echo_remote")], capture_output=True, text=True, check=False, timeout=120)
        manifest["artifacts"]["echo_pull_rc"] = echo_pull.returncode
    if server_process is not None:
        if profile["workload"]["kind"] == "udp_echo_rtt" and use_ledger:
            script = f"kill -TERM $(cat {shlex.quote(remote_dir + '/helper.pid')})"
            subprocess.run(remote_command(args.server_ssh, ["sh", "-c", script]), capture_output=True, timeout=30)
        elif not use_ledger:
            server_process.terminate()
        try:
            server_rc = server_process.wait(timeout=600 if use_ledger else 10)
        except subprocess.TimeoutExpired:
            server_process.terminate()
            server_process.kill()
            server_process.wait()
            server_rc = 124
    else:
        server_rc = 0
    manifest["lifecycle"]["server_helper_exit_code"] = server_rc
    checkpoint(run_dir, manifest, "server_helper_finished", exit_code=server_rc)
    server_role = run_dir / "roles" / "server"
    pull_rc = 0
    if server_process is not None:
        try:
            pull_rc = pull_server_artifacts(run_dir, manifest, profile)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pull_rc = 1
            manifest["artifacts"]["server_pull_rc"] = 1
            manifest["failure_reason"] = str(error)
    if use_ledger:
        manifest["artifacts"]["phone_pcap"] = {"path": f"{phone_remote_dir}/root_capture.pcap", "transfer": "phone_until_verified"}
        direction = profile["workload"]["direction"]
        filename = f"udp_echo_{profile['server']['port']}.pcap" if profile["workload"]["kind"] == "udp_echo_rtt" else f"iperf3_{direction}_udp_{profile['server']['port']}.pcap"
        manifest["artifacts"]["server_pcap"] = {"path": f"{remote_dir}/{filename}", "transfer": "remote_only"}
        manifest["status"] = "recoverable"
        checkpoint(run_dir, manifest)
        if workload_rc != 0 or capture_rc != 0 or server_rc != 0 or (ping_process is not None and manifest["lifecycle"].get("ping_exit_code") != 0):
            manifest.update({"exit_code": 1, "failure_reason": "measurement completion was not confirmed; recover will inspect durable exit markers"})
            checkpoint(run_dir, manifest, "finished", status=manifest["status"])
            return 1, run_dir
        return finish_ledger(run_dir, manifest, profile, serial), run_dir
    pcap = phone_dir / "root_capture.pcap"
    if pcap.is_file():
        pcap_summary = process_root_pcap(pcap, run_dir, port=int(profile["server"]["port"]), duration_ms=round(float(profile["workload"]["duration_s"]) * 1000), payload_bytes=int(profile["workload"].get("payload_bytes", 0) or 0), source="phone_pcap", direction=str(profile["workload"].get("direction", "uplink")), clock_sync=clock)
    else:
        pcap_summary = {"status": "unavailable", "reason": "phone pcap was not pulled", "clock_sync": clock}
        write_json(run_dir / "derived" / "root_transport_summary.json", pcap_summary)
    direction = str(profile["workload"].get("direction", "uplink"))
    postprocess_rc = 0
    postprocess_path = server_role / "remote" / "pcap_postprocess.json"
    if postprocess_path.is_file():
        try:
            postprocess = json.loads(postprocess_path.read_text(encoding="utf-8"))
            postprocess_rc = int(postprocess.get("exit_code", 1 if postprocess.get("status") != "complete" else 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            postprocess_rc = 1
    manifest["artifacts"].update({"phone_pcap": {"path": str(pcap.relative_to(run_dir)) if pcap.exists() else "", "exists": pcap.exists(), "size_bytes": pcap.stat().st_size if pcap.exists() else 0}, "server_pcap": {"path": f"{remote_dir}/iperf3_{direction}_udp_{profile['server']['port']}.pcap", "transfer": "remote_only"}, "server_pull_rc": pull_rc, "phone_capture_rc": capture_rc, "server_pcap_postprocess_rc": postprocess_rc})
    manifest["root_transport"] = pcap_summary
    manifest["status"] = "complete" if workload_rc == 0 and capture_rc == 0 and pull_rc == 0 and postprocess_rc == 0 else "failed"
    manifest["exit_code"] = 0 if manifest["status"] == "complete" else 1
    write_json(run_dir / "manifest.json", manifest)
    append_event(run_dir, "finished", status=manifest["status"], workload_exit_code=workload_rc)
    return manifest["exit_code"], run_dir


def _sweep_config(args: argparse.Namespace, profile: dict[str, Any], roles: dict[str, str], rates: list[str]) -> dict[str, Any]:
    workload = profile["workload"]
    return {
        "profile": profile["name"], "profile_hash": profile.get("_hash"),
        "role_names": sorted(roles), "server_ip": args.server_ip,
        "server_ssh": args.server_ssh, "server_repo": args.server_repo,
        "rates": rates, "duration_s": float(workload["duration_s"]),
        "interval_s": float(workload.get("interval_s", 0.2)),
        "payload_bytes": int(workload.get("payload_bytes", 0) or 0),
        "icmp_pps": float(args.icmp_pps), "server_ledger": bool(args.server_ledger),
    }


def _save_sweep_state(session: Path, state: dict[str, Any]) -> None:
    attempts_by_rate: dict[str, list[dict[str, Any]]] = {rate: [] for rate in state["config"]["rates"]}
    for attempt in state["attempts"]:
        attempts_by_rate.setdefault(attempt["rate"], []).append(attempt)
    valid_rates = []
    terminal_invalid_rates = []
    pending_rates = []
    for rate in state["config"]["rates"]:
        attempts = attempts_by_rate.get(rate, [])
        selected_id = state["selected_run_ids"].get(rate)
        selected = next((item for item in reversed(attempts) if item["run_id"] == selected_id), None)
        if selected and selected.get("analysis_valid") is True and selected["status"] == "complete":
            valid_rates.append(rate)
        elif (len(attempts) >= 2 and selected and selected["status"] == "invalid"
              and attempts[-1]["run_id"] == selected_id and attempts[-1]["status"] == "invalid"):
            terminal_invalid_rates.append(rate)
        else:
            pending_rates.append(rate)
    state["completed_rates"] = valid_rates
    state["invalid_rates"] = terminal_invalid_rates
    state["pending_rates"] = pending_rates
    if state.get("status") != "planned":
        if not pending_rates and not terminal_invalid_rates:
            state["status"] = "complete"
        elif not pending_rates:
            state["status"] = "complete_with_invalid"
        else:
            state["status"] = "in_progress"
    temporary = session / "sweep_summary.json.part"
    write_json(temporary, state)
    temporary.replace(session / "sweep_summary.json")


def _attempt_from_run(run_dir: Path, rate: str, index: int, code: int) -> dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    summary_path = run_dir / "derived" / "reference_flow_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    analysis_valid = summary.get("analysis_valid")
    if analysis_valid is True and manifest.get("status") == "complete" and code == 0:
        status = "complete"
    elif analysis_valid is False:
        status = "invalid"
    else:
        status = str(manifest.get("status", "failed"))
    return {
        "index": index, "rate": rate, "run_id": run_dir.name,
        "status": status, "exit_code": code,
        "analysis_valid": analysis_valid,
        "invalid_reasons": summary.get("invalid_reasons", []),
        "failure_reason": manifest.get("failure_reason", ""),
    }


def _reconcile_sweep_attempts(session: Path, state: dict[str, Any]) -> None:
    """Refresh checkpoint rows in case recover completed after the sweep stopped."""
    for attempt in state["attempts"]:
        run_dir = session / attempt["run_id"]
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        summary_path = run_dir / "derived" / "reference_flow_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        valid = summary.get("analysis_valid")
        if valid is True and manifest.get("status") == "complete":
            attempt.update({"status": "complete", "analysis_valid": True, "invalid_reasons": [],
                            "failure_reason": "", "exit_code": 0})
            state["selected_run_ids"][attempt["rate"]] = attempt["run_id"]
        elif valid is False:
            attempt.update({"status": "invalid", "analysis_valid": False,
                            "invalid_reasons": summary.get("invalid_reasons", []),
                            "failure_reason": manifest.get("failure_reason", "")})
            state["selected_run_ids"][attempt["rate"]] = attempt["run_id"]
        elif attempt.get("status") == "running":
            attempt.update({"status": str(manifest.get("status", "recoverable")),
                            "failure_reason": manifest.get("failure_reason", "")})


def _aggregate_selected_sweep(session: Path, state: dict[str, Any]) -> None:
    selected_ids = state["selected_run_ids"]
    summaries = []
    for rate, run_id in selected_ids.items():
        path = session / run_id / "derived" / "reference_flow_summary.json"
        if path.is_file():
            summaries.append(json.loads(path.read_text(encoding="utf-8")))
    relative_latency = any(row.get("latency_mode") == "relative_per_rate_p1_subtracted" for row in summaries)
    aggregate_sweep(session, relative_latency=relative_latency, selected_run_ids=selected_ids)


def _run_sweep(args: argparse.Namespace, profile: dict[str, Any], roles: dict[str, str],
               session: Path, rates: list[str]) -> int:
    if not rates or len(set(rates)) != len(rates):
        raise RootProfileError("--rates must contain unique, non-empty rates")
    if args.retry_rate and not args.resume:
        raise RootProfileError("--retry-rate requires --resume")
    if args.resume and args.dry_run:
        raise RootProfileError("--resume cannot be combined with --dry-run")
    if args.resume and args.session_dir is None:
        raise RootProfileError("--resume requires the original --session-dir")
    state_path = session / "sweep_summary.json"
    config = _sweep_config(args, profile, roles, rates)
    if args.resume:
        if not state_path.is_file():
            raise RootProfileError("--resume requires an existing sweep_summary.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("config") != config:
            raise RootProfileError("resume settings do not match the original sweep; rates, duration, server, and profile must match")
        session_path = session / "session.json"
        session_info = json.loads(session_path.read_text(encoding="utf-8")) if session_path.is_file() else {}
        session_info["roles"] = roles
        session_info["resumed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        write_json(session_path, session_info)
        _reconcile_sweep_attempts(session, state)
    else:
        if state_path.exists() or any(session.glob("root_udp_downlink_capacity_*")):
            raise RootProfileError("session directory already contains sweep data; use --resume or choose a new --session-dir")
        write_session(session, args, profile, roles, "sweep")
        state = {"schema_version": 1, "status": "planned" if args.dry_run else "in_progress",
                 "config": config, "attempts": [], "selected_run_ids": {}}
        session_info = json.loads((session / "session.json").read_text(encoding="utf-8"))
        session_info["requested_rates"] = rates
        session_info["duration_s"] = config["duration_s"]
        write_json(session / "session.json", session_info)

    retry_rates = list(args.retry_rate)
    if len(set(retry_rates)) != len(retry_rates) or any(rate not in rates for rate in retry_rates):
        raise RootProfileError("--retry-rate values must be unique members of --rates")
    _reconcile_sweep_attempts(session, state)
    _save_sweep_state(session, state)

    for rate in rates:
        prior = [item for item in state["attempts"] if item["rate"] == rate]
        selected_id = state["selected_run_ids"].get(rate)
        selected = next((item for item in reversed(prior) if item["run_id"] == selected_id), None)
        if selected and selected.get("status") == "complete" and selected.get("analysis_valid") is True:
            if rate in retry_rates:
                raise RootProfileError(f"{rate} already has a valid selected run; refusing an unnecessary retry")
            continue
        if prior:
            last = prior[-1]
            if len(prior) >= 2:
                if last["status"] == "invalid":
                    if rate in retry_rates:
                        raise RootProfileError(f"{rate} already used its single retry")
                    continue
                print(f"Sweep stopped at {rate}: latest attempt is {last['status']}; recover or inspect it before continuing", file=sys.stderr)
                _aggregate_selected_sweep(session, state)
                _save_sweep_state(session, state)
                return 1
            if rate not in retry_rates:
                print(f"Sweep stopped at {rate}: inspect the previous attempt, then use --resume --retry-rate {rate}", file=sys.stderr)
                _aggregate_selected_sweep(session, state)
                _save_sweep_state(session, state)
                return 1
            if last["status"] == "recoverable":
                previous_run = session / last["run_id"]
                previous_summary = previous_run / "derived" / "reference_flow_summary.json"
                if not previous_summary.is_file():
                    raise RootProfileError(f"{rate} has incomplete artifacts; run recover on {previous_run} before retrying traffic")
        elif rate in retry_rates:
            raise RootProfileError(f"{rate} has no previous attempt to retry")

        child = copy.deepcopy(profile)
        child["workload"]["rate"] = rate
        attempt_index = len(state["attempts"]) + 1
        attempt_row: dict[str, Any] = {"index": attempt_index, "rate": rate, "run_id": "", "status": "running"}

        def record_created(run_dir: Path) -> None:
            attempt_row["run_id"] = run_dir.name
            state["attempts"].append(attempt_row)
            _save_sweep_state(session, state)

        code, run_dir = run_once(args, child, roles, session, attempt_index, on_created=record_created)
        updated = _attempt_from_run(run_dir, rate, attempt_index, code)
        attempt_row.update(updated)
        if updated["analysis_valid"] is not None:
            state["selected_run_ids"][rate] = run_dir.name
        _save_sweep_state(session, state)
        if not args.dry_run:
            _aggregate_selected_sweep(session, state)
        elif len(state["attempts"]) == len(rates):
            state["status"] = "planned"
            _save_sweep_state(session, state)

        if args.dry_run:
            continue
        if code != 0:
            if updated["status"] == "invalid" and len([x for x in state["attempts"] if x["rate"] == rate]) >= 2:
                continue
            return code or 1
        if updated["status"] != "complete":
            return 1

    _save_sweep_state(session, state)
    if not args.dry_run:
        _aggregate_selected_sweep(session, state)
    return 0 if state["status"] == "complete" else (0 if args.dry_run else 1)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "recover":
            return recover_run(args)
        base = load_root_profile(args.profile)
        profile = apply_overrides(base, args)
        roles = parse_roles(args.role)
        required = set(profile["roles"].get("required", []))
        missing = sorted(required - set(roles))
        if missing:
            raise RootProfileError("required role is missing: " + ", ".join(missing))
        if len(set(roles.values())) != len(roles):
            raise RootProfileError("ADB role serials must be distinct")
        session = session_dir(args, profile)
        if args.command == "run":
            write_session(session, args, profile, roles, "run")
            code, _ = run_once(args, profile, roles, session)
            return code
        if args.command == "sweep":
            rates = [value.strip() for value in args.rates.split(",") if value.strip()]
            return _run_sweep(args, profile, roles, session, rates)
        write_session(session, args, profile, roles, "repeat")
        target = args.successes
        maximum = args.max_attempts or target
        if maximum < target:
            raise RootProfileError("--max-attempts must be >= --successes")
        successes = 0
        rows = []
        for index in range(1, maximum + 1):
            code, run_dir = run_once(args, profile, roles, session, index)
            rows.append({"attempt": index, "run_id": run_dir.name, "status": "complete" if code == 0 else "failed"})
            if code == 0:
                successes += 1
            if successes >= target:
                break
            if not args.dry_run and args.cooldown_s:
                time.sleep(args.cooldown_s)
        write_json(session / "repeat_summary.json", {"status": "complete" if successes >= target else "failed", "successes": successes, "attempts": len(rows), "target": target, "runs": rows})
        return 0 if successes >= target else 1
    except (RootProfileError, LedgerError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
