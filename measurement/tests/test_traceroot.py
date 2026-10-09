from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from starlive_measure.root.runtime import (
    RootProfileError,
    build_su_command,
    build_su_script_command,
    decode_iperf3_udp_header,
    estimate_clock_offset,
    load_root_profile,
    match_transport_packets,
    process_root_pcap,
    run_root_checked,
)


REPO = Path(__file__).resolve().parents[2]
ROOT_PROFILES = REPO / "traces" / "profiles" / "root"
RUNNER = REPO / "measurement" / "scripts" / "traceroot.py"


def ipv4_udp(payload: bytes, port: int = 5201) -> bytes:
    import struct

    ip_header = bytearray(20)
    ip_header[0] = 0x45
    ip_header[2:4] = struct.pack("!H", 20 + 8 + len(payload))
    ip_header[8] = 64
    ip_header[9] = 17
    ip_header[12:16] = bytes((192, 0, 2, 1))
    ip_header[16:20] = bytes((192, 0, 2, 2))
    udp = struct.pack("!HHHH", 40000, port, 8 + len(payload), 0)
    return bytes(ip_header) + udp + payload


def write_pcap(path: Path) -> None:
    import struct

    frame1 = b"\x00" * 12 + b"\x08\x00" + ipv4_udp(b"a" * 40)
    frame2 = b"\x00" * 12 + b"\x08\x00" + ipv4_udp(b"b" * 60)
    with path.open("wb") as handle:
        handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 2048, 1))
        for sec, frame in ((1, frame1), (1, frame2)):
            handle.write(struct.pack("<IIII", sec, 0, len(frame), len(frame)))
            handle.write(frame)


class TraceRootTest(unittest.TestCase):
    def test_checked_root_reads_inner_exit_even_when_adb_returns_zero(self) -> None:
        from unittest import mock

        with mock.patch("starlive_measure.root.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "__STARLIVE_ROOT_EXIT__=7\n", "")):
            self.assertEqual(run_root_checked("phone", ["false"]).returncode, 7)
        with mock.patch("starlive_measure.root.runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            self.assertEqual(run_root_checked("phone", ["false"]).returncode, 255)

    def test_root_profiles_use_v3_for_phone_capture(self) -> None:
        profiles = sorted(ROOT_PROFILES.glob("*.json"))
        self.assertEqual(len(profiles), 8)
        for path in profiles:
            with self.subTest(path=path.name):
                profile = load_root_profile(path)
                self.assertEqual(profile["schema_version"], 3 if profile["collection"]["phone_capture"] else 2)
                self.assertTrue(profile["name"].startswith("root_"))
                self.assertEqual(profile["collection"]["root_executor"], "su")

    def test_root_profile_rejects_non_root_name(self) -> None:
        profile = json.loads((ROOT_PROFILES / "root_iperf_uplink.json").read_text())
        profile["name"] = "iperf_uplink"
        with self.assertRaises(RootProfileError):
            from starlive_measure.root.runtime import validate_root_profile

            validate_root_profile(profile)

    def test_su_command_quotes_arguments(self) -> None:
        command = build_su_command("phone 1", ["tcpdump", "-i", "wlan0", "udp port 5201", "--label", "a'b"])
        self.assertEqual(command[:5], ["adb", "-s", "phone 1", "shell", "su"])
        self.assertIn("tcpdump", command[-1])
        self.assertIn("'udp port 5201'", command[-1])
        self.assertIn("a'\"'\"'b", command[-1])

    def test_su_script_is_not_nested_under_an_extra_sh(self) -> None:
        command = build_su_script_command("phone 1", "mkdir -p '/data/local/tmp/a b'; echo ready")
        self.assertEqual(command[:5], ["adb", "-s", "phone 1", "shell", "su"])
        self.assertNotIn("sh -c", command[-1])
        self.assertIn("mkdir -p", command[-1])

    def test_clock_offset_uses_minimum_rtt_sample(self) -> None:
        result = estimate_clock_offset([
            {"phone_send_ns": 0, "server_receive_ns": 110, "server_send_ns": 120, "phone_receive_ns": 240},
            {"phone_send_ns": 1000, "server_receive_ns": 1110, "server_send_ns": 1120, "phone_receive_ns": 1180},
        ])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["offset_ns"], 25)

    def test_iperf_header_decoder_fails_closed_and_matches_sequences(self) -> None:
        import struct

        payload = struct.pack("!qII", 7, 12, 345)
        self.assertEqual(decode_iperf3_udp_header(payload), {"sequence": 7, "send_time_ns": 12_000_345_000})
        self.assertIsNone(decode_iperf3_udp_header(payload[:8]))
        self.assertIsNone(decode_iperf3_udp_header(struct.pack("!qII", 7, 1, 1_000_000)))
        result = match_transport_packets(
            [{"sequence": 1}, {"sequence": 2}, {"sequence": 2}],
            [{"sequence": 2}, {"sequence": 3}],
        )
        self.assertEqual(result["sent_packets"], 2)
        self.assertEqual(result["delivered_packets"], 1)
        self.assertEqual(result["missing_sequences"], [1])
        self.assertEqual(result["loss_rate"], 0.5)

    def test_root_pcap_writes_common_derived_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / "root_capture.pcap"
            run = root / "run"
            pcap.parent.mkdir(parents=True, exist_ok=True)
            write_pcap(pcap)
            summary = process_root_pcap(pcap, run, port=5201, duration_ms=1000, payload_bytes=40, source="phone_pcap")
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["latency_status"], "unavailable")
            for name in (
                "udp_arrival_time_precise.tsv", "udp_latency_precise.tsv", "delay_precise.tsv",
                "loss_100ms.tsv", "rtt_10ms.tsv", "bandwidth_100ms.tsv", "loss_summary.json",
                "root_transport_summary.json", "pcap_packets.tsv",
            ):
                self.assertTrue((run / "derived" / name).is_file(), name)

    def test_downlink_pcap_matches_server_source_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pcap = root / "root_capture.pcap"
            run = root / "run"
            import struct

            frame = b"\x00" * 12 + b"\x08\x00" + ipv4_udp(b"z" * 32, port=40000)
            # Rewrite the UDP source port to the server port and destination
            # to a phone ephemeral port.
            frame = frame[:14 + 20] + struct.pack("!H", 5201) + struct.pack("!H", 40000) + frame[14 + 20 + 4:]
            with pcap.open("wb") as handle:
                handle.write(struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 2048, 1))
                handle.write(struct.pack("<IIII", 1, 0, len(frame), len(frame)))
                handle.write(frame)
            summary = process_root_pcap(pcap, run, port=5201, duration_ms=1000, payload_bytes=32, source="phone_pcap", direction="downlink")
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["port_direction"], "source")
            self.assertEqual(summary["packet_count"], 1)

    def test_cli_dry_run_records_root_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                [sys.executable, str(RUNNER), "run", "--profile", str(ROOT_PROFILES / "root_iperf_uplink.json"),
                 "--server-ip", "192.0.2.1", "--server-ssh", "example", "--session-dir", temp,
                 "--role", "udp_sender=phone", "--dry-run"],
                cwd=REPO, capture_output=True, text=True, check=False,
                env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(next(Path(temp).glob("*/manifest.json")).read_text())
            self.assertEqual(manifest["collection_mode"], "root")
            self.assertEqual(manifest["root_execution"]["executor"], "su")
            self.assertEqual(manifest["status"], "planned")
            self.assertTrue(any(command["argv"][0] == "adb" for command in manifest["commands"]))

    def test_v3_runner_waits_for_both_captures_before_ledger(self) -> None:
        import argparse
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("traceroot_order", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        profile = load_root_profile(ROOT_PROFILES / "root_iperf_uplink.json")
        profile["workload"]["duration_s"] = 0.01
        args = argparse.Namespace(server_ip="192.0.2.1", server_ssh="example", server_repo="/tmp/remote",
                                  phone_tcpdump="/data/local/tmp/tcpdump", phone_iperf3="/data/local/tmp/iperf3", dry_run=False,
                                  icmp_workers=1, icmp_pps=100.0, server_ledger=False)
        order = []

        class Process:
            def __init__(self, kind):
                self.kind = kind

            def wait(self, timeout=None):
                order.append(self.kind + "_exited")
                return 0

        def start(_command, stdout, _stderr):
            kind = "server" if "ssh.stdout" in str(stdout) else "ping" if "icmp_ping.log" in str(stdout) else "workload"
            order.append(kind + "_started")
            return Process(kind)

        def stop_capture(*_args, **_kwargs):
            self.assertIn("workload_exited", order)
            self.assertIn("ping_exited", order)
            order.append("capture_exited")
            return 0

        def finish(*_args):
            self.assertIn("server_exited", order)
            self.assertLess(order.index("workload_exited"), order.index("capture_exited"))
            self.assertLess(order.index("ping_started"), order.index("workload_started"))
            self.assertLess(order.index("ping_exited"), order.index("capture_exited"))
            self.assertLess(order.index("capture_exited"), order.index("server_exited"))
            order.append("ledger_started")
            return 0

        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(runner.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "0", "")), \
             mock.patch.object(runner, "collect_clock_sync", return_value={"status": "ok"}), \
             mock.patch.object(runner, "install_extractor"), \
             mock.patch.object(runner, "wait_remote_ready", return_value=True), \
             mock.patch.object(runner, "start_process", side_effect=start), \
             mock.patch.object(runner, "start_phone_capture", return_value=(Process("capture"), "/phone", "/phone/root_capture.pcap")), \
             mock.patch.object(runner, "stop_phone_capture", side_effect=stop_capture), \
             mock.patch.object(runner, "pull_server_artifacts", return_value=0), \
             mock.patch.object(runner, "finish_ledger", side_effect=finish), \
             mock.patch.object(runner.time, "sleep"):
            fake_binary = Path(temp) / "binary"
            fake_binary.write_bytes(b"test extractor")
            with mock.patch("starlive_measure.trace.trace_runtime.android_binary", return_value=fake_binary):
                code, _run = runner.run_once(args, profile, {"udp_sender": "phone"}, Path(temp))
        self.assertEqual(code, 0)
        self.assertEqual(order[-1], "ledger_started")

    def test_v3_server_pull_keeps_large_arrival_text_remote(self) -> None:
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location("traceroot_artifacts", RUNNER)
        runner = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(runner)
        profile = load_root_profile(ROOT_PROFILES / "root_iperf_downlink.json")
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            remote = run / "roles/server/remote"
            remote.mkdir(parents=True)
            (remote / "helper.exit_code").write_text("0\n")
            (remote / "exit_code.txt").write_text("iperf3=0\ntcpdump=0\npcap_postprocess=0\n")
            (remote / "pcap_postprocess.json").write_text(json.dumps({"status": "complete", "exit_code": 0}))
            manifest = {"server": {"ssh": "example", "run_dir": "/remote"}, "artifacts": {}}
            with mock.patch.object(runner, "scp_remote_files", return_value=0) as pull:
                self.assertEqual(runner.pull_server_artifacts(run, manifest, profile), 0)
            names = pull.call_args.args[2]
            self.assertIn("derived/pcap_bandwidth_summary.json", names)
            self.assertNotIn("derived", names)
            self.assertNotIn("derived/udp_arrival_time_ms.txt", names)


if __name__ == "__main__":
    unittest.main()
