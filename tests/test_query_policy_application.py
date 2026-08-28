from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from query_policy_runtime import select_query_policy  # noqa: E402
from validate_query_policy_application import validate_application_fail_open  # noqa: E402


def fact_ir(query: str) -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": query,
            "analysis_goal": "返回支付GMV事实",
            "metrics": [{"metric_id": "gmv", "name": "支付GMV", "metric_object": "volume", "unit": "待元信息解析"}],
            "periods": {"analysis": "2026-05"},
            "scope": "TOP6平台合计大盘",
            "filters": [],
            "assumptions": [{"id": "default_scope", "description": "未指定平台时按TOP6平台合计大盘"}],
        },
        "views": [{"view_id": "overall"}],
        "dimension_trees": [],
        "input_adaptations": [],
        "fact_observations": [{
            "requirement_id": "gmv_fact", "metric_ref": "gmv", "period_roles": ["analysis"],
            "view_id": "overall", "dimension_refs": [],
            "semantic_text": "TOP6平台合计大盘支付GMV",
            "resolution_intent": {
                "operation": "aggregate_level", "output_metric_object": "volume",
                "operand": {
                    "concept_ref": "gmv",
                    "scope": {
                        "scope_kind": "source_dimension_all",
                        "dimension_hint": "TOP6平台",
                    },
                },
                "provenance": "business_policy",
            },
        }],
        "metric_compositions": [],
        "derived_requirements": [],
        "custom_calculations": [],
        "attribution_targets": [],
        "output_requirements": [],
        "clarifications": [],
    }


def decision(query: str, packet: dict, actions: list[dict] | None = None) -> dict:
    return {
        "schema_version": "query_policy_decision/1.0",
        "policy_version": packet["policy_version"],
        "raw_query": query,
        "status": "applied",
        "application_rounds": 1,
        "applied_actions": actions or [],
        "clarifications": [],
    }


def formula_ir(query: str, *, target_id: str = "payment_attr") -> dict:
    ir = fact_ir(query)
    ir["analysis_task"].update({
        "analysis_goal": "返回支付GMV同比和公式归因",
        "metrics": [
            {"metric_id": "payment", "name": "支付GMV", "metric_object": "volume", "unit": "待元信息解析"},
            {"metric_id": "mac", "name": "MAC", "metric_object": "volume", "unit": "待元信息解析"},
        ],
        "periods": {"analysis": "2026-07", "analysis_last_year": "2025-07"},
        "scope": "单个平台",
    })
    ir["fact_observations"] = []
    ir["attribution_targets"] = [{
        "target_id": target_id,
        "metric_ref": "payment",
        "metric_object": "volume",
        "scenario": "metric_change",
        "target_semantics": "同比变化归因",
        "decomposition": "formula",
        "periods": {"analysis": "2026-07", "comparison": "2025-07"},
        "view_id": "overall",
        "factors": [{"factor_id": "mac", "kind": "metric", "metric_ref": "mac"}],
        "formula": {"factor_ref": "mac"},
        "criticality": "core",
    }]
    ir["output_requirements"] = [{
        "requirement_id": "output",
        "source_requirement_refs": [target_id],
        "criticality": "core",
    }]
    return ir


def formula_action(target_id: str) -> dict:
    return {
        "task_id": "default",
        "rule_id": "single-platform-payment-gmv-attribution",
        "action_id": "default_payment_gmv_formula",
        "target_scope_fingerprint": f"payment:{target_id}",
        "produced_refs": [{"collection": "attribution_targets", "id": target_id}],
    }


class QueryPolicyApplicationTests(unittest.TestCase):
    def test_valid_enhanced_ir_commits(self) -> None:
        query = "看一下2026年5月支付GMV"
        packet = select_query_policy(query)
        action = {
            "task_id": "default", "rule_id": "gmv-defaults",
            "action_id": "default_missing_platform_to_top6", "target_scope_fingerprint": "gmv:2026-05",
        }
        result = validate_application_fail_open(packet, decision(query, packet, [action]), fact_ir(query))
        self.assertEqual(result["status"], "commit")

    def test_duplicate_action_rolls_back_to_raw_query(self) -> None:
        query = "看一下2026年5月支付GMV"
        packet = select_query_policy(query)
        action = {
            "task_id": "default", "rule_id": "gmv-defaults",
            "action_id": "default_missing_platform_to_top6", "target_scope_fingerprint": "gmv:2026-05",
        }
        result = validate_application_fail_open(packet, decision(query, packet, [action, action]), fact_ir(query))
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["code"], "QP_ACTION_DUPLICATED")

    def test_mutated_raw_query_rolls_back(self) -> None:
        query = "看一下2026年5月支付GMV"
        packet = select_query_policy(query)
        candidate = fact_ir(query)
        candidate["analysis_task"]["query"] = "看一下2026年5月TOP6支付GMV"
        result = validate_application_fail_open(packet, decision(query, packet), candidate)
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["code"], "QP_RAW_QUERY_MUTATED")

    def test_invalid_enhanced_ir_rolls_back_instead_of_blocking(self) -> None:
        query = "看一下2026年5月支付GMV"
        packet = select_query_policy(query)
        candidate = fact_ir(query)
        candidate["ir_version"] = "invalid"
        result = validate_application_fail_open(packet, decision(query, packet), candidate)
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["fallback"], "raw_query")

    def test_ambiguous_attribution_is_valid_business_confirmation(self) -> None:
        query = "2026年5月支付GMV变化归因"
        packet = select_query_policy(query)
        candidate = fact_ir(query)
        candidate["attribution_targets"] = [{
            "target_id": "gmv_attr", "metric_ref": "gmv", "metric_object": "volume",
            "scenario": "metric_change", "target_semantics": "absolute_delta",
            "decomposition": "dimension", "group_dimensions": ["平台"],
        }]
        result = validate_application_fail_open(packet, decision(query, packet), candidate)
        self.assertEqual(result["status"], "commit_pending_confirmation")
        self.assertGreater(result["business_parameter_cases"], 0)

    def test_rule_business_clarification_commits_without_ir(self) -> None:
        query = "看一下GMV"
        packet = select_query_policy(query)
        item = decision(query, packet)
        item.update({
            "status": "needs_clarification",
            "clarifications": [{"id": "missing_scope", "question": "要看哪家平台或TOP6大盘？"}],
        })
        result = validate_application_fail_open(packet, item, None)
        self.assertEqual(result["status"], "commit_clarification")

    def test_one_task_failure_does_not_change_another_task_decision(self) -> None:
        good_query = "看一下2026年5月支付GMV"
        bad_query = "看一下2026年6月支付GMV"
        good_packet = select_query_policy(good_query)
        bad_packet = select_query_policy(bad_query)
        good = validate_application_fail_open(good_packet, decision(good_query, good_packet), fact_ir(good_query))
        bad_ir = fact_ir(bad_query)
        bad_ir["analysis_task"]["query"] = "changed"
        bad = validate_application_fail_open(bad_packet, decision(bad_query, bad_packet), bad_ir)
        self.assertEqual(good["status"], "commit")
        self.assertEqual(bad["status"], "fallback_raw")

    def test_policy_owned_natural_semantics_is_canonicalized_and_committed(self) -> None:
        query = "26年7月，拼多多支付GMV同比增速，从用户视角归因"
        packet = select_query_policy(query)
        candidate = formula_ir(query)
        with tempfile.TemporaryDirectory() as temporary:
            committed = Path(temporary) / "committed-ir.json"
            result = validate_application_fail_open(
                packet,
                decision(query, packet, [formula_action("payment_attr")]),
                candidate,
                committed_ir_output=committed,
            )
            saved = json.loads(committed.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "commit")
        self.assertEqual(result["canonicalization"]["change_count"], 1)
        self.assertEqual(
            saved["attribution_targets"][0]["target_semantics"], "absolute_delta"
        )
        self.assertEqual(result["committed_ir_path"], str(committed))

    def test_cli_writes_the_default_committed_ir_artifact(self) -> None:
        query = "26年7月，拼多多支付GMV同比增速，从用户视角归因"
        packet = select_query_policy(query)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            packet_path = directory / "packet.json"
            decision_path = directory / "decision.json"
            candidate_path = directory / "candidate-ir.json"
            output_path = directory / "validation.json"
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            decision_path.write_text(
                json.dumps(
                    decision(query, packet, [formula_action("payment_attr")]),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            candidate_path.write_text(
                json.dumps(formula_ir(query), ensure_ascii=False), encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "validate_query_policy_application.py"),
                    "--packet", str(packet_path),
                    "--decision", str(decision_path),
                    "--ir", str(candidate_path),
                    "--output", str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            validation = json.loads(output_path.read_text(encoding="utf-8"))
            committed = json.loads(
                (directory / "committed-ir.json").read_text(encoding="utf-8")
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(validation["status"], "commit")
        self.assertEqual(
            validation["committed_ir_path"], str(directory / "committed-ir.json")
        )
        self.assertEqual(
            committed["attribution_targets"][0]["target_semantics"], "absolute_delta"
        )

    def test_settlement_expansion_keeps_policy_formula_after_canonicalization(self) -> None:
        query = "26年7月，淘系结算GMV表现及同比归因"
        packet = select_query_policy(query)
        candidate = formula_ir(query, target_id="payment_gmv_yoy_attribution")
        candidate["analysis_task"]["metrics"].append({
            "metric_id": "settlement",
            "name": "结算GMV",
            "metric_object": "volume",
            "unit": "待元信息解析",
        })
        candidate["fact_observations"] = [{
            "requirement_id": "settlement_level",
            "metric_ref": "settlement",
            "period_roles": ["analysis", "analysis_last_year"],
            "view_id": "overall",
            "criticality": "core",
        }]
        candidate["output_requirements"][0]["source_requirement_refs"].insert(
            0, "settlement_level"
        )
        action = formula_action("payment_gmv_yoy_attribution")
        with tempfile.TemporaryDirectory() as temporary:
            committed = Path(temporary) / "committed-ir.json"
            result = validate_application_fail_open(
                packet,
                decision(query, packet, [action]),
                candidate,
                committed_ir_output=committed,
            )
            saved = json.loads(committed.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "commit")
        self.assertEqual(saved["fact_observations"][0]["requirement_id"], "settlement_level")
        self.assertEqual(
            saved["attribution_targets"][0]["target_semantics"], "absolute_delta"
        )

    def test_effect_contract_requires_an_explicit_target_binding(self) -> None:
        query = "分析26年7月京东支付GMV下滑原因"
        packet = select_query_policy(query)
        action = formula_action("payment_attr")
        action.pop("produced_refs")
        result = validate_application_fail_open(
            packet, decision(query, packet, [action]), formula_ir(query)
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["code"], "QP_EFFECT_BINDING_MISSING")

    def test_canonical_semantics_conflict_is_not_rewritten(self) -> None:
        query = "分析26年7月京东支付GMV下滑原因"
        packet = select_query_policy(query)
        candidate = formula_ir(query)
        candidate["attribution_targets"][0]["target_semantics"] = "relative_yoy_trend"
        result = validate_application_fail_open(
            packet,
            decision(query, packet, [formula_action("payment_attr")]),
            candidate,
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["code"], "QP_EFFECT_CONTRACT_CONFLICT")
        self.assertEqual(
            result["failure"]["details"]["path"],
            "attribution_targets[0].target_semantics",
        )

    def test_effect_contract_does_not_repair_scenario_or_formula_shape(self) -> None:
        query = "分析26年7月京东支付GMV下滑原因"
        packet = select_query_policy(query)
        candidate = formula_ir(query)
        candidate["attribution_targets"][0]["scenario"] = "yoy_trend_change"
        result = validate_application_fail_open(
            packet,
            decision(query, packet, [formula_action("payment_attr")]),
            candidate,
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["details"]["path"], "attribution_targets[0].scenario")

        candidate = formula_ir(query)
        candidate["attribution_targets"][0]["decomposition"] = "user_perspective"
        result = validate_application_fail_open(
            packet,
            decision(query, packet, [formula_action("payment_attr")]),
            candidate,
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(
            result["failure"]["details"]["path"],
            "attribution_targets[0].decomposition",
        )

        candidate = formula_ir(query)
        candidate["attribution_targets"][0].pop("factors")
        result = validate_application_fail_open(
            packet,
            decision(query, packet, [formula_action("payment_attr")]),
            candidate,
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["details"]["path"], "attribution_targets[0].factors")

    def test_effect_contract_does_not_rewrite_an_unbound_user_target(self) -> None:
        query = "分析26年7月京东支付GMV下滑原因"
        packet = select_query_policy(query)
        candidate = formula_ir(query)
        user_target = deepcopy(candidate["attribution_targets"][0])
        user_target["target_id"] = "user_target"
        user_target["target_semantics"] = "用户自己的变化归因"
        candidate["attribution_targets"].append(user_target)
        result = validate_application_fail_open(
            packet,
            decision(query, packet, [formula_action("payment_attr")]),
            candidate,
        )
        self.assertEqual(result["status"], "fallback_raw")
        self.assertEqual(result["failure"]["code"], "ATTR-IR-008")
        self.assertEqual(
            result["failure"]["details"]["path"],
            "attribution_targets[1]",
        )


if __name__ == "__main__":
    unittest.main()
