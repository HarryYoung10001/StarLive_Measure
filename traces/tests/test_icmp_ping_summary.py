#!/usr/bin/env python3
"""Tests for strict Android ping summary validation."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
SCRIPT = REPO_DIR / "traces" / "scripts" / "ICMPPingSummaryProcess.py"
from starlive_measure.trace import ICMPPingSummaryProcess as ping_summary


class ICMPPingSummaryTest(unittest.TestCase):
    def test_parses_android_summary(self) -> None:
        transmitted, received, loss = ping_summary.parse_ping_summary(
            "750 packets transmitted, 731 received, 2% packet loss, time 150446ms\n"
        )
        self.assertEqual((transmitted, received, loss), (750, 731, Decimal("2")))

    def test_parses_packets_received_variant(self) -> None:
        transmitted, received, loss = ping_summary.parse_ping_summary(
            "50 packets transmitted, 49 packets received, 2.0% packet loss\n"
        )
        self.assertEqual((transmitted, received, loss), (50, 49, Decimal("2.0")))

    def run_cli(self, text: str, expected: int) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "ping.log"
            output = Path(temp_dir) / "ping_summary.txt"
            source.write_text(text, encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.ICMPPingSummaryProcess",
                    str(source),
                    "--expected-transmitted",
                    str(expected),
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            result.summary = output.read_text(encoding="utf-8") if output.exists() else ""
            return result

    def test_cli_writes_summary(self) -> None:
        result = self.run_cli(
            "50 packets transmitted, 47 received, 6% packet loss, time 10000ms\n", 50
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.summary,
            "icmp_transmitted_packets=50\n"
            "icmp_received_packets=47\n"
            "icmp_packet_loss_percent=6\n",
        )

    def test_cli_rejects_count_mismatch_and_zero_replies(self) -> None:
        mismatch = self.run_cli(
            "49 packets transmitted, 49 received, 0% packet loss\n", 50
        )
        self.assertEqual(mismatch.returncode, 1)
        zero = self.run_cli(
            "50 packets transmitted, 0 received, 100% packet loss\n", 50
        )
        self.assertEqual(zero.returncode, 1)

    def test_missing_summary_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ping_summary.parse_ping_summary("ping: network unreachable\n")


if __name__ == "__main__":
    unittest.main()
