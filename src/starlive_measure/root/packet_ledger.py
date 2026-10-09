"""Verified, resumable phone packet-ledger transfer and local derivation."""

from __future__ import annotations

from collections import defaultdict
import csv
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import zlib
from typing import Any, Callable, Iterator

from starlive_measure.root.runtime import _empty_derived_outputs, run_root_checked
from starlive_measure.trace.PcapBandwidthTrace import read_tcpdump_stats
from starlive_measure.trace.trace_runtime import write_json


REMOTE_BINARY = "/data/local/tmp/pcap_packet_ledger"
HEADER = struct.Struct("<8sHHIIBBH8s")
RECORD = struct.Struct("<QQIIBBH16s16sHHIIqQH6x")
CHUNK_LIMIT = 32 * 1024 * 1024
FLAG_TARGET, FLAG_FILTERED, FLAG_PARSE, FLAG_TRUNCATED, FLAG_IPERF = (1, 2, 4, 8, 16)


class LedgerError(ValueError):
    """The phone ledger is incomplete or failed verification."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for data in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def read_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("format") != "starlive-packet-ledger":
        raise LedgerError("unsupported phone ledger manifest")
    if data.get("record_size") != RECORD.size or data.get("header_size") != HEADER.size:
        raise LedgerError("phone ledger record layout differs from reader")
    for name in ("records_seen", "ledger_bytes", "pcap_size_bytes", "target_packets", "filtered_records",
                 "parse_errors", "truncated_records", "iperf_decoded_records", "linktype", "snaplen", "port"):
        if type(data.get(name)) is not int or data[name] < 0:
            raise LedgerError(f"invalid phone ledger {name}")
    if not 1 <= data["port"] <= 65535 or data["snaplen"] == 0:
        raise LedgerError("invalid ledger port or snaplen")
    if data.get("direction") not in {"source", "destination"} or data.get("timestamp_resolution") not in {"microsecond", "nanosecond"}:
        raise LedgerError("invalid ledger direction or timestamp resolution")
    if not isinstance(data.get("tcpdump"), dict) or any(type(data["tcpdump"].get(name)) is not int or data["tcpdump"][name] < 0 for name in
        ("packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel")):
        raise LedgerError("missing tcpdump final counters")
    if data["target_packets"] + data["filtered_records"] + data["parse_errors"] != data["records_seen"]:
        raise LedgerError("ledger classification counts disagree")
    if data["iperf_decoded_records"] > data["target_packets"] or data["truncated_records"] > data["records_seen"]:
        raise LedgerError("ledger status counts disagree")
    if not isinstance(data.get("chunks"), list) or not data["chunks"]:
        raise LedgerError("phone ledger has no chunks")
    if data.get("ledger_bytes") != HEADER.size + data.get("records_seen", -1) * RECORD.size:
        raise LedgerError("ledger size and record count disagree")
    if data.get("tcpdump", {}).get("packets_captured", -1) != data["records_seen"]:
        raise LedgerError("tcpdump captured count and ledger record count disagree")
    for index, chunk in enumerate(data["chunks"]):
        if not isinstance(chunk, dict) or chunk.get("name") != f"chunk-{index:06d}.gz":
            raise LedgerError("invalid ledger chunk name")
        if type(chunk.get("uncompressed_bytes")) is not int or not 0 < chunk["uncompressed_bytes"] <= CHUNK_LIMIT:
            raise LedgerError("invalid ledger chunk name or length")
        if type(chunk.get("size_bytes")) is not int or chunk["size_bytes"] <= 0:
            raise LedgerError("invalid compressed chunk size")
        if not re.fullmatch(r"[0-9a-f]{64}", str(chunk.get("sha256", ""))):
            raise LedgerError("invalid compressed chunk hash")
    if sum(chunk["uncompressed_bytes"] for chunk in data["chunks"]) != data["ledger_bytes"]:
        raise LedgerError("ledger chunk lengths disagree")
    for name in ("pcap_sha256", "ledger_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(data.get(name, ""))):
            raise LedgerError(f"invalid {name}")
    return data


def valid_chunk(path: Path, chunk: dict[str, Any], *, verify_gzip: bool = True) -> bool:
    if not path.is_file() or path.stat().st_size != chunk["size_bytes"] or _sha256(path) != chunk["sha256"]:
        return False
    if verify_gzip:
        try:
            size = 0
            with gzip.open(path, "rb") as handle:
                for data in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(data)
                    if size > chunk["uncompressed_bytes"]:
                        return False
            if size != chunk["uncompressed_bytes"]:
                return False
        except (OSError, EOFError, zlib.error):
            return False
    return True


def install_extractor(serial: str, binary: Path) -> None:
    result = subprocess.run(["adb", "-s", serial, "push", str(binary), REMOTE_BINARY], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "failed to push phone ledger extractor")
    result = run_root_checked(serial, ["chmod", "755", REMOTE_BINARY], timeout=30)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "failed to chmod phone ledger extractor")


def extract_on_phone(serial: str, remote_dir: str, stderr: Path, *, port: int, direction: str, decode_iperf: bool) -> None:
    stats = read_tcpdump_stats(stderr)
    if any(stats[key] is None for key in ("packets_captured", "packets_received_by_filter", "packets_dropped_by_kernel")):
        raise LedgerError("tcpdump final counters are unavailable; keeping original phone pcap")
    command = [REMOTE_BINARY, "--input", f"{remote_dir}/root_capture.pcap", "--out-dir", f"{remote_dir}/packet_ledger",
               "--port", str(port), "--direction", direction,
               "--tcpdump-captured", str(stats["packets_captured"]),
               "--tcpdump-received-by-filter", str(stats["packets_received_by_filter"]),
               "--tcpdump-dropped-by-kernel", str(stats["packets_dropped_by_kernel"])]
    if decode_iperf:
        command.append("--decode-iperf")
    # A local interruption may leave the extraction process running on Android.
    # Do not create a second writer against its temporary directory.
    pid_path = shlex.quote(f"{remote_dir}/ledger.pid")
    script = (f"if test -f {pid_path} && kill -0 $(cat {pid_path}) 2>/dev/null; then "
              f"echo 'phone extraction is still running; retry recover when it exits' >&2; exit 75; fi; "
              f"{shlex.join(command)} & pid=$!; echo $pid > {pid_path}; wait $pid; rc=$?; "
              f"rm -f {pid_path}; exit $rc")
    result = run_root_checked(serial, script, timeout=1800)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or result.stdout.strip() or "phone ledger extraction failed")


def transfer_from_phone(serial: str, remote_dir: str, local_dir: Path) -> tuple[dict[str, Any], int]:
    """Pull only missing or invalid chunks; rename each verified pull atomically."""
    local_dir.mkdir(parents=True, exist_ok=True)
    remote = f"{remote_dir}/packet_ledger"
    manifest_path = local_dir / "ledger_manifest.json"
    part = local_dir / "ledger_manifest.json.part"
    result = subprocess.run(["adb", "-s", serial, "pull", f"{remote}/ledger_manifest.json", str(part)], capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "phone ledger manifest pull failed")
    candidate = read_manifest(part)
    if manifest_path.is_file() and manifest_path.read_bytes() != part.read_bytes():
        raise LedgerError("phone ledger manifest changed across recovery attempts")
    os.replace(part, manifest_path)
    fetched = 0
    for chunk in candidate["chunks"]:
        target = local_dir / chunk["name"]
        if valid_chunk(target, chunk):
            continue
        partial = local_dir / (chunk["name"] + ".part")
        for attempt in range(3):
            partial.unlink(missing_ok=True)
            try:
                pull = subprocess.run(["adb", "-s", serial, "pull", f"{remote}/{chunk['name']}", str(partial)], capture_output=True, text=True, timeout=120)
                if pull.returncode == 0 and valid_chunk(partial, chunk):
                    os.replace(partial, target)
                    fetched += 1
                    break
            except subprocess.TimeoutExpired:
                pass
        else:
            raise LedgerError(f"failed to retrieve verified {chunk['name']} after 3 attempts")
    return candidate, fetched


def transfer_from_server(ssh: str, remote_dir: str, local_dir: Path) -> tuple[dict[str, Any], int]:
    """Pull and verify server ledger chunks without transferring the raw pcap."""
    local_dir.mkdir(parents=True, exist_ok=True)
    remote = f"{remote_dir}/packet_ledger"
    manifest_path = local_dir / "ledger_manifest.json"
    part = local_dir / "ledger_manifest.json.part"
    result = subprocess.run(["scp", "-O", "-q", "-o", "ConnectTimeout=30", f"{ssh}:{remote}/ledger_manifest.json", str(part)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "server ledger manifest pull failed")
    candidate = read_manifest(part)
    if manifest_path.is_file() and manifest_path.read_bytes() != part.read_bytes():
        raise LedgerError("server ledger manifest changed across recovery attempts")
    os.replace(part, manifest_path)
    fetched = 0
    for chunk in candidate["chunks"]:
        target = local_dir / chunk["name"]
        if valid_chunk(target, chunk):
            continue
        partial = local_dir / (chunk["name"] + ".part")
        for _attempt in range(3):
            partial.unlink(missing_ok=True)
            try:
                pull = subprocess.run(["scp", "-O", "-q", "-o", "ConnectTimeout=30", f"{ssh}:{remote}/{chunk['name']}", str(partial)],
                                      capture_output=True, text=True, timeout=120)
                if pull.returncode == 0 and valid_chunk(partial, chunk):
                    os.replace(partial, target)
                    fetched += 1
                    break
            except subprocess.TimeoutExpired:
                pass
        else:
            raise LedgerError(f"failed to retrieve verified server {chunk['name']} after 3 attempts")
    return candidate, fetched


def _records(local_dir: Path, manifest: dict[str, Any]) -> Iterator[tuple[int, tuple[Any, ...]]]:
    whole = hashlib.sha256()
    record_index = 0
    totals = {"target_packets": 0, "filtered_records": 0, "parse_errors": 0,
              "truncated_records": 0, "iperf_decoded_records": 0}
    for chunk_index, chunk in enumerate(manifest["chunks"]):
        path = local_dir / chunk["name"]
        if not valid_chunk(path, chunk, verify_gzip=False):
            raise LedgerError(f"missing or corrupt {chunk['name']}")
        size = 0
        try:
            with gzip.open(path, "rb") as handle:
                if chunk_index == 0:
                    raw = handle.read(HEADER.size)
                    size += len(raw)
                    whole.update(raw)
                    if len(raw) != HEADER.size:
                        raise LedgerError("truncated ledger header")
                    magic, version, record_size, linktype, snaplen, resolution, direction, port, _reserved = HEADER.unpack(raw)
                    if (magic, version, record_size, linktype, snaplen, port) != (b"STLGR001", 1, RECORD.size, manifest["linktype"], manifest["snaplen"], manifest["port"]):
                        raise LedgerError("ledger header and manifest disagree")
                    if resolution != (1 if manifest["timestamp_resolution"] == "nanosecond" else 0) or direction != (1 if manifest["direction"] == "source" else 0):
                        raise LedgerError("ledger capture metadata disagrees")
                while raw := handle.read(RECORD.size):
                    size += len(raw)
                    whole.update(raw)
                    if len(raw) != RECORD.size:
                        raise LedgerError("partial ledger record")
                    row = RECORD.unpack(raw)
                    if row[0] != record_index:
                        raise LedgerError("ledger record index is out of sequence")
                    status = row[5]
                    if status & FLAG_TARGET:
                        totals["target_packets"] += 1
                    elif status & FLAG_PARSE:
                        totals["parse_errors"] += 1
                    elif status & FLAG_FILTERED:
                        totals["filtered_records"] += 1
                    else:
                        raise LedgerError("record has no target, parse, or filtered state")
                    if status & FLAG_TRUNCATED:
                        totals["truncated_records"] += 1
                    if status & FLAG_IPERF:
                        totals["iperf_decoded_records"] += 1
                    yield record_index, row
                    record_index += 1
        except (OSError, EOFError, zlib.error) as error:
            raise LedgerError(f"gzip verification failed for {chunk['name']}: {error}") from error
        if size != chunk["uncompressed_bytes"]:
            raise LedgerError(f"uncompressed size mismatch for {chunk['name']}")
    if record_index != manifest["records_seen"] or whole.hexdigest() != manifest["ledger_sha256"]:
        raise LedgerError("complete ledger count or SHA-256 mismatch")
    for key, value in totals.items():
        if value != manifest[key]:
            raise LedgerError(f"ledger {key} disagrees with phone manifest")


def _address(version: int, raw: bytes) -> str:
    return str(ipaddress.ip_address(raw[:4] if version == 4 else raw)) if version in (4, 6) else ""


def iter_decoded_packets(local_dir: Path, manifest: dict[str, Any]) -> Iterator[dict[str, int]]:
    """Yield verified iperf UDP records in capture order from a complete ledger."""
    for _record_index, row in _records(local_dir, manifest):
        if row[5] & FLAG_TARGET and row[5] & FLAG_IPERF:
            yield {"sequence": int(row[13]), "timestamp_ns": int(row[1]), "payload_bytes": int(row[11]),
                   "captured_payload_bytes": int(row[12]), "send_time_ns": int(row[14]), "status_flags": int(row[5])}


def decode_failure_summary(local_dir: Path, manifest: dict[str, Any]) -> dict[str, int]:
    """Count target records omitted from sequence matching by decoder status."""
    names = {0: "decoder_disabled", 1: "decoded", 2: "truncated_header", 3: "invalid_microseconds", 4: "negative_sequence"}
    counts: dict[str, int] = {}
    for _index, row in _records(local_dir, manifest):
        if row[5] & FLAG_TARGET and not row[5] & FLAG_IPERF:
            label = names.get(int(row[6]), f"unknown_decode_status_{row[6]}")
            counts[label] = counts.get(label, 0) + 1
    return counts


def process_ledger(local_dir: Path, run_dir: Path, *, duration_ms: int, clock_sync: dict[str, Any] | None = None) -> dict[str, Any]:
    """Verify gzip and full ledger while streaming records into the old outputs."""
    manifest = read_manifest(local_dir / "ledger_manifest.json")
    derived = run_dir / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    packets_tmp = derived / "pcap_packets.tsv.part"
    arrival_tmp = derived / "udp_arrival_time_ms.txt.part"
    buckets: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    first_ns = last_ns = None
    previous_ms = 0
    packet_count = payload_sum = captured_sum = truncated_packets = undecoded = 0
    try:
        with packets_tmp.open("w", encoding="utf-8", newline="") as packets, arrival_tmp.open("w", encoding="utf-8") as arrivals:
            writer = csv.writer(packets, delimiter="\t", lineterminator="\n")
            writer.writerow(["packet_index", "timestamp_ns", "payload_bytes", "captured_payload_bytes", "truncated",
                             "record_index", "captured_length", "original_length", "ip_version", "source_ip", "destination_ip",
                             "source_port", "destination_port", "iperf_sequence", "iperf_send_time_ns", "status_flags", "iperf_decode_status", "udp_length"])
            for record_index, row in _records(local_dir, manifest):
                stored_index, timestamp, captured, original, version, status, decode_status, source, destination, src_port, dst_port, payload, captured_payload, seq, send_ns, udp_length = row
                if not status & FLAG_TARGET:
                    continue
                if first_ns is None:
                    first_ns = timestamp
                last_ns = timestamp
                relative_ms = max(previous_ms, (timestamp - first_ns) // 1_000_000)
                previous_ms = relative_ms
                buckets[relative_ms][0] += 1
                buckets[relative_ms][1] += payload
                arrivals.write(f"{relative_ms}\n")
                decoded = bool(status & FLAG_IPERF)
                if not decoded:
                    undecoded += 1
                truncated_packets += bool(status & FLAG_TRUNCATED)
                payload_sum += payload
                captured_sum += captured_payload
                writer.writerow([packet_count, timestamp, payload, captured_payload, int(bool(status & FLAG_TRUNCATED)),
                                 stored_index, captured, original, version, _address(version, source), _address(version, destination),
                                 src_port, dst_port, seq if decoded else "", send_ns if decoded else "", status,
                                 {0: "decoder_disabled", 1: "decoded", 2: "truncated_header", 3: "invalid_microseconds", 4: "negative_sequence"}.get(decode_status, "unknown_decode_status"), udp_length])
                packet_count += 1
        if packet_count == 0:
            raise LedgerError("ledger has no target UDP packets")
        # Only publish data after the full stream, CRCs, sizes, counts and hash verify.
        _empty_derived_outputs(derived, "root packet latency/loss unavailable without calibrated matching")
        os.replace(packets_tmp, derived / "pcap_packets.tsv")
        os.replace(arrival_tmp, derived / "udp_arrival_time_ms.txt")
        last_ms = max(previous_ms, duration_ms)
        trace_tmp = derived / "bandwidth_trace.tsv.part"
        bandwidth_tmp = derived / "bandwidth_100ms.tsv.part"
        rolling_count = rolling_bytes = 0
        with trace_tmp.open("w", encoding="utf-8") as trace, bandwidth_tmp.open("w", encoding="utf-8") as bandwidth:
            trace.write("time_ms\tpacket_count\tpayload_bytes\twindow_packet_count\twindow_payload_bytes\tbandwidth_mbps\n")
            bandwidth.write("time_s\tbandwidth_mbps\n")
            for time_ms in range(last_ms + 1):
                count, payload = buckets.get(time_ms, (0, 0))
                expired_count, expired_payload = buckets.get(time_ms - 100, (0, 0))
                rolling_count += count - expired_count
                rolling_bytes += payload - expired_payload
                mbps = rolling_bytes * 8 / 100_000.0
                trace.write(f"{time_ms}\t{count}\t{payload}\t{rolling_count}\t{rolling_bytes}\t{mbps:.9f}\n")
                bandwidth.write(f"{time_ms / 1000.0:.6f}\t{mbps:.9f}\n")
        os.replace(trace_tmp, derived / "bandwidth_trace.tsv")
        os.replace(bandwidth_tmp, derived / "bandwidth_100ms.tsv")
        detail = {"source_pcap": f"phone:{manifest['pcap_sha256']}", "linktype": manifest["linktype"], "snaplen": manifest["snaplen"],
                  "timestamp_resolution": manifest["timestamp_resolution"], "udp_destination_port": manifest["port"],
                  "records_seen": manifest["records_seen"], "skipped_packets": manifest["filtered_records"] + manifest["parse_errors"],
                  "filtered_packets": manifest["filtered_records"], "parse_errors": manifest["parse_errors"],
                  "truncation_errors": manifest["truncated_records"], "packet_count": packet_count,
                  "payload_bytes": payload_sum, "captured_payload_bytes": captured_sum, "truncated_packets": truncated_packets,
                  "first_timestamp_ns": first_ns, "last_timestamp_ns": last_ns,
                  "capture_duration_ms": (last_ns - first_ns) / 1_000_000, "trace_rows": last_ms + 1,
                  "window_ms": 100, "duration_ms": duration_ms, "tcpdump": manifest["tcpdump"],
                  "arrival_file": str(derived / "udp_arrival_time_ms.txt"), "bandwidth_trace_file": str(derived / "bandwidth_trace.tsv")}
        write_json(derived / "pcap_bandwidth_summary.json", detail)
        summary = {"status": "complete", "source": "phone_packet_ledger", "packet_count": packet_count,
                   "records_seen": manifest["records_seen"], "filtered_packets": manifest["filtered_records"],
                   "parse_errors": manifest["parse_errors"], "truncation_errors": manifest["truncated_records"],
                   "capture_duration_ms": detail["capture_duration_ms"], "bandwidth_source": "phone_packet_ledger",
                   "port_direction": manifest["direction"], "latency_status": "unavailable", "loss_status": "unavailable",
                   "iperf_decoded_packets": manifest["iperf_decoded_records"], "iperf_undecoded_packets": undecoded,
                   "iperf_decode_note": "Undecoded packets are not used to infer loss", "clock_sync": clock_sync or {"status": "unavailable"}}
        write_json(derived / "root_transport_summary.json", summary)
        return summary
    finally:
        for temporary in (packets_tmp, arrival_tmp):
            temporary.unlink(missing_ok=True)


def delete_phone_pcap(serial: str, remote_dir: str) -> None:
    result = run_root_checked(serial, ["rm", "-f", f"{remote_dir}/root_capture.pcap"], timeout=30)
    if result.returncode:
        raise LedgerError(result.stderr.strip() or "failed to delete verified phone pcap")
    check = run_root_checked(serial, ["test", "!", "-e", f"{remote_dir}/root_capture.pcap"], timeout=30)
    if check.returncode:
        raise LedgerError("phone pcap still exists after cleanup")
