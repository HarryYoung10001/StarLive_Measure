from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from starlive_measure.root.reference_sweep import select_sweep_rows
from measurement.scripts.traceroot import _save_sweep_state


class ReferenceSweepSelectionTest(unittest.TestCase):
    def test_selects_one_attempt_per_rate_and_orders_by_rate(self) -> None:
        attempts = [
            {"offered_rate": "70M", "offered_rate_mbps": 70.0, "run_id": "70-first", "status": "invalid"},
            {"offered_rate": "60M", "offered_rate_mbps": 60.0, "run_id": "60-only", "status": "complete"},
            {"offered_rate": "70M", "offered_rate_mbps": 70.0, "run_id": "70-retry", "status": "complete"},
        ]
        selected = select_sweep_rows(attempts, {"60M": "60-only", "70M": "70-retry"})
        self.assertEqual([row["run_id"] for row in selected], ["60-only", "70-retry"])

    def test_unselected_invalid_attempt_is_omitted_but_can_be_selected_when_no_retry_passes(self) -> None:
        attempts = [
            {"offered_rate": "170M", "offered_rate_mbps": 170.0, "run_id": "first", "status": "invalid"},
            {"offered_rate": "170M", "offered_rate_mbps": 170.0, "run_id": "retry", "status": "invalid"},
        ]
        self.assertEqual(select_sweep_rows(attempts, {"170M": "retry"})[0]["run_id"], "retry")
        self.assertEqual(select_sweep_rows(attempts, {"170M": "first"})[0]["run_id"], "first")


class SweepCheckpointTest(unittest.TestCase):
    def test_checkpoint_records_complete_invalid_and_pending_rates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            state = {
                "status": "in_progress",
                "config": {"rates": ["60M", "70M", "80M"]},
                "attempts": [
                    {"rate": "60M", "run_id": "run-60", "status": "complete", "analysis_valid": True},
                    {"rate": "70M", "run_id": "run-70", "status": "invalid", "analysis_valid": False},
                    {"rate": "70M", "run_id": "run-70-retry", "status": "invalid", "analysis_valid": False},
                ],
                "selected_run_ids": {"60M": "run-60", "70M": "run-70-retry"},
            }
            _save_sweep_state(session, state)
            saved = json.loads((session / "sweep_summary.json").read_text())
            self.assertEqual(saved["status"], "in_progress")
            self.assertEqual(saved["completed_rates"], ["60M"])
            self.assertEqual(saved["invalid_rates"], ["70M"])
            self.assertEqual(saved["pending_rates"], ["80M"])

    def test_checkpoint_marks_completed_sweep_with_invalid_point(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            state = {
                "status": "in_progress",
                "config": {"rates": ["60M", "70M"]},
                "attempts": [
                    {"rate": "60M", "run_id": "run-60", "status": "complete", "analysis_valid": True},
                    {"rate": "70M", "run_id": "run-70", "status": "invalid", "analysis_valid": False},
                    {"rate": "70M", "run_id": "run-70-retry", "status": "invalid", "analysis_valid": False},
                ],
                "selected_run_ids": {"60M": "run-60", "70M": "run-70-retry"},
            }
            _save_sweep_state(session, state)
            saved = json.loads((session / "sweep_summary.json").read_text())
            self.assertEqual(saved["status"], "complete_with_invalid")
            self.assertEqual(saved["pending_rates"], [])

    def test_failed_retry_does_not_resolve_a_prior_invalid_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            state = {
                "status": "in_progress",
                "config": {"rates": ["70M"]},
                "attempts": [
                    {"rate": "70M", "run_id": "measured-invalid", "status": "invalid", "analysis_valid": False},
                    {"rate": "70M", "run_id": "preflight-failed", "status": "failed", "analysis_valid": None},
                ],
                "selected_run_ids": {"70M": "measured-invalid"},
            }
            _save_sweep_state(session, state)
            saved = json.loads((session / "sweep_summary.json").read_text())
            self.assertEqual(saved["status"], "in_progress")
            self.assertEqual(saved["invalid_rates"], [])
            self.assertEqual(saved["pending_rates"], ["70M"])
