from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metric_constraints import (  # noqa: E402
    MetricConstraintError,
    metric_constraints_fingerprint,
    normalize_metric_constraints,
)


class MetricConstraintTests(unittest.TestCase):
    def test_and_constraint_fingerprint_is_order_independent(self) -> None:
        first = {
            "kind": "dimension_filter", "operator": "eq", "values": ["京东"],
            "dimension_hint": "平台", "provenance": "model_inferred",
        }
        second = {
            "kind": "dimension_filter", "operator": "exclude", "values": ["自营"],
            "dimension_hint": "经营模式", "provenance": "model_inferred",
        }
        self.assertEqual(
            metric_constraints_fingerprint([first, second]),
            metric_constraints_fingerprint([second, first]),
        )

    def test_normalization_deduplicates_values_and_constraints(self) -> None:
        constraint = {
            "kind": "dimension_filter", "operator": "in", "values": ["京东", " 京东 ", "抖音"],
            "dimension_hint": "平台", "provenance": "model_inferred",
        }
        normalized = normalize_metric_constraints([constraint, constraint])
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["values"], ["京东", "抖音"])

    def test_eq_requires_exactly_one_value(self) -> None:
        with self.assertRaises(MetricConstraintError):
            normalize_metric_constraints([{
                "kind": "dimension_filter", "operator": "eq", "values": ["京东", "抖音"]
            }])


if __name__ == "__main__":
    unittest.main()
