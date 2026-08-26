from __future__ import annotations

import sys
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


if __name__ == "__main__":
    unittest.main()
