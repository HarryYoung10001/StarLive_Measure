#!/usr/bin/env python3
"""Root Android collection helpers.

The root collector deliberately keeps device execution in one module.  The
runner records the exact argv passed to ``su -c`` so a run can be audited
without reconstructing shell snippets from logs.
"""

from __future__ import annotations

import copy
import csv
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import time
from typing import Any, Iterable

from starlive_measure.paths import REPO_ROOT
from starlive_measure.trace.PcapBandwidthTrace import PcapError, process_pcap, read_pcap
from starlive_measure.trace.trace_runtime import (
    append_event,
    make_run_paths,
    remote_command,
    scp_remote_files,
    start_process,
    write_json,
)


ROOT = REPO_ROOT
ROOT_PROFILE_SCHEMA = 3
ROOT_EXECUTORS = {"su"}
ROOT_RETENTION = {"phone_local_server_remote", "phone_ledger_local_server_remote"}
ROOT_KINDS = {"iperf3", "udp_flow", "udp_echo_rtt", "icmp"}


class RootProfileError(ValueError):
    """Raised when a root profile or root execution contract is invalid."""


def decode_iperf3_udp_header(payload: bytes, *, version: int = 1) -> dict[str, int] | None:
    """Decode the documented 16-byte iperf3 UDP timestamp header.

    iperf3 carries a signed 64-bit sequence followed by a 32-bit seconds and
    microseconds timestamp.  A short packet, invalid microseconds, negative
    sequence, or unknown decoder version returns ``None`` rather than treating
    arbitrary application payload bytes as a packet identity.
    """

    if version != 1 or len(payload) < 16:
        return None
    sequence = int.from_bytes(payload[0:8], "big", signed=True)
    seconds = int.from_bytes(payload[8:12], "big", signed=False)
    microseconds = int.from_bytes(payload[12:16], "big", signed=False)
    if sequence < 0 or microseconds >= 1_000_000:
        return None
    return {"sequence": sequence, "send_time_ns": seconds * 1_000_000_000 + microseconds * 1_000}


def match_transport_packets(sender: Iterable[dict[str, Any]], receiver: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Match decoded packet records by sequence without inventing losses."""

    sent: dict[int, dict[str, Any]] = {}
    for row in sender:
        try:
            sequence = int(row["sequence"])
        except (KeyError, TypeError, ValueError):
            continue
        sent.setdefault(sequence, row)
    received: dict[int, dict[str, Any]] = {}
    for row in receiver:
        try:
            sequence = int(row["sequence"])
        except (KeyError, TypeError, ValueError):
            continue
        received.setdefault(sequence, row)
    delivered = sorted(set(sent) & set(received))
    missing = sorted(set(sent) - set(received))
    return {
        "status": "ok" if sent else "unavailable",
        "sent_packets": len(sent),
        "received_unique_packets": len(received),
        "delivered_packets": len(delivered),
        "missing_sequences": missing,
        "loss_rate": (len(missing) / len(sent)) if sent else None,
        "matched": [{"sequence": sequence, "sender": sent[sequence], "receiver": received[sequence]} for sequence in delivered],
    }


def _positive(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise RootProfileError(f"{label} must be numeric") from error
    if not math.isfinite(parsed) or parsed <= 0:
        raise RootProfileError(f"{label} must be positive")
    return parsed


def validate_root_profile(profile: dict[str, Any]) -> None:
    """Validate root profiles, retaining the historical v2 contract."""

    schema = profile.get("schema_version")
    if schema not in {2, ROOT_PROFILE_SCHEMA}:
        raise RootProfileError("root profile schema_version must be 2 or 3")
    name = profile.get("name")
    if not isinstance(name, str) or not name.startswith("root_"):
        raise RootProfileError("root profile name must start with root_")
    workload = profile.get("workload")
    if not isinstance(workload, dict):
        raise RootProfileError("workload must be an object")
    kind = workload.get("kind")
    direction = workload.get("direction")
    if kind not in ROOT_KINDS:
        raise RootProfileError(f"unsupported root workload kind: {kind}")
    if direction not in {"uplink", "downlink", "none"}:
        raise RootProfileError("workload.direction must be uplink, downlink, or none")
    if kind == "icmp" and direction != "none":
        raise RootProfileError("icmp workload direction must be none")
    if kind != "icmp" and direction == "none":
        raise RootProfileError("non-ICMP root workload requires a direction")
    _positive(workload.get("duration_s"), "workload.duration_s")
    if kind == "udp_echo_rtt":
        _positive(workload.get("payload_bytes"), "workload.payload_bytes")
        _positive(workload.get("interval_s"), "workload.interval_s")
    elif kind != "icmp":
        rate = workload.get("rate")
        if not isinstance(rate, str) or not rate:
            raise RootProfileError("workload.rate is required")
        _positive(workload.get("payload_bytes"), "workload.payload_bytes")
        _positive(workload.get("interval_s"), "workload.interval_s")
    roles = profile.get("roles")
    if not isinstance(roles, dict):
        raise RootProfileError("roles must be an object")
    required = roles.get("required", [])
    optional = roles.get("optional", [])
    known_roles = {"udp_sender", "udp_receiver", "udp_echo_phone", "icmp_phone"}
    if not isinstance(required, list) or not isinstance(optional, list):
        raise RootProfileError("roles.required and roles.optional must be lists")
    if any(role not in known_roles for role in [*required, *optional]):
        raise RootProfileError("roles contains an unknown role")
    if set(required) & set(optional):
        raise RootProfileError("a role cannot be both required and optional")
    required_set = set(required)
    if kind == "icmp" and "icmp_phone" not in required_set:
        raise RootProfileError("icmp root profile must require icmp_phone")
    if kind == "udp_echo_rtt" and "udp_echo_phone" not in required_set:
        raise RootProfileError("udp_echo_rtt root profile must require udp_echo_phone")
    if kind in {"iperf3", "udp_flow"}:
        wanted = "udp_sender" if direction == "uplink" else "udp_receiver"
        if wanted not in required_set:
            raise RootProfileError(f"root profile must require {wanted}")
    collection = profile.get("collection")
    if not isinstance(collection, dict):
        raise RootProfileError("collection must be an object")
    if collection.get("mode") != "root":
        raise RootProfileError("collection.mode must be root")
    if collection.get("root_executor", "su") not in ROOT_EXECUTORS:
        raise RootProfileError("collection.root_executor must be su")
    if int(collection.get("require_uid", 0)) != 0:
        raise RootProfileError("collection.require_uid must be 0")
    if not isinstance(collection.get("phone_capture", True), bool):
        raise RootProfileError("collection.phone_capture must be boolean")
    if not isinstance(collection.get("server_capture", True), bool):
        raise RootProfileError("collection.server_capture must be boolean")
    snaplen = int(collection.get("pcap_snaplen", 256))
    buffer_mb = int(collection.get("pcap_buffer_mb", 8))
    if snaplen <= 0 or buffer_mb <= 0:
        raise RootProfileError("pcap_snaplen and pcap_buffer_mb must be positive")
    if collection.get("pcap_retention", "phone_local_server_remote") not in ROOT_RETENTION:
        raise RootProfileError("unsupported pcap_retention")
    if schema == 3 and collection.get("phone_capture", True) and collection.get("pcap_retention") != "phone_ledger_local_server_remote":
        raise RootProfileError("v3 phone capture requires phone_ledger_local_server_remote")
    if schema == 2 and collection.get("pcap_retention", "phone_local_server_remote") != "phone_local_server_remote":
        raise RootProfileError("v2 phone capture requires phone_local_server_remote")
    clock = profile.get("clock_sync", {})
    if not isinstance(clock, dict):
        raise RootProfileError("clock_sync must be an object")
    if not isinstance(clock.get("required", True), bool):
        raise RootProfileError("clock_sync.required must be boolean")
    _positive(clock.get("max_uncertainty_ms", 5.0), "clock_sync.max_uncertainty_ms")
    server = profile.get("server", {})
    if not isinstance(server, dict) or not 1 <= int(server.get("port", 0)) <= 65535:
        raise RootProfileError("server.port must be in 1..65535")


def load_root_profile(path: Path) -> dict[str, Any]:
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RootProfileError(f"cannot read root profile {path}: {error}") from error
    if not isinstance(profile, dict):
        raise RootProfileError("root profile must be an object")
    validate_root_profile(profile)
    result = copy.deepcopy(profile)
    result["_source"] = str(path.resolve())
    result["_hash"] = __import__("hashlib").sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def build_su_command(serial: str, argv: Iterable[str]) -> list[str]:
    """Build an argv-safe ``adb shell su -c`` invocation."""

    values = [str(value) for value in argv]
    if not serial or not values:
        raise RootProfileError("serial and root argv are required")
    return ["adb", "-s", serial, "shell", "su", "-c", shlex.join(values)]


def root_command_text(argv: Iterable[str]) -> str:
    return shlex.join([str(value) for value in argv])


def build_su_script_command(serial: str, script: str) -> list[str]:
    """Build a root command whose final argument is one shell script.

    ``adb shell su -c`` already evaluates its final argument as a command.
    Passing an additional ``sh -c`` argv tuple makes Android's shell perform
    another round of parsing and can split quoted paths such as ``mkdir -p``.
    """

    if not script.strip():
        raise RootProfileError("root script is required")
    return build_su_command(serial, [script])


def run_root_checked(serial: str, command: Iterable[str] | str, *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    """Get the inner root exit status even when adb/su reports success.

    Some Android su builds return zero to adb for a failed child.  An explicit
    sentinel from the root shell is required for file and extraction checks.
    """
    script = command if isinstance(command, str) else shlex.join([str(item) for item in command])
    marker = "__STARLIVE_ROOT_EXIT__="
    wrapped = f"({script}); root_rc=$?; echo {marker}$root_rc"
    result = subprocess.run(build_su_script_command(serial, wrapped), capture_output=True, text=True, check=False, timeout=timeout)
    lines = result.stdout.splitlines()
    if result.returncode == 0 and lines and lines[-1].strip().startswith(marker):
        try:
            inner_rc = int(lines[-1].strip()[len(marker):])
        except ValueError:
            inner_rc = 255
        output = "\n".join(lines[:-1]) + ("\n" if len(lines) > 1 else "")
        return subprocess.CompletedProcess(result.args, inner_rc, output, result.stderr)
    return subprocess.CompletedProcess(result.args, result.returncode or 255, result.stdout, result.stderr)


def estimate_clock_offset(samples: Iterable[dict[str, float]]) -> dict[str, Any]:
    """Estimate offset using NTP-style four timestamps.

    Each sample contains phone send/receive and server receive/send times in
    the same unit.  The minimum-RTT sample is used as the least-contended
    estimate; uncertainty is half the corresponding RTT.
    """

    normalized: list[dict[str, float]] = []
    for sample in samples:
        try:
            t1 = float(sample["phone_send_ns"])
            t2 = float(sample["server_receive_ns"])
            t3 = float(sample["server_send_ns"])
            t4 = float(sample["phone_receive_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        rtt = (t4 - t1) - (t3 - t2)
        offset = ((t2 - t1) + (t3 - t4)) / 2.0
        if rtt >= 0 and math.isfinite(offset):
            normalized.append({"rtt_ns": rtt, "offset_ns": offset})
    if not normalized:
        return {"status": "unavailable", "sample_count": 0, "offset_ns": None, "uncertainty_ns": None}
    best = min(normalized, key=lambda row: row["rtt_ns"])
    offsets = [row["offset_ns"] for row in normalized]
    spread = max(offsets) - min(offsets)
    return {
        "status": "ok",
        "sample_count": len(normalized),
        "offset_ns": best["offset_ns"],
        "rtt_ns": best["rtt_ns"],
        "uncertainty_ns": max(best["rtt_ns"] / 2.0, spread / 2.0),
        "samples": normalized,
    }


def collect_clock_sync(serial: str, server_ssh: str, samples: int = 5) -> dict[str, Any]:
    """Sample phone/server wall clocks through the same control plane.

    This is a coarse fallback for devices without chrony/NTP control.  The
    command round trips are retained as uncertainty; callers must enforce the
    profile threshold before accepting one-way latency.
    """

    rows: list[dict[str, float]] = []
    for _ in range(max(1, samples)):
        phone_start = time.time_ns()
        phone = subprocess.run(build_su_command(serial, ["date", "+%s%N"]), capture_output=True, text=True, check=False, timeout=30)
        phone_end = time.time_ns()
        server_start = time.time_ns()
        server = subprocess.run(remote_command(server_ssh, ["date", "+%s%N"]), capture_output=True, text=True, check=False, timeout=60)
        server_end = time.time_ns()
        if phone.returncode != 0 or server.returncode != 0:
            continue
        try:
            phone_ns = int(phone.stdout.strip().splitlines()[-1])
            server_ns = int(server.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            continue
        rows.append({
            "phone_ns": float(phone_ns),
            "server_ns": float(server_ns),
            "phone_uncertainty_ns": float(phone_end - phone_start) / 2.0,
            "server_uncertainty_ns": float(server_end - server_start) / 2.0,
            "offset_ns": float(phone_ns - server_ns),
        })
    if not rows:
        return {"status": "unavailable", "sample_count": 0, "reason": "phone/server date sampling failed"}
    best = min(rows, key=lambda row: row["phone_uncertainty_ns"] + row["server_uncertainty_ns"])
    uncertainty = best["phone_uncertainty_ns"] + best["server_uncertainty_ns"]
    return {
        "status": "ok",
        "sample_count": len(rows),
        "offset_ns": best["offset_ns"],
        "offset_ms": best["offset_ns"] / 1_000_000.0,
        "uncertainty_ns": uncertainty,
        "uncertainty_ms": uncertainty / 1_000_000.0,
        "samples": rows,
        "method": "adb_su_date_and_ssh_date",
        "sampled_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def root_metadata(profile: dict[str, Any], phone_role: str, serial: str) -> dict[str, Any]:
    collection = profile["collection"]
    return {
        "collection_mode": "root",
        "root_execution": {
            "executor": collection.get("root_executor", "su"),
            "phone_uid": 0,
            "role": phone_role,
            "serial": serial,
        },
        "capture_sources": {
            "phone_pcap": ("phone_until_verified" if profile["schema_version"] == 3 else "local") if collection.get("phone_capture", True) else "disabled",
            "phone_packet_ledger": "local" if profile["schema_version"] == 3 and collection.get("phone_capture", True) else "disabled",
            "server_pcap": "remote_only" if collection.get("server_capture", True) else "disabled",
        },
        "evidence_level": "packet_capture",
    }


def root_preflight_commands(profile: dict[str, Any], serial: str, *, tcpdump: str = "/data/local/tmp/tcpdump", iperf3: str = "/data/local/tmp/iperf3") -> list[list[str]]:
    """Return the commands used by preflight, suitable for dry-run manifests."""

    commands = [build_su_command(serial, ["id", "-u"])]
    if profile.get("collection", {}).get("phone_capture", True):
        commands.append(build_su_command(serial, ["test", "-x", tcpdump]))
    if profile["workload"]["kind"] in {"iperf3", "udp_flow"}:
        commands.append(build_su_command(serial, ["test", "-x", iperf3]))
    commands.append(build_su_command(serial, ["ip", "route", "get", "8.8.8.8"]))
    return commands


def _empty_derived_outputs(derived: Path, reason: str) -> None:
    derived.mkdir(parents=True, exist_ok=True)
    headers = {
        "udp_arrival_time_precise.tsv": "seq\tsend_time_s\treceive_time_s\tpayload_bytes\n",
        "udp_latency_precise.tsv": "seq\tsend_time_s\treceive_time_s\tdelay_ms\tpayload_bytes\n",
        "delay_precise.tsv": "window_start_ms\tmean_delay_ms\tsample_count\n",
        "bandwidth_100ms.tsv": "time_s\tbandwidth_mbps\n",
        "rtt_10ms.tsv": "time_s\trtt_ms\n",
        "loss_100ms.tsv": "time_s\tloss_rate\n",
    }
    for name, header in headers.items():
        (derived / name).write_text(header, encoding="utf-8")
    write_json(derived / "loss_summary.json", {"status": "unavailable", "reason": reason})


def process_root_pcap(
    pcap_path: Path,
    run_dir: Path,
    *,
    port: int,
    duration_ms: int,
    payload_bytes: int,
    source: str,
    direction: str = "uplink",
    clock_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create common measurement outputs from a root pcap.

    ``PcapBandwidthTrace`` is intentionally reused for packet parsing.  Root
    captures are never treated as application-level latency unless a valid
    clock calibration and decodable packet association are present.
    """

    derived = run_dir / "derived"
    # Create the complete compatibility surface up front.  The pcap parser
    # fills bandwidth files; latency/loss files remain header-only until a
    # decoder and clock calibration make them trustworthy.
    _empty_derived_outputs(derived, "root packet decoder or clock calibration unavailable")
    try:
        result = process_pcap(
            pcap_path,
            derived,
            port=port,
            duration_ms=duration_ms,
            window_ms=100,
            payload_bytes=payload_bytes,
            port_direction=("source" if direction == "downlink" else "destination"),
        )
        capture = read_pcap(
            pcap_path,
            port,
            port_direction=("source" if direction == "downlink" else "destination"),
        )
        with (derived / "pcap_packets.tsv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(["packet_index", "timestamp_ns", "payload_bytes", "captured_payload_bytes", "truncated"])
            for index, packet in enumerate(capture.packets):
                writer.writerow([index, packet.timestamp_ns, packet.payload_bytes, packet.captured_payload_bytes, int(packet.truncated)])
        summary = {
            "status": "complete",
            "source": source,
            "packet_count": len(capture.packets),
            "records_seen": capture.records_seen,
            "filtered_packets": capture.filtered_packets,
            "parse_errors": capture.parse_errors,
            "truncation_errors": capture.truncation_errors,
            "capture_duration_ms": result.get("capture_duration_ms", 0),
            "bandwidth_source": "root_pcap",
            "port_direction": "source" if direction == "downlink" else "destination",
            "latency_status": "unavailable",
            "loss_status": "unavailable",
            "clock_sync": clock_sync or {"status": "unavailable"},
        }
        # The common measurement readers use this stable filename.  Keep the
        # packet parser's detailed trace as well, but expose a 100 ms view for
        # root runs without making the analysis layer root-aware.
        trace = derived / "bandwidth_trace.tsv"
        bandwidth = derived / "bandwidth_100ms.tsv"
        if trace.is_file():
            lines = trace.read_text(encoding="utf-8", errors="replace").splitlines()
            if lines:
                bandwidth.write_text("time_s\tbandwidth_mbps\n" + "\n".join(
                    "{:.6f}\t{}".format(float(row.split("\t", 1)[0]) / 1000.0, row.rsplit("\t", 1)[-1])
                    for row in lines[1:] if "\t" in row
                ) + "\n", encoding="utf-8")
    except (OSError, PcapError, ValueError) as error:
        _empty_derived_outputs(derived, str(error))
        summary = {"status": "failed", "source": source, "error": str(error), "latency_status": "unavailable", "loss_status": "unavailable"}
    write_json(derived / "root_transport_summary.json", summary)
    return summary


def write_root_manifest(run_dir: Path, profile: dict[str, Any], server_ip: str, server_ssh: str, roles: dict[str, str], commands: list[list[str]], *, status: str = "planned") -> dict[str, Any]:
    phone_role = next((role for role in ("udp_sender", "udp_receiver", "udp_echo_phone", "icmp_phone") if role in roles), "")
    metadata = root_metadata(profile, phone_role, roles.get(phone_role, ""))
    manifest = {
        "schema_version": profile["schema_version"],
        "collection_mode": "root",
        "time_precision": "nanosecond",
        "run_id": run_dir.name,
        "profile": profile["name"],
        "profile_hash": profile.get("_hash"),
        "server": {"ip": server_ip, "ssh": server_ssh, "port": profile["server"]["port"]},
        "roles": roles,
        "workload": profile["workload"],
        "root_execution": metadata["root_execution"],
        "capture_sources": metadata["capture_sources"],
        "evidence_level": metadata["evidence_level"],
        "clock_sync": {"status": "pending", "required": profile["clock_sync"].get("required", True), "max_uncertainty_ms": profile["clock_sync"].get("max_uncertainty_ms", 5.0)},
        "collection": profile["collection"],
        "status": status,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commands": [{"argv": command} for command in commands],
        "artifacts": {},
    }
    write_json(run_dir / "manifest.json", manifest)
    write_json(run_dir / "profile.json", {key: value for key, value in profile.items() if not key.startswith("_")})
    append_event(run_dir, "created", profile=profile["name"], collection_mode="root")
    return manifest


def start_phone_capture(profile: dict[str, Any], serial: str, run_id: str, port: int, output_dir: Path) -> tuple[subprocess.Popen[bytes], str, str]:
    remote_dir = f"/data/local/tmp/traceroot_{run_id}"
    remote_pcap = f"{remote_dir}/root_capture.pcap"
    remote_pid = f"{remote_dir}/tcpdump.pid"
    collection = profile["collection"]
    interface = str(collection.get("phone_interface", "any"))
    tcpdump = str(collection.get("phone_tcpdump", "/data/local/tmp/tcpdump"))
    snaplen = int(collection.get("pcap_snaplen", 256))
    buffer_mb = int(collection.get("pcap_buffer_mb", 8))
    command = [
        "sh", "-c",
        "mkdir -p {directory}; {tcpdump} -i {interface} -nn -s {snaplen} -B {buffer} -w {pcap} 'tcp port {port} or udp port {port}' 2> {directory}/tcpdump.stderr & echo $! > {pid}; wait $!; rc=$?; if test $rc -eq 130; then rc=0; fi; echo $rc > {directory}/capture.exit_code; cat {directory}/tcpdump.stderr >&2; exit $rc".format(
            directory=shlex.quote(remote_dir), tcpdump=shlex.quote(tcpdump), interface=shlex.quote(interface), snaplen=snaplen, buffer=buffer_mb * 1024, pcap=shlex.quote(remote_pcap), pid=shlex.quote(remote_pid), port=port,
        ),
    ]
    process = start_process(build_su_script_command(serial, command[2]), output_dir / "tcpdump.stdout", output_dir / "tcpdump.stderr")
    return process, remote_dir, remote_pcap


def stop_phone_capture(serial: str, remote_dir: str, process: subprocess.Popen[bytes], output_dir: Path, *, pull: bool = True) -> int:
    kill = build_su_script_command(serial, f"test -f {shlex.quote(remote_dir + '/tcpdump.pid')} && kill -INT $(cat {shlex.quote(remote_dir + '/tcpdump.pid')}) || true")
    result = subprocess.run(kill, capture_output=True, check=False, timeout=30)
    try:
        process_rc = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.terminate()
        process_rc = process.wait(timeout=5)
    stderr_part = output_dir / "tcpdump.stderr.part"
    stderr_pull = subprocess.run(
        ["adb", "-s", serial, "pull", f"{remote_dir}/tcpdump.stderr", str(stderr_part)],
        capture_output=True, check=False, timeout=120,
    )
    if stderr_pull.returncode == 0 and stderr_part.is_file():
        os.replace(stderr_part, output_dir / "tcpdump.stderr")
    else:
        stderr_part.unlink(missing_ok=True)
        return result.returncode or process_rc or stderr_pull.returncode or 1
    if not pull:
        return result.returncode or process_rc
    try:
        pull = subprocess.run(
            ["adb", "-s", serial, "pull", f"{remote_dir}/root_capture.pcap", str(output_dir / "root_capture.pcap")],
            capture_output=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        # Keep any partial local pcap and let the caller finalize a failed
        # manifest while still collecting server-side artifacts.
        return result.returncode or 124
    return result.returncode or pull.returncode
