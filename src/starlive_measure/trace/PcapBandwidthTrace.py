#!/usr/bin/env python3
"""Extract a millisecond bandwidth trace from a classic tcpdump PCAP.

The trace format mirrors the existing UDP-flow artifacts: one relative arrival
timestamp per received packet plus a time-indexed, rolling-window bandwidth
table.  The parser deliberately uses only the Python standard library so it
can run on the measurement host without installing packet-analysis packages.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import struct
import sys
from typing import BinaryIO, Iterable

try:
    from .BandwidthTrace import build_bandwidth_trace
except ImportError:  # Direct execution by the server helper.
    from BandwidthTrace import build_bandwidth_trace


class PcapError(ValueError):
    """Raised when a PCAP cannot be parsed safely."""


@dataclass(frozen=True)
class PcapPacket:
    timestamp_ns: int
    payload_bytes: int
    captured_payload_bytes: int
    captured_length: int
    original_length: int
    truncated: bool


@dataclass(frozen=True)
class PcapCapture:
    linktype: int
    snaplen: int
    timestamp_resolution: str
    packets: tuple[PcapPacket, ...]
    records_seen: int
    skipped_packets: int
    filtered_packets: int = 0
    parse_errors: int = 0
    truncation_errors: int = 0


_MAGIC = {
    b"\xd4\xc3\xb2\xa1": ("<", "microsecond"),
    b"\xa1\xb2\xc3\xd4": (">", "microsecond"),
    b"M<\xb2\xa1": ("<", "nanosecond"),
    b"\xa1\xb2<M": (">", "nanosecond"),
}

_LINKTYPE_RAW_IPV4 = 228
_LINKTYPE_RAW_IPV6 = 229


def _read_exact(handle: BinaryIO, size: int, label: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise PcapError(f"truncated {label}")
    return data


def _network_offset(frame: bytes, linktype: int) -> tuple[int, int] | None:
    """Return (network-header offset, EtherType) for supported link types."""

    if linktype == 1:  # Ethernet.
        if len(frame) < 14:
            return None
        offset = 14
        ethertype = int.from_bytes(frame[12:14], "big")
        while ethertype in {0x8100, 0x88A8, 0x9100}:
            if len(frame) < offset + 4:
                return None
            ethertype = int.from_bytes(frame[offset + 2:offset + 4], "big")
            offset += 4
        return offset, ethertype
    if linktype == 113:  # Linux cooked capture v1.
        if len(frame) < 16:
            return None
        return 16, int.from_bytes(frame[14:16], "big")
    if linktype == 276:  # Linux cooked capture v2.
        if len(frame) < 20:
            return None
        return 20, int.from_bytes(frame[0:2], "big")
    if linktype in {101, _LINKTYPE_RAW_IPV4, _LINKTYPE_RAW_IPV6}:  # Raw IPv4 / IPv6.
        if not frame:
            return None
        version = frame[0] >> 4
        return 0, 0x0800 if version == 4 else 0x86DD if version == 6 else 0
    if linktype in {0, 108}:  # Loopback/null; inspect the IP version after AF.
        if len(frame) < 4:
            return None
        version = frame[4] >> 4 if len(frame) > 4 else 0
        return 4, 0x0800 if version == 4 else 0x86DD if version == 6 else 0
    return None


def _ipv4_udp(frame: bytes, offset: int) -> tuple[int, int, int, int] | None:
    if len(frame) < offset + 20:
        return None
    first = frame[offset]
    if first >> 4 != 4:
        return None
    header_length = (first & 0x0F) * 4
    if header_length < 20 or len(frame) < offset + header_length:
        return None
    if frame[offset + 9] != 17:
        return None
    udp_offset = offset + header_length
    if len(frame) < udp_offset + 8:
        return None
    source_port, destination_port, udp_length = struct.unpack_from(">HHH", frame, udp_offset)
    if udp_length < 8:
        return None
    captured_payload = max(0, len(frame) - udp_offset - 8)
    return source_port, destination_port, udp_length - 8, captured_payload


def _ipv6_udp(frame: bytes, offset: int) -> tuple[int, int, int, int] | None:
    if len(frame) < offset + 40 or frame[offset] >> 4 != 6:
        return None
    next_header = frame[offset + 6]
    cursor = offset + 40
    # Walk the extension headers that have an unambiguous byte length.  ESP
    # cannot be inspected without decrypting it and is intentionally skipped.
    while next_header in {0, 43, 60, 44, 51}:
        if next_header == 44:
            length = 8
        elif next_header == 51:
            if len(frame) < cursor + 2:
                return None
            length = (frame[cursor + 1] + 2) * 4
        else:
            if len(frame) < cursor + 2:
                return None
            length = (frame[cursor + 1] + 1) * 8
        if len(frame) < cursor + length:
            return None
        next_header = frame[cursor]
        cursor += length
    if next_header != 17 or len(frame) < cursor + 8:
        return None
    source_port, destination_port, udp_length = struct.unpack_from(">HHH", frame, cursor)
    if udp_length < 8:
        return None
    captured_payload = max(0, len(frame) - cursor - 8)
    return source_port, destination_port, udp_length - 8, captured_payload


def _udp_packet_status(
    frame: bytes, linktype: int
) -> tuple[tuple[int, int, int, int] | None, str]:
    network = _network_offset(frame, linktype)
    if network is None:
        return None, "parse"
    offset, ethertype = network
    if ethertype == 0x0800:
        if len(frame) < offset + 20:
            return None, "parse"
        if frame[offset + 9] != 17:
            return None, "filtered"
        parsed = _ipv4_udp(frame, offset)
        return (parsed, "target" if parsed is not None else "parse")
    if ethertype == 0x86DD:
        if len(frame) < offset + 40:
            return None, "parse"
        parsed = _ipv6_udp(frame, offset)
        return (parsed, "target" if parsed is not None else "parse")
    return None, "filtered"


def _udp_packet(frame: bytes, linktype: int) -> tuple[int, int, int, int] | None:
    """Return UDP fields while retaining the old private helper contract."""

    return _udp_packet_status(frame, linktype)[0]


def read_tcpdump_stats(path: Path | None) -> dict[str, int | str | None]:
    """Extract tcpdump's end-of-run packet counters when stderr is available."""

    stats: dict[str, int | str | None] = {
        "packets_captured": None,
        "packets_received_by_filter": None,
        "packets_dropped_by_kernel": None,
        "source": str(path) if path else "",
    }
    if path is None or not path.is_file():
        return stats
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return stats
    patterns = {
        "packets_captured": r"(\d+)\s+packets captured",
        "packets_received_by_filter": r"(\d+)\s+packets received by filter",
        "packets_dropped_by_kernel": r"(\d+)\s+packets dropped by kernel",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            stats[key] = int(match.group(1))
    return stats


def read_pcap(path: Path, port: int, *, port_direction: str = "destination") -> PcapCapture:
    if not 1 <= port <= 65535:
        raise PcapError("UDP port must be in 1..65535")
    if port_direction not in {"source", "destination", "either"}:
        raise PcapError("port_direction must be source, destination, or either")
    try:
        handle = path.open("rb")
    except OSError as error:
        raise PcapError(f"cannot open PCAP {path}: {error}") from error
    with handle:
        global_header = _read_exact(handle, 24, "PCAP global header")
        magic = global_header[:4]
        if magic not in _MAGIC:
            raise PcapError(f"unsupported PCAP magic: {magic.hex()}")
        endian, resolution = _MAGIC[magic]
        _version_major, _version_minor, _tz, _sigfigs, snaplen, linktype = struct.unpack(
            f"{endian}HHIIII", global_header[4:]
        )
        if snaplen <= 0:
            raise PcapError("PCAP snaplen must be positive")
        packets: list[PcapPacket] = []
        records_seen = 0
        skipped_packets = 0
        filtered_packets = 0
        parse_errors = 0
        truncation_errors = 0
        divisor = 1_000_000_000 if resolution == "nanosecond" else 1_000_000
        while True:
            header = handle.read(16)
            if not header:
                break
            if len(header) != 16:
                raise PcapError("truncated PCAP packet header")
            seconds, fraction, captured_length, original_length = struct.unpack(
                f"{endian}IIII", header
            )
            if captured_length > snaplen:
                raise PcapError("PCAP packet exceeds global snaplen")
            frame = _read_exact(handle, captured_length, "PCAP packet data")
            records_seen += 1
            if captured_length < original_length:
                truncation_errors += 1
            parsed, status = _udp_packet_status(frame, linktype)
            if parsed is None:
                skipped_packets += 1
                if status == "parse":
                    parse_errors += 1
                else:
                    filtered_packets += 1
                continue
            source_port, destination_port, payload_bytes, captured_payload_bytes = parsed
            matches = (
                source_port == port if port_direction == "source"
                else destination_port == port if port_direction == "destination"
                else source_port == port or destination_port == port
            )
            if not matches:
                skipped_packets += 1
                filtered_packets += 1
                continue
            timestamp_ns = seconds * 1_000_000_000 + fraction * (1_000_000_000 // divisor)
            packets.append(
                PcapPacket(
                    timestamp_ns=timestamp_ns,
                    payload_bytes=payload_bytes,
                    captured_payload_bytes=captured_payload_bytes,
                    captured_length=captured_length,
                    original_length=original_length,
                    truncated=captured_length < original_length or captured_payload_bytes < payload_bytes,
                )
            )
    return PcapCapture(
        linktype,
        snaplen,
        resolution,
        tuple(packets),
        records_seen,
        skipped_packets,
        filtered_packets,
        parse_errors,
        truncation_errors,
    )


def build_trace(
    packets: Iterable[PcapPacket],
    *,
    duration_ms: int | None = None,
    window_ms: int = 10,
) -> tuple[list[int], list[dict[str, int | float]]]:
    packet_list = list(packets)
    if not packet_list:
        raise PcapError("PCAP contains no UDP packets for the requested port")
    if window_ms <= 0:
        raise PcapError("window_ms must be positive")
    if duration_ms is not None and duration_ms < 0:
        raise PcapError("duration_ms must be non-negative")
    first = packet_list[0].timestamp_ns
    return build_bandwidth_trace(
        (((packet.timestamp_ns - first) // 1_000_000, packet.payload_bytes) for packet in packet_list),
        duration_ms=duration_ms,
        window_ms=window_ms,
    )


def write_outputs(
    capture: PcapCapture,
    output_dir: Path,
    *,
    port: int,
    duration_ms: int | None = None,
    window_ms: int = 10,
    pcap_path: Path | None = None,
    tcpdump_stderr: Path | None = None,
) -> dict[str, object]:
    arrivals, rows = build_trace(capture.packets, duration_ms=duration_ms, window_ms=window_ms)
    output_dir.mkdir(parents=True, exist_ok=True)
    arrival_path = output_dir / "udp_arrival_time_ms.txt"
    arrival_path.write_text("".join(f"{value}\n" for value in arrivals), encoding="utf-8")
    trace_path = output_dir / "bandwidth_trace.tsv"
    with trace_path.open("w", encoding="utf-8") as handle:
        handle.write("time_ms\tpacket_count\tpayload_bytes\twindow_packet_count\twindow_payload_bytes\tbandwidth_mbps\n")
        for row in rows:
            handle.write(
                f"{row['time_ms']}\t{row['packet_count']}\t{row['payload_bytes']}\t"
                f"{row['window_packet_count']}\t{row['window_payload_bytes']}\t"
                f"{row['bandwidth_mbps']:.9f}\n"
            )
    first_ns = capture.packets[0].timestamp_ns
    last_ns = capture.packets[-1].timestamp_ns
    summary = {
        "source_pcap": str(pcap_path) if pcap_path else "",
        "linktype": capture.linktype,
        "snaplen": capture.snaplen,
        "timestamp_resolution": capture.timestamp_resolution,
        "udp_destination_port": port,
        "records_seen": capture.records_seen,
        "skipped_packets": capture.skipped_packets,
        "filtered_packets": capture.filtered_packets,
        "parse_errors": capture.parse_errors,
        "truncation_errors": capture.truncation_errors,
        "packet_count": len(capture.packets),
        "payload_bytes": sum(packet.payload_bytes for packet in capture.packets),
        "captured_payload_bytes": sum(packet.captured_payload_bytes for packet in capture.packets),
        "truncated_packets": sum(packet.truncated for packet in capture.packets),
        "first_timestamp_ns": first_ns,
        "last_timestamp_ns": last_ns,
        "capture_duration_ms": (last_ns - first_ns) / 1_000_000,
        "trace_rows": len(rows),
        "window_ms": window_ms,
        "duration_ms": duration_ms,
        "tcpdump": read_tcpdump_stats(tcpdump_stderr),
        "arrival_file": str(arrival_path),
        "bandwidth_trace_file": str(trace_path),
    }
    summary_path = output_dir / "pcap_bandwidth_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**summary, "arrival_path": arrival_path, "trace_path": trace_path, "summary_path": summary_path}


def process_pcap(
    pcap_path: Path,
    output_dir: Path,
    *,
    port: int,
    duration_ms: int | None = None,
    window_ms: int = 10,
    payload_bytes: int | None = None,
    tcpdump_stderr: Path | None = None,
    port_direction: str = "destination",
) -> dict[str, object]:
    capture = read_pcap(pcap_path, port, port_direction=port_direction)
    if not capture.packets:
        raise PcapError(f"no valid UDP packets for destination port {port}")
    result = write_outputs(
        capture,
        output_dir,
        port=port,
        duration_ms=duration_ms,
        window_ms=window_ms,
        pcap_path=pcap_path,
        tcpdump_stderr=tcpdump_stderr,
    )
    plot_path = output_dir / "bandwidth_time_scatter.png"
    plot_error = ""
    try:
        from .BandwidthTimeScatter import plot_run
        inferred_payload = payload_bytes or int(round(sum(packet.payload_bytes for packet in capture.packets) / len(capture.packets)))
        plot_run(output_dir, result["arrival_path"], inferred_payload, output=plot_path, end_ms=duration_ms)
    except Exception as error:  # Plotting is optional; preserve parsed artifacts.
        plot_error = str(error)
    result["plot_path"] = plot_path if plot_path.exists() else None
    result["plot_error"] = plot_error
    return result


def _relative_file(run_dir: Path, path: Path | None) -> dict[str, object]:
    if path is None:
        return {"path": "", "exists": False, "size_bytes": 0}
    try:
        relative = str(path.relative_to(run_dir))
    except ValueError:
        relative = str(path)
    exists = path.is_file()
    return {"path": relative, "exists": exists, "size_bytes": path.stat().st_size if exists else 0}


def process_run_pcap(run_dir: Path, profile: dict[str, object]) -> dict[str, object]:
    """Process the server PCAP for an iperf uplink run and describe artifacts."""

    workload = profile.get("workload", {})
    server = profile.get("server", {})
    if not isinstance(workload, dict) or not isinstance(server, dict):
        return {"status": "failed", "error": "invalid profile workload/server"}
    if workload.get("kind") != "iperf3" or workload.get("direction") != "uplink":
        return {"status": "skipped"}
    tcpdump = server.get("tcpdump", {})
    if not isinstance(tcpdump, dict) or not tcpdump.get("enabled", False):
        return {"status": "skipped"}
    try:
        port = int(server["port"])
        duration_ms = round(float(workload["duration_s"]) * 1000)
        payload = int(workload.get("payload_bytes", 0))
    except (KeyError, TypeError, ValueError) as error:
        return {"status": "failed", "error": f"invalid PCAP profile: {error}"}

    server_dir = run_dir / "roles" / "server"
    client_json = run_dir / "roles" / "udp_sender" / "iperf3_client.json"
    server_json = server_dir / "iperf3_server.json"
    expected = server_dir / f"iperf3_uplink_udp_{port}.pcap"
    candidates = [
        expected,
        server_dir / "remote" / expected.name,
        server_dir / "remote" / run_dir.name / expected.name,
    ]
    source = next((candidate for candidate in candidates if candidate.is_file()), None)
    if source is not None and source != expected:
        expected.parent.mkdir(parents=True, exist_ok=True)
        try:
            import shutil

            shutil.copy2(source, expected)
            source = expected
        except OSError as error:
            source = None
            copy_error = str(error)
    else:
        copy_error = ""

    artifacts: dict[str, object] = {
        "phone_iperf3_json": _relative_file(run_dir, client_json),
        "server_iperf3_json": _relative_file(run_dir, server_json),
        "server_pcap": _relative_file(run_dir, expected),
    }

    def failed(error: str) -> dict[str, object]:
        artifacts["pcap_bandwidth"] = {"status": "failed", "exit_code": 1, "error": error}
        return {"status": "failed", "error": error, "artifacts": artifacts}

    for label, path in (("phone iperf3 JSON", client_json), ("server iperf3 JSON", server_json)):
        if not path.is_file() or path.stat().st_size == 0:
            return failed(f"missing or empty {label}: {path}")
    if source is None:
        return failed(copy_error or f"missing server PCAP: {expected}")
    if expected.stat().st_size == 0:
        return failed(f"empty server PCAP: {expected}")

    tcpdump_stderr = next(
        (
            candidate
            for candidate in (
                server_dir / "tcpdump.stderr",
                server_dir / "remote" / "tcpdump.stderr",
                server_dir / "remote" / run_dir.name / "tcpdump.stderr",
            )
            if candidate.is_file()
        ),
        None,
    )
    derived = run_dir / "derived"
    try:
        result = process_pcap(
            expected,
            derived,
            port=port,
            duration_ms=duration_ms,
            window_ms=10,
            payload_bytes=payload or None,
            tcpdump_stderr=tcpdump_stderr,
        )
    except (OSError, PcapError) as error:
        return failed(str(error))

    output_paths = {
        "udp_arrival_time_ms": result["arrival_path"],
        "bandwidth_trace": result["trace_path"],
        "pcap_bandwidth_summary": result["summary_path"],
        "bandwidth_time_scatter": result.get("plot_path"),
    }
    artifacts.update({name: _relative_file(run_dir, path) for name, path in output_paths.items()})
    artifacts["pcap_bandwidth"] = {
        "status": "complete",
        "exit_code": 0,
        "packet_count": result["packet_count"],
        "payload_bytes": result["payload_bytes"],
        "plot_error": result.get("plot_error", ""),
    }
    trace_path = result["trace_path"]
    if not isinstance(trace_path, Path) or not trace_path.is_file() or trace_path.stat().st_size == 0:
        return failed("PCAP produced no valid bandwidth trace")
    return {"status": "complete", "artifacts": artifacts, "summary": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--duration-ms", type=int)
    parser.add_argument("--window-ms", type=int, default=10)
    parser.add_argument("--payload-bytes", type=int)
    parser.add_argument("--port-direction", choices=("source", "destination", "either"), default="destination")
    parser.add_argument("--tcpdump-stderr", type=Path)
    args = parser.parse_args(argv)
    try:
        result = process_pcap(
            args.pcap,
            args.output_dir,
            port=args.port,
            duration_ms=args.duration_ms,
            window_ms=args.window_ms,
            payload_bytes=args.payload_bytes,
            port_direction=args.port_direction,
            tcpdump_stderr=args.tcpdump_stderr,
        )
    except (OSError, PcapError) as error:
        print(f"ERROR: failed to process PCAP: {error}", file=sys.stderr)
        return 2
    print(json.dumps({key: value for key, value in result.items() if not isinstance(value, Path)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
