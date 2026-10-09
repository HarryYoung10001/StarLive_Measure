#!/usr/bin/env python3
"""Local loopback tests for compact DWLP downlink traces."""

from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
SENDER = REPO_DIR / "runtime" / "server" / "downlink_udp_sender.py"
RECEIVER = REPO_DIR / "runtime" / "android" / "probes" / "downlink_udp_receiver.go"


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


class DownlinkCompactUdpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = tempfile.TemporaryDirectory()
        cls.receiver_bin = Path(cls.build_dir.name) / "downlink_udp_receiver"
        subprocess.run(
            ["go", "build", "-o", str(cls.receiver_bin), str(RECEIVER)],
            cwd=REPO_DIR,
            check=True,
            capture_output=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def run_mode(self, mode: str, receive_api: str) -> Path:
        if receive_api == "recvmmsg" and not sys.platform.startswith("linux"):
            self.skipTest("recvmmsg integration requires Linux")
        temp_context = tempfile.TemporaryDirectory()
        self.addCleanup(temp_context.cleanup)
        temp_dir = Path(temp_context.name)
        server_dir = temp_dir / "server"
        phone_dir = temp_dir / "phone"
        ready = temp_dir / "ready"
        port = free_udp_port()
        sender = subprocess.Popen(
            [
                "python3", str(SENDER), "--port", str(port), "--duration-s", "0.3",
                "--rate", "50pps", "--payload-bytes", "1200", "--run-id", "loopback",
                "--out-dir", str(server_dir), "--no-csv", "--duration-mode",
                "wall-clock" if mode == "arrival" else "packet-count", "--ready-file", str(ready),
                "--progress-interval-s", "0.05",
            ],
            cwd=REPO_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: sender.poll() is None and sender.kill())
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "sender did not create ready file")
        receiver = subprocess.run(
            [
                str(self.receiver_bin), "--server", "127.0.0.1", "--port", str(port),
                "--duration-s", "0.3", "--timeout-s", "0.15", "--wait-timeout-s", "5",
                "--payload-bytes", "1200", "--run-id", "loopback", "--out-dir", str(phone_dir),
                "--output-mode", mode, "--window-ms", "10", "--progress-interval", "50ms",
                "--gap-ms", "5", "--receive-api", receive_api,
            ],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        sender_stdout, sender_stderr = sender.communicate(timeout=10)
        self.assertEqual(receiver.returncode, 0, receiver.stdout + receiver.stderr)
        self.assertEqual(sender.returncode, 0, sender_stdout + sender_stderr)
        self.assertFalse((server_dir / "downlink_udp_sender.csv.gz").exists())
        self.assertFalse((phone_dir / "downlink_udp_receiver.csv").exists())
        receiver_values = read_values(phone_dir / "downlink_udp_receiver_summary.txt")
        self.assertEqual(receiver_values["clock_sync_samples"], "5")
        self.assertEqual(receiver_values["payload_bytes_min"], "1200")
        self.assertEqual(receiver_values["payload_bytes_max"], "1200")
        self.assertEqual(receiver_values["receive_api_effective"], receive_api)
        self.assertEqual(receiver_values["control_truncated_packets"], "0")
        if receive_api == "recvmmsg":
            self.assertGreater(int(receiver_values["header_only_packets"]), 0)
            self.assertEqual(receiver_values["receive_capture_bytes"], "256")
        else:
            self.assertEqual(receiver_values["header_only_packets"], "0")
        self.assertEqual(read_values(server_dir / "downlink_udp_sender_summary.txt")["clock_sync_samples"], "5")
        self.assertGreater(len((phone_dir / "udp_receive_gap_report.tsv").read_text(encoding="utf-8").splitlines()), 1)
        return temp_dir

    def test_arrival_mode_streams_compact_trace(self) -> None:
        temp_dir = self.run_mode("arrival", "recvmmsg")
        phone_dir = temp_dir / "phone"
        values = (phone_dir / "udp_arrival_time_ms.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(values)
        self.assertEqual(values[0], "0")
        self.assertEqual(values, sorted(values, key=int))
        self.assertFalse((phone_dir / "delay.txt").exists())

    def test_latency_mode_streams_window_trace(self) -> None:
        temp_dir = self.run_mode("latency", "recvmmsg")
        phone_dir = temp_dir / "phone"
        delays = (phone_dir / "delay.txt").read_text(encoding="utf-8").splitlines()
        times = (phone_dir / "delay_window_time_ms.txt").read_text(encoding="utf-8").splitlines()
        self.assertTrue(delays)
        self.assertEqual(len(delays), len(times))
        self.assertFalse((phone_dir / "udp_arrival_time_ms.txt").exists())

    def test_both_mode_writes_arrival_latency_and_bandwidth(self) -> None:
        temp_dir = self.run_mode("both", "readmsg")
        phone_dir = temp_dir / "phone"
        for name in (
            "udp_arrival_time_ms.txt",
            "delay.txt",
            "delay_window_time_ms.txt",
            "delay_summary.txt",
            "bandwidth_trace.tsv",
            "custom_udp_trace_summary.json",
        ):
            self.assertTrue((phone_dir / name).is_file(), name)
            self.assertGreater((phone_dir / name).stat().st_size, 0, name)
        delays = (phone_dir / "delay.txt").read_text(encoding="utf-8").splitlines()
        times = (phone_dir / "delay_window_time_ms.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(delays), len(times))
        self.assertFalse((phone_dir / "udp_trace.utrc").exists())

    def test_readmsg_compatibility_preserves_full_copy_path(self) -> None:
        temp_dir = self.run_mode("arrival", "readmsg")
        values = read_values(temp_dir / "phone" / "downlink_udp_receiver_summary.txt")
        self.assertEqual(values["receive_capture_bytes"], "65535")
        self.assertEqual(values["payload_bytes_source"], "wire_length")


if __name__ == "__main__":
    unittest.main()
