#!/usr/bin/env python3
"""Stable server-side helpers for the traces experiment runner.

The Mac-side runner starts this module through SSH.  It deliberately exposes
small, blocking commands instead of accepting generated shell scripts.  Each
command owns its output directory and writes a ready marker and exit code so a
caller can recover a run after an interrupted SSH session.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


class Service:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.children: list[subprocess.Popen[bytes]] = []
        self.stopping = False

    def start(self, command: list[str], stdout_name: str, stderr_name: str) -> subprocess.Popen[bytes]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        stdout = (self.run_dir / stdout_name).open("wb")
        stderr = (self.run_dir / stderr_name).open("wb")
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr)
        self.children.append(child)
        return child

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 5
        for child in reversed(self.children):
            if child.poll() is None:
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    child.kill()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def write_kv(path: Path, values: dict[str, object]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def resolve_interface(value: str) -> str:
    if value != "auto":
        return value
    route = subprocess.run(
        ["ip", "route", "get", "8.8.8.8"], capture_output=True, text=True, check=False
    ).stdout.split()
    if "dev" in route:
        index = route.index("dev")
        if index + 1 < len(route):
            return route[index + 1]
    return "any"


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def process_pcap_on_server(args: argparse.Namespace, pcap: Path) -> int:
    processor = Path(args.pcap_processor)
    derived = args.derived_dir
    metadata_path = args.run_dir / "pcap_postprocess.json"
    stdout_path = args.run_dir / "pcap_postprocess.stdout"
    stderr_path = args.run_dir / "pcap_postprocess.stderr"
    command = [
        sys.executable, str(processor), str(pcap),
        "--output-dir", str(derived),
        "--port", str(args.port),
        "--duration-ms", str(args.duration_ms),
        "--window-ms", "10",
        "--payload-bytes", str(args.payload_bytes),
        "--port-direction", "source" if getattr(args, "direction", "uplink") == "downlink" else "destination",
        "--tcpdump-stderr", str(args.run_dir / "tcpdump.stderr"),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        summary: dict[str, object] = {}
        if result.stdout.strip():
            try:
                summary = json.loads(result.stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                summary = {}
        processor_rc = result.returncode
    except OSError as error:
        stdout_path.write_text("", encoding="utf-8")
        stderr_path.write_text(str(error) + "\n", encoding="utf-8")
        summary = {}
        processor_rc = 127

    validation_error = ""
    if processor_rc == 0:
        try:
            packet_count = int(summary.get("packet_count", 0))
            capture_duration_ms = float(summary.get("capture_duration_ms", 0.0))
        except (TypeError, ValueError):
            packet_count = 0
            capture_duration_ms = 0.0
        if packet_count <= 0:
            validation_error = "server-side PCAP contains no valid UDP packets"
        elif args.duration_ms > 0 and capture_duration_ms < max(1000.0, args.duration_ms * 0.5):
            validation_error = (
                f"server-side PCAP span {capture_duration_ms:.3f} ms is too short "
                f"for requested {args.duration_ms} ms"
            )
        if validation_error:
            processor_rc = 1

    derived_files = {}
    for name in (
        "udp_arrival_time_ms.txt",
        "bandwidth_trace.tsv",
        "pcap_bandwidth_summary.json",
        "bandwidth_time_scatter.png",
    ):
        path = derived / name
        exists = path.is_file()
        derived_files[name] = {
            "path": str(path),
            "exists": exists,
            "size_bytes": path.stat().st_size if exists else 0,
        }
    pcap_exists = pcap.is_file()
    payload = {
        "status": "complete" if processor_rc == 0 else "failed",
        "exit_code": processor_rc,
        "server_pcap": {
            "path": str(pcap),
            "exists": pcap_exists,
            "size_bytes": pcap.stat().st_size if pcap_exists else 0,
            "transfer": "remote_only",
        },
        "derived": derived_files,
        "summary": summary,
    }
    if validation_error:
        payload["error"] = validation_error
    metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return processor_rc


def run_iperf(args: argparse.Namespace) -> int:
    run_dir = args.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    if not command_exists("iperf3"):
        write_kv(run_dir / "exit_code.txt", {"iperf3": 127, "tcpdump": "disabled"})
        (run_dir / "error.txt").write_text("iperf3 not found\n", encoding="utf-8")
        return 127

    tcpdump_proc: subprocess.Popen[bytes] | None = None
    iperf_proc: subprocess.Popen[bytes] | None = None
    interface = resolve_interface(args.interface)
    pcap = run_dir / f"iperf3_{args.direction}_udp_{args.port}.pcap"
    service = Service(run_dir)
    stop_requested = False

    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        service.stop()

    old_term = signal.signal(signal.SIGTERM, handle_signal)
    old_int = signal.signal(signal.SIGINT, handle_signal)
    try:
        write_kv(
            run_dir / "metadata.txt",
            {
                "type": "trace_server_iperf_receiver",
                "direction": args.direction,
                "port": args.port,
                "interval_s": args.interval,
                "tcpdump_enabled": str(bool(args.tcpdump)).lower(),
                "interface": interface,
                "snaplen": args.snaplen,
                "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        if args.tcpdump:
            if not command_exists("tcpdump"):
                (run_dir / "tcpdump.stderr").write_text("tcpdump not found\n", encoding="utf-8")
                write_kv(run_dir / "exit_code.txt", {"iperf3": 127, "tcpdump": 127})
                return 127
            tcpdump_command = [
                "tcpdump", "-i", interface, "-nn", "-s", str(args.snaplen), "-B", "8192",
                "-w", str(pcap), f"tcp port {args.port} or udp port {args.port}",
            ]
            tcpdump_proc = service.start(tcpdump_command, "tcpdump.stdout", "tcpdump.stderr")
            time.sleep(max(0.0, args.ready_wait_s))
            if tcpdump_proc.poll() is not None:
                code = tcpdump_proc.returncode or 1
                write_kv(run_dir / "exit_code.txt", {"iperf3": 125, "tcpdump": code})
                return code
        iperf_command = [
            "iperf3", "-s", "-p", str(args.port), "-i", str(args.interval), "-1", "-J", "--forceflush",
        ]
        iperf_proc = service.start(iperf_command, "iperf3_server.json", "iperf3_server.stderr")
        time.sleep(max(0.0, args.ready_wait_s))
        if iperf_proc.poll() is not None:
            code = iperf_proc.returncode or 1
            write_kv(run_dir / "exit_code.txt", {"iperf3": code, "tcpdump": 0})
            return code
        (run_dir / "ready").touch()
        timeout = args.timeout_s if args.timeout_s > 0 else None
        deadline = time.monotonic() + timeout if timeout else None
        while iperf_proc.poll() is None:
            if stop_requested:
                break
            if deadline is not None and time.monotonic() >= deadline:
                iperf_proc.terminate()
                break
            time.sleep(0.2)
        iperf_code = iperf_proc.wait(timeout=5) if iperf_proc.poll() is None else iperf_proc.returncode
        tcpdump_code = 0
        if tcpdump_proc is not None:
            # SIGINT lets tcpdump flush the pcap footer before exiting.
            tcpdump_proc.send_signal(signal.SIGINT)
            try:
                tcpdump_code = tcpdump_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tcpdump_proc.kill()
                tcpdump_code = tcpdump_proc.wait()
        pcap_code = 0
        if args.pcap_processor:
            pcap_code = process_pcap_on_server(args, pcap)
        write_kv(
            run_dir / "exit_code.txt",
            {
                "iperf3": iperf_code if iperf_code is not None else 1,
                "tcpdump": tcpdump_code,
                "pcap_postprocess": pcap_code,
            },
        )
        (run_dir / "server_end_time_utc.txt").write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8"
        )
        return int(iperf_code or tcpdump_code or pcap_code or 0)
    finally:
        service.stop()
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def run_udp_downlink(args: argparse.Namespace) -> int:
    script = Path(args.sender_script) if args.sender_script else Path(args.server_repo) / "runtime" / "server" / "downlink_udp_sender.py"
    if not script.exists():
        print(f"ERROR: missing downlink sender: {script}", file=sys.stderr)
        return 127
    command = [
        sys.executable, str(script), "--port", str(args.port), "--duration-s", str(args.duration_s),
        "--rate", args.rate, "--payload-bytes", str(args.payload_bytes),
        "--wait-timeout-s", str(args.wait_timeout_s), "--run-id", args.run_id,
        "--out-dir", str(args.run_dir), "--duration-mode", args.duration_mode,
        "--progress-interval-s", str(args.progress_interval_s), "--clock-samples", "5",
    ]
    if args.ready_file:
        command += ["--ready-file", str(args.ready_file)]
    if args.no_csv:
        command.append("--no-csv")
    result = subprocess.run(command, check=False)
    write_kv(args.run_dir / "exit_code.txt", {"udp_downlink_sender": result.returncode})
    return result.returncode


def run_udp_uplink(args: argparse.Namespace) -> int:
    receiver = Path(args.receiver_bin) if args.receiver_bin else Path(args.server_repo) / "runtime" / "server" / "uplink_udp_receiver.py"
    if not receiver.exists():
        print(f"ERROR: missing uplink receiver: {receiver}", file=sys.stderr)
        return 127
    progress_flag = "--progress-interval-s" if receiver.suffix == ".py" else "--progress-interval"
    progress_value = str(args.progress_interval_s) if receiver.suffix == ".py" else f"{args.progress_interval_s}s"
    command = [str(receiver), "--port", str(args.port), "--duration-s", str(args.duration_s),
               "--timeout-s", str(args.timeout_s), "--wait-timeout-s", str(args.wait_timeout_s),
               "--run-id", args.run_id, "--out-dir", str(args.run_dir), "--output-mode", "compact",
               "--postprocess", args.postprocess, "--window-ms", str(args.window_ms),
               "--payload-bytes", str(args.payload_bytes), "--receive-api", args.receive_api,
               "--receive-batch-size", str(args.receive_batch_size), progress_flag, progress_value]
    if receiver.suffix == ".py":
        command.insert(0, sys.executable)
    if args.ready_file:
        command += ["--ready-file", str(args.ready_file)]
    result = subprocess.run(command, check=False)
    if result.returncode == 0 and args.compact_script and args.postprocess != "none":
        capture = args.run_dir / "udp_trace.utrc"
        script = Path(args.compact_script)
        if capture.exists() and script.exists():
            post = subprocess.run(
                [sys.executable, str(script), str(capture), "--output-dir", str(args.run_dir),
                 "--mode", args.postprocess, "--window-ms", str(args.window_ms),
                 "--precision-mode", args.precision_mode],
                check=False,
            )
            if post.returncode != 0:
                result = post
    write_kv(args.run_dir / "exit_code.txt", {"udp_uplink_receiver": result.returncode})
    return result.returncode


def run_udp_echo(args: argparse.Namespace) -> int:
    receiver = Path(args.receiver_bin)
    if not receiver.exists():
        print(f"ERROR: missing UDP echo receiver: {receiver}", file=sys.stderr)
        return 127
    command = [str(receiver), "--port", str(args.port), "--run-id", args.run_id,
               "--out-dir", str(args.run_dir), "--ready-file", str(args.ready_file)]
    if not os.access(receiver, os.X_OK):
        receiver.chmod(0o755)
    service = Service(args.run_dir)
    stop_requested = False
    child = None
    tcpdump_proc = None
    pcap = args.run_dir / f"udp_echo_{args.port}.pcap"
    def handle_signal(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        if child is not None and child.poll() is None:
            child.terminate()
    old_term = signal.signal(signal.SIGTERM, handle_signal)
    old_int = signal.signal(signal.SIGINT, handle_signal)
    try:
        if getattr(args, "tcpdump", False):
            if not command_exists("tcpdump"):
                return 127
            tcpdump_proc = service.start([
                "tcpdump", "-i", resolve_interface(args.interface), "-nn", "-s", str(args.snaplen),
                "-B", "8192", "-w", str(pcap), f"udp port {args.port}",
            ], "tcpdump.stdout", "tcpdump.stderr")
            time.sleep(0.5)
            if tcpdump_proc.poll() is not None:
                return int(tcpdump_proc.returncode or 1)
        child = service.start(command, "udp_echo_receiver.stdout", "udp_echo_receiver.stderr")
        while child.poll() is None and not stop_requested:
            time.sleep(0.2)
        result_code = child.wait() if child.poll() is None else int(child.returncode or 0)
        if stop_requested and result_code != 0:
            result_code = 0
        tcpdump_code = pcap_code = 0
        if tcpdump_proc is not None:
            tcpdump_proc.send_signal(signal.SIGINT)
            try:
                tcpdump_code = tcpdump_proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                tcpdump_proc.kill()
                tcpdump_code = tcpdump_proc.wait()
        if getattr(args, "pcap_processor", None):
            pcap_code = process_pcap_on_server(args, pcap)
        write_kv(args.run_dir / "exit_code.txt", {"udp_echo_receiver": result_code, "tcpdump": tcpdump_code, "pcap_postprocess": pcap_code})
        return result_code or tcpdump_code or pcap_code
    finally:
        service.stop()
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    iperf = sub.add_parser("iperf-receiver", help="run one iperf3 receiver, optionally with tcpdump")
    iperf.add_argument("--run-dir", type=Path, required=True)
    iperf.add_argument("--port", type=int, required=True)
    iperf.add_argument("--interval", type=float, default=0.2)
    iperf.add_argument("--direction", choices=("uplink", "downlink"), default="uplink")
    iperf.add_argument("--tcpdump", action="store_true")
    iperf.add_argument("--interface", default="auto")
    iperf.add_argument("--snaplen", type=int, default=256)
    iperf.add_argument("--ready-wait-s", type=float, default=0.5)
    iperf.add_argument("--timeout-s", type=float, default=0.0)
    iperf.add_argument("--pcap-processor", type=Path)
    iperf.add_argument("--derived-dir", type=Path)
    iperf.add_argument("--duration-ms", type=int, default=0)
    iperf.add_argument("--payload-bytes", type=int, default=0)

    down = sub.add_parser("udp-downlink-sender", help="run the stable DWLP sender")
    down.add_argument("--server-repo", required=True)
    down.add_argument("--sender-script", type=Path)
    down.add_argument("--run-dir", type=Path, required=True)
    down.add_argument("--port", type=int, required=True)
    down.add_argument("--duration-s", type=float, required=True)
    down.add_argument("--rate", required=True)
    down.add_argument("--payload-bytes", type=int, required=True)
    down.add_argument("--wait-timeout-s", type=float, default=120)
    down.add_argument("--run-id", required=True)
    down.add_argument("--duration-mode", choices=("wall-clock", "packet-count"), default="wall-clock")
    down.add_argument("--progress-interval-s", type=float, default=1)
    down.add_argument("--ready-file", type=Path)
    down.add_argument("--no-csv", action="store_true")

    up = sub.add_parser("udp-uplink-receiver", help="run the stable UWLP receiver")
    up.add_argument("--server-repo", required=True)
    up.add_argument("--run-dir", type=Path, required=True)
    up.add_argument("--receiver-bin")
    up.add_argument("--compact-script")
    up.add_argument("--port", type=int, required=True)
    up.add_argument("--duration-s", type=float, required=True)
    up.add_argument("--timeout-s", type=float, default=5)
    up.add_argument("--wait-timeout-s", type=float, default=120)
    up.add_argument("--run-id", required=True)
    up.add_argument("--postprocess", choices=("arrival", "latency", "none"), default="arrival")
    up.add_argument("--window-ms", type=float, default=100)
    up.add_argument("--payload-bytes", type=int, default=1472)
    up.add_argument("--precision-mode", choices=("trace", "measurement"), default="trace")
    up.add_argument("--receive-api", choices=("auto", "readmsg", "recvmmsg"), default="auto")
    up.add_argument("--receive-batch-size", type=int, default=64)
    up.add_argument("--progress-interval-s", type=float, default=1)
    up.add_argument("--ready-file", type=Path)
    echo = sub.add_parser("udp-echo-receiver", help="run header-only UDP echo receiver")
    echo.add_argument("--run-dir", type=Path, required=True)
    echo.add_argument("--receiver-bin", required=True)
    echo.add_argument("--port", type=int, required=True)
    echo.add_argument("--run-id", required=True)
    echo.add_argument("--ready-file", type=Path, required=True)
    echo.add_argument("--tcpdump", action="store_true")
    echo.add_argument("--interface", default="auto")
    echo.add_argument("--snaplen", type=int, default=256)
    echo.add_argument("--pcap-processor", type=Path)
    echo.add_argument("--derived-dir", type=Path)
    echo.add_argument("--duration-ms", type=int, default=0)
    echo.add_argument("--payload-bytes", type=int, default=0)
    return root


def main() -> int:
    args = parser().parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "iperf-receiver":
        return run_iperf(args)
    if args.command == "udp-downlink-sender":
        return run_udp_downlink(args)
    if args.command == "udp-echo-receiver":
        return run_udp_echo(args)
    return run_udp_uplink(args)


if __name__ == "__main__":
    raise SystemExit(main())
