from __future__ import annotations

import json
import math
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from run_analysis import compact_task_answer  # noqa: E402
from run_fast_query import (  # noqa: E402
    answer_payload,
    build_answer_basis,
    enrich_answer_basis_definitions,
    enrich_composition_display_values,
)


def fact(
    slot_id: str,
    *,
    metric_ref: str = "sales",
    metric: str = "销售额",
    source_metric_name: str = "源销售额",
    period_role: str = "analysis",
    dimensions: dict | None = None,
    requirement_refs: list[str] | None = None,
) -> dict:
    return {
        "fact_slot_id": slot_id,
        "metric_ref": metric_ref,
        "metric": metric,
        "source_metric_name": source_metric_name,
        "period_role": period_role,
        "dimensions": dimensions or {},
        "unit": "亿元",
        "definition": "含税支付金额；按类目拆解时以支付商品所属类目为准",
        "requirement_refs": requirement_refs or ["observe"],
    }


def base_manifest() -> dict:
    return {
        "status": "success",
        "analysis_ir": {
            "output_requirements": [{
                "requirement_id": "answer",
                "source_requirement_refs": ["observe"],
            }],
            "dimension_trees": [],
            "fact_observations": [{
                "requirement_id": "observe",
                "dimension_refs": [],
            }],
            "metric_compositions": [],
            "derived_requirements": [],
            "custom_calculations": [],
            "attribution_targets": [],
            "input_adaptations": [],
        },
        "analysis_task": {
            "metrics": [{"metric_id": "sales", "name": "销售额"}],
            "selector_dimensions": {},
            "operator_contracts": [],
        },
        "requirement_compilation": [{
            "requirement_id": "observe",
            "node_ids": ["fact_artifact"],
            "fact_slot_ids": ["slot_1"],
        }],
        "nodes": [{
            "node_id": "fact_artifact",
            "depends_on": [],
            "execution": {"handler": "fact_artifact"},
        }],
        "normalized_facts": [fact("slot_1")],
        "derived_results": [],
        "attribution_results": [],
    }


class AnswerBasisTests(unittest.TestCase):
    def test_final_presentation_contract_uses_non_empty_lineage_definitions(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        output_contract = (
            ROOT / "references" / "output-contract.md"
        ).read_text(encoding="utf-8")
        conclusion_rules = skill.split(
            "结论输出遵循以下通用原则：", 1
        )[1].split("- 归因结果完整展示", 1)[0]

        for token in (
            "实际成功结果血缘",
            "派生或组合结果",
            "静默省略定义文本",
            "不得从 Query、指标名、候选指标或公式文本推测定义",
        ):
            self.assertIn(token, conclusion_rules)
        self.assertNotIn("其他字段缺失时如实写“未提供”", conclusion_rules)

        for token in (
            "最终结论中的口径说明只做 `answer_basis` 的展示投影",
            "源侧预计算指标仍按事实结果处理",
            "省略定义文本",
            "不得触发额外 Resolve、Prepare、Compile、Fetch、Execute",
        ):
            self.assertIn(token, output_contract)

    def test_registered_composition_projects_scalar_display_value(self) -> None:
        manifest = base_manifest()
        manifest["derived_results"] = [{
            "derived_metric_id": "competitor_comprehensive_settlement_tr",
            "node_id": "tr",
            "status": "success",
            "definition_status": "registered",
            "definition_source": "references/metric-composition-registry.json",
            "unit": "rate",
            "value": 0.09326,
        }]
        registry = {
            "definitions": {
                "competitor_comprehensive_settlement_tr": {
                    "unit": "rate",
                    "display": {"unit": "%", "multiplier": 100},
                }
            }
        }

        answer = answer_payload(manifest, "fast_fact", registry)
        result = answer["derived_results"][0]
        self.assertEqual(result["value"], 0.09326)
        self.assertEqual(result["unit"], "rate")
        self.assertAlmostEqual(result["display_value"], 9.326)
        self.assertEqual(result["display_unit"], "%")
        self.assertNotIn("display_value", manifest["derived_results"][0])

    def test_registered_composition_projects_grouped_display_values(self) -> None:
        answer = {"derived_results": [{
            "derived_metric_id": "competitor_settlement_rate",
            "status": "partial_success",
            "definition_status": "registered",
            "definition_source": "references/metric-composition-registry.json",
            "unit": "rate",
            "value": [
                {"dimensions": {"平台": "京东"}, "value": 0.08},
                {"dimensions": {"平台": "拼多多"}, "value": 0.12},
                {"dimensions": {"平台": "缺失"}, "value": None},
            ],
        }]}
        registry = {"definitions": {"competitor_settlement_rate": {
            "unit": "rate",
            "display": {"unit": "%", "multiplier": 100},
        }}}

        enrich_composition_display_values(answer, registry)
        result = answer["derived_results"][0]
        self.assertEqual(result["display_unit"], "%")
        self.assertEqual(result["value"][0]["display_value"], 8.0)
        self.assertEqual(result["value"][1]["display_value"], 12.0)
        self.assertNotIn("display_value", result["value"][2])

    def test_composition_display_metadata_is_fail_open_and_source_scoped(self) -> None:
        base_result = {
            "derived_metric_id": "competitor_settlement_rate",
            "status": "success",
            "definition_status": "registered",
            "definition_source": "references/metric-composition-registry.json",
            "unit": "rate",
            "value": 0.1,
        }
        registry = {"definitions": {"competitor_settlement_rate": {
            "unit": "rate",
            "display": {"unit": "%", "multiplier": "100"},
        }}}
        answer = {"status": "success", "derived_results": [deepcopy(base_result)]}
        enrich_composition_display_values(answer, registry)
        self.assertNotIn("display_value", answer["derived_results"][0])
        self.assertEqual(answer["status"], "success")

        generic = deepcopy(base_result)
        generic["definition_source"] = "references/derived-metric-registry.json"
        valid_registry = {"definitions": {"competitor_settlement_rate": {
            "unit": "rate",
            "display": {"unit": "%", "multiplier": 100},
        }}}
        answer = {"derived_results": [generic]}
        enrich_composition_display_values(answer, valid_registry)
        self.assertNotIn("display_value", answer["derived_results"][0])

    def test_configured_composition_displays_are_valid(self) -> None:
        registry = json.loads(
            (ROOT / "references" / "metric-composition-registry.json").read_text(
                encoding="utf-8"
            )
        )
        configured_ids = {
            "competitor_settlement_rate",
            "competitor_ad_payment_tr",
            "competitor_ad_settlement_tr",
            "competitor_commission_payment_tr",
            "competitor_commission_settlement_tr",
            "competitor_comprehensive_payment_tr",
            "competitor_comprehensive_settlement_tr",
        }
        for composition_id in configured_ids:
            definition = registry["definitions"][composition_id]
            self.assertEqual(definition["unit"], "rate")
            self.assertEqual(definition["display"], {
                "unit": "%", "multiplier": 100,
            })
        for composition_id, definition in registry["definitions"].items():
            display = definition.get("display")
            if display is None:
                continue
            with self.subTest(composition_id=composition_id):
                self.assertIsInstance(definition.get("unit"), str)
                self.assertTrue(definition["unit"].strip())
                self.assertIsInstance(display, dict)
                self.assertIsInstance(display.get("unit"), str)
                self.assertTrue(display["unit"].strip())
                multiplier = display.get("multiplier")
                self.assertIsInstance(multiplier, (int, float))
                self.assertNotIsInstance(multiplier, bool)
                self.assertTrue(math.isfinite(multiplier))
                self.assertGreater(multiplier, 0)

    def test_direct_fact_uses_source_metadata_and_output_refs(self) -> None:
        manifest = base_manifest()
        manifest["normalized_facts"].append(fact(
            "unrelated_slot",
            metric_ref="orders",
            metric="订单量",
            source_metric_name="源订单量",
            requirement_refs=["unrelated"],
        ))
        basis = build_answer_basis(manifest)
        self.assertEqual(basis["schema_version"], "answer_basis/1.0")
        self.assertEqual(basis["metrics"], [{
            "metric": "销售额",
            "source_metric_name": "源销售额",
            "unit": "亿元",
            "definition": "含税支付金额；按类目拆解时以支付商品所属类目为准",
        }])

    def test_definition_source_is_preserved_when_present(self) -> None:
        manifest = base_manifest()
        manifest["normalized_facts"][0]["definition_source"] = "指标元信息.口径定义"
        metric = build_answer_basis(manifest)["metrics"][0]
        self.assertEqual(metric["definition_source"], "指标元信息.口径定义")

    def test_terminal_metadata_projection_only_updates_selected_metrics(self) -> None:
        answer = {"answer_basis": build_answer_basis(base_manifest())}
        enrich_answer_basis_definitions(answer, {
            "源销售额": {
                "definition": "源索引中的最新口径",
                "definition_source": "指标元信息.口径定义",
            },
            "未使用指标": {"definition": "不应展示"},
        })
        metric = answer["answer_basis"]["metrics"][0]
        self.assertEqual(metric["definition"], "源索引中的最新口径")
        self.assertNotIn("未使用指标", json.dumps(answer, ensure_ascii=False))

    def test_same_source_metric_across_periods_is_deduplicated(self) -> None:
        manifest = base_manifest()
        manifest["requirement_compilation"][0]["fact_slot_ids"].append("slot_2")
        manifest["normalized_facts"].append(fact(
            "slot_2", period_role="comparison", requirement_refs=["observe"]
        ))
        self.assertEqual(len(build_answer_basis(manifest)["metrics"]), 1)

    def test_filter_values_and_breakdown_dimension_are_compact(self) -> None:
        manifest = base_manifest()
        manifest["analysis_task"]["selector_dimensions"] = {"平台": "抖音"}
        manifest["analysis_ir"]["dimension_trees"] = [{
            "tree_id": "category_tree",
            "levels": [{"dimension_ref": "类目"}],
        }]
        manifest["analysis_ir"]["fact_observations"][0]["dimension_refs"] = ["类目"]
        manifest["normalized_facts"][0]["dimensions"] = {"平台": "抖音", "类目": "服饰"}
        self.assertEqual(build_answer_basis(manifest)["dimensions"], [
            {"dimension": "平台", "usage": "filter", "values": ["抖音"]},
            {"dimension": "类目", "usage": "breakdown"},
        ])

    def test_registered_derived_formula_is_human_readable(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"][0]["source_requirement_refs"] = ["yoy"]
        manifest["requirement_compilation"] = [{
            "requirement_id": "yoy",
            "node_ids": ["derived_yoy"],
            "fact_slot_ids": ["slot_1"],
        }]
        manifest["nodes"].append({
            "node_id": "derived_yoy",
            "depends_on": ["fact_artifact"],
            "execution": {"handler": "derived"},
        })
        manifest["derived_results"] = [{
            "node_id": "derived_yoy",
            "status": "success",
            "definition_status": "registered",
            "metric": "销售额",
            "formula": {
                "op": "subtract",
                "args": [
                    {"op": "divide", "args": [
                        {"fact": {"metric": "销售额", "period_role": "analysis"}},
                        {"fact": {"metric": "销售额", "period_role": "analysis_last_year"}},
                    ]},
                    {"literal": 1},
                ],
            },
        }]
        calculation = build_answer_basis(manifest)["calculations"][0]
        self.assertEqual(calculation["name"], "销售额同比增速")
        self.assertEqual(
            calculation["formula"],
            "销售额[分析期] / 销售额[上年同期] - 1",
        )

    def test_custom_formula_is_human_readable(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"][0]["source_requirement_refs"] = ["share"]
        manifest["requirement_compilation"] = [{
            "requirement_id": "share",
            "node_ids": ["custom_share"],
            "fact_slot_ids": ["slot_1"],
        }]
        manifest["nodes"].append({
            "node_id": "custom_share",
            "depends_on": ["fact_artifact"],
            "execution": {"handler": "derived"},
        })
        manifest["derived_results"] = [{
            "node_id": "custom_share",
            "status": "success",
            "definition_status": "custom",
            "formula": {
                "op": "multiply",
                "args": [
                    {"op": "divide", "args": [
                        {"fact": {"metric": "抖音包裹量", "period_role": "analysis"}},
                        {"fact": {"metric": "快递大盘", "period_role": "analysis"}},
                    ]},
                    {"literal": 100},
                ],
            },
        }]
        calculation = build_answer_basis(manifest)["calculations"][0]
        self.assertEqual(calculation["name"], "自定义计算")
        self.assertEqual(
            calculation["formula"],
            "抖音包裹量[分析期] / 快递大盘[分析期] × 100",
        )

    def test_week_to_month_rollup_is_collapsed_and_deduplicated(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"][0]["source_requirement_refs"] = ["monthly_yoy"]
        manifest["analysis_ir"]["input_adaptations"] = [
            {
                "requirement_id": requirement_id,
                "rollup": {
                    "calendar": "iso8601",
                    "target_period": period,
                    "components": [],
                },
            }
            for requirement_id, period in (("rollup_a", "2026-07"), ("rollup_b", "2025-07"))
        ]
        manifest["requirement_compilation"] = [
            {
                "requirement_id": "monthly_yoy",
                "node_ids": ["derived_monthly_yoy"],
                "fact_slot_ids": ["slot_1"],
            },
            {
                "requirement_id": "rollup_a",
                "node_ids": ["adapt_a"],
                "fact_slot_ids": ["slot_a"],
            },
            {
                "requirement_id": "rollup_b",
                "node_ids": ["adapt_b"],
                "fact_slot_ids": ["slot_b"],
            },
        ]
        manifest["nodes"].extend([
            {
                "node_id": "derived_monthly_yoy",
                "depends_on": ["adapt_a", "adapt_b"],
                "execution": {"handler": "derived"},
            },
            {"node_id": "adapt_a", "depends_on": [], "execution": {"handler": "derived"}},
            {"node_id": "adapt_b", "depends_on": [], "execution": {"handler": "derived"}},
        ])
        rollup_formula = {"op": "sum", "args": [{"fact": {"metric": "销售额"}}]}
        manifest["derived_results"] = [
            {"node_id": "adapt_a", "status": "success", "formula": rollup_formula},
            {"node_id": "adapt_b", "status": "success", "formula": rollup_formula},
            {
                "node_id": "derived_monthly_yoy",
                "status": "success",
                "metric": "销售额",
                "formula": {"op": "subtract", "args": [
                    {"fact": {"metric": "销售额", "period_role": "analysis"}},
                    {"fact": {"metric": "销售额", "period_role": "analysis_last_year"}},
                ]},
            },
        ]
        calculations = build_answer_basis(manifest)["calculations"]
        self.assertEqual(
            sum(item["name"] == "周上卷月" for item in calculations),
            1,
        )
        self.assertIn({
            "name": "周上卷月",
            "formula": "周上卷月 = Σ(周值 × 当周落入目标月的天数 / 7)",
        }, calculations)

    def test_attribution_uses_existing_description_and_allows_missing_description(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"][0]["source_requirement_refs"] = ["target"]
        manifest["requirement_compilation"] = [{
            "requirement_id": "target",
            "node_ids": ["attribution_target"],
            "fact_slot_ids": ["slot_1"],
        }]
        manifest["nodes"].append({
            "node_id": "attribution_target",
            "depends_on": ["fact_artifact"],
            "execution": {"handler": "attribution"},
        })
        manifest["analysis_task"]["operator_contracts"] = [{
            "operator": "dimension_contribution",
            "description": "按维度分解绝对变化贡献",
        }]
        manifest["attribution_results"] = [
            {
                "node_id": "attribution_target",
                "status": "success",
                "result": {"operator": "dimension_contribution"},
            },
            {
                "node_id": "attribution_target",
                "status": "success",
                "result": {"operator": "fallback_operator"},
            },
        ]
        self.assertEqual(build_answer_basis(manifest)["attribution"], [
            {
                "operator": "dimension_contribution",
                "description": "按维度分解绝对变化贡献",
            },
            {"operator": "fallback_operator"},
        ])

    def test_unsupported_formula_returns_null_without_changing_status(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"] = []
        manifest["derived_results"] = [{
            "node_id": "unsupported",
            "status": "success",
            "formula": {"op": "power", "args": [{"literal": 2}, {"literal": 3}]},
        }]
        basis = build_answer_basis(manifest)
        self.assertEqual(basis["calculations"], [{"name": "派生计算", "formula": None}])
        self.assertEqual(manifest["status"], "success")

    def test_compact_answer_preserves_answer_basis(self) -> None:
        answer = {
            "schema_version": "fast_query_answer/1.0",
            "status": "success",
            "answer_basis": build_answer_basis(base_manifest()),
            "views": [],
            "derived_results": [],
            "attribution_results": [],
        }
        self.assertEqual(
            compact_task_answer(answer)["answer_basis"],
            answer["answer_basis"],
        )

    def test_compact_answer_preserves_period_resolutions(self) -> None:
        resolutions = [{
            "period_role": "analysis", "label": "2026年上半年",
            "mode": "period_only", "output_grain": "month",
            "source_periods": ["2026-01", "2026-02"],
        }]
        answer = {
            "schema_version": "fast_query_answer/1.0", "status": "success",
            "period_resolutions": resolutions, "views": [],
            "derived_results": [], "attribution_results": [],
        }
        self.assertEqual(
            compact_task_answer(answer)["period_resolutions"], resolutions
        )

    def test_compact_answer_preserves_composition_display_fields(self) -> None:
        answer = {
            "schema_version": "fast_query_answer/1.0",
            "status": "success",
            "views": [],
            "derived_results": [{
                "derived_metric_id": "competitor_settlement_rate",
                "status": "success",
                "unit": "rate",
                "value": 0.1,
                "display_value": 10.0,
                "display_unit": "%",
            }],
            "attribution_results": [],
        }
        result = compact_task_answer(answer)["derived_results"][0]
        self.assertEqual(result["display_value"], 10.0)
        self.assertEqual(result["display_unit"], "%")

    def test_answer_payload_hides_internal_span_source_facts(self) -> None:
        manifest = base_manifest()
        manifest["normalized_facts"].append({
            **manifest["normalized_facts"][0],
            "fact_slot_id": "internal", "period_role": "__fact_2026_01",
            "period": "2026-01",
        })
        manifest["analysis_task"]["period_resolutions"] = [{
            "period_role": "analysis", "label": "2026年上半年",
            "start": "2026-01-01", "end": "2026-06-30",
            "mode": "sum", "target_period": "span:2026-01-01/2026-06-30",
            "source_periods": ["2026-01", "2026-02"],
            "candidate_partitions": [["internal detail"]],
        }]
        answer = answer_payload(manifest, "fast_fact")
        rows = [row for view in answer["views"] for row in view["rows"]]
        self.assertFalse(any(row["period_role"].startswith("__fact_") for row in rows))
        self.assertNotIn("candidate_partitions", answer["period_resolutions"][0])

    def test_span_rollup_formula_is_collapsed(self) -> None:
        manifest = base_manifest()
        manifest["analysis_ir"]["output_requirements"][0]["source_requirement_refs"] = ["span"]
        manifest["analysis_ir"]["input_adaptations"] = [{
            "requirement_id": "span",
            "rollup": {
                "calendar": "iso8601",
                "target_period": "span:2026-01-01/2026-06-30",
                "components": [{"period": "2026-Q1", "weight": 1.0}],
            },
        }]
        manifest["requirement_compilation"] = [{
            "requirement_id": "span", "node_ids": ["span_node"],
            "fact_slot_ids": ["slot_1"],
        }]
        manifest["nodes"].append({
            "node_id": "span_node", "depends_on": [],
            "execution": {"handler": "derived"},
        })
        manifest["derived_results"] = [{
            "node_id": "span_node", "status": "success",
            "metric": "销售额", "formula": {"literal": 1},
        }]
        self.assertEqual(
            build_answer_basis(manifest)["calculations"],
            [{"name": "区间累计", "formula": "区间累计 = Σ(完整子周期值)"}],
        )

    def test_task_basis_is_isolated_and_ordering_is_deterministic(self) -> None:
        first = base_manifest()
        second = deepcopy(first)
        second["analysis_task"]["metrics"][0]["name"] = "订单量"
        second["normalized_facts"][0].update(
            metric="订单量", source_metric_name="源订单量", definition="支付订单数"
        )
        first_basis = build_answer_basis(first)
        self.assertEqual(first_basis, build_answer_basis(first))
        self.assertEqual(first_basis["metrics"][0]["source_metric_name"], "源销售额")
        self.assertEqual(build_answer_basis(second)["metrics"][0]["source_metric_name"], "源订单量")

    def test_basis_payload_is_bounded(self) -> None:
        manifest = base_manifest()
        manifest["analysis_task"]["selector_dimensions"] = {
            "类目": [f"类目{i}" for i in range(100)]
        }
        manifest["requirement_compilation"][0]["fact_slot_ids"] = [
            f"slot_{index}" for index in range(100)
        ]
        manifest["normalized_facts"] = [
            fact(f"slot_{index}", dimensions={"类目": f"类目{index}"})
            for index in range(100)
        ]
        basis = build_answer_basis(manifest)
        self.assertEqual(len(basis["metrics"]), 1)
        self.assertEqual(len(basis["dimensions"][0]["values"]), 20)
        self.assertTrue(basis["dimensions"][0]["values_truncated"])
        self.assertLess(len(json.dumps(basis, ensure_ascii=False)), 2000)


if __name__ == "__main__":
    unittest.main()
