#!/usr/bin/env python3
"""Regression tests for dual-phone trace extraction."""

from __future__ import annotations

import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
from starlive_measure.trace import TraceDataProcess as processor
from starlive_measure.trace import TraceVisual as visual
from starlive_measure.trace.trace_discovery import dual_trace_run_dirs


def interval(start: float, end: float, bps: float, sender: bool, *, omitted: bool = False) -> dict[str, object]:
    seconds = end - start
    return {
        "sum": {
            "start": start,
            "end": end,
            "seconds": seconds,
            "bits_per_second": bps,
            "bytes": int(bps * seconds / 8),
            "lost_packets": 1,
            "packets": 100,
            "lost_percent": 1.0,
            "omitted": omitted,
            "sender": sender,
        }
    }


class TraceProcessingTest(unittest.TestCase):
    def make_trace(self, root: Path, *, direction: str = "downlink") -> Path:
        run_dir = root / "dual_icmp_heavy_udp_20260805T120000Z_10M_3s"
        (run_dir / "icmp_phone" / "remote").mkdir(parents=True)
        (run_dir / "heavy_udp_phone" / "remote").mkdir(parents=True)
        (run_dir / "server").mkdir()
        (run_dir / "metadata.txt").write_text(
            "\n".join((
                f"run_id={run_dir.name}",
                "server=203.0.113.1",
                f"udp_direction={direction}",
                "udp_rate=10M",
                "duration_s=3",
                "ping_count=3",
                "",
            )),
            encoding="utf-8",
        )
        (run_dir / "icmp_phone" / "remote" / "ping.log").write_text(
            "\n".join((
                "PING 203.0.113.1 (203.0.113.1) 56(84) bytes of data.",
                "[100.000000] 64 bytes from 203.0.113.1: icmp_seq=1 ttl=42 time=10 ms",
                "[102.000000] 64 bytes from 203.0.113.1: icmp_seq=3 ttl=42 time=30 ms",
                "3 packets transmitted, 2 received, 33% packet loss, time 2000ms",
                "",
            )),
            encoding="utf-8",
        )
        (run_dir / "heavy_udp_phone" / "remote" / "iperf3_client.json").write_text(
            json.dumps({"intervals": [
                interval(0.0, 1.0, 8_000_000.0, direction == "uplink"),
                interval(1.0, 2.0, 16_000_000.0, direction == "uplink", omitted=True),
                interval(2.0, 3.0, 10_000_000.0, True),
            ]}),
            encoding="utf-8",
        )
        (run_dir / "server" / "iperf3_server.json").write_text(
            json.dumps({"intervals": [interval(0.0, 1.0, 4_000_000.0, False)]}),
            encoding="utf-8",
        )
        return run_dir

    def test_downlink_uses_phone_receiver_and_computes_rtt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            run_dir = self.make_trace(logs_dir)
            summary, complete = processor.process_session(run_dir)

            self.assertTrue(complete)
            self.assertEqual(summary["rtt_sample_count"], 2)
            self.assertEqual(summary["ping_transmitted"], 3)
            self.assertEqual(summary["ping_received"], 2)
            self.assertEqual(summary["mean_rtt_ms"], "20.000000")
            self.assertEqual(summary["p99_rtt_ms"], "30.000000")
            self.assertEqual(summary["ping_loss_pct"], "33.333333")
            self.assertEqual(summary["capacity_source"], "heavy_udp_phone/remote/iperf3_client.json")
            self.assertEqual(summary["mean_capacity_mbps"], "8.000000")
            self.assertEqual(summary["weighted_capacity_mbps"], "8.000000")

            with (run_dir / "rtt_timeseries.csv").open(newline="", encoding="utf-8") as handle:
                rtt_rows = list(csv.DictReader(handle))
            self.assertEqual(rtt_rows[1]["seq_gap"], "2")
            self.assertEqual(rtt_rows[1]["lost_before"], "1")

    def test_uplink_uses_server_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self.make_trace(Path(temp_dir), direction="uplink")
            summary, complete = processor.process_session(run_dir)

            self.assertTrue(complete)
            self.assertEqual(summary["capacity_source"], "server/iperf3_server.json")
            self.assertEqual(summary["weighted_capacity_mbps"], "4.000000")

    def test_uplink_without_server_receiver_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self.make_trace(Path(temp_dir), direction="uplink")
            (run_dir / "server" / "iperf3_server.json").write_text(
                json.dumps({"intervals": [interval(0.0, 1.0, 4_000_000.0, True)]}),
                encoding="utf-8",
            )
            summary, complete = processor.process_session(run_dir)

            self.assertFalse(complete)
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["capacity_source"], "")

    def test_discovery_skips_latest_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            run_dir = self.make_trace(logs_dir)
            latest = logs_dir / "latest_dual_icmp_heavy_udp"
            try:
                latest.symlink_to(run_dir, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")

            self.assertEqual(dual_trace_run_dirs(logs_dir), [run_dir])

    def test_invalid_capacity_json_is_partial_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = self.make_trace(Path(temp_dir))
            (run_dir / "heavy_udp_phone" / "remote" / "iperf3_client.json").write_text(
                "not JSON", encoding="utf-8"
            )
            (run_dir / "server" / "iperf3_server.json").write_text("not JSON", encoding="utf-8")
            summary, complete = processor.process_session(run_dir)

            self.assertFalse(complete)
            self.assertEqual(summary["status"], "partial")
            with (run_dir / "capacity_timeseries.csv").open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_complete_only_selects_complete_trace_rows(self) -> None:
        rows = [
            {"run_id": "complete", "status": "complete"},
            {"run_id": "partial", "status": "partial"},
            {"run_id": "failed", "status": "failed"},
        ]
        self.assertEqual(visual.select_summary_rows(rows, complete_only=False), rows)
        self.assertEqual(
            visual.select_summary_rows(rows, complete_only=True),
            [{"run_id": "complete", "status": "complete"}],
        )

    def test_aggregate_rows_prefer_latest_complete_attempt_per_rate(self) -> None:
        rows = [
            {"run_id": "failed-old", "trace_dir": "dual_20240101", "offered_rate": "370M", "offered_rate_mbps": "370", "status": "failed"},
            {"run_id": "complete-new", "trace_dir": "dual_20240102", "offered_rate": "370M", "offered_rate_mbps": "370", "status": "complete"},
            {"run_id": "complete-100", "trace_dir": "dual_20240103", "offered_rate": "100M", "offered_rate_mbps": "100", "status": "complete"},
        ]
        selected = visual.dedupe_summary_rows(rows)
        self.assertEqual(
            {(row["offered_rate"], row["run_id"]) for row in selected},
            {("370M", "complete-new"), ("100M", "complete-100")},
        )

    def test_custom_udp_latency_pairs_timestamped_sender_and_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            run_dir = logs_dir / "dual_udp_latency_downlink_20260805T120000Z_10M_3s"
            (run_dir / "icmp_phone" / "remote").mkdir(parents=True)
            (run_dir / "udp_phone").mkdir()
            (run_dir / "server").mkdir()
            (run_dir / "metadata.txt").write_text(
                "\n".join((
                    f"run_id={run_dir.name}",
                    "type=dual_udp_latency_downlink",
                    "server=203.0.113.1",
                    "udp_direction=downlink",
                    "udp_rate=10M",
                    "udp_payload_len=1200",
                    "duration_s=3",
                    "ping_count=1",
                    "",
                )),
                encoding="utf-8",
            )
            (run_dir / "icmp_phone" / "remote" / "ping.log").write_text(
                "[100.000000] 64 bytes from 203.0.113.1: icmp_seq=1 ttl=42 time=10 ms\n"
                "1 packets transmitted, 1 received, 0% packet loss\n",
                encoding="utf-8",
            )
            sender_rows = [
                {
                    "run_id": run_dir.name, "target_phone": "203.0.113.1",
                    "target_endpoint": "203.0.113.1:5201", "port": "5201",
                    "rate": "10M", "pps": "10", "payload_bytes": "1200",
                    "seq": "1", "scheduled_send_rel_s": "0.0", "send_rel_s": "0.0",
                    "send_unix_ns": "1000000000000", "send_epoch_s": "1000.0", "send_lag_ms": "0",
                },
                {
                    "run_id": run_dir.name, "target_phone": "203.0.113.1",
                    "target_endpoint": "203.0.113.1:5201", "port": "5201",
                    "rate": "10M", "pps": "10", "payload_bytes": "1200",
                    "seq": "2", "scheduled_send_rel_s": "0.1", "send_rel_s": "0.1",
                    "send_unix_ns": "1100000000000", "send_epoch_s": "1100.0", "send_lag_ms": "0",
                },
                {
                    "run_id": run_dir.name, "target_phone": "203.0.113.1",
                    "target_endpoint": "203.0.113.1:5201", "port": "5201",
                    "rate": "10M", "pps": "10", "payload_bytes": "1200",
                    "seq": "3", "scheduled_send_rel_s": "0.2", "send_rel_s": "0.2",
                    "send_unix_ns": "1200000000000", "send_epoch_s": "1200.0", "send_lag_ms": "0",
                },
            ]
            with gzip.open(run_dir / "server" / "downlink_udp_sender.csv.gz", "wt", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=sender_rows[0].keys())
                writer.writeheader()
                writer.writerows(sender_rows)
            receiver_fields = [
                "run_id", "port", "seq", "scheduled_send_rel_s", "send_rel_s", "recv_rel_s",
                "send_unix_ns", "send_epoch_s", "recv_unix_ns", "recv_epoch_s",
                "payload_bytes", "duplicate", "remote_addr",
            ]
            receiver_rows = [
                [run_dir.name, "5201", "1", "0.0", "0.0", "0.005", "1000000000000", "1000.0", "1000005000000", "1000.005", "1200", "false", "203.0.113.1:5201"],
                [run_dir.name, "5201", "2", "0.1", "0.1", "0.110", "1100000000000", "1100.0", "1100010000000", "1100.010", "1200", "false", "203.0.113.1:5201"],
            ]
            with (run_dir / "udp_phone" / "downlink_udp_receiver.csv").open("w", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(receiver_fields)
                writer.writerows(receiver_rows)

            self.assertEqual(dual_trace_run_dirs(logs_dir), [run_dir])
            summary, complete = processor.process_session(run_dir)
            self.assertTrue(complete)
            self.assertEqual(summary["udp_sent_count"], 3)
            self.assertEqual(summary["udp_received_count"], 2)
            self.assertEqual(summary["udp_loss_pct"], "33.333333")
            self.assertEqual(summary["udp_flow_latency_sample_count"], 2)
            self.assertEqual(summary["udp_flow_latency_mean_ms"], "7.500000")
            self.assertEqual(summary["udp_flow_latency_p75_ms"], "10.000000")
            self.assertEqual(summary["capacity_interval_count"], 2)
            self.assertEqual(summary["mean_capacity_mbps"], "0.960000")
            self.assertEqual(summary["capacity_source"], "udp_phone/downlink_udp_receiver.csv")
            self.assertEqual(summary["udp_arrival_span_s"], "0.105000")
            self.assertEqual(summary["udp_arrival_interval_mean_ms"], "105.000000")
            with (run_dir / "udp_flow_latency.csv").open(newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["one_way_delay_ms"], "5.000000")
            self.assertEqual(rows[1]["one_way_delay_ms"], "10.000000")
            self.assertEqual(rows[2]["lost"], "true")
            with (run_dir / "capacity_timeseries.csv").open(newline="") as handle:
                capacity_rows = list(csv.DictReader(handle))
            self.assertEqual(len(capacity_rows), 2)
            self.assertTrue(all(row["seconds"] == "0.01" for row in capacity_rows))
            self.assertTrue(all(row["capacity_mbps"] == "0.96" for row in capacity_rows))

    def test_custom_udp_latency_applies_clock_offset_and_preserves_raw_delay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            run_dir = logs_dir / "dual_udp_latency_downlink_20260805T120000Z_10M_3s"
            # Build the same minimal fixture, then add the receiver's clock-sync result.
            (run_dir / "icmp_phone" / "remote").mkdir(parents=True)
            (run_dir / "udp_phone").mkdir()
            (run_dir / "server").mkdir()
            (run_dir / "metadata.txt").write_text(
                "\n".join((
                    f"run_id={run_dir.name}", "type=dual_udp_latency_downlink",
                    "server=203.0.113.1", "udp_direction=downlink", "udp_rate=10M",
                    "duration_s=1", "ping_count=1", "",
                )), encoding="utf-8"
            )
            (run_dir / "icmp_phone" / "remote" / "ping.log").write_text(
                "[100.000000] 64 bytes from 203.0.113.1: icmp_seq=1 ttl=42 time=10 ms\n"
                "1 packets transmitted, 1 received, 0% packet loss\n", encoding="utf-8"
            )
            with gzip.open(run_dir / "server" / "downlink_udp_sender.csv.gz", "wt", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["seq", "send_rel_s", "send_unix_ns", "payload_bytes"])
                writer.writeheader()
                writer.writerow({"seq": "1", "send_rel_s": "0", "send_unix_ns": "1000000000000", "payload_bytes": "1200"})
            with (run_dir / "udp_phone" / "downlink_udp_receiver.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["seq", "recv_rel_s", "recv_unix_ns", "payload_bytes", "duplicate"])
                writer.writeheader()
                writer.writerow({"seq": "1", "recv_rel_s": "0.005", "recv_unix_ns": "1000005000000", "payload_bytes": "1200", "duplicate": "false"})
            (run_dir / "udp_phone" / "downlink_udp_receiver_summary.txt").write_text(
                "clock_sync_samples=5\nclock_offset_ms=270.000000\nclock_rtt_ms=2.000000\n", encoding="utf-8"
            )

            summary, complete = processor.process_session(run_dir)
            self.assertTrue(complete)
            self.assertEqual(summary["udp_flow_latency_mean_ms"], "275.000000")
            self.assertEqual(summary["udp_clock_offset_ms"], "270.000000")
            with (run_dir / "udp_flow_latency.csv").open(newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["raw_one_way_delay_ms"], "5.000000")
            self.assertEqual(row["one_way_delay_ms"], "275.000000")

    def test_custom_udp_latency_without_icmp_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            run_dir = logs_dir / "dual_udp_latency_downlink_20260805T120000Z_10M_3s"
            (run_dir / "udp_phone").mkdir(parents=True)
            (run_dir / "server").mkdir()
            (run_dir / "metadata.txt").write_text(
                "\n".join((
                    f"run_id={run_dir.name}", "type=dual_udp_latency_downlink",
                    "server=203.0.113.1", "udp_direction=downlink", "udp_rate=10M",
                    "udp_payload_len=1200", "duration_s=1", "icmp_enabled=false", "",
                )), encoding="utf-8"
            )
            with gzip.open(run_dir / "server" / "downlink_udp_sender.csv.gz", "wt", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["seq", "send_rel_s", "send_unix_ns", "payload_bytes"])
                writer.writeheader()
                writer.writerow({"seq": "1", "send_rel_s": "0", "send_unix_ns": "1000000000000", "payload_bytes": "1200"})
            with (run_dir / "udp_phone" / "downlink_udp_receiver.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["seq", "recv_rel_s", "recv_unix_ns", "payload_bytes", "duplicate"])
                writer.writeheader()
                writer.writerow({"seq": "1", "recv_rel_s": "0.005", "recv_unix_ns": "1000005000000", "payload_bytes": "1200", "duplicate": "false"})

            summary, complete = processor.process_session(run_dir)

            self.assertTrue(complete)
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["icmp_enabled"], "false")
            self.assertEqual(summary["rtt_sample_count"], 0)
            self.assertEqual(summary["capacity_interval_count"], 1)


if __name__ == "__main__":
    unittest.main()
