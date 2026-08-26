from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from business_parameter_preflight import (  # noqa: E402
    BusinessParameterPreflightError,
    preflight_business_parameters,
)
from ir_contract_guard import IRContractError  # noqa: E402


def base_ir() -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "2026年6月支付GMV同比变化归因",
            "metrics": [
                {"metric_id": "gmv", "name": "支付GMV", "metric_object": "volume", "unit": "待元信息解析"},
                {"metric_id": "mac", "name": "MAC", "metric_object": "volume", "unit": "待元信息解析"},
            ],
            "periods": {"analysis": "2026-06"},
        },
        "attribution_targets": [{
            "target_id": "gmv_attr",
            "metric_ref": "gmv",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "factors": [{"factor_id": "mac", "kind": "metric", "metric_ref": "mac"}],
            "formula": {"factor_ref": "mac"},
        }],
    }


class BusinessParameterPreflightTests(unittest.TestCase):
    def test_complete_non_attribution_ir_is_unchanged(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "query": "2026年6月支付GMV",
                "metrics": [{"metric_id": "gmv", "name": "支付GMV", "unit": "待元信息解析"}],
                "periods": {"analysis": "2026-06"},
            },
            "attribution_targets": [],
        }
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["analysis_ir"], ir)

    def test_complete_attribution_ir_is_unchanged(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-06"
        ir["attribution_targets"][0]["periods"] = {
            "analysis": "2026-06",
            "comparison": "2025-06",
        }
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["analysis_ir"], ir)

    def test_explicit_yoy_deterministically_fills_comparison(self) -> None:
        result = preflight_business_parameters(base_ir())
        self.assertEqual(result["status"], "continue")
        self.assertEqual(
            result["analysis_ir"]["attribution_targets"][0]["periods"],
            {"analysis": "2026-06", "comparison": "2025-06"},
        )
        self.assertEqual(
            result["analysis_ir"]["analysis_task"]["metrics"][0]["unit"],
            "待元信息解析",
        )

    def test_ambiguous_comparison_returns_bounded_options(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年6月支付GMV变化归因"
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "waiting_confirmation")
        case = next(
            item for item in result["resolution_cases"]
            if item["parameter_code"] == "ATTRIBUTION_COMPARISON_PERIOD_MISSING"
        )
        self.assertEqual(len(case["candidates"]), 2)
        self.assertEqual(
            {item["value"] for item in case["candidates"]},
            {"2025-06", "2026-05"},
        )

    def test_query_with_yoy_and_mom_does_not_choose_one_relation(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年6月支付GMV同比和环比变化归因"
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "waiting_confirmation")
        self.assertIn(
            "ATTRIBUTION_COMPARISON_PERIOD_MISSING",
            {item["parameter_code"] for item in result["resolution_cases"]},
        )

    def test_yoy_trend_fills_both_last_year_roles(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年7月同比涨幅相比上月如何归因"
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-07",
            "comparison": "2026-06",
        }
        target = ir["attribution_targets"][0]
        target["scenario"] = "yoy_trend_change"
        target["target_semantics"] = "relative_yoy_trend"
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["analysis_ir"]["attribution_targets"][0]["periods"], {
            "analysis": "2026-07",
            "analysis_last_year": "2025-07",
            "comparison": "2026-06",
            "comparison_last_year": "2025-06",
        })

    def test_missing_scenario_requires_confirmation(self) -> None:
        ir = base_ir()
        ir["attribution_targets"][0].pop("scenario")
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "waiting_confirmation")
        self.assertEqual(result["resolution_cases"][0]["parameter_code"], "ATTRIBUTION_SCENARIO_MISSING")

    def test_unknown_metric_ref_remains_a_strict_ir_error(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年6月支付GMV变化归因"
        ir["attribution_targets"][0]["metric_ref"] = "missing"
        with self.assertRaises(IRContractError) as caught:
            preflight_business_parameters(ir)
        self.assertEqual(caught.exception.code, "ATTR-IR-003")

    def test_missing_formula_requires_business_confirmation(self) -> None:
        ir = base_ir()
        ir["attribution_targets"][0].pop("formula")
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "waiting_confirmation")
        self.assertIn(
            "ATTRIBUTION_FORMULA_MISSING",
            {item["parameter_code"] for item in result["resolution_cases"]},
        )

    def test_formula_without_factors_requires_business_confirmation(self) -> None:
        ir = base_ir()
        ir["attribution_targets"][0].pop("factors")
        result = preflight_business_parameters(ir)
        self.assertEqual(result["status"], "waiting_confirmation")
        self.assertIn(
            "ATTRIBUTION_FACTORS_MISSING",
            {item["parameter_code"] for item in result["resolution_cases"]},
        )

    def test_malformed_formula_without_factors_remains_a_strict_ir_error(self) -> None:
        ir = base_ir()
        ir["attribution_targets"][0].pop("factors")
        ir["attribution_targets"][0]["formula"] = "mac"
        with self.assertRaises(IRContractError) as caught:
            preflight_business_parameters(ir)
        self.assertEqual(caught.exception.code, "ATTR-IR-002")

    def test_business_patch_applies_without_reaching_provider_patch_context(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年6月支付GMV变化归因"
        waiting = preflight_business_parameters(ir)
        case = next(
            item for item in waiting["resolution_cases"]
            if item["parameter_code"] == "ATTRIBUTION_COMPARISON_PERIOD_MISSING"
        )
        patched = deepcopy(ir)
        patched["resolution_patches"] = [{
            "kind": "business_parameter",
            "case_id": case["case_id"],
            "candidate_id": "yoy",
            "context_fingerprint": case["context_fingerprint"],
        }]
        result = preflight_business_parameters(patched)
        self.assertEqual(result["status"], "continue")
        self.assertEqual(
            result["analysis_ir"]["attribution_targets"][0]["periods"]["comparison"],
            "2025-06",
        )
        self.assertNotIn("resolution_patches", result["analysis_ir"])

    def test_stale_business_patch_is_rejected(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["query"] = "2026年6月支付GMV变化归因"
        waiting = preflight_business_parameters(ir)
        case = next(
            item for item in waiting["resolution_cases"]
            if item["parameter_code"] == "ATTRIBUTION_COMPARISON_PERIOD_MISSING"
        )
        ir["resolution_patches"] = [{
            "kind": "business_parameter",
            "case_id": case["case_id"],
            "candidate_id": "yoy",
            "context_fingerprint": "stale",
        }]
        with self.assertRaises(BusinessParameterPreflightError):
            preflight_business_parameters(ir)


if __name__ == "__main__":
    unittest.main()
