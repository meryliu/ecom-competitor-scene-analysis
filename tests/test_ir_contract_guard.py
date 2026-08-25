from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ir_contract_guard import (  # noqa: E402
    IRContractError,
    validate_analysis_ir_contract,
)
from prepare_analysis import normalize_analysis_ir  # noqa: E402


def valid_ir() -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "attribute formula",
            "metrics": [
                {"metric_id": "target", "name": "支付GMV"},
                {"metric_id": "input", "name": "MAC"},
            ],
            "periods": {"analysis": "2026-06", "comparison": "2025-06"},
        },
        "attribution_targets": [{
            "target_id": "formula",
            "metric_ref": "target",
            "scenario": "metric_change",
            "periods": {"analysis": "2026-06", "comparison": "2025-06"},
            "factors": [
                {"factor_id": "input", "kind": "metric", "metric_ref": "input"},
                {
                    "factor_id": "days",
                    "kind": "literal",
                    "values_by_period_role": {"analysis": 1.0, "comparison": 1.0},
                },
            ],
            "formula": {
                "op": "multiply",
                "args": [{"factor_ref": "input"}, {"factor_ref": "days"}],
            },
        }],
    }


class IRContractGuardTests(unittest.TestCase):
    def assert_contract_code(self, ir: dict, code: str) -> None:
        with self.assertRaises(IRContractError) as caught:
            validate_analysis_ir_contract(ir)
        self.assertEqual(caught.exception.code, code)

    def test_valid_formula_passes(self) -> None:
        validate_analysis_ir_contract(valid_ir())

    def test_missing_metric_factor_ref_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["factors"][0].pop("metric_ref")
        self.assert_contract_code(ir, "ATTR-IR-001")

    def test_unknown_metric_ref_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["factors"][0]["metric_ref"] = "missing"
        self.assert_contract_code(ir, "ATTR-IR-003")

    def test_string_formula_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["formula"] = "input * days"
        self.assert_contract_code(ir, "ATTR-IR-002")

    def test_unknown_factor_ref_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["formula"]["args"][0] = {"factor_ref": "missing"}
        self.assert_contract_code(ir, "ATTR-IR-004")

    def test_unreferenced_factor_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["formula"] = {"factor_ref": "input"}
        self.assert_contract_code(ir, "ATTR-IR-004")

    def test_missing_literal_comparison_role_fails(self) -> None:
        ir = valid_ir()
        ir["attribution_targets"][0]["factors"][1]["values_by_period_role"].pop(
            "comparison"
        )
        self.assert_contract_code(ir, "ATTR-IR-005")

    def test_metric_change_legacy_yoy_role_normalizes_to_comparison(self) -> None:
        ir = valid_ir()
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-06",
            "analysis_last_year": "2025-06",
        }
        ir["attribution_targets"][0].pop("periods")
        normalized = normalize_analysis_ir(ir)
        self.assertEqual(
            normalized["attribution_targets"][0]["periods"],
            {"analysis": "2026-06", "comparison": "2025-06"},
        )
        self.assertEqual(
            normalized["analysis_task"]["periods"]["analysis_last_year"], "2025-06"
        )

    def test_yoy_derived_requirement_and_metric_change_roles_coexist(self) -> None:
        ir = valid_ir()
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-06",
            "analysis_last_year": "2025-06",
        }
        ir["derived_requirements"] = [{
            "requirement_id": "yoy", "metric_ref": "target",
            "required_period_roles": ["analysis", "analysis_last_year"],
        }]
        ir["attribution_targets"][0].pop("periods")
        normalized = normalize_analysis_ir(ir)
        validate_analysis_ir_contract(normalized)
        self.assertEqual(
            normalized["derived_requirements"][0]["required_period_roles"],
            ["analysis", "analysis_last_year"],
        )
        self.assertEqual(
            normalized["attribution_targets"][0]["periods"],
            {"analysis": "2026-06", "comparison": "2025-06"},
        )

    def test_duplicate_physical_period_roles_fail(self) -> None:
        ir = deepcopy(valid_ir())
        ir["attribution_targets"][0]["periods"]["comparison"] = "2026-06"
        self.assert_contract_code(ir, "ATTR-IR-006")


if __name__ == "__main__":
    unittest.main()
