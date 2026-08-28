from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from query_policy_runtime import (  # noqa: E402
    QueryPolicyError,
    load_policy,
    select_query_policy,
    select_query_policy_fail_open,
    validate_policy,
    validate_rule,
    value_hash,
)
from compile_query_policy import QueryPolicyCompileError, _runtime_rule  # noqa: E402


def refreshed_policy(
    index: dict, manifest: dict, rules: dict[str, dict]
) -> tuple[dict, dict, dict[str, dict]]:
    updated = deepcopy(manifest)
    updated["index_sha256"] = value_hash(index)
    updated["rule_hashes"] = {rule_id: value_hash(rule) for rule_id, rule in rules.items()}
    updated["policy_sha256"] = value_hash({"index": index, "rules": rules})
    return index, updated, rules


class QueryPolicyRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.manifest, self.rules, self.limits = load_policy()

    def test_current_policy_has_eight_active_rules_and_no_draft_runtime_state(self) -> None:
        self.assertEqual(len(self.rules), 8)
        self.assertEqual(set(self.rules), set(self.index["active_rule_ids"]))
        self.assertEqual(validate_policy(self.index, self.manifest, self.rules), self.limits)

    def test_selection_is_bounded_and_expands_declared_dependency(self) -> None:
        packet = select_query_policy("看一下8月京东结算GMV表现")
        self.assertEqual(packet["status"], "selected")
        selected = packet["selected_rule_ids"]
        self.assertIn("user-explicit-priority", selected)
        self.assertIn("single-platform-settlement-gmv", selected)
        self.assertIn("single-platform-payment-gmv-attribution", selected)
        self.assertLessEqual(len(selected), packet["limits"]["max_selected_rules"])
        encoded = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertLessEqual(len(encoded.encode("utf-8")), packet["limits"]["max_packet_bytes"])

    def test_unrelated_query_does_not_load_rule_cards(self) -> None:
        packet = select_query_policy("查询昨天的天气")
        self.assertEqual(packet["status"], "no_match")
        self.assertEqual(packet["rules"], [])

    def test_missing_dependency_fails_open_to_raw_query(self) -> None:
        index = deepcopy(self.index)
        rules = deepcopy(self.rules)
        rules["single-platform-settlement-gmv"]["relations"]["depends_on"] = ["missing-rule"]
        policy = refreshed_policy(index, self.manifest, rules)
        packet = select_query_policy_fail_open("8月京东结算GMV表现", policy_data=policy)
        self.assertEqual(packet["status"], "fallback_raw")
        self.assertEqual(packet["failure"]["code"], "QP_DEPENDENCY_NOT_FOUND")
        self.assertEqual(packet["raw_query"], "8月京东结算GMV表现")

    def test_dependency_cycle_fails_open_to_raw_query(self) -> None:
        index = deepcopy(self.index)
        rules = deepcopy(self.rules)
        rules["single-platform-payment-gmv-attribution"]["relations"] = {
            "depends_on": ["single-platform-settlement-gmv"]
        }
        policy = refreshed_policy(index, self.manifest, rules)
        packet = select_query_policy_fail_open("8月京东结算GMV表现", policy_data=policy)
        self.assertEqual(packet["status"], "fallback_raw")
        self.assertEqual(packet["failure"]["code"], "QP_DEPENDENCY_CYCLE")

    def test_resource_hash_mismatch_fails_open(self) -> None:
        manifest = deepcopy(self.manifest)
        manifest["rule_hashes"]["gmv-defaults"] = "bad"
        packet = select_query_policy_fail_open(
            "看一下GMV", policy_data=(deepcopy(self.index), manifest, deepcopy(self.rules))
        )
        self.assertEqual(packet["status"], "fallback_raw")
        self.assertEqual(packet["failure"]["code"], "QP_RESOURCE_HASH_MISMATCH")

    def test_compiler_requires_stable_action_id(self) -> None:
        source = deepcopy(self.rules["gmv-defaults"])
        source["actions"][0].pop("action_id")
        with self.assertRaises(QueryPolicyCompileError):
            _runtime_rule(source, self.index["policy_version"])

    def test_attribution_effect_contract_must_match_the_protocol(self) -> None:
        rule = deepcopy(self.rules["single-platform-payment-gmv-attribution"])
        rule["actions"][0]["ir_effect_contract"]["target_semantics"] = "relative_yoy_trend"
        with self.assertRaises(QueryPolicyError) as caught:
            validate_rule(rule)
        self.assertEqual(caught.exception.code, "QP_EFFECT_CONTRACT_INVALID")

    def test_fixture_rule_references_exist_and_positive_rules_are_recalled(self) -> None:
        fixture_path = ROOT / "tests" / "fixtures" / "query-policy" / "behavior-fixtures.json"
        fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(len(fixtures["cases"]), 12)
        for case in fixtures["cases"]:
            references = set(case.get("expected_rules", [])) | set(case.get("forbidden_rules", []))
            self.assertLessEqual(references, set(self.rules), case["case_id"])
            if case.get("kind") == "positive":
                selected = set(select_query_policy(case["query"])["selected_rule_ids"])
                self.assertLessEqual(set(case.get("expected_rules", [])), selected, case["case_id"])


if __name__ == "__main__":
    unittest.main()
