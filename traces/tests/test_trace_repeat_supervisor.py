from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from traces.scripts.trace_repeat_supervisor import successful_runs


class TraceRepeatSupervisorTest(unittest.TestCase):
    def test_successful_runs_requires_complete_contiguous_latency_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            run = session / "run_001"
            derived = run / "derived"
            derived.mkdir(parents=True)
            for name, content in {
                "udp_arrival_time_ms.txt": "0\n",
                "delay.txt": "5\n6\n",
                "delay_window_time_ms.txt": "0\n10\n",
                "delay_summary.txt": "delay_interpolation=linear\n",
                "bandwidth_trace.tsv": "time_ms\n0\n",
            }.items():
                (derived / name).write_text(content, encoding="utf-8")
            (derived / "time_delay_scatter.png").write_bytes(b"png")
            (derived / "custom_udp_trace_summary.json").write_text(json.dumps({
                "capture_duration_ms": 150000,
                "latency": {"window_ms": 10},
            }), encoding="utf-8")
            (run / "manifest.json").write_text(json.dumps({
                "status": "complete",
                "custom_udp": {
                    "status": "complete",
                    "server_utrc": {"transfer": "remote_only", "exists": True, "size_bytes": 1},
                    "sender": {"done_ack": True, "clock_sync_samples": 5, "sent_packets": 1},
                },
                "artifacts": {"server_pull_rc": 0, "phone_pull_rc": 0},
            }), encoding="utf-8")
            self.assertEqual(successful_runs(session, 145, 160), [run])


if __name__ == "__main__":
    unittest.main()
