from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
from starlive_measure.trace import BandwidthTimeScatter as scatter


class BandwidthTimeScatterTest(unittest.TestCase):
    def test_per_millisecond_bandwidth_fills_empty_bins(self) -> None:
        times, values = scatter.build_bandwidth_points([0, 0, 1, 3], 1000)
        self.assertEqual(times, [0, 1, 2, 3])
        self.assertEqual(len(values), 4)
        for actual, expected in zip(values, [1.6, 2.4, 2.4, 3.2]):
            self.assertAlmostEqual(actual, expected)

    def test_skip_empty_bins(self) -> None:
        times, values = scatter.build_bandwidth_points([0, 0, 1, 3], 1000, False)
        self.assertEqual(times, [0, 1, 3])
        for actual, expected in zip(values, [1.6, 2.4, 3.2]):
            self.assertAlmostEqual(actual, expected)

    def test_window_width_changes_denominator(self) -> None:
        times, values = scatter.build_bandwidth_points([0, 1], 1000, window_ms=1)
        self.assertEqual(times, [0, 1])
        self.assertEqual(values, [8.0, 8.0])

    def test_duration_extends_empty_tail(self) -> None:
        times, values = scatter.build_bandwidth_points([0, 1], 1000, window_ms=1, end_ms=4)
        self.assertEqual(times, [0, 1, 2, 3, 4])
        self.assertEqual(values, [8.0, 8.0, 0.0, 0.0, 0.0])

    def test_payload_is_inferred_from_run_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "dual_udp_latency_uplink_test"
            run_dir.mkdir()
            arrival = run_dir / "udp_arrival_time_ms.txt"
            arrival.write_text("0\n1\n", encoding="utf-8")
            (run_dir / "metadata.txt").write_text("udp_payload_len=1472\n", encoding="utf-8")
            self.assertEqual(scatter.infer_payload_bytes(arrival), 1472)

    def test_standalone_txt_requires_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "udp_arrival_time_ms.txt"
            path.write_text("0\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                scatter.infer_payload_bytes(path)

    def test_cli_writes_png_for_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "dual_udp_latency_uplink_test"
            run_dir.mkdir()
            (run_dir / "udp_arrival_time_ms.txt").write_text("0\n0\n1\n", encoding="utf-8")
            (run_dir / "metadata.txt").write_text("udp_payload_len=1472\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.BandwidthTimeScatter",
                    str(run_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = run_dir / "bandwidth_time_scatter.png"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    def test_cli_names_non_default_window_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "dual_udp_latency_uplink_test"
            run_dir.mkdir()
            (run_dir / "udp_arrival_time_ms.txt").write_text("0\n1\n", encoding="utf-8")
            (run_dir / "metadata.txt").write_text("udp_payload_len=1472\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.BandwidthTimeScatter",
                    str(run_dir),
                    "--window-ms",
                    "100",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = run_dir / "bandwidth_time_scatter_100ms.png"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
