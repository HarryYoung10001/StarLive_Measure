"""End-to-end packet-ledger fixtures and recovery invariants."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from starlive_measure.root import packet_ledger
from starlive_measure.trace.PcapBandwidthTrace import build_trace, read_pcap


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "runtime" / "android" / "probes" / "pcap_packet_ledger.go"


def udp_ipv4(payload: bytes, *, port: int = 5201) -> bytes:
    ip = bytearray(20)
    ip[0], ip[8], ip[9] = 0x45, 64, 17
    ip[2:4] = struct.pack("!H", 28 + len(payload))
    ip[12:16], ip[16:20] = b"\xc0\x00\x02\x01", b"\xc0\x00\x02\x02"
    return bytes(ip) + struct.pack("!HHHH", 40000, port, 8 + len(payload), 0) + payload


def udp_ipv6(payload: bytes, *, port: int = 5201) -> bytes:
    ip = bytearray(40)
    ip[0], ip[6], ip[7] = 0x60, 17, 64
    ip[4:6] = struct.pack("!H", 8 + len(payload))
    ip[8:24], ip[24:40] = bytes(range(16)), bytes(range(16, 32))
    return bytes(ip) + struct.pack("!HHHH", 40000, port, 8 + len(payload), 0) + payload


def write_pcap(path: Path, frames: list[tuple[int, int, bytes, int]], *, linktype: int, nanos: bool = False) -> None:
    magic = b"\x4d\x3c\xb2\xa1" if nanos else b"\xd4\xc3\xb2\xa1"
    with path.open("wb") as handle:
        handle.write(magic + struct.pack("<HHIIII", 2, 4, 0, 0, 256, linktype))
        for sec, fraction, frame, original in frames:
            handle.write(struct.pack("<IIII", sec, fraction, len(frame), original))
            handle.write(frame)


class PacketLedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not shutil.which("go"):
            raise unittest.SkipTest("Go toolchain unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / "packet_ledger"
        subprocess.run(["go", "build", "-o", str(cls.binary), str(SOURCE)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def extract(self, pcap: Path, out: Path, *, count: int, nanos: bool = False, decode: bool = False,
                direction: str = "destination") -> dict:
        command = [str(self.binary), "--input", str(pcap), "--out-dir", str(out), "--port", "5201", "--direction", direction,
                   "--tcpdump-captured", str(count), "--tcpdump-received-by-filter", str(count), "--tcpdump-dropped-by-kernel", "0"]
        if decode:
            command.append("--decode-iperf")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return packet_ledger.read_manifest(out / "ledger_manifest.json")

    def test_sll2_microsecond_matches_old_parser_with_duplicates_and_bad_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pcap = root / "capture.pcap"
            header = b"\x08\x00" + b"\x00" * 18
            iperf = lambda seq: struct.pack("!IIQ", 1, 123, seq) + b"x" * 24
            frames = [(1, 100, header + udp_ipv4(iperf(seq)), len(header + udp_ipv4(iperf(seq)))) for seq in (2, 1, 1)]
            frames.append((1, 200, header + udp_ipv4(b"z" * 8), len(header + udp_ipv4(b"z" * 40))))
            frames.append((1, 300, b"bad", 3))
            write_pcap(pcap, frames, linktype=276)
            old = read_pcap(pcap, 5201)
            ledger = self.extract(pcap, root / "ledger", count=len(frames), decode=True)
            summary = packet_ledger.process_ledger(root / "ledger", root / "run", duration_ms=100)
            self.assertEqual(ledger["pcap_sha256"], hashlib.sha256(pcap.read_bytes()).hexdigest())
            self.assertEqual(ledger["records_seen"], old.records_seen)
            self.assertEqual(summary["packet_count"], len(old.packets))
            self.assertEqual(summary["parse_errors"], old.parse_errors)
            self.assertEqual(summary["iperf_decoded_packets"], 3)
            rows = (root / "run" / "derived" / "pcap_packets.tsv").read_text().splitlines()
            self.assertEqual([int(row.split("\t")[1]) for row in rows[1:]], [p.timestamp_ns for p in old.packets])
            self.assertEqual([row.split("\t")[13] for row in rows[1:4]], ["2", "1", "1"])
            arrivals, old_trace = build_trace(old.packets, duration_ms=100, window_ms=100)
            self.assertEqual([int(x) for x in (root / "run" / "derived" / "udp_arrival_time_ms.txt").read_text().splitlines()], arrivals)
            new_trace = (root / "run" / "derived" / "bandwidth_trace.tsv").read_text().splitlines()[1:]
            self.assertEqual([float(row.split("\t")[-1]) for row in new_trace], [round(row["bandwidth_mbps"], 9) for row in old_trace])

    def test_ethernet_ipv6_nanosecond_and_chunk_crc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = b"\x00" * 12 + b"\x86\xdd" + udp_ipv6(b"v" * 32)
            write_pcap(root / "capture.pcap", [(1, 123456789, frame, len(frame))], linktype=1, nanos=True)
            manifest = self.extract(root / "capture.pcap", root / "ledger", count=1)
            self.assertEqual(manifest["timestamp_resolution"], "nanosecond")
            packet_ledger.process_ledger(root / "ledger", root / "run", duration_ms=10)
            rows = (root / "run" / "derived" / "pcap_packets.tsv").read_text().splitlines()
            self.assertIn("1123456789", rows[1])
            self.assertIn("6", rows[1])
            chunk = root / "ledger" / manifest["chunks"][0]["name"]
            damaged = bytearray(chunk.read_bytes())
            damaged[-1] ^= 1
            chunk.write_bytes(damaged)
            with self.assertRaises(packet_ledger.LedgerError):
                packet_ledger.process_ledger(root / "ledger", root / "bad", duration_ms=10)
            self.assertFalse((root / "bad" / "derived" / "root_transport_summary.json").exists())
            manifest["chunks"][0]["sha256"] = hashlib.sha256(damaged).hexdigest()
            (root / "ledger" / "ledger_manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(packet_ledger.LedgerError):
                packet_ledger.process_ledger(root / "ledger", root / "bad_crc", duration_ms=10)
            chunk.unlink()
            with self.assertRaises(packet_ledger.LedgerError):
                packet_ledger.process_ledger(root / "ledger", root / "missing", duration_ms=10)

    def test_incomplete_pcap_or_bad_tcpdump_count_keeps_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = b"\x00" * 12 + b"\x08\x00" + udp_ipv4(b"a" * 16)
            write_pcap(root / "capture.pcap", [(1, 0, frame, len(frame))], linktype=1)
            result = subprocess.run([str(self.binary), "--input", str(root / "capture.pcap"), "--out-dir", str(root / "ledger"),
                                     "--port", "5201", "--tcpdump-captured", "2", "--tcpdump-received-by-filter", "2", "--tcpdump-dropped-by-kernel", "0"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "capture.pcap").exists())
            self.assertFalse((root / "ledger").exists())
            (root / "capture.pcap").write_bytes((root / "capture.pcap").read_bytes()[:-1])
            result = subprocess.run([str(self.binary), "--input", str(root / "capture.pcap"), "--out-dir", str(root / "ledger"),
                                     "--port", "5201", "--tcpdump-captured", "1", "--tcpdump-received-by-filter", "1", "--tcpdump-dropped-by-kernel", "0"], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / "capture.pcap").exists())

    def test_transfer_reuses_valid_chunk_and_repairs_missing_or_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = b"\x00" * 12 + b"\x08\x00" + udp_ipv4(b"a" * 16)
            write_pcap(root / "capture.pcap", [(1, 0, frame, len(frame))], linktype=1)
            manifest = self.extract(root / "capture.pcap", root / "remote" / "packet_ledger", count=1)
            calls = []

            def fake_pull(command, **kwargs):
                calls.append(command[4])
                source = Path(command[4].replace("/phone/packet_ledger", str(root / "remote" / "packet_ledger")))
                shutil.copyfile(source, command[5])
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(packet_ledger.subprocess, "run", side_effect=fake_pull):
                got, fetched = packet_ledger.transfer_from_phone("new-port", "/phone", root / "local")
                self.assertEqual(fetched, 1)
                calls.clear()
                got, fetched = packet_ledger.transfer_from_phone("new-port", "/phone", root / "local")
                self.assertEqual(fetched, 0)
                self.assertEqual(len(calls), 1)
                (root / "local" / manifest["chunks"][0]["name"]).write_bytes(b"bad")
                (root / "local" / (manifest["chunks"][0]["name"] + ".part")).write_bytes(b"partial")
                got, fetched = packet_ledger.transfer_from_phone("new-port", "/phone", root / "local")
                self.assertEqual(fetched, 1)
                self.assertEqual(got["ledger_sha256"], manifest["ledger_sha256"])

    def test_server_transfer_uses_verified_chunks_and_dual_ledgers_match_sequences(self) -> None:
        from starlive_measure.root.reference_sweep import derive_reference_run

        def frame(sequence: int, *, source: bool) -> bytes:
            payload = struct.pack("!IIQ", 10, 123_000, sequence) + b"x" * 32
            ip = bytearray(20)
            ip[0], ip[8], ip[9] = 0x45, 64, 17
            ip[2:4] = struct.pack("!H", 28 + len(payload))
            ip[12:16], ip[16:20] = b"\xc0\x00\x02\x01", b"\xc0\x00\x02\x02"
            src_port, dst_port = (5201, 40000) if source else (40000, 5201)
            return b"\x00" * 12 + b"\x08\x00" + bytes(ip) + struct.pack("!HHHH", src_port, dst_port, 8 + len(payload), 0) + payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server_pcap, phone_pcap = root / "server.pcap", root / "phone.pcap"
            write_pcap(server_pcap, [(10, seq * 10_000, frame(seq, source=True), len(frame(seq, source=True))) for seq in (0, 1, 2)], linktype=1)
            write_pcap(phone_pcap, [(10, seq * 10_000 + 5_000, frame(seq, source=True), len(frame(seq, source=True))) for seq in (0, 2, 3)], linktype=1)
            run = root / "run"
            server_remote = root / "server_remote"
            server_ledger = server_remote / "packet_ledger"
            (run / "roles" / "server").mkdir(parents=True)
            phone_ledger = run / "roles" / "udp_receiver" / "packet_ledger"
            server_manifest = self.extract(server_pcap, server_ledger, count=3, decode=True, direction="source")
            phone_manifest = self.extract(phone_pcap, phone_ledger, count=3, decode=True, direction="source")
            # The sender ledger was transferred over scp; publish chunks only after hashes and gzip pass.
            local_server = run / "roles" / "server" / "packet_ledger"

            def fake_scp(command, **_kwargs):
                source = server_ledger / Path(command[-2].split(":", 1)[1]).name
                shutil.copyfile(source, command[-1])
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(packet_ledger.subprocess, "run", side_effect=fake_scp):
                transferred, fetched = packet_ledger.transfer_from_server("sgp-server", str(server_remote), local_server)
            self.assertEqual(fetched, len(server_manifest["chunks"]))
            self.assertEqual(transferred["ledger_sha256"], server_manifest["ledger_sha256"])
            (run / "manifest.json").write_text(json.dumps({"clock_sync": {"status": "ok", "uncertainty_ms": 130.0},
                                                             "icmp_companion": {"elapsed_s": 0.02, "exit_code": 0}}))
            ping = run / "roles" / "udp_receiver" / "icmp_ping.log"
            ping.write_text("[1000.0] 64 bytes from 192.0.2.1: icmp_seq=1 ttl=64 time=10.0 ms\n"
                            "[1000.1] 64 bytes from 192.0.2.1: icmp_seq=2 ttl=64 time=12.0 ms\n"
                            "2 packets transmitted, 2 received, 0% packet loss, time 20ms\n")
            summary = derive_reference_run(run, offered_rate="60M", duration_s=10.0, server_ip="192.0.2.1")
            self.assertEqual(summary["udp_sent_count"], 3)
            self.assertEqual(summary["udp_received_count"], 2)
            self.assertEqual(summary["udp_loss_pct"], 100 / 3)
            self.assertEqual(summary["latency_mode"], "relative_per_rate_p1_subtracted")
            self.assertEqual(summary["ping_loss_pct"], 0.0)
            self.assertEqual(summary["mean_rtt_ms"], 11.0)
            self.assertEqual(summary["receiver_orphan_sequences"], 1)
            self.assertFalse(any(reason.startswith("ping_actual_pps=") for reason in summary["invalid_reasons"]))
            self.assertTrue((run / "derived" / "iperf_flow_latency_packets.tsv.gz").is_file())

            # The sweep accepts any actual ping rate above 80 pps, but rejects the boundary itself.
            ping.write_text(ping.read_text().replace("time 20ms", "time 25ms"))
            boundary = derive_reference_run(run, offered_rate="60M", duration_s=10.0, server_ip="192.0.2.1")
            self.assertIn("ping_actual_pps=80.0", boundary["invalid_reasons"])

    def test_ping_parser_aggregates_workers_and_deduplicates_replies(self) -> None:
        from starlive_measure.trace.TraceDataProcess import parse_ping_log

        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            roles = run / "roles" / "udp_receiver"
            roles.mkdir(parents=True)
            ping = roles / "icmp_ping.log"
            ping.write_text(
                "PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n"
                "[1000.0] 64 bytes from 192.0.2.1: icmp_seq=1 ttl=64 time=10.0 ms\n"
                "[1000.1] 64 bytes from 192.0.2.1: icmp_seq=2 ttl=64 time=11.0 ms\n"
                "[1000.2] 64 bytes from 192.0.2.1: icmp_seq=2 ttl=64 time=11.1 ms\n"
                "3 packets transmitted, 2 received, +1 duplicates, 0% packet loss, time 1000ms\n"
                "PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.\n"
                "[1000.0] 64 bytes from 192.0.2.1: icmp_seq=1 ttl=64 time=12.0 ms\n"
                "[1000.2] 64 bytes from 192.0.2.1: icmp_seq=3 ttl=64 time=13.0 ms\n"
                "3 packets transmitted, 2 received, 33% packet loss, time 990ms\n"
            )
            rows, stats = parse_ping_log(ping, run, {"run_id": "r", "server": "192.0.2.1", "udp_rate": "60M"})
            self.assertEqual((stats["transmitted"], stats["received"]), (6, 4))
            self.assertAlmostEqual(stats["loss_pct"], 100 / 3)
            self.assertEqual(stats["sample_count"], 4)
            self.assertEqual(stats["elapsed_s"], 1.0)

    def test_failed_transfer_retries_three_times_and_publishes_no_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = b"\x00" * 12 + b"\x08\x00" + udp_ipv4(b"a" * 16)
            write_pcap(root / "capture.pcap", [(1, 0, frame, len(frame))], linktype=1)
            manifest = self.extract(root / "capture.pcap", root / "remote" / "packet_ledger", count=1)
            attempts = []

            def disconnected(command, **kwargs):
                if command[4].endswith(".gz"):
                    attempts.append(command[4])
                    raise subprocess.TimeoutExpired(command, 120)
                shutil.copyfile(root / "remote" / "packet_ledger" / "ledger_manifest.json", command[5])
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(packet_ledger.subprocess, "run", side_effect=disconnected):
                with self.assertRaises(packet_ledger.LedgerError):
                    packet_ledger.transfer_from_phone("new-port", "/phone", root / "local")
            self.assertEqual(len(attempts), 3)
            self.assertFalse((root / "local" / manifest["chunks"][0]["name"]).exists())

    def test_extraction_barrier_and_failed_derivation_preserve_pcap(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("traceroot", ROOT / "measurement" / "scripts" / "traceroot.py")
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            manifest = {"lifecycle": {"workload_exit_code": 0, "phone_capture_exit_code": 0, "server_helper_exit_code": None},
                        "phone_transfer": {"remote_dir": "/phone", "local_dir": "roles/udp_sender/packet_ledger", "extraction": "pending"},
                        "root_execution": {"role": "udp_sender"}, "clock_sync": {}, "artifacts": {}}
            profile = {"server": {"port": 5201}, "workload": {"kind": "iperf3", "direction": "uplink", "duration_s": 1}}
            with mock.patch.object(runner, "extract_on_phone") as extract:
                with self.assertRaises(packet_ledger.LedgerError):
                    runner.finish_ledger(run, manifest, profile, "new-port")
                extract.assert_not_called()
            manifest["lifecycle"]["server_helper_exit_code"] = 0
            (run / "roles" / "udp_sender").mkdir(parents=True)
            with mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                 mock.patch.object(runner, "run_root_checked", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                 mock.patch.object(runner, "extract_on_phone"), \
                 mock.patch.object(runner, "transfer_from_phone", return_value=({"chunks": [{}], "pcap_sha256": "0" * 64, "ledger_sha256": "1" * 64, "pcap_size_bytes": 100,
                                   "port": 5201, "direction": "destination", "tcpdump": {"packets_captured": 1, "packets_received_by_filter": 1, "packets_dropped_by_kernel": 0}}, 1)), \
                 mock.patch.object(runner, "read_tcpdump_stats", return_value={"packets_captured": 1, "packets_received_by_filter": 1, "packets_dropped_by_kernel": 0}), \
                 mock.patch.object(runner, "process_ledger", side_effect=packet_ledger.LedgerError("bad ledger")), \
                 mock.patch.object(runner, "delete_phone_pcap") as cleanup:
                result = runner.finish_ledger(run, manifest, profile, "new-port")
                self.assertEqual(result, 1)
                self.assertEqual(manifest["status"], "recoverable")
                cleanup.assert_not_called()

    def test_chunk_boundary_keeps_records_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            count = (packet_ledger.CHUNK_LIMIT - packet_ledger.HEADER.size) // packet_ledger.RECORD.size + 1
            with (root / "capture.pcap").open("wb") as handle:
                handle.write(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 256, 276))
                record = struct.pack("<IIII", 1, 0, 1, 1) + b"x"
                for _ in range(count):
                    handle.write(record)
            manifest = self.extract(root / "capture.pcap", root / "ledger", count=count)
            self.assertEqual(len(manifest["chunks"]), 2)
            self.assertLessEqual(manifest["chunks"][0]["uncompressed_bytes"], packet_ledger.CHUNK_LIMIT)
            self.assertEqual(sum(1 for _ in packet_ledger._records(root / "ledger", manifest)), count)

    def test_recover_new_adb_port_retries_cleanup_without_traffic(self) -> None:
        import argparse
        import importlib.util

        spec = importlib.util.spec_from_file_location("traceroot_recover", ROOT / "measurement" / "scripts" / "traceroot.py")
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            role = run / "roles" / "udp_sender"
            role.mkdir(parents=True)
            profile = json.loads((ROOT / "traces" / "profiles" / "root" / "root_iperf_uplink.json").read_text())
            profile["workload"]["duration_s"] = 0.01
            (run / "profile.json").write_text(json.dumps(profile))
            frame = b"\x00" * 12 + b"\x08\x00" + udp_ipv4(b"a" * 16)
            write_pcap(root / "capture.pcap", [(1, 0, frame, len(frame))], linktype=1)
            self.extract(root / "capture.pcap", role / "packet_ledger", count=1)
            (role / "tcpdump.stderr").write_text("1 packets captured\n1 packets received by filter\n0 packets dropped by kernel\n")
            summary = packet_ledger.process_ledger(role / "packet_ledger", run, duration_ms=10)
            manifest = {"root_execution": {"role": "udp_sender"}, "roles": {"udp_sender": "old-port"}, "server": {},
                        "clock_sync": {}, "root_transport": summary, "artifacts": {"server_pull_rc": 0, "server_pcap_postprocess_rc": 0},
                        "lifecycle": {"workload_exit_code": 0, "phone_capture_exit_code": 0, "server_helper_exit_code": 0},
                        "phone_transfer": {"remote_dir": "/phone", "local_dir": "roles/udp_sender/packet_ledger", "extraction": "complete",
                                           "transfer": "complete", "verification": "complete", "derivation": "complete", "pcap_cleanup": "failed"}}
            (run / "manifest.json").write_text(json.dumps(manifest))
            args = argparse.Namespace(run_dir=run, role=["udp_sender=new-port"], server_ssh=None, server_repo=None)
            with mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
                 mock.patch.object(runner, "run_root_checked", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                 mock.patch.object(runner, "delete_phone_pcap") as cleanup, \
                 mock.patch.object(runner, "transfer_from_phone") as transfer, \
                 mock.patch.object(runner, "extract_on_phone") as extract, \
                 mock.patch.object(runner, "start_process") as traffic:
                self.assertEqual(runner.recover_run(args), 0)
                cleanup.assert_called_once_with("new-port", "/phone")
                transfer.assert_not_called()
                extract.assert_not_called()
                traffic.assert_not_called()
            final = json.loads((run / "manifest.json").read_text())
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["phone_transfer"]["pcap_cleanup"], "complete")
            self.assertEqual(final["roles"]["udp_sender"], "old-port")
            self.assertEqual(final["recovery"]["serial"], "new-port")


if __name__ == "__main__":
    unittest.main()
