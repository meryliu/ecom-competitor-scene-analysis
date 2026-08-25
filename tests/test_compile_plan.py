from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compile_plan import CompileError, compile_and_validate  # noqa: E402
from validate_execution import Validator  # noqa: E402


REGISTRY = ROOT / "references" / "derived-metric-registry.json"
COMPOSITIONS = ROOT / "references" / "metric-composition-registry.json"


def base_ir() -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "test",
            "analysis_goal": "test",
            "metrics": [
                {"metric_id": "payment", "name": "支付GMV", "metric_object": "volume", "unit": "亿元"},
                {"metric_id": "settlement", "name": "结算GMV", "metric_object": "volume", "unit": "亿元"},
                {"metric_id": "rate", "name": "竞品结算率", "metric_object": "ratio", "unit": "rate"},
            ],
            "periods": {"analysis": "2026-05", "analysis_last_year": "2025-05"},
            "scope": "TOP6平台",
            "filters": [],
        },
        "views": [{"view_id": "platform_view"}],
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


class CompilePlanTests(unittest.TestCase):
    def compile(self, ir: dict) -> tuple[dict, dict]:
        return compile_and_validate(ir, REGISTRY, COMPOSITIONS)

    def test_provider_request_is_structured(self) -> None:
        ir = base_ir()
        ir["fact_observations"] = [{
            "requirement_id": "payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        request = plan["fetch_requests"][0]
        self.assertNotIn("provider", request)
        self.assertNotIn("provider_contract", request)
        self.assertNotIn("source_id", request)
        self.assertEqual(len(request["fact_demands"]), 1)
        self.assertNotIn("natural_language_query", request)

    def test_derived_metric_object_cannot_override_metric_declaration(self) -> None:
        ir = base_ir()
        ir["derived_requirements"] = [{
            "requirement_id": "bad_yoy",
            "metric_ref": "rate",
            "metric_object": "volume",
            "derived_metric_id": "yoy_growth",
            "definition_status": "registered",
            "required_period_roles": ["analysis", "analysis_last_year"],
            "view_id": "platform_view",
        }]
        with self.assertRaisesRegex(ValueError, "metric_object conflicts"):
            self.compile(ir)

    def test_source_precomputed_derived_compiles_as_passthrough_fact(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"].append({
            "metric_id": "rate_yoy_source",
            "name": "竞品结算率::yoy_growth",
            "source_metric_name": "竞品结算率同比增速",
            "metric_object": "ratio",
            "unit": "%",
        })
        ir["canonical_fact_selectors"] = [{
            "metric_ref": "rate_yoy_source",
            "period_role": "analysis",
            "period": "2026-05",
            "grain": "month",
            "selector_dimensions": {},
            "source_metric_name": "竞品结算率同比增速",
            "capability_path": "direct_fact",
        }]
        ir["derived_requirements"] = [{
            "requirement_id": "rate_yoy",
            "metric_ref": "rate",
            "metric_object": "ratio",
            "derived_metric_id": "yoy_growth",
            "definition_status": "registered",
            "fulfillment_mode": "source_derived_fact",
            "source_metric_ref": "rate_yoy_source",
            "source_period_role": "analysis",
            "view_id": "platform_view",
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(item for item in plan["nodes"] if item["type"] == "derived_metric")
        self.assertEqual(node["execution"]["definition_status"], "source_precomputed")
        self.assertEqual(plan["fetch_requests"][0]["fact_demands"][0]["source_metric_name"], "竞品结算率同比增速")

    def test_composition_metric_object_must_match_registry(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"][2]["metric_object"] = "volume"
        ir["metric_compositions"] = [{
            "requirement_id": "bad_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
        }]
        with self.assertRaisesRegex(ValueError, "metric_object conflicts"):
            self.compile(ir)

    def test_attribution_metric_object_must_match_metric_declaration(self) -> None:
        ir = base_ir()
        ir["attribution_targets"] = [{
            "target_id": "bad_target",
            "metric_ref": "rate",
            "metric_object": "volume",
            "scenario": "metric_change",
            "decomposition": "dimension",
            "periods": {"analysis": "2026-05", "comparison": "2025-05"},
            "group_dimensions": ["平台"],
            "view_id": "platform_view",
        }]
        with self.assertRaisesRegex(ValueError, "metric_object conflicts"):
            self.compile(ir)

    def test_scalar_selector_implies_physical_dimension(self) -> None:
        ir = base_ir()
        ir["fact_observations"] = [{
            "requirement_id": "jd_payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": [],
            "dimensions": {"平台": "京东"},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        slot = plan["fetch_requests"][0]["fact_slots"][0]
        self.assertEqual(slot["selector_dimensions"], {"平台": "京东"})
        self.assertEqual(slot["dimension_refs"], ["平台"])

    def test_metric_scoped_source_dimension_does_not_replace_logical_dimension(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"][0].update({
            "source_metric_name": "支付GMV",
            "source_dimension_bindings": {"平台": "TOP6平台"},
        })
        ir["fact_observations"] = [{
            "requirement_id": "payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
            "dimensions": {"平台": "京东"},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        slot = plan["fetch_requests"][0]["fact_slots"][0]
        self.assertEqual(slot["dimension_refs"], ["平台"])
        self.assertEqual(slot["source_dimension_refs"], ["TOP6平台"])
        demand = plan["fetch_requests"][0]["fact_demands"][0]
        self.assertEqual(demand["dimension_refs"], ["TOP6平台"])
        self.assertEqual(demand["selector_dimensions"], {"TOP6平台": ["京东"]})

    def test_scalar_dimension_is_propagated_for_derived_and_composition(self) -> None:
        ir = base_ir()
        ir["derived_requirements"] = [{
            "requirement_id": "pdd_settlement_yoy",
            "metric_ref": "settlement",
            "derived_metric_id": "yoy_growth",
            "definition_status": "registered",
            "view_id": "platform_view",
            "dimension_refs": [],
            "dimensions": {"平台": "拼多多"},
        }]
        ir["metric_compositions"] = [{
            "requirement_id": "jd_settlement_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": [],
            "dimensions": {"平台": "京东"},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        slots = plan["fetch_requests"][0]["fact_slots"]
        self.assertTrue(all(slot["dimension_refs"] == ["平台"] for slot in slots))

    def test_formula_factors_are_complete_stable_and_dimension_bound(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [
            {"metric_id": "target", "name": "target_metric", "metric_object": "volume", "unit": "u"},
            {"metric_id": "input_a", "name": "input_metric_a", "metric_object": "volume", "unit": "u"},
            {"metric_id": "input_b", "name": "input_metric_b", "metric_object": "volume", "unit": "u"},
        ]
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "comparison": "2025-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "formula_target",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "dimensions": {"entity": "entity_x"},
            "factors": [
                {"factor_id": "factor_a", "kind": "metric", "metric_ref": "input_a"},
                {"factor_id": "factor_b", "kind": "metric", "metric_ref": "input_b"},
                {
                    "factor_id": "factor_c",
                    "kind": "literal",
                    "values_by_period_role": {"analysis": 2.0, "comparison": 1.0},
                },
            ],
            "formula": {
                "op": "multiply",
                "args": [
                    {"factor_ref": "factor_a"},
                    {"factor_ref": "factor_b"},
                    {"factor_ref": "factor_c"},
                ],
            },
            "criticality": "required",
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(node for node in plan["nodes"] if node["type"] == "formula_attribution")
        binding = node["execution"]["binding"]
        self.assertEqual(node["execution"]["operator"], "multiplicative_change")
        self.assertEqual(binding["factor_order"], ["factor_a", "factor_b", "factor_c"])
        self.assertEqual(
            [factor["factor_id"] for factor in binding["factors"]],
            binding["factor_order"],
        )
        self.assertEqual(
            binding["factors"][2]["values_by_period_role"],
            {"analysis": 2.0, "comparison": 1.0},
        )
        slots = plan["fetch_requests"][0]["fact_slots"]
        self.assertTrue(all(slot["selector_dimensions"] == {"entity": "entity_x"} for slot in slots))
        self.assertTrue(all("entity" in slot["dimension_refs"] for slot in slots))

    def test_yoy_division_formula_selects_scenario_specific_operator(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [
            {"metric_id": metric_id, "name": metric_id, "metric_object": "volume", "unit": "u"}
            for metric_id in ("target", "input_a", "input_b", "input_c")
        ]
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "analysis_last_year": "2025-05",
            "comparison": "2026-04",
            "comparison_last_year": "2025-04",
        }
        ir["attribution_targets"] = [{
            "target_id": "trend_formula",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "yoy_trend_change",
            "target_semantics": "relative_yoy_trend",
            "decomposition": "formula",
            "view_id": "platform_view",
            "factors": [
                {"factor_id": "a", "metric_ref": "input_a"},
                {"factor_id": "b", "metric_ref": "input_b"},
                {"factor_id": "c", "metric_ref": "input_c", "role": "denominator"},
            ],
            "formula": {
                "op": "divide",
                "args": [
                    {"op": "multiply", "args": [{"factor_ref": "a"}, {"factor_ref": "b"}]},
                    {"factor_ref": "c"},
                ],
            },
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(node for node in plan["nodes"] if node["type"] == "formula_attribution")
        self.assertEqual(node["execution"]["operator"], "division_yoy_trend")
        self.assertEqual(
            [factor["role"] for factor in node["execution"]["binding"]["factors"]],
            ["multiplier", "multiplier", "denominator"],
        )

    def test_derived_formula_factor_compiles_period_role_expressions(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [
            {"metric_id": metric_id, "name": metric_id, "metric_object": "volume", "unit": "u"}
            for metric_id in ("target", "input_a", "input_b")
        ]
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "comparison": "2025-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "derived_formula",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "dimensions": {"entity": "entity_x"},
            "factors": [
                {"factor_id": "a", "metric_ref": "input_a"},
                {
                    "factor_id": "b",
                    "kind": "derived",
                    "expressions_by_period_role": {
                        role: {
                            "op": "divide",
                            "args": [
                                {"fact": {"metric_ref": "input_b", "period_role": role}},
                                {"literal": 2},
                            ],
                        }
                        for role in ("analysis", "comparison")
                    },
                },
            ],
            "formula": {
                "op": "multiply",
                "args": [{"factor_ref": "a"}, {"factor_ref": "b"}],
            },
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(node for node in plan["nodes"] if node["type"] == "formula_attribution")
        derived = node["execution"]["binding"]["factors"][1]
        self.assertEqual(set(derived["expressions_by_period_role"]), {"analysis", "comparison"})
        slots = plan["fetch_requests"][0]["fact_slots"]
        self.assertTrue(any(slot["metric_ref"] == "input_b" for slot in slots))
        self.assertTrue(all(slot["selector_dimensions"] == {"entity": "entity_x"} for slot in slots))

    def test_derived_formula_leaf_dimensions_extend_target_dimensions(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [
            {"metric_id": metric_id, "name": metric_id, "metric_object": "volume", "unit": "u"}
            for metric_id in ("target", "input_a", "input_b")
        ]
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-05",
            "comparison": "2025-05",
        }
        ir["attribution_targets"] = [{
            "target_id": "derived_formula",
            "metric_ref": "target",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "dimensions": {"entity": "entity_x"},
            "factors": [
                {"factor_id": "a", "metric_ref": "input_a"},
                {
                    "factor_id": "b",
                    "kind": "derived",
                    "expressions_by_period_role": {
                        role: {
                            "fact": {
                                "metric_ref": "input_b",
                                "period_role": role,
                                "dimensions": {"segment": "segment_y"},
                            }
                        }
                        for role in ("analysis", "comparison")
                    },
                },
            ],
            "formula": {
                "op": "multiply",
                "args": [{"factor_ref": "a"}, {"factor_ref": "b"}],
            },
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        derived_slots = [
            slot
            for request in plan["fetch_requests"]
            for slot in request["fact_slots"]
            if slot["metric_ref"] == "input_b"
        ]
        self.assertTrue(derived_slots)
        self.assertTrue(all(
            slot["selector_dimensions"]
            == {"entity": "entity_x", "segment": "segment_y"}
            for slot in derived_slots
        ))

    def test_derived_formula_leaf_dimension_conflict_is_blocked(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-04"
        ir["attribution_targets"] = [{
            "target_id": "derived_formula",
            "metric_ref": "payment",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "dimensions": {"entity": "entity_x"},
            "factors": [{
                "factor_id": "a",
                "kind": "derived",
                "expressions_by_period_role": {
                    role: {
                        "fact": {
                            "metric_ref": "payment",
                            "period_role": role,
                            "dimensions": {"entity": "entity_z"},
                        }
                    }
                    for role in ("analysis", "comparison")
                },
            }],
            "formula": {"factor_ref": "a"},
        }]
        with self.assertRaisesRegex(ValueError, "conflicts with target dimensions"):
            self.compile(ir)

    def test_formula_factor_set_must_match_ast(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-04"
        ir["attribution_targets"] = [{
            "target_id": "invalid_formula",
            "metric_ref": "payment",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "factors": [
                {"factor_id": "a", "metric_ref": "payment"},
                {"factor_id": "b", "metric_ref": "settlement"},
            ],
            "formula": {"factor_ref": "a"},
        }]
        with self.assertRaisesRegex(ValueError, "factor set"):
            self.compile(ir)

    def test_supplied_binding_cannot_add_an_untracked_factor(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-04"
        ir["attribution_targets"] = [{
            "target_id": "binding_mismatch",
            "metric_ref": "payment",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "factors": [{"factor_id": "a", "metric_ref": "payment"}],
            "formula": {"factor_ref": "a"},
            "binding": {
                "scenario": "metric_change",
                "metric_object": "volume",
                "decomposition": "multiplication",
                "periods": {"analysis": "2026-05", "comparison": "2025-04"},
                "factors": [
                    {"factor_id": "a", "name": "payment", "literal": 1},
                    {"factor_id": "extra", "name": "extra", "literal": 1},
                ],
            },
        }]
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.compile(ir)

    def test_supplied_binding_cannot_swap_factor_selectors(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-04"
        ir["attribution_targets"] = [{
            "target_id": "binding_mismatch",
            "metric_ref": "payment",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "factors": [
                {"factor_id": "a", "metric_ref": "payment"},
                {"factor_id": "b", "metric_ref": "settlement"},
            ],
            "formula": {
                "op": "multiply",
                "args": [{"factor_ref": "a"}, {"factor_ref": "b"}],
            },
            "binding": {
                "factors": [
                    {
                        "factor_id": "a",
                        "selector": {
                            "metric": "结算GMV",
                            "view_id": "platform_view",
                            "dimensions": {},
                            "dimensions_exact": True,
                        },
                    },
                    {
                        "factor_id": "b",
                        "selector": {
                            "metric": "支付GMV",
                            "view_id": "platform_view",
                            "dimensions": {},
                            "dimensions_exact": True,
                        },
                    },
                ],
            },
        }]
        with self.assertRaisesRegex(ValueError, "source must match target factor"):
            self.compile(ir)

    def test_matching_supplied_binding_is_rebuilt_canonically(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"]["comparison"] = "2025-04"
        target = {
            "target_id": "binding_match",
            "metric_ref": "payment",
            "metric_object": "volume",
            "scenario": "metric_change",
            "target_semantics": "absolute_delta",
            "decomposition": "formula",
            "view_id": "platform_view",
            "factors": [
                {"factor_id": "a", "metric_ref": "payment"},
                {"factor_id": "b", "metric_ref": "settlement"},
            ],
            "formula": {
                "op": "multiply",
                "args": [{"factor_ref": "a"}, {"factor_ref": "b"}],
            },
        }
        ir["attribution_targets"] = [target]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        canonical = next(
            node["execution"]["binding"]
            for node in plan["nodes"]
            if node["type"] == "formula_attribution"
        )

        target["binding"] = canonical
        rebuilt_plan, rebuilt_report = self.compile(ir)
        self.assertTrue(rebuilt_report["valid"], rebuilt_report)
        rebuilt = next(
            node["execution"]["binding"]
            for node in rebuilt_plan["nodes"]
            if node["type"] == "formula_attribution"
        )
        self.assertEqual(rebuilt, canonical)

    def test_ir_cannot_override_residual_tolerance(self) -> None:
        ir = base_ir()
        ir["runtime"] = {"residual_tolerance": 1.0}
        with self.assertRaisesRegex(ValueError, "runner-owned"):
            self.compile(ir)

    def test_plan_validation_rejects_selector_outside_physical_grain(self) -> None:
        ir = base_ir()
        ir["fact_observations"] = [{
            "requirement_id": "jd_payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimensions": {"平台": "京东"},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        plan["fetch_requests"][0]["fact_slots"][0]["dimension_refs"] = []
        invalid = Validator(plan, "plan", ROOT).validate()
        self.assertIn("FETCH-023", {item["rule_id"] for item in invalid["issues"]})

    def test_output_requirement_resolves_fact_observation_to_fact_artifact(self) -> None:
        ir = base_ir()
        ir["fact_observations"] = [{
            "requirement_id": "payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
        }]
        ir["output_requirements"] = [{
            "requirement_id": "payment_answer",
            "source_requirement_refs": ["payment_fact"],
            "criticality": "core",
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        conclusion = next(
            node for node in plan["nodes"]
            if node["node_id"] == "conclusion_organization"
        )
        self.assertEqual(conclusion["depends_on"], ["fact_artifact"])

    def test_output_requirement_resolves_fact_and_derived_producers(self) -> None:
        ir = base_ir()
        ir["fact_observations"] = [{
            "requirement_id": "settlement_fact",
            "metric_ref": "settlement",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
        }]
        ir["derived_requirements"] = [{
            "requirement_id": "settlement_yoy",
            "metric_ref": "settlement",
            "derived_metric_id": "yoy_growth",
            "definition_status": "registered",
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
        }]
        ir["output_requirements"] = [{
            "requirement_id": "settlement_answer",
            "source_requirement_refs": ["settlement_fact", "settlement_yoy"],
            "criticality": "core",
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        derived = next(
            node for node in plan["nodes"]
            if node["node_id"].startswith("derived_settlement_yoy")
        )
        conclusion = next(
            node for node in plan["nodes"]
            if node["node_id"] == "conclusion_organization"
        )
        self.assertEqual(
            conclusion["depends_on"],
            sorted(["fact_artifact", derived["node_id"]]),
        )

    def test_settlement_rate_composition_fetches_only_components(self) -> None:
        ir = base_ir()
        ir["metric_compositions"] = [{
            "requirement_id": "settlement_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        metrics = {slot["metric"] for slot in plan["analysis_task"]["fact_requirements"]}
        self.assertEqual(metrics, {"支付GMV", "结算GMV"})
        self.assertNotIn("provider", plan["fetch_requests"][0])
        node = next(node for node in plan["nodes"] if node["node_id"].startswith("composition_settlement_rate"))
        facts = node["execution"]["expression"]["args"]
        self.assertEqual([item["fact"]["dimensions"] for item in facts], [{}, {}])

    def test_composition_inputs_are_auto_declared(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [
            {"metric_id": "rate", "name": "结算率", "metric_object": "ratio", "unit": "rate"}
        ]
        ir["metric_compositions"] = [{
            "requirement_id": "settlement_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        self.assertEqual(
            {slot["metric"] for slot in plan["fetch_requests"][0]["fact_slots"]},
            {"支付GMV", "结算GMV"},
        )

    def test_composition_uses_registered_output_unit_over_placeholder(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [{
            "metric_id": "rate",
            "name": "结算率",
            "metric_object": "ratio",
            "unit": "待元信息解析",
        }]
        ir["metric_compositions"] = [{
            "requirement_id": "settlement_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["平台"],
            "dimensions": {"平台": ["京东", "拼多多"]},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(
            node for node in plan["nodes"]
            if node["node_id"].startswith("composition_settlement_rate")
        )
        self.assertEqual(node["execution"]["unit"], "rate")

    def test_comprehensive_tr_compiles_nested_ast_and_reuses_denominator_slot(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [{
            "metric_id": "tr", "name": "综合支付TR",
            "metric_object": "ratio", "unit": "rate",
        }]
        ir["metric_compositions"] = [{
            "requirement_id": "comprehensive_tr",
            "metric_ref": "tr",
            "composition_id": "competitor_comprehensive_payment_tr",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(
            item for item in plan["nodes"]
            if item["node_id"].startswith("composition_comprehensive_tr")
        )
        expression = node["execution"]["expression"]
        self.assertEqual(expression["op"], "add")
        self.assertEqual([item["op"] for item in expression["args"]], ["divide", "divide"])
        self.assertEqual(
            [item["fact"]["metric"] for item in expression["args"][0]["args"]],
            ["闭环电商广告收入", "支付GMV"],
        )
        self.assertEqual(
            [item["fact"]["metric"] for item in expression["args"][1]["args"]],
            ["闭环电商佣金收入", "支付GMV"],
        )
        self.assertEqual(len(plan["fetch_requests"][0]["fact_slots"]), 3)
        self.assertEqual(
            sum(
                slot["metric"] == "支付GMV"
                for slot in plan["fetch_requests"][0]["fact_slots"]
            ),
            1,
        )

    def test_composition_ast_rejects_unknown_and_unused_input_roles(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"] = [{
            "metric_id": "tr", "name": "测试TR",
            "metric_object": "ratio", "unit": "rate",
        }]
        ir["metric_compositions"] = [{
            "requirement_id": "bad_tr", "metric_ref": "tr",
            "composition_id": "bad_tr", "period_roles": ["analysis"],
            "view_id": "platform_view", "dimension_refs": [], "dimensions": {},
        }]
        invalid_definitions = [
            {
                "inputs": [{"role": "declared", "metric": "支付GMV"}],
                "expression": {"input_role": "missing"},
            },
            {
                "inputs": [
                    {"role": "used", "metric": "支付GMV"},
                    {"role": "unused", "metric": "结算GMV"},
                ],
                "expression": {"input_role": "used"},
            },
        ]
        for definition in invalid_definitions:
            definition.update({
                "definition_version": "1.0.0", "metric_object": "ratio", "unit": "rate"
            })
            registry = {
                "registry_name": "test", "registry_version": "1.0.0",
                "definitions": {"bad_tr": definition},
            }
            with self.subTest(definition=definition), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "compositions.json"
                path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(CompileError):
                    compile_and_validate(ir, REGISTRY, path)

    def test_legacy_two_input_divide_composition_remains_supported(self) -> None:
        ir = base_ir()
        ir["metric_compositions"] = [{
            "requirement_id": "legacy_rate", "metric_ref": "rate",
            "composition_id": "legacy_rate", "period_roles": ["analysis"],
            "view_id": "platform_view", "dimension_refs": [], "dimensions": {},
        }]
        registry = {
            "registry_name": "test", "registry_version": "1.0.0",
            "definitions": {"legacy_rate": {
                "definition_version": "1.0.0", "metric_object": "ratio",
                "operator": "divide",
                "inputs": [
                    {"role": "numerator", "metric_ref": "settlement"},
                    {"role": "denominator", "metric_ref": "payment"},
                ],
                "unit": "rate",
            }},
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compositions.json"
            path.write_text(json.dumps(registry, ensure_ascii=False), encoding="utf-8")
            plan, report = compile_and_validate(ir, REGISTRY, path)
        self.assertTrue(report["valid"], report)
        node = next(item for item in plan["nodes"] if item["type"] == "metric_composition")
        self.assertEqual(node["execution"]["expression"]["op"], "divide")

    def test_selected_set_share_records_physical_dimension_domain(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["metrics"][0]["source_dimension_bindings"] = {
            "TOP6平台": "TOP6平台"
        }
        ir["derived_requirements"] = [{
            "requirement_id": "payment_share",
            "metric_ref": "payment",
            "derived_metric_id": "selected_set_share",
            "definition_status": "registered",
            "required_period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(node for node in plan["nodes"] if node["node_id"].startswith("derived_payment_share"))
        self.assertEqual(node["execution"]["expression"]["share_type"], "selected_set_share")
        self.assertEqual(node["execution"]["expression"]["denominator_domain"], {
            "kind": "source_dimension_all",
            "dimension": "TOP6平台",
        })
        aggregate = node["execution"]["expression"]["args"][1]["aggregate"]
        self.assertTrue(aggregate["domain_ref"].startswith("domain_"))
        self.assertNotIn("values", aggregate)
        slot = plan["fetch_requests"][0]["fact_slots"][0]
        self.assertEqual(slot["selector_dimensions"], {})
        self.assertEqual(slot["source_dimension_domains"], {
            "TOP6平台": aggregate["domain_ref"]
        })

    def test_aggregate_composition_sums_components_before_dividing(self) -> None:
        ir = base_ir()
        ir["metric_compositions"] = [{
            "requirement_id": "top6_settlement_rate",
            "metric_ref": "rate",
            "composition_id": "competitor_settlement_rate",
            "period_roles": ["analysis"],
            "view_id": "platform_view",
            "dimension_refs": [],
            "dimensions": {"平台": ["京东", "拼多多"]},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        node = next(node for node in plan["nodes"] if node["node_id"].startswith("composition_top6"))
        args = node["execution"]["expression"]["args"]
        self.assertTrue(all("aggregate" in item for item in args))
        self.assertEqual({item["aggregate"]["dimension"] for item in args}, {"平台"})
        self.assertEqual(len({item["aggregate"]["domain_ref"] for item in args}), 1)
        self.assertEqual(
            {tuple(slot["dimension_refs"]) for slot in plan["analysis_task"]["fact_requirements"]},
            {("平台",)},
        )

    def test_input_adaptation_fetches_sources_and_wires_consumers(self) -> None:
        ir = base_ir()
        ir["analysis_task"]["periods"] = {
            "analysis": "2026-Q2",
            "analysis_apr": "2026-04",
            "analysis_may": "2026-05",
            "analysis_jun": "2026-06",
        }
        ir["input_adaptations"] = [{
            "requirement_id": "quarter_settlement",
            "metric_ref": "settlement",
            "target_period_role": "analysis",
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
            "expression": {
                "op": "sum",
                "args": [
                    {"fact": {"metric_ref": "settlement", "period_role": role}}
                    for role in ("analysis_apr", "analysis_may", "analysis_jun")
                ],
            },
            "rule_source": "feishu_metric_metadata",
            "validation": ["facts_present", "unit_consistent", "metric_additive"],
        }]
        ir["derived_requirements"] = [{
            "requirement_id": "quarter_share",
            "metric_ref": "settlement",
            "derived_metric_id": "selected_set_share",
            "definition_status": "registered",
            "view_id": "platform_view",
            "dimension_refs": ["TOP6平台"],
            "dimensions": {},
        }]
        plan, report = self.compile(ir)
        self.assertTrue(report["valid"], report)
        request = plan["fetch_requests"][0]
        self.assertEqual(set(request["periods"]), {"2026-04", "2026-05", "2026-06"})
        self.assertNotIn("2026-Q2", request["periods"])
        self.assertTrue(all(
            slot["selector_dimensions"] == {}
            and slot["dimension_refs"] == ["TOP6平台"]
            for slot in request["fact_slots"]
        ))
        adaptation = next(node for node in plan["nodes"] if node["type"] == "input_adaptation")
        share = next(node for node in plan["nodes"] if node["node_id"].startswith("derived_quarter_share"))
        domain_ref = share["execution"]["expression"]["denominator_domain_ref"]
        self.assertTrue(all(
            slot["source_dimension_domains"] == {"TOP6平台": domain_ref}
            for slot in request["fact_slots"]
        ))
        self.assertIn(adaptation["node_id"], share["depends_on"])
        self.assertTrue(any(
            slot.get("materialized_by") == adaptation["node_id"]
            for slot in plan["analysis_task"]["fact_requirements"]
        ))


if __name__ == "__main__":
    unittest.main()
