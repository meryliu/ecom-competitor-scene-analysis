from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compile_plan import compile_and_validate  # noqa: E402
from execution_runner import (  # noqa: E402
    EventLog,
    ExecutionError,
    FactStore,
    compute_run_status,
    execute_attribution,
    execute_plan,
    materialize_intermediate_facts,
    normalize_facts,
)


def fact(slot_id: str, value: float = 1.0) -> dict:
    return {
        "fact_slot_id": slot_id,
        "metric_ref": "payment_gmv",
        "metric": "支付GMV",
        "view_id": "platform",
        "period": "2026-05",
        "period_role": "analysis",
        "dimensions": {"平台": "京东"},
        "value": value,
        "unit": "亿元",
        "definition": "支付GMV",
        "additive": True,
        "missing": False,
        "raw_missing": False,
        "source_request_id": "r1",
        "source_ref": {"sheet": "月度", "row": 1, "column": "B", "revision": 1},
    }


class NormalizeFactsTests(unittest.TestCase):
    def test_formula_materialization_applies_verified_unit_scale(self) -> None:
        rows = []
        for metric, value, unit in (
            ("规模", 48292.0, "万人"),
            ("频次", 15.5, "次"),
            ("价格", 52.0, "元"),
        ):
            rows.append({
                "fact_id": metric,
                "metric": metric,
                "period_role": "analysis",
                "period": "2026-06",
                "view_id": "v",
                "dimensions": {},
                "value": value,
                "unit": unit,
                "missing": False,
                "source_ref": {"revision": 1},
            })
        execution = {
            "expression": {"op": "multiply", "args": [
                {"fact": {"metric": metric, "period_role": "analysis"}}
                for metric in ("规模", "频次", "价格")
            ]},
            "materialize_as": {
                "metric_ref": "target",
                "metric": "目标",
                "period_role": "analysis",
                "period": "2026-06",
                "view_id": "v",
                "unit": "亿元",
                "rule_source": "user_query_formula",
                "validation": ["facts_present", "unit_scale_verified"],
                "unit_conversion": {
                    "expected_input_units": {"规模": "万人", "频次": "次", "价格": "元"},
                    "target_unit": "亿元",
                    "scale_factor": 1e-4,
                },
            },
        }
        materialized = materialize_intermediate_facts(
            "formula", execution, 48292.0 * 15.5 * 52.0, FactStore(rows)
        )
        self.assertAlmostEqual(materialized[0]["value"], 3892.3352)

    def test_formula_materialization_rejects_runtime_unit_drift(self) -> None:
        row = {
            "fact_id": "input",
            "metric": "输入",
            "period_role": "analysis",
            "period": "2026-06",
            "view_id": "v",
            "dimensions": {},
            "value": 2.0,
            "unit": "万元",
            "missing": False,
            "source_ref": {"revision": 1},
        }
        execution = {
            "expression": {"fact": {"metric": "输入", "period_role": "analysis"}},
            "materialize_as": {
                "metric_ref": "target",
                "metric": "目标",
                "period_role": "analysis",
                "period": "2026-06",
                "view_id": "v",
                "unit": "亿元",
                "rule_source": "user_query_formula",
                "validation": ["facts_present", "unit_scale_verified"],
                "unit_conversion": {
                    "expected_input_units": {"输入": "元"},
                    "target_unit": "亿元",
                    "scale_factor": 1e-8,
                },
            },
        }
        with self.assertRaisesRegex(ExecutionError, "unit mismatch"):
            materialize_intermediate_facts("formula", execution, 2.0, FactStore([row]))

    def test_optional_partial_result_downgrades_run_status(self) -> None:
        nodes = {
            "optional_attribution": {
                "criticality": "optional",
                "status": "planned",
            }
        }
        results = {
            "optional_attribution": {
                "status": "partial_success",
                "result": {"summary": {"residual": 0.25}},
            }
        }
        self.assertEqual(compute_run_status(nodes, results), "partial_success")

    def test_core_partial_result_is_not_promoted_to_blocked(self) -> None:
        nodes = {"core_attribution": {"criticality": "core", "status": "planned"}}
        results = {"core_attribution": {"status": "partial_success"}}
        self.assertEqual(compute_run_status(nodes, results), "partial_success")

    def test_residual_failure_preserves_attribution_result_as_partial(self) -> None:
        engine_result = {
            "ok": True,
            "operator": "multiplicative_change",
            "summary": {"residual": 0.25},
            "rows": [{"name": "factor_a", "contribution_value": 1.0}],
            "warnings": [],
            "boundary_cases": [],
        }
        result = execute_attribution(
            {
                "mode": "none",
                "items": [{"parent": None, "payload": {"test": True}}],
            },
            "multiplicative_change",
            lambda payload, explain_routing: engine_result,
            1,
            1e-8,
            "node_a",
            EventLog(None),
        )
        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(result["result"], engine_result)
        self.assertIn("exceeds tolerance", result["warnings"][0])

    def test_zero_contribution_literal_factor_remains_in_formula_result(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "query": "attribute a generic product formula",
                "analysis_goal": "return every factor contribution",
                "metrics": [
                    {"metric_id": metric_id, "name": metric_id, "metric_object": "volume", "unit": "u"}
                    for metric_id in ("target", "input_a", "input_b")
                ],
                "periods": {"analysis": "2026-05", "comparison": "2025-05"},
                "scope": "entity_x",
                "filters": [],
            },
            "views": [{"view_id": "entity_view"}],
            "dimension_trees": [],
            "input_adaptations": [],
            "fact_observations": [],
            "metric_compositions": [],
            "derived_requirements": [],
            "custom_calculations": [],
            "attribution_targets": [{
                "target_id": "formula_target",
                "metric_ref": "target",
                "metric_object": "volume",
                "scenario": "metric_change",
                "target_semantics": "absolute_delta",
                "decomposition": "formula",
                "view_id": "entity_view",
                "dimensions": {"entity": "entity_x"},
                "factors": [
                    {"factor_id": "factor_a", "metric_ref": "input_a"},
                    {"factor_id": "factor_b", "metric_ref": "input_b"},
                    {
                        "factor_id": "constant_factor",
                        "kind": "literal",
                        "name": "constant_factor",
                        "values_by_period_role": {"analysis": 2.0, "comparison": 2.0},
                    },
                ],
                "formula": {
                    "op": "multiply",
                    "args": [
                        {"factor_ref": "factor_a"},
                        {"factor_ref": "factor_b"},
                        {"factor_ref": "constant_factor"},
                    ],
                },
                "criticality": "required",
            }],
            "output_requirements": [],
            "clarifications": [],
        }
        plan, report = compile_and_validate(
            ir,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        values = {
            ("target", "analysis"): 12.0,
            ("target", "comparison"): 4.0,
            ("input_a", "analysis"): 2.0,
            ("input_a", "comparison"): 1.0,
            ("input_b", "analysis"): 3.0,
            ("input_b", "comparison"): 2.0,
        }
        rows = []
        for slot in plan["fetch_requests"][0]["fact_slots"]:
            rows.append({
                "fact_id": f"row_{slot['fact_slot_id']}",
                "fact_slot_id": slot["fact_slot_id"],
                "metric_ref": slot["metric_ref"],
                "metric": slot["metric"],
                "metric_object": "volume",
                "view_id": slot["view_id"],
                "period": slot["period"],
                "period_role": slot["period_role"],
                "dimensions": {"entity": "entity_x"},
                "value": values[(slot["metric_ref"], slot["period_role"])],
                "unit": "u",
                "definition": slot["metric"],
                "additive": True,
                "missing": False,
                "raw_missing": False,
                "source_request_id": "request_1",
                "source_ref": {"revision": 1},
            })
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = execute_plan(
                plan, rows, root / "manifest.json", root / "events.jsonl"
            )
        self.assertEqual(manifest["status"], "success")
        result = manifest["attribution_results"][0]["result"]
        self.assertEqual(len(result["rows"]), 3)
        constant = next(row for row in result["rows"] if row["name"] == "constant_factor")
        self.assertEqual(constant["analysis_value"], constant["comparison_value"])
        self.assertAlmostEqual(constant["contribution_value"], 0.0)

    def test_identical_fact_from_multiple_slots_is_deduplicated(self) -> None:
        rows = normalize_facts(
            [fact("share_slot"), fact("attribution_slot")],
            {"analysis": "2026-05"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fact_slot_ids"], ["attribution_slot", "share_slot"])

    def test_conflicting_duplicate_fact_is_rejected(self) -> None:
        with self.assertRaises(ExecutionError):
            normalize_facts(
                [fact("share_slot", 1.0), fact("attribution_slot", 2.0)],
                {"analysis": "2026-05"},
            )

    def test_collection_aggregate_uses_resolved_domain(self) -> None:
        jd = fact("slot", 2.0)
        pdd = {**fact("slot", 3.0), "fact_id": "pdd", "dimensions": {"平台": "拼多多"}}
        store = FactStore(
            normalize_facts([jd, pdd], {"analysis": "2026-05"}),
            {"domain_test": {"dimension": "平台", "members": ["京东", "拼多多"]}},
        )
        self.assertEqual(
            store.aggregate_value(
                {"metric": "支付GMV", "view_id": "platform", "period_role": "analysis"},
                "domain_test",
            ),
            5.0,
        )

    def test_collection_aggregate_rejects_non_additive_source_metric(self) -> None:
        row = fact("slot", 2.0)
        row["additive"] = False
        store = FactStore(
            normalize_facts([row], {"analysis": "2026-05"}),
            {"domain_test": {"dimension": "平台", "members": ["京东"]}},
        )
        with self.assertRaises(ExecutionError):
            store.aggregate_value(
                {"metric": "支付GMV", "view_id": "platform", "period_role": "analysis"},
                "domain_test",
            )

    def test_selected_set_share_executes_without_business_name_in_executor(self) -> None:
        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "query": "TOP6支付GMV市占率",
                "analysis_goal": "逐平台返回集合内占比",
                "metrics": [{"metric_id": "payment", "name": "支付GMV", "metric_object": "volume", "unit": "亿元"}],
                "periods": {"analysis": "2026-05"},
                "scope": "TOP6平台",
                "filters": [],
            },
            "views": [{"view_id": "platform"}],
            "dimension_trees": [],
            "fact_observations": [],
            "metric_compositions": [],
            "derived_requirements": [{
                "requirement_id": "share",
                "metric_ref": "payment",
                "derived_metric_id": "selected_set_share",
                "definition_status": "registered",
                "view_id": "platform",
                "dimension_refs": ["TOP6平台"],
                "dimensions": {},
            }],
            "custom_calculations": [],
            "attribution_targets": [],
            "output_requirements": [],
            "clarifications": [],
        }
        plan, report = compile_and_validate(
            ir,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        expression = next(
            node["execution"]["expression"]
            for node in plan["nodes"]
            if node["node_id"].startswith("derived_share")
        )
        domain_ref = expression["denominator_domain_ref"]
        platforms = ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"]
        plan["resolved_dimension_domains"] = {
            domain_ref: {"dimension": "TOP6平台", "members": platforms}
        }
        rows = []
        for index, platform in enumerate(platforms, start=1):
            row = fact("share", float(index))
            row.update({
                "fact_id": f"fact_{index}",
                "dimensions": {"TOP6平台": platform},
            })
            rows.append(row)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = execute_plan(
                plan,
                rows,
                root / "manifest.json",
                root / "events.jsonl",
            )
        result = next(
            item["result"] for item in manifest["node_results"]
            if item["node_id"].startswith("derived_share")
        )
        self.assertEqual(len(result), 6)
        by_platform = {item["dimensions"]["TOP6平台"]: item["value"] for item in result}
        self.assertAlmostEqual(by_platform["淘系"], 1 / 21)

    def test_adapted_quarter_facts_feed_share_and_attribution(self) -> None:
        periods = {
            "analysis": "2026-Q2",
            "comparison": "2025-Q2",
            "a_apr": "2026-04",
            "a_may": "2026-05",
            "a_jun": "2026-06",
            "c_apr": "2025-04",
            "c_may": "2025-05",
            "c_jun": "2025-06",
        }
        domain = ["京东", "拼多多"]

        def adaptation(requirement_id: str, target_role: str, source_roles: list[str]) -> dict:
            return {
                "requirement_id": requirement_id,
                "metric_ref": "settlement",
                "target_period_role": target_role,
                "view_id": "platform",
                "dimension_refs": ["平台"],
                "dimensions": {"平台": domain},
                "expression": {
                    "op": "sum",
                    "args": [
                        {"fact": {"metric_ref": "settlement", "period_role": role}}
                        for role in source_roles
                    ],
                },
                "rule_source": "feishu_metric_metadata",
                "validation": ["facts_present", "unit_consistent", "metric_additive"],
                "criticality": "core",
            }

        ir = {
            "ir_version": "analysis_ir/1.0",
            "analysis_task": {
                "query": "季度市占率和同比贡献",
                "analysis_goal": "返回季度市占率和贡献",
                "metrics": [{
                    "metric_id": "settlement",
                    "name": "结算GMV",
                    "metric_object": "volume",
                    "unit": "亿元",
                }],
                "periods": periods,
                "scope": "选择平台",
                "filters": [],
            },
            "views": [{"view_id": "platform"}],
            "dimension_trees": [],
            "input_adaptations": [
                adaptation("current_quarter", "analysis", ["a_apr", "a_may", "a_jun"]),
                adaptation("previous_quarter", "comparison", ["c_apr", "c_may", "c_jun"]),
            ],
            "fact_observations": [],
            "metric_compositions": [],
            "derived_requirements": [{
                "requirement_id": "quarter_share",
                "metric_ref": "settlement",
                "derived_metric_id": "selected_set_share",
                "definition_status": "registered",
                "view_id": "platform",
                "dimension_refs": ["平台"],
                "dimensions": {"平台": domain},
                "criticality": "core",
            }],
            "custom_calculations": [],
            "attribution_targets": [{
                "target_id": "quarter_contribution",
                "metric_ref": "settlement",
                "metric_object": "volume",
                "scenario": "metric_change",
                "target_semantics": "absolute_delta",
                "decomposition": "dimension",
                "periods": {"analysis": "2026-Q2", "comparison": "2025-Q2"},
                "view_id": "platform",
                "group_dimensions": ["平台"],
                "criticality": "required",
            }],
            "output_requirements": [],
            "clarifications": [],
        }
        plan, report = compile_and_validate(
            ir,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        share_node = next(node for node in plan["nodes"] if node["node_id"].startswith("derived_quarter_share"))
        domain_ref = share_node["execution"]["expression"]["denominator_domain_ref"]
        plan["resolved_dimension_domains"] = {
            domain_ref: {"dimension": "平台", "members": domain}
        }
        values = {
            "京东": {"a_apr": 10, "a_may": 20, "a_jun": 30, "c_apr": 8, "c_may": 16, "c_jun": 24},
            "拼多多": {"a_apr": 20, "a_may": 30, "a_jun": 40, "c_apr": 15, "c_may": 25, "c_jun": 35},
        }
        rows = []
        for slot in plan["fetch_requests"][0]["fact_slots"]:
            for platform in domain:
                row = fact(slot["fact_slot_id"], float(values[platform][slot["period_role"]]))
                row.update({
                    "fact_id": f"{slot['fact_slot_id']}_{platform}",
                    "metric_ref": "settlement",
                    "metric": "结算GMV",
                    "period": slot["period"],
                    "period_role": slot["period_role"],
                    "dimensions": {"平台": platform},
                    "additive": True,
                })
                rows.append(row)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = execute_plan(plan, rows, root / "manifest.json", root / "events.jsonl")
        self.assertEqual(manifest["status"], "success")
        share_result = next(
            item["result"] for item in manifest["node_results"]
            if item["node_id"] == share_node["node_id"]
        )
        shares = {item["dimensions"]["平台"]: item["value"] for item in share_result}
        self.assertAlmostEqual(shares["京东"], 0.4)
        self.assertAlmostEqual(shares["拼多多"], 0.6)
        attribution = next(
            item for item in manifest["attribution_results"]
            if item["node_id"].startswith("attribution_quarter_contribution")
        )
        self.assertEqual(attribution["status"], "success")
        self.assertEqual(attribution["unit"], "亿元")
        result_rows = attribution["result"]["rows"]
        contributions = {item["name"]: item["contribution_value"] for item in result_rows}
        self.assertEqual(contributions, {"京东": 12.0, "拼多多": 15.0})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rejected = execute_plan(
                plan,
                [dict(row, additive=False) for row in rows],
                root / "manifest.json",
                root / "events.jsonl",
            )
        self.assertEqual(rejected["status"], "blocked")
        self.assertEqual(len(rejected["normalized_facts"]), len(rows))
        failed_adaptations = [
            item for item in rejected["node_results"]
            if item["node_id"].startswith("adaptation_") and item["status"] == "failed"
        ]
        self.assertEqual(len(failed_adaptations), 2)


if __name__ == "__main__":
    unittest.main()
