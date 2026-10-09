from __future__ import annotations

import struct
import tempfile
import unittest
import json
from pathlib import Path

from traces.scripts.PcapBandwidthTrace import PcapError, build_trace, process_pcap, process_run_pcap, read_pcap


def ethernet_ipv4_udp(destination_port: int, payload_size: int = 1200) -> bytes:
    ethernet = bytes.fromhex("00112233445566778899aabb0800")
    ip_header = bytearray(20)
    ip_header[0] = 0x45
    struct.pack_into(">H", ip_header, 2, 20 + 8 + payload_size)
    ip_header[8] = 64
    ip_header[9] = 17
    ip_header[12:16] = bytes.fromhex("c0000201")
    ip_header[16:20] = bytes.fromhex("c6336401")
    udp_header = struct.pack(">HHHH", 40000, destination_port, 8 + payload_size, 0)
    return ethernet + bytes(ip_header) + udp_header + bytes(payload_size)


def ethernet_ipv6_udp(destination_port: int, payload_size: int = 1200) -> bytes:
    ethernet = bytes.fromhex("00112233445566778899aabb86dd")
    ip_header = bytearray(40)
    ip_header[0] = 0x60
    struct.pack_into(">H", ip_header, 4, 8 + payload_size)
    ip_header[6] = 17
    ip_header[7] = 64
    udp_header = struct.pack(">HHHH", 40000, destination_port, 8 + payload_size, 0)
    return ethernet + bytes(ip_header) + udp_header + bytes(payload_size)


def write_pcap(path: Path, *, magic: bytes, records: list[tuple[int, int, bytes]], snaplen: int = 2048) -> None:
    endian = "<" if magic in {b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1"} else ">"
    resolution = 1_000_000_000 if magic in {b"M<\xb2\xa1", b"\xa1\xb2<M"} else 1_000_000
    with path.open("wb") as handle:
        handle.write(magic)
        handle.write(struct.pack(f"{endian}HHIIII", 2, 4, 0, 0, snaplen, 1))
        for seconds, fraction, frame in records:
            handle.write(struct.pack(f"{endian}IIII", seconds, fraction, len(frame), len(frame)))
            handle.write(frame)
    assert resolution in {1_000_000, 1_000_000_000}


class PcapBandwidthTraceTest(unittest.TestCase):
    def test_reads_little_endian_microsecond_pcap_and_filters_destination_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcap = root / "capture.pcap"
            write_pcap(
                pcap,
                magic=b"\xd4\xc3\xb2\xa1",
                records=[
                    (10, 100_000, ethernet_ipv4_udp(5201)),
                    (10, 101_500, ethernet_ipv4_udp(5201)),
                    (10, 102_000, ethernet_ipv4_udp(9999)),
                ],
            )
            capture = read_pcap(pcap, 5201)
            self.assertEqual(capture.timestamp_resolution, "microsecond")
            self.assertEqual(len(capture.packets), 2)
            self.assertEqual(capture.skipped_packets, 1)
            self.assertEqual(capture.packets[0].payload_bytes, 1200)
            arrivals, rows = build_trace(capture.packets, duration_ms=3, window_ms=1)
            self.assertEqual(arrivals, [0, 1])
            self.assertEqual([row["packet_count"] for row in rows], [1, 1, 0, 0])
            self.assertEqual([row["bandwidth_mbps"] for row in rows[:2]], [9.6, 9.6])

    def test_reads_big_endian_nanosecond_pcap_and_writes_trace_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcap = root / "capture.pcap"
            output = root / "derived"
            write_pcap(
                pcap,
                magic=b"\xa1\xb2<M",
                records=[
                    (20, 0, ethernet_ipv4_udp(5201, 100)),
                    (20, 2_000_000, ethernet_ipv4_udp(5201, 100)),
                ],
            )
            result = process_pcap(pcap, output, port=5201, duration_ms=4, window_ms=2, payload_bytes=100)
            self.assertEqual((output / "udp_arrival_time_ms.txt").read_text().splitlines(), ["0", "2"])
            lines = (output / "bandwidth_trace.tsv").read_text().splitlines()
            self.assertEqual(lines[0].split("\t")[0], "time_ms")
            self.assertEqual(len(lines), 6)
            self.assertTrue((output / "pcap_bandwidth_summary.json").is_file())
            self.assertEqual(result["packet_count"], 2)

    def test_truncated_packet_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcap = root / "truncated.pcap"
            write_pcap(pcap, magic=b"\xd4\xc3\xb2\xa1", records=[])
            with pcap.open("ab") as handle:
                handle.write(struct.pack("<IIII", 1, 0, 20, 20))
                handle.write(b"short")
            with self.assertRaises(PcapError):
                read_pcap(pcap, 5201)

    def test_counts_captured_payload_truncation_and_tcpdump_drops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcap = root / "snaplen.pcap"
            frame = ethernet_ipv4_udp(5201, 1200)
            captured = frame[:256]
            with pcap.open("wb") as handle:
                handle.write(b"\xd4\xc3\xb2\xa1")
                handle.write(struct.pack("<HHIIII", 2, 4, 0, 0, 256, 1))
                handle.write(struct.pack("<IIII", 10, 0, len(captured), len(frame)))
                handle.write(captured)
            tcpdump_stderr = root / "tcpdump.stderr"
            tcpdump_stderr.write_text(
                "2 packets captured\n2 packets received by filter\n1 packets dropped by kernel\n",
                encoding="utf-8",
            )
            capture = read_pcap(pcap, 5201)
            self.assertEqual(capture.packets[0].payload_bytes, 1200)
            self.assertEqual(capture.packets[0].captured_payload_bytes, 214)
            self.assertEqual(capture.truncation_errors, 1)
            self.assertTrue(capture.packets[0].truncated)
            result = process_pcap(
                pcap, root / "derived", port=5201, duration_ms=2,
                tcpdump_stderr=tcpdump_stderr,
            )
            summary = json.loads((root / "derived" / "pcap_bandwidth_summary.json").read_text())
            self.assertEqual(summary["truncated_packets"], 1)
            self.assertEqual(summary["tcpdump"]["packets_dropped_by_kernel"], 1)
            self.assertEqual(result["packet_count"], 1)

    def test_reads_ipv6_udp_payload_on_ethernet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pcap = Path(temporary) / "ipv6.pcap"
            write_pcap(
                pcap,
                magic=b"\xd4\xc3\xb2\xa1",
                records=[(1, 0, ethernet_ipv6_udp(5201, 64))],
            )
            capture = read_pcap(pcap, 5201)
            self.assertEqual(len(capture.packets), 1)
            self.assertEqual(capture.packets[0].payload_bytes, 64)

    def test_process_run_registers_raw_and_derived_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            server_dir = run_dir / "roles" / "server"
            (run_dir / "roles" / "udp_sender").mkdir(parents=True)
            server_dir.mkdir(parents=True)
            (run_dir / "roles" / "udp_sender" / "iperf3_client.json").write_text("{}\n", encoding="utf-8")
            (server_dir / "iperf3_server.json").write_text("{}\n", encoding="utf-8")
            write_pcap(
                server_dir / "iperf3_uplink_udp_5201.pcap",
                magic=b"\xd4\xc3\xb2\xa1",
                records=[(1, 0, ethernet_ipv4_udp(5201, 100))],
            )
            result = process_run_pcap(
                run_dir,
                {
                    "workload": {"kind": "iperf3", "direction": "uplink", "duration_s": 1, "payload_bytes": 100},
                    "server": {"port": 5201, "tcpdump": {"enabled": True}},
                },
            )
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["artifacts"]["server_pcap"]["exists"])
            self.assertTrue(result["artifacts"]["bandwidth_trace"]["exists"])

    def test_process_run_rejects_missing_pcap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "roles" / "server").mkdir(parents=True)
            (run_dir / "roles" / "udp_sender").mkdir(parents=True)
            (run_dir / "roles" / "udp_sender" / "iperf3_client.json").write_text("{}\n", encoding="utf-8")
            (run_dir / "roles" / "server" / "iperf3_server.json").write_text("{}\n", encoding="utf-8")
            result = process_run_pcap(
                run_dir,
                {
                    "workload": {"kind": "iperf3", "direction": "uplink", "duration_s": 1},
                    "server": {"port": 5201, "tcpdump": {"enabled": True}},
                },
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["artifacts"]["pcap_bandwidth"]["exit_code"], 1)

    def test_process_pcap_rejects_capture_without_target_udp_packets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pcap = root / "non_target.pcap"
            write_pcap(
                pcap,
                magic=b"\xd4\xc3\xb2\xa1",
                records=[(1, 0, ethernet_ipv4_udp(5202, 100))],
            )
            with self.assertRaises(PcapError):
                process_pcap(pcap, root / "derived", port=5201, duration_ms=1000)


if __name__ == "__main__":
    unittest.main()
