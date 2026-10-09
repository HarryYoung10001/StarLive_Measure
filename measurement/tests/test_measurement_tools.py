from __future__ import annotations

import unittest
import tempfile
import json
import csv
import gzip
from pathlib import Path

from measurement.scripts.align_15k_b import _aligned
from measurement.scripts.detect_config_phase import detect_metric_phase
from starlive_measure.trace.Bandwidth100ms import build_100ms_points
from measurement.scripts.detect_config_phase import detect
from measurement.scripts.plot_measurement_series import aligned as phase_aligned, aligned_ticks, _loss_rows, _oneway_rows
from measurement.scripts.extend_bandwidth_aligned import append_complete_cycles, detect_trace_phase


class MeasurementToolsTest(unittest.TestCase):
    def test_100ms_bins_use_interval_start_and_empty_zero(self) -> None:
        points = build_100ms_points([(0.0, 1200), (99.999, 1200), (100.0, 1200)], duration_ms=300)
        self.assertEqual([point["time_ms"] for point in points], [0.0, 100.0, 200.0])
        self.assertEqual([point["packet_count"] for point in points], [2, 1, 0])
        self.assertAlmostEqual(float(points[0]["bandwidth_mbps"]), 0.192)

    def test_alignment_drops_negative_and_keeps_100_seconds(self) -> None:
        rows = _aligned([(-13000.0, 1.0), (0.0, 2.0), (88000.0, 3.0), (89000.0, 4.0)], 12.0)
        self.assertEqual(rows, [(12.0, 2.0), (100.0, 3.0)])

    def test_phase_alignment_anchors_b_plus_15_at_12(self) -> None:
        rows = phase_aligned([(0.0, 1.0), (9.0, 2.0), (21.0, 3.0), (111.0, 4.0), (112.0, 5.0)], 6.0)
        self.assertEqual(rows, [(0.0, 2.0), (12.0, 3.0), (102.0, 4.0)])

    def test_phase_alignment_accepts_custom_end(self) -> None:
        rows = phase_aligned([(0.0, 1.0), (9.0, 2.0), (21.0, 3.0), (111.0, 4.0), (201.0, 5.0)], 6.0, end_s=192.0)
        self.assertEqual(rows, [(0.0, 2.0), (12.0, 3.0), (102.0, 4.0), (192.0, 5.0)])
        self.assertEqual(aligned_ticks(192.0), [0.0, 12.0, 27.0, 42.0, 57.0, 72.0, 87.0, 102.0, 117.0, 132.0, 147.0, 162.0, 177.0, 192.0])

    def test_bandwidth_append_uses_complete_cycles_without_duplicate_boundary(self) -> None:
        base = [(float(x), 1.0) for x in range(0, 103)]
        trace = [(float(x), 2.0) for x in range(0, 106)]
        result = append_complete_cycles(base, trace, trace_b_s=0.0, base_end_s=102.0, target_end_s=192.0)
        self.assertEqual(result[-1][0], 192.0)
        self.assertEqual(len({x for x, _ in result}), len(result))
        self.assertEqual(result[-1][1], 2.0)

    def test_bandwidth_append_truncates_incomplete_final_cycle(self) -> None:
        base = [(float(x), 1.0) for x in range(0, 103)]
        trace = [(float(x), 2.0) for x in range(0, 101)]
        result = append_complete_cycles(base, trace, trace_b_s=0.0, base_end_s=102.0, target_end_s=192.0)
        self.assertEqual(result[-1][0], 177.0)

    def test_trace_bandwidth_phase_is_detected_from_trace_series(self) -> None:
        values = [0.0] * 1500
        for event in (4.4, 19.4, 34.4, 49.4):
            index = int(event * 10)
            values[index:index + 8] = [10.0] * 8
        self.assertAlmostEqual(detect_trace_phase([(i / 10.0, value) for i, value in enumerate(values)]), 4.4, delta=0.25)

    def test_phase_detector_finds_synthetic_15_second_phase(self) -> None:
        values = [0.0] * 1500
        for event in (12.0, 27.0, 42.0, 57.0, 72.0, 87.0):
            index = int(event * 10)
            values[index:index + 8] = [10.0] * 8
        result = detect_metric_phase(values)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(float(result["b_s"]), 12.0, delta=1.0)

    def test_phase_output_is_per_run_and_metric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("run_a", "run_b"):
                run = root / name
                run.mkdir()
                (run / "manifest.json").write_text("{}")
                (run / "profile.json").write_text(json.dumps({"workload": {"duration_s": 1}}))
            result = detect(root)
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(set(result["runs"]), {"run_a", "run_b"})
            self.assertEqual(set(result["runs"]["run_a"]["metrics"]), {"bandwidth", "latency", "loss", "rtt"})

    def test_oneway_latency_uses_receive_time_10ms_means(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory); derived = run / "derived"; derived.mkdir()
            (derived / "delay_precise.tsv").write_text(
                "window_start_ms\tmean_delay_ms\tsample_count\n0\t4.5\t2\n20\t8.0\t1\n", encoding="utf-8"
            )
            self.assertEqual(_oneway_rows(run, 10), [(0.0, 4.5, 2), (0.02, 8.0, 1)])

    def test_loss_rows_are_centered_200ms_at_50ms_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory); sender = run / "roles" / "udp_sender"; derived = run / "derived"
            sender.mkdir(parents=True); derived.mkdir()
            with gzip.open(sender / "uplink_udp_sender.csv.gz", "wt", newline="") as handle:
                writer = csv.writer(handle); writer.writerow(["seq", "send_rel_s"])
                for seq, timestamp in ((1, 0.0), (2, 0.05), (3, 0.10), (4, 0.15)):
                    writer.writerow([seq, timestamp])
            (derived / "udp_latency_precise.tsv").write_text(
                "seq\n1\n3\n4\n", encoding="utf-8"
            )
            points, meta = _loss_rows(run, 50, 200, 0.2)
            self.assertEqual(meta["lost_packets"], 1)
            self.assertAlmostEqual(points[0][1], 0.5)
            self.assertAlmostEqual(points[1][1], 1.0 / 3.0)
            self.assertAlmostEqual(points[2][1], 0.25)


if __name__ == "__main__":
    unittest.main()
