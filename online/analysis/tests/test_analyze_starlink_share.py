"""Focused checks for paired regional Starlink share exports."""

import csv
import tempfile
import unittest
from pathlib import Path

from online.analysis.analyze_starlink_share import analyze_pair, load_export


class StarlinkShareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def export(self, name, metric, regions, rows):
        path = self.root / name
        header = ["日期"]
        for region in regions:
            header.extend(
                f"{region}-{metric}-{suffix}"
                for suffix in ("指标值", "采样量", "占比")
            )
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for when, values in rows:
                cells = [when]
                for region in regions:
                    value = values.get(region, "")
                    cells.extend((value, "999999" if value != "" else "", "99%"))
                writer.writerow(cells)
            writer.writerow(["平均值"] + ["123.45", "-", "-"] * len(regions))
        return load_export(path)

    def test_aligns_by_region_and_time_and_uses_weighted_counts(self):
        day1 = "2026-10-02T00:00:00Z"
        day2 = "2026-10-03T00:00:00Z"
        day3 = "2026-10-04T00:00:00Z"
        overall = self.export(
            "overall.csv", "播放总量", ("B", "A", "C"),
            [(day1, {"A": 10, "B": 20}),
             (day2, {"A": 30, "B": 20, "C": 0}),
             (day3, {"A": 999, "B": 999})],
        )
        starlink = self.export(
            "starlink.csv", "播放总量", ("A", "B"),
            [(day1, {"A": 4}), (day2, {"A": 6, "B": 2})],
        )
        result = analyze_pair("SG", overall, starlink)
        by_region = {row["region"]: row for row in result.region_rows}

        self.assertEqual(len(result.time_rows), 2)
        self.assertEqual(result.overall_only_times, [day3])
        self.assertEqual((result.starlink_total, result.overall_total), (12, 80))
        self.assertEqual(by_region["A"]["share_pct"], "25.000000")
        self.assertEqual(by_region["B"]["share_pct"], "5.000000")
        self.assertEqual(by_region["B"]["starlink_zero_inferred_periods"], 1)
        self.assertEqual(by_region["C"]["starlink_zero_inferred_periods"], 2)
        self.assertEqual(by_region["C"]["share_pct"], "")
        self.assertEqual(result.time_rows[0]["share_pct"], "13.333333")
        self.assertEqual(result.time_rows[1]["share_pct"], "16.000000")
        self.assertEqual(result.detail_rows[2]["status"], "zero_denominator")

    def test_rejects_starlink_count_above_overall(self):
        when = "2026-10-02T00:00:00Z"
        overall = self.export("overall.csv", "events", ("A",), [(when, {"A": 3})])
        starlink = self.export("starlink.csv", "events", ("A",), [(when, {"A": 4})])
        with self.assertRaisesRegex(ValueError, "Starlink exceeds Overall"):
            analyze_pair("SG", overall, starlink)

    def test_rejects_mismatched_metrics(self):
        when = "2026-10-02T00:00:00Z"
        overall = self.export("overall.csv", "events", ("A",), [(when, {"A": 3})])
        starlink = self.export("starlink.csv", "other", ("A",), [(when, {"A": 2})])
        with self.assertRaisesRegex(ValueError, "mismatched metrics"):
            analyze_pair("SG", overall, starlink)


if __name__ == "__main__":
    unittest.main()
