#!/usr/bin/env python3
"""NAT-registered timestamped UDP downlink sender."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import math
import select
import socket
import struct
import time
from pathlib import Path
from typing import TextIO


MAGIC = 0x44574C50  # "DWLP"
VERSION = 2
HEADER_LEN = 48
CONTROL_KINDS = {"DWLP_HELLO", "DWLP_READY", "DWLP_KEEPALIVE", "DWLP_DONE_ACK"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for an Android clock-synchronized READY, then send sequenced downlink UDP."
    )
    parser.add_argument("--port", type=int, default=5003)
    parser.add_argument("--duration-s", type=float, default=150.0)
    parser.add_argument("--rate", default="100pps")
    parser.add_argument("--payload-bytes", type=int, default=64)
    parser.add_argument("--wait-timeout-s", type=float, default=120.0)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--out-root", type=Path, default=Path("logs"))
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--duration-mode", choices=("wall-clock", "packet-count"), default="packet-count")
    parser.add_argument("--no-csv", action="store_true", help="Skip per-packet sender CSV")
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--progress-path", type=Path)
    parser.add_argument("--progress-interval-s", type=float, default=1.0)
    parser.add_argument("--clock-samples", type=int, default=5)
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def parse_rate(rate: str, payload_bytes: int) -> float:
    normalized = rate.strip().lower()
    try:
        if normalized.endswith("pps"):
            pps = float(normalized[:-3])
        else:
            multiplier = 1.0
            number_text = normalized
            for suffix, value in (("gbps", 1e9), ("mbps", 1e6), ("kbps", 1e3)):
                if normalized.endswith(suffix):
                    multiplier = value
                    number_text = normalized[: -len(suffix)]
                    break
            else:
                for suffix, value in (("g", 1e9), ("m", 1e6), ("k", 1e3)):
                    if normalized.endswith(suffix):
                        multiplier = value
                        number_text = normalized[:-1]
                        break
            pps = float(number_text) * multiplier / (payload_bytes * 8.0)
    except ValueError:
        fail(f"invalid rate: {rate}")
    if pps <= 0:
        fail(f"invalid rate: {rate}")
    return pps


def sanitize_run_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in value.strip())
    return cleaned.strip("_") or f"downlink_udp_loss_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"


def run_hash(run_id: str) -> int:
    return struct.unpack("!Q", hashlib.blake2s(run_id.encode(), digest_size=8).digest())[0]


def make_payload(payload_bytes: int, run_id_hash: int, seq: int, scheduled_ns: int, send_ns: int, send_unix_ns: int) -> bytes:
    payload = bytearray(payload_bytes)
    struct.pack_into("!IHHQQQQQ", payload, 0, MAGIC, VERSION, HEADER_LEN, run_id_hash, seq, scheduled_ns, send_ns, send_unix_ns)
    return bytes(payload)


def parse_control(data: bytes) -> tuple[str, dict[str, str], str]:
    text = data.decode("utf-8", errors="replace").strip()
    parts = text.split()
    if not parts or parts[0] not in CONTROL_KINDS:
        return "", {}, text
    fields = dict(token.split("=", 1) for token in parts[1:] if "=" in token)
    return parts[0], fields, text


def append_control(controls: list[dict[str, object]], data: bytes, addr: tuple[str, int]) -> tuple[str, dict[str, str]]:
    kind, fields, raw = parse_control(data)
    if kind:
        controls.append({"time": time.monotonic(), "source_ip": addr[0], "source_port": addr[1], "kind": kind, "run_id": fields.get("run_id", ""), "raw": raw})
    return kind, fields


def send_hello_ack(sock: socket.socket, addr: tuple[str, int], fields: dict[str, str], server_recv_unix_ns: int, total_packets: int, clock_samples: int) -> bool:
    client_unix_ns = fields.get("client_unix_ns", "")
    index = fields.get("index", "")
    if not client_unix_ns.isdigit() or not index.isdigit():
        return False
    server_send_unix_ns = time.time_ns()
    ack = (
        "DWLP_HELLO_ACK "
        f"run_id={fields.get('run_id', '')} index={index} client_unix_ns={client_unix_ns} "
        f"server_recv_unix_ns={server_recv_unix_ns} server_send_unix_ns={server_send_unix_ns} "
        f"scheduled_packets={total_packets} clock_samples={clock_samples}\n"
    ).encode("ascii")
    sock.sendto(ack, addr)
    return True


def wait_for_ready(sock: socket.socket, wait_timeout_s: float, total_packets: int, clock_samples: int) -> tuple[tuple[str, int], dict[str, str], list[dict[str, object]], int]:
    controls: list[dict[str, object]] = []
    deadline = time.monotonic() + wait_timeout_s
    endpoint: tuple[str, int] | None = None
    hello_fields: dict[str, str] = {}
    acked_indices: set[str] = set()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail(f"timed out waiting {wait_timeout_s:.3f}s for DWLP_READY")
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            fail(f"timed out waiting {wait_timeout_s:.3f}s for DWLP_READY")
        data, addr = sock.recvfrom(65535)
        server_recv_unix_ns = time.time_ns()
        kind, fields = append_control(controls, data, addr)
        if kind == "DWLP_HELLO":
            if endpoint is None:
                endpoint, hello_fields = addr, fields
            if addr != endpoint or fields.get("run_id") != hello_fields.get("run_id"):
                continue
            if send_hello_ack(sock, addr, fields, server_recv_unix_ns, total_packets, clock_samples):
                acked_indices.add(fields["index"])
        elif kind == "DWLP_READY" and endpoint == addr and fields.get("run_id") == hello_fields.get("run_id"):
            reported = fields.get("index", "")
            if len(acked_indices) >= clock_samples and reported.isdigit():
                return endpoint, hello_fields, controls, len(acked_indices)


def drain_controls(sock: socket.socket, controls: list[dict[str, object]], max_packets: int = 64) -> None:
    for _ in range(max_packets):
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return
        data, addr = sock.recvfrom(65535)
        append_control(controls, data, addr)


def wait_until(sock: socket.socket, deadline: float, controls: list[dict[str, object]]) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        ready, _, _ = select.select([sock], [], [], min(remaining, 0.2))
        if ready:
            drain_controls(sock, controls)


def write_controls(path: Path, controls: list[dict[str, object]], start: float) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["time_rel_s", "source_ip", "source_port", "kind", "run_id", "raw"])
        writer.writeheader()
        for row in controls:
            writer.writerow({
                "time_rel_s": f"{float(row['time']) - start:.9f}",
                "source_ip": row["source_ip"],
                "source_port": row["source_port"],
                "kind": row["kind"],
                "run_id": row["run_id"],
                "raw": row["raw"],
            })


def write_summary(path: Path, values: dict[str, object]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def open_sender_csv(path: Path, no_csv: bool) -> tuple[TextIO | None, csv.DictWriter | None]:
    if no_csv:
        return None, None
    handle = gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=6)
    writer = csv.DictWriter(handle, fieldnames=["run_id", "target_phone", "target_endpoint", "port", "rate", "pps", "payload_bytes", "seq", "scheduled_send_rel_s", "send_rel_s", "send_unix_ns", "send_epoch_s", "send_lag_ms"])
    writer.writeheader()
    return handle, writer


def main() -> int:
    args = parse_args()
    if not (0 < args.port <= 65535) or args.duration_s <= 0 or args.wait_timeout_s <= 0:
        fail("port/duration/wait values are invalid")
    if args.payload_bytes < HEADER_LEN:
        fail(f"--payload-bytes must be at least {HEADER_LEN}")
    if args.progress_interval_s <= 0 or args.clock_samples <= 0:
        fail("progress interval and clock samples must be positive")

    pps = parse_rate(args.rate, args.payload_bytes)
    interval_s = 1.0 / pps
    scheduled_packets = int(math.ceil(args.duration_s * pps))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.setblocking(False)
    if args.ready_file:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.touch()

    print(f"waiting_for_ready_port={args.port}", flush=True)
    endpoint, hello_fields, controls, clock_acks = wait_for_ready(sock, args.wait_timeout_s, scheduled_packets, args.clock_samples)
    # High-rate sends should back-pressure on the kernel queue instead of
    # turning transient EAGAIN into artificial sender loss.
    sock.setblocking(True)
    run_id = sanitize_run_id(args.run_id or hello_fields.get("run_id") or f"downlink_udp_loss_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_p{args.port}")
    out_dir = args.out_dir if args.out_dir is not None else args.out_root / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    endpoint_text = f"{endpoint[0]}:{endpoint[1]}"
    csv_path = out_dir / "downlink_udp_sender.csv.gz"
    summary_path = out_dir / "downlink_udp_sender_summary.txt"
    control_path = out_dir / "downlink_udp_control.csv"
    progress_path = args.progress_path or out_dir / "downlink_udp_sender_progress.tsv"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "downlink_udp_endpoint.txt").write_text(endpoint_text + "\n", encoding="utf-8")
    write_summary(out_dir / "metadata.txt", {
        "run_id": run_id, "type": "downlink_udp_sender", "direction": "downlink", "port": args.port,
        "duration_s": f"{args.duration_s:.6f}", "duration_mode": args.duration_mode, "rate": args.rate,
        "pps": f"{pps:.9f}", "payload_bytes": args.payload_bytes, "protocol_version": VERSION,
        "header_len": HEADER_LEN, "wait_timeout_s": f"{args.wait_timeout_s:.6f}",
        "registered_endpoint": endpoint_text, "clock_samples": args.clock_samples, "per_packet_csv": str(not args.no_csv).lower(),
    })

    progress_handle = progress_path.open("w", encoding="utf-8", newline="")
    progress_writer = csv.writer(progress_handle, delimiter="\t", lineterminator="\n")
    progress_writer.writerow(["event", "timestamp_unix_ns", "elapsed_s", "scheduled_packets", "attempted_packets", "sent_packets", "send_failed_packets", "last_seq", "last_send_rel_s", "send_lag_ms"])
    progress_handle.flush()
    csv_handle, packet_writer = open_sender_csv(csv_path, args.no_csv)

    send_start = time.monotonic() + 0.2
    rid_hash = run_hash(run_id)
    attempted = sent = failed = last_seq = 0
    last_send_rel_s = last_send_lag_ms = 0.0
    next_progress = time.monotonic()
    deadline_reached = False

    def write_progress(event: str) -> None:
        nonlocal next_progress
        now = time.monotonic()
        progress_writer.writerow([event, time.time_ns(), f"{max(0.0, now - send_start):.9f}", scheduled_packets, attempted, sent, failed, last_seq, f"{last_send_rel_s:.9f}", f"{last_send_lag_ms:.6f}"])
        progress_handle.flush()
        next_progress = now + args.progress_interval_s

    write_progress("start")
    for index in range(scheduled_packets):
        scheduled = send_start + index * interval_s
        if args.duration_mode == "wall-clock" and time.monotonic() >= send_start + args.duration_s:
            deadline_reached = True
            break
        wait_until(sock, scheduled, controls)
        if time.monotonic() >= next_progress:
            write_progress("heartbeat")
        send_time = time.monotonic()
        if args.duration_mode == "wall-clock" and send_time >= send_start + args.duration_s:
            deadline_reached = True
            break
        seq = index + 1
        scheduled_ns = int((scheduled - send_start) * 1e9)
        send_ns = int((send_time - send_start) * 1e9)
        send_unix_ns = time.time_ns()
        payload = make_payload(args.payload_bytes, rid_hash, seq, scheduled_ns, send_ns, send_unix_ns)
        attempted += 1
        try:
            sock.sendto(payload, endpoint)
        except OSError:
            failed += 1
            continue
        sent += 1
        last_seq = seq
        last_send_rel_s = send_ns / 1e9
        last_send_lag_ms = (send_time - scheduled) * 1000.0
        if packet_writer is not None:
            packet_writer.writerow({
                "run_id": run_id, "target_phone": endpoint[0], "target_endpoint": endpoint_text,
                "port": args.port, "rate": args.rate, "pps": f"{pps:.9f}", "payload_bytes": args.payload_bytes,
                "seq": seq, "scheduled_send_rel_s": f"{scheduled_ns / 1e9:.9f}", "send_rel_s": f"{send_ns / 1e9:.9f}",
                "send_unix_ns": send_unix_ns, "send_epoch_s": f"{send_unix_ns / 1e9:.9f}", "send_lag_ms": f"{last_send_lag_ms:.6f}",
            })
        if seq % 1024 == 0:
            drain_controls(sock, controls)

    sender_elapsed_s = max(0.0, time.monotonic() - send_start)
    if args.duration_mode == "wall-clock":
        # Completing every scheduled slot is also a successful wall-clock run;
        # the last slot is one interval before the nominal deadline.
        deadline_reached = True
    done_payload = (
        f"DWLP_DONE run_id={run_id} last_seq={last_seq} sent_packets={sent} "
        f"sender_elapsed_s={sender_elapsed_s:.9f}\n"
    ).encode("ascii")
    done_ack = False
    sock.setblocking(False)
    done_deadline = time.monotonic() + 2.0
    while time.monotonic() < done_deadline:
        try:
            sock.sendto(done_payload, endpoint)
        except OSError:
            pass
        ready, _, _ = select.select([sock], [], [], 0.1)
        if ready:
            data, _addr = sock.recvfrom(4096)
            if data.startswith(b"DWLP_DONE_ACK "):
                done_ack = True
                break
    sock.setblocking(True)
    write_progress("final")
    progress_handle.close()
    if csv_handle is not None:
        csv_handle.close()
    drain_controls(sock, controls)
    write_controls(control_path, controls, send_start)
    actual_payload_mbps = sent * args.payload_bytes * 8 / sender_elapsed_s / 1e6 if sender_elapsed_s > 0 else 0.0
    write_summary(summary_path, {
        "run_id": run_id, "registered_endpoint": endpoint_text, "port": args.port, "rate": args.rate,
        "pps": f"{pps:.9f}", "payload_bytes": args.payload_bytes, "duration_s": f"{args.duration_s:.6f}",
        "duration_mode": args.duration_mode, "wait_timeout_s": f"{args.wait_timeout_s:.6f}",
        "scheduled_packets": scheduled_packets, "attempted_packets": attempted, "sent_packets": sent,
        "send_failed_packets": failed, "last_seq": last_seq, "sender_elapsed_s": f"{sender_elapsed_s:.9f}",
        "actual_payload_mbps": f"{actual_payload_mbps:.9f}", "deadline_reached": str(deadline_reached).lower(),
        "clock_sync_samples": min(clock_acks, args.clock_samples), "control_packets": len(controls),
        "sender_csv": "disabled" if args.no_csv else csv_path, "sender_progress": progress_path, "control_csv": control_path,
        "done_ack": str(done_ack).lower(),
    })
    print(f"run_id={run_id}\nrun_dir={out_dir}\nendpoint={endpoint_text}\nsent_packets={sent}\nsummary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
