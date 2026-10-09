from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from traces.scripts.CompactUDPTrace import convert, records


RECORD = struct.Struct("!QQQQqIHH")


class CompactUDPTraceTest(unittest.TestCase):
    def capture(self, directory: Path, rows: list[tuple[int, ...]]) -> Path:
        path = directory / "trace.utrc"
        header = bytearray(64)
        header[:4] = b"UTRC"
        with path.open("wb") as handle:
            handle.write(header)
            for row in rows:
                handle.write(RECORD.pack(*row))
        return path

    def test_arrival_conversion_clamps_small_clock_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.capture(root, [(1, 0, 0, 0, 1_000_000_000, 0, 1200, 2), (2, 0, 0, 0, 999_900_000, 0, 1200, 2), (3, 0, 0, 0, 1_002_000_000, 0, 1200, 2)])
            convert(path, root, "arrival")
            self.assertEqual((root / "udp_arrival_time_ms.txt").read_text().splitlines(), ["0", "0", "2"])

    def test_latency_conversion_and_socket_drop_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.capture(root, [(1, 0, 0, 1_000_000_000, 1_005_000_000, 0, 1200, 2), (3, 0, 200_000_000, 1_200_000_000, 1_210_000_000, 4, 1200, 2)])
            convert(path, root, "latency", window_ms=100, gap_ms=100)
            self.assertEqual((root / "delay.txt").read_text().splitlines(), ["5", "10"])
            self.assertIn("\t4\n", (root / "udp_receive_gap_report.tsv").read_text())

    def test_both_uses_clock_offset_and_actual_payload_bandwidth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.capture(
                root,
                [
                    (1, 0, 0, 1_000_000_000_000, 1_000_005_000_000, 0, 1200, 2),
                    (2, 0, 1, 1_000_010_000_000, 1_000_020_000_000, 3, 800, 2),
                ],
            )
            sender = root / "uplink_udp_sender_summary.txt"
            sender.write_text("clock_offset_ms=-2.000000\nclock_sync_samples=5\n", encoding="utf-8")
            convert(path, root / "derived", "both", window_ms=10, sender_summary=sender, duration_ms=20)
            derived = root / "derived"
            self.assertEqual((derived / "udp_arrival_time_ms.txt").read_text().splitlines(), ["0", "15"])
            self.assertEqual((derived / "delay.txt").read_text().splitlines(), ["3", "8"])
            fields = (derived / "bandwidth_trace.tsv").read_text().splitlines()[0].split("\t")
            self.assertEqual(
                fields,
                ["time_ms", "packet_count", "payload_bytes", "window_packet_count", "window_payload_bytes", "bandwidth_mbps"],
            )
            self.assertIn("\t1200\t", (derived / "bandwidth_trace.tsv").read_text())
            summary = __import__("json").loads((derived / "custom_udp_trace_summary.json").read_text())
            self.assertEqual(summary["payload_bytes"], 2000)
            self.assertEqual(summary["clock_sync_samples"], 5)
            self.assertEqual(summary["server_utrc_transfer"], "remote_only")

    def test_both_fills_internal_empty_latency_windows_linearly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.capture(
                root,
                [
                    (1, 0, 0, 1_000_000_000_000, 1_000_005_000_000, 0, 1200, 2),
                    (2, 0, 0, 1_000_020_000_000, 1_000_035_000_000, 0, 1200, 2),
                ],
            )
            convert(path, root / "derived", "both", window_ms=10, duration_ms=30)
            derived = root / "derived"
            self.assertEqual((derived / "delay_window_time_ms.txt").read_text().splitlines(), ["0", "10", "20", "30"])
            self.assertEqual((derived / "delay.txt").read_text().splitlines(), ["5", "8", "12", "15"])
            summary = (derived / "delay_summary.txt").read_text()
            self.assertIn("observed_delay_windows=2", summary)
            self.assertIn("interpolated_delay_windows=2", summary)
            self.assertIn("raw_empty_windows=2", summary)
            self.assertIn("delay_interpolation=linear", summary)

    def test_truncated_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "truncated.utrc"
            path.write_bytes(b"UTRC" + bytes(60) + bytes(RECORD.size - 1))
            with self.assertRaisesRegex(ValueError, "truncated UTRC record"):
                list(records(path))


if __name__ == "__main__":
    unittest.main()
