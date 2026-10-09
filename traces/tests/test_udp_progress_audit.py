#!/usr/bin/env python3
"""Tests for UDP receive-gap and sender-progress classification."""

from __future__ import annotations

import csv
import gzip
import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
from starlive_measure.trace import UDPProgressAudit as audit
from starlive_measure.trace import MonitorUDPProgress as monitor


class UDPProgressAuditTest(unittest.TestCase):
    def write_receiver_csv(self, path: Path) -> None:
        rows = [
            {"seq": "1", "send_rel_s": "0.0", "recv_unix_ns": "1000000000000"},
            {"seq": "2", "send_rel_s": "0.1", "recv_unix_ns": "1000050000000"},
            {"seq": "100", "send_rel_s": "5.1", "recv_unix_ns": "1005050000000"},
        ]
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def write_progress(self, path: Path, sender: bool, rows: list[list[str]]) -> None:
        if sender:
            fields = [
                "event",
                "timestamp_unix_ns",
                "elapsed_s",
                "scheduled_packets",
                "attempted_packets",
                "sent_packets",
                "send_failed_packets",
                "last_seq",
                "last_send_rel_s",
                "send_lag_ms",
            ]
        else:
            fields = [
                "event",
                "timestamp_unix_ns",
                "elapsed_s",
                "received_packets",
                "last_seq",
                "last_packet_recv_unix_ns",
                "socket_timeout_count",
            ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(fields)
            writer.writerows(rows)

    def classify(self, temp_path: Path, sender_rows: list[list[str]], receiver_rows: list[list[str]]) -> str:
        receiver_csv = temp_path / "receiver.csv.gz"
        gap_report = temp_path / "gaps.tsv"
        sender_progress = temp_path / "sender.tsv"
        receiver_progress = temp_path / "receiver.tsv"
        sender_summary = temp_path / "sender_summary.txt"
        output = temp_path / "audit.tsv"
        self.write_receiver_csv(receiver_csv)
        audit.build_gap_report(receiver_csv, gap_report, gap_ms=100)
        self.write_progress(sender_progress, True, sender_rows)
        self.write_progress(receiver_progress, False, receiver_rows)
        sender_summary.write_text(
            "clock_server_minus_phone_ms=0\nprogress_interval_s=1\n",
            encoding="utf-8",
        )
        audit.classify_gap_report(
            gap_report,
            sender_progress,
            receiver_progress,
            sender_summary,
            output,
        )
        with output.open(encoding="utf-8", newline="") as handle:
            return next(csv.DictReader(handle, delimiter="\t"))["classification"]

    def sender_rows(self, sent_at_end: int) -> list[list[str]]:
        return [
            ["heartbeat", "1000000000000", "1", "100", "10", "10", "0", "10", "1", "0"],
            ["heartbeat", "1002000000000", "3", "300", str(sent_at_end), str(sent_at_end), "0", str(sent_at_end), "3", "0"],
            ["heartbeat", "1006000000000", "7", "700", str(sent_at_end + 1), str(sent_at_end + 1), "0", str(sent_at_end + 1), "7", "0"],
        ]

    def receiver_rows(self) -> list[list[str]]:
        return [
            ["heartbeat", "1000000000000", "1", "10", "10", "1000000000000", "1"],
            ["heartbeat", "1002000000000", "3", "10", "10", "1000000000000", "3"],
            ["heartbeat", "1006000000000", "7", "10", "10", "1000000000000", "7"],
        ]

    def test_build_gap_report_preserves_send_progress(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            report = temp_path / "gaps.tsv"
            self.write_receiver_csv(temp_path / "receiver.csv.gz")
            count = audit.build_gap_report(temp_path / "receiver.csv.gz", report)
            self.assertEqual(count, 1)
            with report.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["seq_jump"], "98")
            self.assertEqual(row["send_advance_s"], "5.000000000")
            self.assertEqual(row["gap_ms"], "5000.000")

    def test_active_sender_and_alive_receiver_is_path_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            classification = self.classify(Path(temp_dir), self.sender_rows(20), self.receiver_rows())
            self.assertEqual(classification, "udp_path_or_receiver_drop")

    def test_flat_sender_counter_is_sender_pause(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sender_rows = self.sender_rows(10)
            sender_rows[-1][4] = "10"
            sender_rows[-1][5] = "10"
            sender_rows[-1][7] = "10"
            classification = self.classify(Path(temp_dir), sender_rows, self.receiver_rows())
            self.assertEqual(classification, "sender_paused")

    def test_missing_receiver_heartbeat_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            receiver_rows = [self.receiver_rows()[0]]
            classification = self.classify(Path(temp_dir), self.sender_rows(20), receiver_rows)
            self.assertEqual(classification, "receiver_heartbeat_missing")

    def test_trailing_gap_is_added_from_sender_final_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            receiver_csv = temp_path / "receiver.csv.gz"
            gap_report = temp_path / "gaps.tsv"
            sender_progress = temp_path / "sender.tsv"
            receiver_progress = temp_path / "receiver.tsv"
            sender_summary = temp_path / "sender_summary.txt"
            output = temp_path / "audit.tsv"
            self.write_receiver_csv(receiver_csv)
            audit.build_gap_report(receiver_csv, gap_report)
            sender_rows = self.sender_rows(20)
            sender_rows[-1][0] = "final"
            self.write_progress(sender_progress, True, sender_rows)
            self.write_progress(receiver_progress, False, self.receiver_rows())
            sender_summary.write_text(
                "clock_server_minus_phone_ms=0\nprogress_interval_s=1\n",
                encoding="utf-8",
            )
            audit.classify_gap_report(
                gap_report,
                sender_progress,
                receiver_progress,
                sender_summary,
                output,
            )
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            trailing = next(row for row in rows if row["gap_index"] == "trailing")
            self.assertEqual(trailing["classification"], "udp_path_or_receiver_drop")
            self.assertEqual(trailing["next_seq"], "")

    def test_missing_sender_telemetry_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            self.write_receiver_csv(temp_path / "receiver.csv.gz")
            gap_report = temp_path / "gaps.tsv"
            audit.build_gap_report(temp_path / "receiver.csv.gz", gap_report)
            (temp_path / "sender.tsv").write_text("event\ttimestamp_unix_ns\n", encoding="utf-8")
            self.write_progress(temp_path / "receiver.tsv", False, self.receiver_rows())
            (temp_path / "sender_summary.txt").write_text(
                "clock_server_minus_phone_ms=0\nprogress_interval_s=1\n", encoding="utf-8"
            )
            output = temp_path / "audit.tsv"
            audit.classify_gap_report(
                gap_report,
                temp_path / "sender.tsv",
                temp_path / "receiver.tsv",
                temp_path / "sender_summary.txt",
                output,
            )
            with output.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["classification"], "telemetry_unavailable")

    def test_downlink_converts_server_progress_into_phone_clock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            receiver_csv = temp_path / "receiver.csv.gz"
            gap_report = temp_path / "gaps.tsv"
            sender_progress = temp_path / "sender.tsv"
            receiver_progress = temp_path / "receiver.tsv"
            clock_summary = temp_path / "receiver_summary.txt"
            output = temp_path / "audit.tsv"
            self.write_receiver_csv(receiver_csv)
            audit.build_gap_report(receiver_csv, gap_report, gap_ms=100)
            sender_rows = self.sender_rows(20)
            for row in sender_rows:
                row[1] = str(int(row[1]) + 100_000_000)
            self.write_progress(sender_progress, True, sender_rows)
            self.write_progress(receiver_progress, False, self.receiver_rows())
            clock_summary.write_text("clock_offset_ms=100\n", encoding="utf-8")
            audit.classify_gap_report(
                gap_report,
                sender_progress,
                receiver_progress,
                clock_summary,
                output,
                direction="downlink",
            )
            with output.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["classification"], "udp_path_or_receiver_drop")

    def test_monitor_distinguishes_command_failure_from_sender_pause(self) -> None:
        progress_line = "\t".join(
            ["heartbeat", "1000000000", "1", "100", "10", "10", "0", "10", "1", "0"]
        )
        status, values, error = monitor._poll(
            [sys.executable, "-c", f"print({progress_line!r})"], "sender", 1
        )
        self.assertEqual(status, "ok")
        self.assertEqual(values["sent_packets"], "10")
        self.assertEqual(error, "")

        status, values, error = monitor._poll(
            [sys.executable, "-c", "raise SystemExit(7)"], "sender", 1
        )
        self.assertEqual(status, "unavailable")
        self.assertEqual(values, {})
        self.assertIn("exit_code=7", error)

    def test_historical_report_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            self.write_receiver_csv(temp_path / "receiver.csv.gz")
            audit.build_gap_report(temp_path / "receiver.csv.gz", temp_path / "gaps.tsv")
            count = audit.mark_report_unavailable(temp_path / "gaps.tsv", temp_path / "audit.tsv")
            self.assertEqual(count, 1)
            with (temp_path / "audit.tsv").open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(row["sender_telemetry"], "unavailable")
            self.assertEqual(row["classification"], "telemetry_unavailable")


if __name__ == "__main__":
    unittest.main()
