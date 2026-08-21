from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_analysis import (  # noqa: E402
    PreparationError,
    _apply_business_intent_selection,
    _requirement_roles,
    normalize_analysis_ir,
    prepare_analysis_ir,
)
from compile_plan import compile_and_validate  # noqa: E402


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

    def test_direct_capability_uses_metadata_grain_and_dimension(self) -> None:
        index = source_index()
        index["metrics"]["支付GMV"]["supported_grains"] = ["month"]
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {"analysis": "2026-06"}
        ir["fact_observations"] = [{
            "requirement_id": "observe",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
            "view_id": "v",
        }]
        prepared, _ = prepare_analysis_ir(ir, index, self.compositions, self.derived)
        self.assertEqual(prepared["fact_capability_plan"][0]["path"], "direct_fact")
        self.assertEqual(prepared["fact_capability_plan"][0]["direct"]["status"], "available")
        self.assertEqual(prepared["canonical_fact_selectors"][0]["grain"], "month")
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        slot = next(iter(plan["analysis_task"]["fact_requirements"]))
        self.assertEqual(slot["capability_path"], "direct_fact")
        self.assertEqual(slot["grain"], "month")

    def test_unsupported_metadata_grain_does_not_use_fact_block(self) -> None:
        index = source_index()
        index["metrics"]["支付GMV"]["supported_grains"] = ["year"]
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {"analysis": "2026-06"}
        ir["fact_observations"] = [{
            "requirement_id": "observe",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "dimensions": {"平台": "京东"},
            "dimension_refs": [],
            "view_id": "v",
        }]
        with self.assertRaises(PreparationError) as raised:
            prepare_analysis_ir(ir, index, self.compositions, self.derived)
        self.assertEqual(raised.exception.code, "SOURCE_PATH_UNAVAILABLE")

    def test_missing_formula_target_is_materialized_from_user_formula(self) -> None:
        index = source_index()
        index["availability"]["month"]["metrics"].pop("支付GMV")
        ir = base_ir("支付GMV")
        ir["analysis_task"]["metrics"].append({
            "metric_id": "factor",
            "name": "结算GMV",
            "metric_object": "volume",
            "unit": "待元信息解析",
        })
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-06",
            "comparison": "2026-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "formula_target",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "periods": {"analysis": "2026-06", "comparison": "2026-05"},
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "group_dimensions": [],
            "factors": [{
                "factor_id": "source_factor",
                "kind": "metric",
                "metric_ref": "factor",
                "role": "multiplier",
            }],
            "formula": {"factor_ref": "source_factor"},
        }]
        prepared, _ = prepare_analysis_ir(ir, index, self.compositions, self.derived)
        self.assertEqual(prepared["attribution_targets"][0]["target_fact_source"], "formula_computed")
        formula_adaptations = [
            item for item in prepared["input_adaptations"]
            if item.get("rule_source") == "user_query_formula"
        ]
        self.assertEqual(len(formula_adaptations), 2)
        self.assertEqual(
            {item["target_period_role"] for item in formula_adaptations},
            {"analysis", "comparison"},
        )
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertNotEqual(report.get("status"), "failed")
        adaptation_nodes = {
            item["node_id"] for item in plan["nodes"]
            if item.get("type") == "input_adaptation"
        }
        attribution_node = next(
            item for item in plan["nodes"] if item.get("type") == "formula_attribution"
        )
        self.assertTrue(adaptation_nodes.issubset(set(attribution_node["depends_on"])))

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

    def test_task_filter_is_inherited_by_capability_and_physical_selector(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {"analysis": "2026-06"}
        ir["analysis_task"]["filters"] = [{
            "dimension_ref": "平台", "operator": "eq", "value": "京东"
        }]
        ir["fact_observations"] = [{
            "requirement_id": "observe",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {},
            "dimension_refs": [],
        }]
        prepared, _ = prepare_analysis_ir(
            ir, source_index(), self.compositions, self.derived
        )
        self.assertEqual(
            prepared["fact_observations"][0]["dimensions"], {"平台": "京东"}
        )
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        slot = plan["fetch_requests"][0]["fact_slots"][0]
        self.assertEqual(slot["selector_dimensions"], {"平台": "京东"})
        self.assertEqual(plan["analysis_task"]["selector_dimensions"], {"平台": "京东"})

    def test_task_filter_conflict_is_rejected_before_capability_planning(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"] = {"analysis": "2026-06"}
        ir["analysis_task"]["filters"] = {"平台": "京东"}
        ir["fact_observations"] = [{
            "requirement_id": "observe",
            "metric_ref": "target",
            "period_roles": ["analysis"],
            "view_id": "v",
            "dimensions": {"平台": "其他平台"},
            "dimension_refs": [],
        }]
        with self.assertRaises(PreparationError) as raised:
            prepare_analysis_ir(ir, source_index(), self.compositions, self.derived)
        self.assertEqual(raised.exception.code, "SELECTOR_CONTEXT_INVALID")

    def test_auto_aggregation_reuses_physical_period_roles_across_metrics(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["metrics"].append({
            "metric_id": "secondary",
            "name": "结算GMV",
            "metric_object": "volume",
            "unit": "待元信息解析",
        })
        ir["fact_observations"] = [
            {
                "requirement_id": f"observe_{metric_ref}",
                "metric_ref": metric_ref,
                "period_roles": ["analysis"],
                "view_id": "v",
                "dimensions": {"平台": "京东"},
                "dimension_refs": [],
            }
            for metric_ref in ("target", "secondary")
        ]
        prepared, _ = prepare_analysis_ir(
            ir, source_index(), self.compositions, self.derived
        )
        physical_roles = {
            role: period for role, period in prepared["analysis_task"]["periods"].items()
            if role.startswith("__fact_")
        }
        self.assertEqual(set(physical_roles.values()), {"2026-04", "2026-05", "2026-06"})
        self.assertEqual(len(physical_roles), 3)
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)

    def test_user_ir_cannot_claim_internal_physical_period_role(self) -> None:
        ir = base_ir("支付GMV")
        ir["analysis_task"]["periods"]["__fact_custom"] = "2026-04"
        with self.assertRaises(PreparationError) as raised:
            prepare_analysis_ir(ir, source_index(), self.compositions, self.derived)
        self.assertEqual(raised.exception.code, "RESERVED_PERIOD_ROLE")

    def test_formula_fallback_emits_explicit_generic_unit_conversion(self) -> None:
        index = source_index()
        units = {
            "目标指标": "亿元",
            "规模因子": "万人",
            "频次因子": "次",
            "价格因子": "元",
        }
        for name, unit in units.items():
            index["metrics"][name] = {
                "unit": unit, "additive": name != "频次因子", "dimensions": ["平台"]
            }
            index["metric_bindings"][name] = name
        for name in ("规模因子", "频次因子", "价格因子"):
            index["availability"]["month"]["metrics"][name] = {"dimension": "平台"}
        ir = base_ir("目标指标")
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-06", "comparison": "2026-05"
        }
        ir["analysis_task"]["metrics"].extend([
            {"metric_id": "scale", "name": "规模因子", "metric_object": "volume", "unit": "待元信息解析"},
            {"metric_id": "frequency", "name": "频次因子", "metric_object": "volume", "unit": "待元信息解析"},
            {"metric_id": "price", "name": "价格因子", "metric_object": "volume", "unit": "待元信息解析"},
        ])
        factors = [
            {"factor_id": metric_ref, "kind": "metric", "metric_ref": metric_ref}
            for metric_ref in ("scale", "frequency", "price")
        ]
        ir["attribution_targets"] = [{
            "target_id": "formula_target",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "periods": {"analysis": "2026-06", "comparison": "2026-05"},
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "group_dimensions": [],
            "factors": factors,
            "formula": {"op": "multiply", "args": [
                {"factor_ref": item["factor_id"]} for item in factors
            ]},
        }]
        prepared, _ = prepare_analysis_ir(ir, index, self.compositions, self.derived)
        adaptations = [
            item for item in prepared["input_adaptations"]
            if item.get("rule_source") == "user_query_formula"
        ]
        self.assertEqual(len(adaptations), 2)
        self.assertAlmostEqual(adaptations[0]["unit_conversion"]["scale_factor"], 1e-4)
        self.assertEqual(
            adaptations[0]["validation"], ["facts_present", "unit_scale_verified"]
        )
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        formula_nodes = [
            node for node in plan["nodes"] if node.get("type") == "input_adaptation"
            and node.get("execution", {}).get("definition_source") == "user_query_formula"
        ]
        self.assertEqual(len(formula_nodes), 2)
        self.assertEqual(
            formula_nodes[0]["execution"]["materialize_as"]["unit_conversion"]["target_unit"],
            "亿元",
        )

    def test_formula_fallback_with_unprovable_unit_fails_closed(self) -> None:
        index = source_index()
        index["metrics"]["目标指标"] = {
            "unit": "目标专用单位", "additive": True, "dimensions": ["平台"]
        }
        index["metrics"]["输入指标"] = {
            "unit": "输入专用单位", "additive": True, "dimensions": ["平台"]
        }
        index["metric_bindings"].update({"目标指标": "目标指标", "输入指标": "输入指标"})
        index["availability"]["month"]["metrics"]["输入指标"] = {"dimension": "平台"}
        ir = base_ir("目标指标")
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-06", "comparison": "2026-05"
        }
        ir["analysis_task"]["metrics"].append({
            "metric_id": "input",
            "name": "输入指标",
            "metric_object": "volume",
            "unit": "待元信息解析",
        })
        ir["attribution_targets"] = [{
            "target_id": "formula_target",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "periods": {"analysis": "2026-06", "comparison": "2026-05"},
            "view_id": "v",
            "dimensions": {"平台": "京东"},
            "group_dimensions": [],
            "factors": [{
                "factor_id": "input", "kind": "metric", "metric_ref": "input"
            }],
            "formula": {"factor_ref": "input"},
        }]
        with self.assertRaises(PreparationError) as raised:
            prepare_analysis_ir(ir, index, self.compositions, self.derived)
        self.assertEqual(raised.exception.code, "SOURCE_PATH_UNAVAILABLE")

    def test_selected_business_intent_updates_model_inferred_metric_object(self) -> None:
        ir = base_ir("线上社零", metric_object="volume")
        ir["derived_requirements"] = [{
            "requirement_id": "growth", "metric_ref": "target",
            "metric_object": "volume",
        }]
        capabilities = {
            "intent_resolutions": {"target": {
                "selected_candidate": {
                    "candidate_id": "intent_1",
                    "intent_id": "growth_ratio_metric",
                    "metric": "实物商品网上零售额同比增速",
                    "metric_object": "ratio",
                    "object_override_allowed": True,
                },
            }},
        }
        _apply_business_intent_selection(ir, capabilities)
        metric = ir["analysis_task"]["metrics"][0]
        self.assertEqual(metric["metric_object"], "ratio")
        self.assertEqual(metric["metric_object_source"], "business_intent_policy")
        self.assertEqual(ir["derived_requirements"][0]["metric_object"], "ratio")


if __name__ == "__main__":
    unittest.main()
