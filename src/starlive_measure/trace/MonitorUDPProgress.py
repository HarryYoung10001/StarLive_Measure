#!/usr/bin/env python3
"""Poll phone and server UDP progress sidecars into one local TSV."""

from __future__ import annotations

import argparse
import csv
import shlex
import subprocess
import threading
import time
from pathlib import Path


def _poll(command: list[str], prefix: str, timeout_s: float) -> tuple[str, dict[str, str], str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout_s)
    output = result.stdout.strip().splitlines()
    if result.returncode != 0:
        error = (result.stderr.strip() or result.stdout.strip() or f"exit_code={result.returncode}").replace("\n", " ")
        return "unavailable", {}, error
    if not output:
        return "missing", {}, "empty progress output"
    values = output[-1].split("\t")
    if values[0] == "event":
        return "missing", {}, "progress header only"
    if prefix == "sender":
        fields = [
            "event",
            "timestamp_unix_ns",
            "elapsed_s",
            "scheduled_packets",
            "attempted_packets",
            "sent_packets",
            "send_failed_packets",
            "last_seq",
            "last_send_rel_s",
            "send_lag_ms",
        ]
    else:
        fields = [
            "event",
            "timestamp_unix_ns",
            "elapsed_s",
            "received_packets",
            "last_seq",
            "last_packet_recv_unix_ns",
            "socket_timeout_count",
        ]
    if len(values) != len(fields):
        message = output[-1].replace("\r", " ")
        if "No such file" in message or "cannot open" in message or "inaccessible" in message:
            return "missing", {}, message
        return "invalid", {}, f"expected {len(fields)} fields, got {len(values)}"
    return "ok", dict(zip(fields, values)), ""


def _ssh_command(host: str, path: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", host, f"tail -n 1 -- {shlex.quote(path)}"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb-device", required=True)
    parser.add_argument("--phone-path", required=True)
    parser.add_argument("--server-ssh", required=True)
    parser.add_argument("--server-path", required=True)
    parser.add_argument("--direction", choices=("uplink", "downlink"), default="uplink")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--interval-s", type=float, default=2.0)
    parser.add_argument("--duration-s", type=float, required=True)
    parser.add_argument("--command-timeout-s", type=float, default=4.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval_s <= 0 or args.duration_s < 0 or args.command_timeout_s <= 0:
        raise SystemExit("interval, duration and command timeout must be valid positive values")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "monitor_timestamp_unix_ns",
        "sender_status",
        "sender_event",
        "sender_timestamp_unix_ns",
        "sender_elapsed_s",
        "sender_scheduled_packets",
        "sender_attempted_packets",
        "sender_sent_packets",
        "sender_send_failed_packets",
        "sender_last_seq",
        "sender_last_send_rel_s",
        "sender_send_lag_ms",
        "receiver_status",
        "receiver_event",
        "receiver_timestamp_unix_ns",
        "receiver_elapsed_s",
        "receiver_received_packets",
        "receiver_last_seq",
        "receiver_last_packet_recv_unix_ns",
        "receiver_socket_timeout_count",
        "adb_error",
        "ssh_error",
    ]
    deadline = time.monotonic() + args.duration_s
    stop = threading.Event()
    state_lock = threading.Lock()
    states: dict[str, tuple[str, dict[str, str], str]] = {
        "sender": ("starting", {}, ""),
        "receiver": ("starting", {}, ""),
    }

    def collect(name: str, command: list[str]) -> None:
        while not stop.is_set():
            try:
                current = _poll(command, name, args.command_timeout_s)
            except subprocess.TimeoutExpired:
                current = ("unavailable", {}, f"{name} command timeout")
            with state_lock:
                states[name] = current
            stop.wait(args.interval_s)

    adb_command = ["adb", "-s", args.adb_device, "shell", "tail", "-n", "1", args.phone_path]
    ssh_command = _ssh_command(args.server_ssh, args.server_path)
    role_commands = (
        {"sender": adb_command, "receiver": ssh_command}
        if args.direction == "uplink"
        else {"sender": ssh_command, "receiver": adb_command}
    )
    collectors = [
        threading.Thread(target=collect, args=(role, command), daemon=True)
        for role, command in role_commands.items()
    ]
    for collector in collectors:
        collector.start()

    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        while True:
            monitor_timestamp = time.time_ns()
            with state_lock:
                sender_status, sender, sender_error = states["sender"]
                receiver_status, receiver, receiver_error = states["receiver"]
            row = {field: "" for field in fields}
            row["monitor_timestamp_unix_ns"] = str(monitor_timestamp)
            row["sender_status"] = sender_status
            row["receiver_status"] = receiver_status
            if args.direction == "uplink":
                row["adb_error"] = sender_error
                row["ssh_error"] = receiver_error
            else:
                row["adb_error"] = receiver_error
                row["ssh_error"] = sender_error
            for key, value in sender.items():
                row[f"sender_{key}"] = value
            for key, value in receiver.items():
                row[f"receiver_{key}"] = value
            writer.writerow(row)
            handle.flush()
            if time.monotonic() >= deadline:
                break
            stop.wait(min(args.interval_s, max(0.0, deadline - time.monotonic())))
    stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
