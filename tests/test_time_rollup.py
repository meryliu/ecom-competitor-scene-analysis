from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from time_rollup import (  # noqa: E402
    iso_weeks_covering,
    normalize_period,
    overlap_days,
    period_bounds,
)


class TimeRollupTests(unittest.TestCase):
    def test_iso_week_examples(self) -> None:
        self.assertEqual(period_bounds("2025-W42"), (date(2025, 10, 13), date(2025, 10, 19)))
        self.assertEqual(period_bounds("2026-W02"), (date(2026, 1, 5), date(2026, 1, 11)))

    def test_invalid_iso_week_is_rejected(self) -> None:
        self.assertIsNone(normalize_period("2021-W53"))
        self.assertIsNone(normalize_period("2025-W53"))
        self.assertIsNone(normalize_period("2025-W00"))

    def test_cross_year_iso_week(self) -> None:
        self.assertEqual(period_bounds("2025-W01"), (date(2024, 12, 30), date(2025, 1, 5)))

    def test_month_boundary_weights(self) -> None:
        components = iso_weeks_covering("2025-02")
        self.assertEqual(components[0]["period"], "2025-W05")
        self.assertEqual(components[0]["overlap_days"], 2)
        self.assertAlmostEqual(components[0]["weight"], 2 / 7)
        self.assertEqual(components[-1]["period"], "2025-W09")
        self.assertEqual(components[-1]["overlap_days"], 5)
        self.assertEqual(overlap_days("2025-W06", "2025-02"), 7)

    def test_leap_year_month_coverage(self) -> None:
        components = iso_weeks_covering("2024-02")
        self.assertEqual(sum(item["overlap_days"] for item in components), 29)


if __name__ == "__main__":
    unittest.main()
