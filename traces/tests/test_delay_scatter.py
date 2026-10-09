from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
from starlive_measure.trace import DelayTimeScatter as scatter


class DelayScatterTest(unittest.TestCase):
    def test_read_delay_values_and_window_axis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "dual_udp_latency_uplink_test"
            run_dir.mkdir()
            delay = run_dir / "delay.txt"
            delay.write_text("7\n8\n-1\n", encoding="utf-8")
            self.assertEqual(scatter.read_delay_values(delay), [7, 8, -1])
            self.assertEqual([index * scatter.WINDOW_MS for index in range(3)], [0, 10, 20])

    def test_cli_writes_one_png_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session = Path(temp_dir) / "session"
            run_dir = session / "dual_udp_latency_uplink_test"
            run_dir.mkdir(parents=True)
            (run_dir / "delay.txt").write_text("7\n8\n9\n", encoding="utf-8")
            (run_dir / scatter.TIME_FILENAME).write_text("0\n10\n30\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m", "starlive_measure.trace.DelayTimeScatter",
                    str(session),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = run_dir / "time_delay_scatter.png"
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    def test_invalid_delay_value_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "delay.txt"
            path.write_text("7.5\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                scatter.read_delay_values(path)

    def test_line_plot_uses_complete_window_axis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir) / "run"
            run_dir.mkdir()
            (run_dir / "delay.txt").write_text("5\n8\n11\n15\n", encoding="utf-8")
            (run_dir / scatter.TIME_FILENAME).write_text("0\n10\n20\n30\n", encoding="utf-8")
            output = scatter.plot_run(run_dir)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
