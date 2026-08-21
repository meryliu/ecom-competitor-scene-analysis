from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_analysis import (  # noqa: E402
    PreparationError,
    _requirement_roles,
    normalize_analysis_ir,
    prepare_analysis_ir,
)


def load(name: str) -> dict:
    return json.loads((ROOT / "references" / name).read_text(encoding="utf-8"))


def source_index(*, quarter_payment: bool = False, direct_rate: bool = False) -> dict:
    metrics = {
        "支付GMV": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
        "结算GMV": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
    }
    if direct_rate:
        metrics["结算率"] = {"unit": "%", "additive": False, "dimensions": ["平台"]}
    month_blocks = {
        name: {"dimension": "平台", "rows": {"京东": row}}
        for row, name in enumerate(metrics, start=3)
    }
    quarter_blocks = {}
    if quarter_payment:
        quarter_blocks["支付GMV"] = {"dimension": "平台", "rows": {"京东": 3}}
    raw = {
        "metrics": metrics,
        "dimensions": {"平台": {"values": ["京东"]}},
        "sheets": {
            "month": {
                "available": True,
                "periods": {
                    "2026-04": "B", "2026-05": "C", "2026-06": "D"
                },
                "blocks": month_blocks,
            },
            "quarter": {
                "available": True,
                "periods": {"2026-Q2": "B"},
                "blocks": quarter_blocks,
            },
            "week": {"available": False},
            "year": {"available": False},
        },
    }
    return {
        "schema_version": "resolved_capabilities/1.0",
        "provider": {"provider_id": "test", "contract_version": "1.0"},
        "source": {"revision": 1, "schema_hash": "test"},
        "metric_bindings": {name: name for name in metrics},
        "dimension_bindings": {"平台": "平台"},
        "metrics": metrics,
        "dimensions": raw["dimensions"],
        "availability": {
            grain: {
                "periods": sorted((sheet.get("periods") or {}).keys()),
                "metrics": {
                    metric: {"dimension": block["dimension"]}
                    for metric, block in (sheet.get("blocks") or {}).items()
                },
            }
            for grain, sheet in raw["sheets"].items()
            if sheet.get("available")
        },
    }


def base_ir(metric_name: str, metric_object: str = "volume") -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "test",
            "analysis_goal": "test",
            "metrics": [{
                "metric_id": "target",
                "name": metric_name,
                "metric_object": metric_object,
                "unit": "待元信息解析",
            }],
            "periods": {"analysis": "2026年Q2"},
            "scope": "京东",
            "filters": [],
        },
        "views": [{"view_id": "v"}],
        "dimension_trees": [],
        "input_adaptations": [],
        "fact_observations": [],
        "metric_compositions": [],
        "derived_requirements": [],
        "custom_calculations": [],
        "attribution_targets": [],
        "output_requirements": [],
        "clarifications": [],
    }


class PrepareAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compositions = load("metric-composition-registry.json")
        self.derived = load("derived-metric-registry.json")

    def test_periods_are_canonicalized_before_compile(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {
            "analysis": "2026年7月",
            "analysis_last_year": "2025年7月",
        }
        normalized = normalize_analysis_ir(ir)
        self.assertEqual(
            normalized["analysis_task"]["periods"],
            {"analysis": "2026-07", "analysis_last_year": "2025-07"},
        )

    def test_attribution_leaf_dimensions_are_merged_before_source_resolution(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "comparison": "2025-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "formula",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "factors": [{
                "factor_id": "derived_factor",
                "kind": "derived",
                "expressions_by_period_role": {
                    role: {
                        "fact": {
                            "metric_ref": "target",
                            "period_role": role,
                            "dimensions": {"分组": "目标组"},
                        }
                    }
                    for role in ("analysis", "comparison")
                },
            }],
            "formula": {"factor_ref": "derived_factor"},
        }]
        consumers = _requirement_roles(ir, self.derived)
        derived_consumers = [
            item for item in consumers
            if item.get("dimensions", {}).get("分组") == "目标组"
        ]
        self.assertEqual(len(derived_consumers), 2)
        self.assertTrue(all(
            item["dimensions"] == {"平台": "京东", "分组": "目标组"}
            for item in derived_consumers
        ))

    def test_attribution_leaf_dimension_conflict_is_blocked_before_source_resolution(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "comparison": "2025-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "formula",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "factors": [{
                "factor_id": "derived_factor",
                "kind": "derived",
                "expressions_by_period_role": {
                    role: {
                        "fact": {
                            "metric_ref": "target",
                            "period_role": role,
                            "dimensions": {"平台": "其他平台"},
                        }
                    }
                    for role in ("analysis", "comparison")
                },
            }],
            "formula": {"factor_ref": "derived_factor"},
        }]
        with self.assertRaises(PreparationError) as caught:
            _requirement_roles(ir, self.derived)
        self.assertEqual(caught.exception.code, "DIMENSION_CONFLICT")

    def test_source_metadata_resolves_declared_metric_placeholder(self) -> None:
        ir = base_ir("支付GMV")
        prepared, _ = prepare_analysis_ir(
            ir, source_index(), self.compositions, self.derived
        )
        metric = prepared["analysis_task"]["metrics"][0]
        self.assertEqual(metric["unit"], "亿元")
        self.assertEqual(metric["unit_source"], "source_metric_metadata")
        self.assertTrue(metric["additive"])

    def test_metric_scoped_dimension_binding_is_preserved_for_compilation(self) -> None:
        capabilities = source_index()
        capabilities["dimensions"] = {
            "TOP6平台": {"values": ["京东", "拼多多", "淘系", "抖音", "快手", "视频号"]}
        }
        capabilities["dimension_bindings"] = {}
        capabilities["metric_dimension_bindings"] = {
            "支付GMV": {"平台": "TOP6平台"}
        }
        capabilities["metrics"]["支付GMV"]["dimensions"] = ["TOP6平台"]
        capabilities["availability"]["month"]["metrics"]["支付GMV"]["dimension"] = "TOP6平台"
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {"analysis": "2026-05"}
        ir["fact_observations"] = [{
            "requirement_id": "payment",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        prepared, _ = prepare_analysis_ir(
            ir, capabilities, self.compositions, self.derived
        )
        metric = prepared["analysis_task"]["metrics"][0]
        self.assertEqual(metric["source_metric_name"], "支付GMV")
        self.assertEqual(metric["source_dimension_bindings"], {"平台": "TOP6平台"})

    def test_concrete_declared_unit_conflict_is_blocked(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["metrics"][0]["unit"] = "万元"
        with self.assertRaisesRegex(ValueError, "声明单位与源表元信息冲突"):
            prepare_analysis_ir(ir, source_index(), self.compositions, self.derived)

    def test_quarter_direct_fact_wins_over_month_aggregation(self) -> None:
        ir = base_ir("支付GMV")
        ir["fact_observations"] = [{
            "requirement_id": "payment",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        prepared, decisions = prepare_analysis_ir(
            ir, source_index(quarter_payment=True), self.compositions, self.derived
        )
        self.assertEqual(prepared["input_adaptations"], [])
        self.assertFalse(any(item.get("mode") == "aggregate" for item in decisions))

    def test_missing_quarter_metric_falls_back_to_complete_months(self) -> None:
        ir = base_ir("支付GMV")
        ir["fact_observations"] = [{
            "requirement_id": "payment",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        prepared, decisions = prepare_analysis_ir(
            ir, source_index(), self.compositions, self.derived
        )
        adaptation = prepared["input_adaptations"][0]
        source_periods = [
            prepared["analysis_task"]["periods"][arg["fact"]["period_role"]]
            for arg in adaptation["expression"]["args"]
        ]
        self.assertEqual(source_periods, ["2026-04", "2026-05", "2026-06"])
        self.assertEqual(
            prepared["analysis_task"]["metrics"][0]["unit"], "亿元"
        )
        self.assertTrue(any(item.get("mode") == "aggregate" for item in decisions))

    def test_direct_settlement_rate_replaces_registered_composition(self) -> None:
        ir = base_ir("结算率", "ratio")
        ir["analysis_task"]["periods"] = {"analysis": "2026年5月"}
        ir["metric_compositions"] = [{
            "requirement_id": "rate",
            "metric_ref": "target",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        prepared, decisions = prepare_analysis_ir(
            ir, source_index(direct_rate=True), self.compositions, self.derived
        )
        self.assertEqual(prepared["metric_compositions"], [])
        self.assertEqual(prepared["fact_observations"][0]["requirement_id"], "rate")
        self.assertEqual(decisions[0]["mode"], "direct")

    def test_missing_settlement_rate_auto_declares_composition_inputs(self) -> None:
        ir = base_ir("结算率", "ratio")
        ir["analysis_task"]["periods"] = {"analysis": "2026年5月"}
        ir["fact_observations"] = [{
            "requirement_id": "rate",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        prepared, decisions = prepare_analysis_ir(
            ir, source_index(), self.compositions, self.derived
        )
        self.assertEqual(prepared["fact_observations"], [])
        self.assertEqual(
            prepared["metric_compositions"][0]["composition_id"],
            "competitor_settlement_rate",
        )
        self.assertEqual(
            {item["name"] for item in prepared["analysis_task"]["metrics"]},
            {"结算率", "结算GMV", "支付GMV"},
        )
        self.assertTrue(any(item.get("mode") == "derived" for item in decisions))

    def test_task_composition_resolution_uses_leaf_bindings_when_direct_metric_is_absent(self) -> None:
        ir = base_ir("结算率", "ratio")
        ir["analysis_task"]["periods"] = {"analysis": "2026年5月"}
        ir["fact_observations"] = [{
            "requirement_id": "rate",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        capabilities = source_index()
        capabilities["metric_bindings"] = {"结算GMV": "结算GMV", "支付GMV": "支付GMV"}
        capabilities["task_resolutions"] = {"q": {
            "metric_bindings": {"结算GMV": "结算GMV", "支付GMV": "支付GMV"},
            "metric_statuses": {"target": {"status": "not_found"}},
            "composition_resolutions": [{
                "metric_ref": "target",
                "composition_id": "competitor_settlement_rate",
                "direct_status": "not_found",
                "input_bindings": {"numerator": "结算GMV", "denominator": "支付GMV"},
                "input_statuses": {
                    "numerator": {"status": "bound", "binding": "结算GMV", "requested_metric": "结算GMV"},
                    "denominator": {"status": "bound", "binding": "支付GMV", "requested_metric": "支付GMV"},
                },
                "fallback_status": "ready",
                "deferred_cases": [],
            }],
            "resolution_cases": [],
        }}
        capabilities["task_metric_dimension_bindings"] = {"q": {
            "结算GMV": {"平台": "平台"}, "支付GMV": {"平台": "平台"}
        }}
        prepared, decisions = prepare_analysis_ir(
            ir, capabilities, self.compositions, self.derived
        )
        self.assertEqual(
            prepared["metric_compositions"][0]["composition_id"],
            "competitor_settlement_rate",
        )
        self.assertTrue(any(item.get("mode") == "derived" for item in decisions))

    def test_composition_leaf_confirmation_is_activated_only_during_fallback(self) -> None:
        ir = base_ir("结算率", "ratio")
        ir["analysis_task"]["periods"] = {"analysis": "2026年5月"}
        ir["fact_observations"] = [{
            "requirement_id": "rate",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
        }]
        capabilities = source_index(direct_rate=True)
        capabilities["metric_bindings"] = {"结算率": "结算率", "结算GMV": "结算GMV", "支付GMV": "支付GMV"}
        capabilities["task_resolutions"] = {"q": {
            "metric_bindings": capabilities["metric_bindings"],
            "metric_statuses": {"target": {"status": "bound", "binding": "结算率"}},
            "composition_resolutions": [{
                "metric_ref": "target",
                "composition_id": "competitor_settlement_rate",
                "direct_status": "bound",
                "input_bindings": {"numerator": "结算GMV", "denominator": "支付GMV"},
                "input_statuses": {},
                "fallback_status": "confirm",
                "deferred_cases": [{"case_id": "deferred", "action": "confirm"}],
            }],
            "resolution_cases": [],
        }}
        prepared, decisions = prepare_analysis_ir(
            ir, capabilities, self.compositions, self.derived
        )
        self.assertNotIn("resolution_blocks", prepared)
        self.assertFalse(any(item.get("mode") == "resolution_case" for item in decisions))


if __name__ == "__main__":
    unittest.main()
