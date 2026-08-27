from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from analysis_ir_normalizer import normalize_analysis_ir  # noqa: E402


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
