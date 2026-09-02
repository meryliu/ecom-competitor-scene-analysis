from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_capability import (  # noqa: E402
    can_rollup_grain,
    evaluate_structural_grain_capability,
    project_task_capabilities,
)


class SourceCapabilityTests(unittest.TestCase):
    def test_registered_grain_graph_includes_week_to_quarter(self) -> None:
        self.assertTrue(can_rollup_grain("week", "month"))
        self.assertTrue(can_rollup_grain("week", "quarter"))
        self.assertTrue(can_rollup_grain("week", "year"))
        self.assertFalse(can_rollup_grain("quarter", "month"))

    def test_non_additive_metric_requires_exact_grain(self) -> None:
        metadata = {
            "supported_grains": ["month"],
            "aggregation_mode": "non_additive",
        }
        result = evaluate_structural_grain_capability(metadata, "quarter")
        self.assertEqual(result["status"], "unavailable")

    def test_additive_metric_accepts_registered_finer_grain(self) -> None:
        metadata = {
            "supported_grains": ["week"],
            "aggregation_mode": "additive",
        }
        result = evaluate_structural_grain_capability(metadata, "quarter")
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["path"], "aggregate_fact")
        self.assertEqual(result["source_grain"], "week")

    def test_unknown_grain_metadata_is_not_declared_executable(self) -> None:
        result = evaluate_structural_grain_capability({}, "month")
        self.assertEqual(result["status"], "unknown")

    def test_unknown_aggregation_metadata_blocks_rollup(self) -> None:
        result = evaluate_structural_grain_capability({"supported_grains": ["week"]}, "month")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "metric_aggregation_unknown")

    def test_task_projection_keeps_requirement_source_dimension(self) -> None:
        capabilities = {
            "schema_version": "resolved_capabilities/1.0",
            "source": {}, "metric_bindings": {}, "dimension_bindings": {},
            "metrics": {"支付GMV": {"dimensions": ["TOP6平台"]}},
            "dimensions": {"TOP6平台": {"values": ["京东", "拼多多"]}},
            "availability": {},
            "task_resolutions": {"q": {
                "metric_bindings": {}, "metric_statuses": {},
                "requirement_bindings": {"total": {
                    "mode": "source_dimension_all_sum",
                    "source_metric": "支付GMV", "source_dimension": "TOP6平台",
                }},
                "composition_resolutions": [], "resolution_cases": [],
            }},
            "task_metric_dimension_bindings": {"q": {}},
        }
        projected = project_task_capabilities(capabilities, "q")
        self.assertIn("支付GMV", projected["metrics"])
        self.assertIn("TOP6平台", projected["dimensions"])

    def test_task_projection_keeps_registered_composition_leaf_capabilities(self) -> None:
        capabilities = {
            "schema_version": "resolved_capabilities/1.0",
            "source": {}, "metric_bindings": {}, "dimension_bindings": {},
            "metrics": {
                "支付GMV": {"dimensions": ["TOP6平台"]},
                "结算GMV": {"dimensions": ["TOP6平台"]},
            },
            "dimensions": {"TOP6平台": {"values": ["拼多多"]}},
            "availability": {},
            "task_resolutions": {"q": {
                "metric_bindings": {}, "metric_statuses": {},
                "requirement_bindings": {"rate": {
                    "mode": "registered_composition",
                    "composition_id": "competitor_settlement_rate",
                    "input_bindings": {
                        "numerator": {
                            "mode": "member_selector", "source_metric": "结算GMV",
                            "metric_constraints": [{
                                "source_dimension": "TOP6平台", "operator": "eq",
                                "values": ["拼多多"],
                            }],
                        },
                        "denominator": {
                            "mode": "member_selector", "source_metric": "支付GMV",
                            "metric_constraints": [{
                                "source_dimension": "TOP6平台", "operator": "eq",
                                "values": ["拼多多"],
                            }],
                        },
                    },
                }},
                "composition_resolutions": [], "resolution_cases": [],
            }},
            "task_metric_dimension_bindings": {"q": {}},
        }
        projected = project_task_capabilities(capabilities, "q")
        self.assertEqual(set(projected["metrics"]), {"支付GMV", "结算GMV"})
        self.assertIn("TOP6平台", projected["dimensions"])


if __name__ == "__main__":
    unittest.main()
