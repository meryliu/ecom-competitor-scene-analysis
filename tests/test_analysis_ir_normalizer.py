from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analysis_ir_normalizer import (  # noqa: E402
    AnalysisIRNormalizationError,
    normalize_analysis_ir,
)


class AnalysisIRNormalizerTests(unittest.TestCase):
    def test_normalization_is_idempotent_and_splits_composition_periods(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "2026年7月", "comparison": "2026年6月"},
                "metrics": [{"metric_id": "m", "name": "结算TR"}],
            },
            "metric_compositions": [{
                "requirement_id": "tr", "metric_ref": "m",
                "composition_id": "registered_tr", "period_roles": ["analysis", "comparison"],
            }],
            "attribution_targets": [],
        }
        normalized = normalize_analysis_ir(ir)
        self.assertEqual(
            normalized["analysis_task"]["periods"],
            {"analysis": "2026-07", "comparison": "2026-06"},
        )
        self.assertEqual(len(normalized["metric_compositions"]), 2)
        self.assertEqual(normalize_analysis_ir(normalized), normalized)
        self.assertEqual(ir["metric_compositions"][0]["period_roles"], ["analysis", "comparison"])

    def test_split_composition_expands_output_refs_in_period_order(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "2026-07", "comparison": "2026-06"},
                "metrics": [{"metric_id": "m", "name": "经营率"}],
            },
            "metric_compositions": [{
                "requirement_id": "rate", "metric_ref": "m",
                "composition_id": "registered_rate",
                "period_roles": ["comparison", "analysis"],
            }],
            "attribution_targets": [],
            "output_requirements": [{
                "requirement_id": "output",
                "source_requirement_refs": ["rate", "rate"],
            }],
        }
        normalized = normalize_analysis_ir(ir)
        children = normalized["metric_compositions"]
        self.assertEqual(
            normalized["output_requirements"][0]["source_requirement_refs"],
            [item["requirement_id"] for item in children],
        )
        self.assertEqual(
            [item["period_roles"] for item in children],
            [["comparison"], ["analysis"]],
        )
        self.assertEqual(normalize_analysis_ir(normalized), normalized)

    def test_existing_split_children_rebuild_parent_ref_mapping(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "2026-07", "comparison": "2026-06"},
                "metrics": [{"metric_id": "m", "name": "经营率"}],
            },
            "metric_compositions": [
                {
                    "requirement_id": "rate_analysis", "metric_ref": "m",
                    "composition_id": "registered_rate", "period_roles": ["analysis"],
                    "normalized_from_requirement_id": "rate",
                },
                {
                    "requirement_id": "rate_comparison", "metric_ref": "m",
                    "composition_id": "registered_rate", "period_roles": ["comparison"],
                    "normalized_from_requirement_id": "rate",
                },
            ],
            "attribution_targets": [],
            "output_requirements": [{
                "requirement_id": "output",
                "source_requirement_refs": ["rate"],
            }],
        }
        normalized = normalize_analysis_ir(ir)
        self.assertEqual(
            normalized["output_requirements"][0]["source_requirement_refs"],
            ["rate_analysis", "rate_comparison"],
        )

    def test_generated_composition_id_collision_fails(self) -> None:
        base_id = "rate"
        role = "analysis"
        from analysis_ir_normalizer import _stable_suffix  # noqa: E402

        child_id = f"{base_id}__{_stable_suffix([base_id, role])}"
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "2026-07", "comparison": "2026-06"},
                "metrics": [{"metric_id": "m", "name": "经营率"}],
            },
            "fact_observations": [{
                "requirement_id": child_id, "metric_ref": "m",
                "period_roles": ["analysis"],
            }],
            "metric_compositions": [{
                "requirement_id": base_id, "metric_ref": "m",
                "composition_id": "registered_rate",
                "period_roles": [role, "comparison"],
            }],
            "attribution_targets": [],
        }
        with self.assertRaises(AnalysisIRNormalizationError) as caught:
            normalize_analysis_ir(ir)
        self.assertEqual(caught.exception.code, "NORMALIZED_REQUIREMENT_ID_COLLISION")

    def test_period_role_name_is_rejected_before_normalization(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "analysis", "comparison": "2025-06"},
                "metrics": [],
            },
        }
        with self.assertRaises(AnalysisIRNormalizationError) as caught:
            normalize_analysis_ir(ir)
        self.assertEqual(caught.exception.code, "INVALID_PERIOD")

    def test_infers_only_structural_factor_kind(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "periods": {"analysis": "2026-07", "comparison": "2025-07"},
                "metrics": [{"metric_id": "gmv", "name": "支付GMV"}],
            },
            "metric_compositions": [],
            "attribution_targets": [{
                "target_id": "a", "metric_ref": "gmv", "scenario": "metric_change",
                "factors": [
                    {"factor_id": "mac", "metric_ref": "gmv"},
                    {"factor_id": "days", "literal": 31},
                ],
                "formula": {"op": "multiply", "args": [
                    {"factor_ref": "mac"}, {"factor_ref": "days"}
                ]},
            }],
        }
        normalized = normalize_analysis_ir(deepcopy(ir))
        self.assertEqual(
            [item["kind"] for item in normalized["attribution_targets"][0]["factors"]],
            ["metric", "literal"],
        )
