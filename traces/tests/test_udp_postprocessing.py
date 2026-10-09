#!/usr/bin/env python3
"""Regression tests for custom UDP text post-processing scripts."""

from __future__ import annotations

import csv
import gzip
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
from starlive_measure.trace import UDPArrivalTimeProcess as arrival
from starlive_measure.trace import UDPLatencyWindowProcess as latency


class UDPPostprocessingTest(unittest.TestCase):
    def write_receiver_csv(self, path: Path) -> None:
        rows = [
            {"seq": "1", "recv_rel_s": "0.0039", "duplicate": "false"},
            {"seq": "2", "recv_rel_s": "0.0031", "duplicate": "false"},
            {"seq": "3", "recv_rel_s": "0.0038", "duplicate": "true"},
            {"seq": "4", "recv_rel_s": "0.0010", "duplicate": "false"},
            {"seq": "5", "recv_rel_s": "invalid", "duplicate": "false"},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["seq", "recv_rel_s", "duplicate"])
            writer.writeheader()
            writer.writerows(rows)

    def write_latency_csv(self, path: Path) -> None:
        rows = [
            {"received": "true", "recv_rel_s": "0.001", "one_way_delay_ms": "7.49"},
            {"received": "true", "recv_rel_s": "0.009", "one_way_delay_ms": "7.51"},
            {"received": "true", "recv_rel_s": "0.010", "one_way_delay_ms": "7.50"},
            {"received": "true", "recv_rel_s": "0.024", "one_way_delay_ms": "8.50"},
            {"received": "false", "recv_rel_s": "0.015", "one_way_delay_ms": "100"},
            {"received": "true", "recv_rel_s": "bad", "one_way_delay_ms": "10"},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["received", "recv_rel_s", "one_way_delay_ms"],
            )
            writer.writeheader()
            writer.writerows(rows)

    def write_raw_receiver_csv(self, path: Path) -> None:
        rows = [
            {
                "recv_rel_s": "0.001",
                "send_unix_ns": "1000000000000",
                "recv_unix_ns": "1000005000000",
            },
            {
                "recv_rel_s": "0.009",
                "send_unix_ns": "1000001000000",
                "recv_unix_ns": "1000008000000",
            },
            {
                "recv_rel_s": "0.024",
                "send_unix_ns": "1000002000000",
                "recv_unix_ns": "1000010500000",
            },
            {"recv_rel_s": "bad", "send_unix_ns": "", "recv_unix_ns": ""},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["recv_rel_s", "send_unix_ns", "recv_unix_ns"],
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_arrivals_floor_sort_and_preserve_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "downlink_udp_receiver.csv"
            self.write_receiver_csv(source)
            values, skipped = arrival.read_arrival_ms(source)
            self.assertEqual(values, [1, 3, 3, 3])
            self.assertEqual(skipped, 1)

    def test_arrivals_support_gzip_and_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            receiver_dir = run_dir / "udp_phone"
            receiver_dir.mkdir(parents=True)
            plain = receiver_dir / "downlink_udp_receiver.csv"
            self.write_receiver_csv(plain)
            compressed = receiver_dir / "downlink_udp_receiver.csv.gz"
            with plain.open("rb") as source, gzip.open(compressed, "wb") as target:
                target.write(source.read())
            plain.unlink()

            found = arrival.find_receiver_csv(run_dir)
            self.assertIsNotNone(found)
            source, output_dir = found
            self.assertEqual(source, compressed)
            self.assertEqual(output_dir, run_dir)
            values, _ = arrival.read_arrival_ms(source)
            self.assertEqual(values, [1, 3, 3, 3])

    def test_latency_windows_skip_empty_windows_and_round_half_up(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "udp_flow_latency.csv"
            self.write_latency_csv(source)
            values, skipped = latency.read_window_values(source, Decimal("10"))
            # [0, 10) => 7.50 -> 8; [10, 20) => 7.50 -> 8; [20, 30) => 8.50 -> 9.
            self.assertEqual(values, [8, 8, 9])
            self.assertEqual(skipped, 2)

    def test_latency_custom_window_and_default_delay_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            source = run_dir / "udp_flow_latency.csv"
            self.write_latency_csv(source)

            values, _ = latency.read_window_values(source, Decimal("5"))
            self.assertEqual(values, [7, 8, 8, 9])
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.UDPLatencyWindowProcess",
                    str(run_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((run_dir / "delay.txt").read_text(encoding="utf-8"), "8\n8\n9\n")

    def test_invalid_window_and_empty_inputs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "udp_flow_latency.csv"
            source.write_text(
                "received,recv_rel_s,one_way_delay_ms\nfalse,,,\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.UDPLatencyWindowProcess",
                    str(source),
                    "--window-ms",
                    "0",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--window-ms", result.stderr)

            values, _ = latency.read_window_values(source, Decimal("10"))
            self.assertEqual(values, [])

    def test_raw_receiver_applies_clock_offset_and_reports_empty_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "uplink_udp_receiver.csv"
            summary = temp_path / "uplink_udp_sender_summary.txt"
            self.write_raw_receiver_csv(source)
            summary.write_text(
                "clock_offset_ms=2.000000\nclock_sync_samples=5\n",
                encoding="utf-8",
            )

            values, skipped, stats = latency.read_receiver_window_values(
                source, Decimal("10"), summary
            )
            self.assertEqual(values, [8, 11])
            self.assertEqual(skipped, 1)
            self.assertEqual(stats["valid_delay_rows"], 3)
            self.assertEqual(stats["delay_windows"], 2)
            self.assertEqual(stats["empty_windows"], 1)
            self.assertEqual(stats["clock_sync_samples"], 5)

            output = temp_path / "delay.txt"
            summary_output = temp_path / "delay_summary.txt"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.UDPLatencyWindowProcess",
                    str(source),
                    "--sender-summary",
                    str(summary),
                    "--output",
                    str(output),
                    "--summary-output",
                    str(summary_output),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "8\n11\n")
            self.assertIn("empty_windows=1", summary_output.read_text(encoding="utf-8"))

    def test_raw_receiver_requires_valid_clock_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "uplink_udp_receiver.csv"
            summary = temp_path / "uplink_udp_sender_summary.txt"
            self.write_raw_receiver_csv(source)
            summary.write_text("clock_offset_ms=1\nclock_sync_samples=0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                latency.read_receiver_window_values(source, Decimal("10"), summary)

    def test_arrival_empty_and_missing_inputs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "downlink_udp_receiver.csv"
            source.write_text("seq,recv_rel_s\n1,invalid\n", encoding="utf-8")
            script = "starlive_measure.trace.UDPArrivalTimeProcess"

            empty_result = subprocess.run(
                [sys.executable, "-m", script, str(source)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(empty_result.returncode, 1)
            self.assertIn("no valid recv_rel_s", empty_result.stderr)

            missing_result = subprocess.run(
                [sys.executable, "-m", script, str(temp_path / "missing.csv")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing_result.returncode, 2)
            self.assertIn("no custom UDP receiver", missing_result.stderr)


if __name__ == "__main__":
    unittest.main()
