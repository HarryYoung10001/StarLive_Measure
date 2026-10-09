#!/usr/bin/env python3
"""Server-side UDP receiver for phone-to-server uplink loss probes."""

from __future__ import annotations

import argparse
import csv
from contextlib import nullcontext
import gzip
import socket
import struct
import time
from pathlib import Path


MAGIC = 0x55574C50  # "UWLP"
VERSION = 2
HEADER_LEN = 48
LEGACY_VERSION = 1
LEGACY_HEADER_LEN = 40
SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)
SO_RXQ_OVFL = getattr(socket, "SO_RXQ_OVFL", 40)
SO_RCVBUFFORCE = 33
UTRC_HEADER_SIZE = 64
UTRC_RECORD = struct.Struct("!QQQQqIHH")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Receive sequenced phone-to-server UDP probes and write receiver logs."
    )
    parser.add_argument("--port", type=int, default=5004, help="UDP port to bind on the server")
    parser.add_argument("--duration-s", type=float, default=150.0, help="Receive duration after first packet")
    parser.add_argument("--timeout-s", type=float, default=5.0, help="Extra wait after duration")
    parser.add_argument("--wait-timeout-s", type=float, default=120.0, help="Wait for first valid packet")
    parser.add_argument("--run-id", required=True, help="Run identifier shared with phone sender")
    parser.add_argument("--out-root", type=Path, default=Path("logs"), help="Root output directory")
    parser.add_argument("--out-dir", type=Path, default=None, help="Exact output directory")
    parser.add_argument("--ready-file", type=Path, default=None, help="Touch after binding the UDP socket")
    parser.add_argument(
        "--progress-path",
        type=Path,
        default=None,
        help="TSV heartbeat path; defaults to uplink_udp_receiver_progress.tsv in out-dir",
    )
    parser.add_argument(
        "--progress-interval-s",
        type=float,
        default=1.0,
        help="Receiver heartbeat interval in seconds",
    )
    parser.add_argument("--output-mode", choices=("raw", "compact"), default="raw")
    parser.add_argument("--postprocess", choices=("arrival", "latency"), default="arrival")
    parser.add_argument("--window-ms", type=float, default=10.0)
    parser.add_argument("--recv-buffer-bytes", type=int, default=64 * 1024 * 1024)
    return parser.parse_args()


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def parse_packet(data: bytes) -> tuple[int, float, float, int] | None:
    if len(data) < LEGACY_HEADER_LEN:
        return None
    magic, version, header_len, _run_id_hash, seq, scheduled_ns, send_ns = struct.unpack_from(
        "!IHHQQQQ", data, 0
    )
    if magic != MAGIC or version not in {VERSION, LEGACY_VERSION} or header_len > len(data):
        return None
    if version == VERSION and (header_len < HEADER_LEN or len(data) < HEADER_LEN):
        return None
    send_unix_ns = struct.unpack_from("!Q", data, 40)[0] if version == VERSION else 0
    return seq, scheduled_ns / 1e9, send_ns / 1e9, send_unix_ns


def clock_request_client_time(data: bytes) -> int | None:
    try:
        fields = dict(token.split("=", 1) for token in data.decode("ascii").split()[1:] if "=" in token)
        if not data.startswith(b"UWLP_CLOCK_REQ "):
            return None
        return int(fields["client_unix_ns"])
    except (UnicodeDecodeError, KeyError, ValueError):
        return None


def write_metadata(path: Path, args: argparse.Namespace, start_utc: str) -> None:
    lines = [
        f"run_id={args.run_id}",
        "type=uplink_udp_receiver",
        "direction=uplink",
        "direction_human=cloud_phone_to_server_udp_probe_receiver",
        f"port={args.port}",
        f"duration_s={args.duration_s:.6f}",
        f"timeout_s={args.timeout_s:.6f}",
        f"wait_timeout_s={args.wait_timeout_s:.6f}",
        f"server_time_utc={start_utc}",
        "note=server-side sequenced UDP receiver for no-root uplink probe loss",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_summary(path: Path, values: dict[str, object]) -> None:
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if not (0 < args.port <= 65535):
        fail("--port must be 1..65535")
    if args.duration_s <= 0:
        fail("--duration-s must be positive")
    if args.timeout_s < 0:
        fail("--timeout-s must be non-negative")
    if args.wait_timeout_s <= 0:
        fail("--wait-timeout-s must be positive")
    if args.progress_interval_s <= 0:
        fail("--progress-interval-s must be positive")
    if args.window_ms <= 0 or args.recv_buffer_bytes < 65536:
        fail("window/buffer values are invalid")

    out_dir = args.out_dir if args.out_dir is not None else args.out_root / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    progress_path = args.progress_path or out_dir / "uplink_udp_receiver_progress.tsv"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_metadata(out_dir / "metadata.txt", args, start_utc)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, args.recv_buffer_bytes)
    actual_recv_buffer = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if args.output_mode == "compact" and actual_recv_buffer < args.recv_buffer_bytes:
        try:
            sock.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, args.recv_buffer_bytes)
            actual_recv_buffer = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except OSError:
            pass
    if args.output_mode == "compact" and actual_recv_buffer < args.recv_buffer_bytes:
        fail(f"actual receive buffer {actual_recv_buffer} is below requested {args.recv_buffer_bytes}")
    if args.output_mode == "compact":
        sock.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
        sock.setsockopt(socket.SOL_SOCKET, SO_RXQ_OVFL, 1)
    sock.settimeout(0.2)
    if args.ready_file is not None:
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.touch()

    progress_handle = progress_path.open("w", encoding="utf-8", newline="")
    progress_writer = csv.writer(progress_handle, delimiter="\t", lineterminator="\n")
    progress_writer.writerow(
        [
            "event",
            "timestamp_unix_ns",
            "elapsed_s",
            "received_packets",
            "last_seq",
            "last_packet_recv_unix_ns",
            "socket_timeout_count",
        ]
    )
    progress_handle.flush()

    csv_path = out_dir / "uplink_udp_receiver.csv.gz"
    capture_path = out_dir / "udp_trace.utrc"
    summary_path = out_dir / "uplink_udp_receiver_summary.txt"

    capture_handle = None
    if args.output_mode == "compact":
        capture_handle = capture_path.open("wb", buffering=4 * 1024 * 1024)
        header = bytearray(UTRC_HEADER_SIZE)
        header[:4] = b"UTRC"
        struct.pack_into("!HHHBBI", header, 4, 1, UTRC_HEADER_SIZE, UTRC_RECORD.size, 1, 1 if args.postprocess == "arrival" else 2, 0)
        capture_handle.write(header)
    missing_kernel_timestamps = 0
    missing_drop_metadata = 0
    max_socket_drops = 0
    done_received = False
    done_last_seq = 0

    seen: set[int] = set()
    first_seq = 0
    last_seq = 0
    received_packets = 0
    duplicate_packets = 0
    first_packet_time: float | None = None
    first_kernel_recv_ns: int | None = None
    first_recv_rel_s: float | None = None
    last_recv_rel_s: float | None = None
    payload_bytes_min: int | None = None
    payload_bytes_max: int | None = None
    last_packet_recv_unix_ns = 0
    socket_timeout_count = 0
    progress_start = time.monotonic()
    next_progress = progress_start

    def write_progress(event: str, now: float | None = None) -> None:
        nonlocal next_progress
        now = time.monotonic() if now is None else now
        progress_writer.writerow(
            [
                event,
                time.time_ns(),
                f"{now - progress_start:.9f}",
                received_packets,
                last_seq,
                last_packet_recv_unix_ns,
                socket_timeout_count,
            ]
        )
        progress_handle.flush()
        next_progress = now + args.progress_interval_s

    write_progress("start", progress_start)
    first_packet_deadline = time.monotonic() + args.wait_timeout_s
    remote_addr = ""

    csv_context = gzip.open(csv_path, "wt", encoding="utf-8", newline="", compresslevel=6) if args.output_mode == "raw" else nullcontext(None)
    with csv_context as handle:
        writer = None
        if args.output_mode == "raw":
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                "run_id",
                "port",
                "seq",
                "scheduled_send_rel_s",
                "send_rel_s",
                "recv_rel_s",
                "send_unix_ns",
                "send_epoch_s",
                "recv_unix_ns",
                "recv_epoch_s",
                "payload_bytes",
                "duplicate",
                "remote_addr",
                ],
            )
            writer.writeheader()

        while True:
            now = time.monotonic()
            if now >= next_progress:
                write_progress("heartbeat", now)
            if first_packet_time is None:
                if now > first_packet_deadline:
                    fail(f"timed out waiting {args.wait_timeout_s:.3f}s for first UWLP packet")
            else:
                if now > first_packet_time + args.duration_s + args.timeout_s:
                    break

            try:
                if args.output_mode == "compact":
                    data, ancdata, _msg_flags, addr = sock.recvmsg(65535, socket.CMSG_SPACE(32))
                    kernel_recv_ns = None
                    socket_drops = None
                    for level, kind, payload in ancdata:
                        if level != socket.SOL_SOCKET:
                            continue
                        if kind == SO_TIMESTAMPNS and len(payload) >= 8:
                            if len(payload) >= 16:
                                sec, nsec = struct.unpack_from("@qq", payload)
                            else:
                                sec, nsec = struct.unpack_from("@ll", payload)
                            kernel_recv_ns = sec * 1_000_000_000 + nsec
                        elif kind == SO_RXQ_OVFL and len(payload) >= 4:
                            socket_drops = struct.unpack_from("=I", payload)[0]
                    if kernel_recv_ns is None:
                        missing_kernel_timestamps += 1
                        continue
                    # Linux may omit SO_RXQ_OVFL ancillary data while the
                    # cumulative drop count is zero; setsockopt is the
                    # capability check and a missing message means zero.
                    if socket_drops is None:
                        socket_drops = 0
                else:
                    data, addr = sock.recvfrom(65535)
                    kernel_recv_ns, socket_drops = None, 0
            except socket.timeout:
                socket_timeout_count += 1
                continue

            clock_client_time = clock_request_client_time(data)
            if clock_client_time is not None:
                server_recv_time = kernel_recv_ns if kernel_recv_ns is not None else time.time_ns()
                server_send_time = time.time_ns()
                response = (
                    "UWLP_CLOCK_ACK "
                    f"client_unix_ns={clock_client_time} "
                    f"server_recv_unix_ns={server_recv_time} "
                    f"server_send_unix_ns={server_send_time}\n"
                )
                sock.sendto(response.encode("ascii"), addr)
                continue

            if data.startswith(b"UWLP_DONE "):
                fields = dict(token.split("=", 1) for token in data.decode("ascii", errors="replace").split()[1:] if "=" in token)
                done_last_seq = int(fields.get("last_seq", "0"))
                done_received = True
                sock.sendto(f"UWLP_DONE_ACK run_id={args.run_id} received_packets={received_packets} socket_drops={max_socket_drops}\n".encode("ascii"), addr)
                continue

            parsed = parse_packet(data)
            if parsed is None:
                continue

            recv_time = time.monotonic()
            recv_unix_ns = kernel_recv_ns if kernel_recv_ns is not None else time.time_ns()
            last_packet_recv_unix_ns = recv_unix_ns
            if first_packet_time is None:
                first_packet_time = recv_time
                first_kernel_recv_ns = recv_unix_ns

            seq, scheduled_send_rel_s, send_rel_s, send_unix_ns = parsed
            duplicate = seq in seen
            if duplicate:
                duplicate_packets += 1
            else:
                seen.add(seq)

            received_packets += 1
            if first_seq == 0 or seq < first_seq:
                first_seq = seq
            if seq > last_seq:
                last_seq = seq
            remote_addr = f"{addr[0]}:{addr[1]}"
            recv_rel_s = ((recv_unix_ns - first_kernel_recv_ns) / 1e9) if args.output_mode == "compact" else recv_time - first_packet_time
            first_recv_rel_s = recv_rel_s if first_recv_rel_s is None else min(first_recv_rel_s, recv_rel_s)
            last_recv_rel_s = recv_rel_s if last_recv_rel_s is None else max(last_recv_rel_s, recv_rel_s)
            payload_bytes_min = len(data) if payload_bytes_min is None else min(payload_bytes_min, len(data))
            payload_bytes_max = len(data) if payload_bytes_max is None else max(payload_bytes_max, len(data))

            if args.output_mode == "compact":
                capture_handle.write(UTRC_RECORD.pack(seq, int(scheduled_send_rel_s * 1e9), int(send_rel_s * 1e9), send_unix_ns, recv_unix_ns, socket_drops, len(data), VERSION))
                max_socket_drops = max(max_socket_drops, socket_drops)
            else:
                writer.writerow(
                {
                    "run_id": args.run_id,
                    "port": args.port,
                    "seq": seq,
                    "scheduled_send_rel_s": f"{scheduled_send_rel_s:.9f}",
                    "send_rel_s": f"{send_rel_s:.9f}",
                    "recv_rel_s": f"{recv_rel_s:.9f}",
                    "send_unix_ns": send_unix_ns or "",
                    "send_epoch_s": f"{send_unix_ns / 1e9:.9f}" if send_unix_ns else "",
                    "recv_unix_ns": recv_unix_ns,
                    "recv_epoch_s": f"{recv_unix_ns / 1e9:.9f}",
                    "payload_bytes": len(data),
                    "duplicate": str(duplicate).lower(),
                    "remote_addr": remote_addr,
                }
                )

    if capture_handle is not None:
        capture_handle.close()
    write_summary(
        summary_path,
        {
            "run_id": args.run_id,
            "port": args.port,
            "duration_s": f"{args.duration_s:.6f}",
            "timeout_s": f"{args.timeout_s:.6f}",
            "wait_timeout_s": f"{args.wait_timeout_s:.6f}",
            "received_packets": received_packets,
            "unique_received_packets": len(seen),
            "duplicate_packets": duplicate_packets,
            "first_recv_rel_s": f"{first_recv_rel_s:.9f}" if first_recv_rel_s is not None else "",
            "last_recv_rel_s": f"{last_recv_rel_s:.9f}" if last_recv_rel_s is not None else "",
            "payload_bytes_min": payload_bytes_min if payload_bytes_min is not None else "",
            "payload_bytes_max": payload_bytes_max if payload_bytes_max is not None else "",
            "first_seq": first_seq,
            "last_seq": last_seq,
            "remote_addr": remote_addr,
            "receiver_csv": csv_path,
            "receiver_progress": progress_path,
            "output_mode": args.output_mode,
            "postprocess": args.postprocess,
            "timestamp_source": "kernel_so_timestampns" if args.output_mode == "compact" else "userspace",
            "requested_rcvbuf_bytes": args.recv_buffer_bytes,
            "actual_rcvbuf_bytes": actual_recv_buffer,
            "missing_kernel_timestamp_packets": missing_kernel_timestamps,
            "missing_drop_metadata_packets": missing_drop_metadata,
            "socket_rxq_overflow_packets": max_socket_drops,
            "receiver_integrity": "clean" if not (missing_kernel_timestamps or missing_drop_metadata or max_socket_drops or (args.output_mode == "compact" and not done_received)) else "contaminated",
            "done_received": str(done_received).lower(),
            "done_last_seq": done_last_seq,
        },
    )

    write_progress("final")
    progress_handle.close()

    print(f"run_id={args.run_id}")
    print(f"run_dir={out_dir}")
    print(f"received_packets={received_packets}")
    print(f"unique_received_packets={len(seen)}")
    print(f"receiver_csv={csv_path}")
    print(f"summary={summary_path}")
    print(f"receiver_progress={progress_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
