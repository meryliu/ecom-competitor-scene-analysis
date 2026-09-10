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

from query_policy_runtime import (  # noqa: E402
    QueryPolicyError,
    load_policy,
    select_query_policy,
    select_query_policy_fail_open,
    validate_policy,
    validate_rule,
    normalize_policy_text,
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

    def test_current_policy_has_active_rules_and_no_draft_runtime_state(self) -> None:
        self.assertEqual(len(self.rules), 10)
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

    def test_route_matching_is_case_unicode_and_spacing_invariant(self) -> None:
        queries = [
            "26年7月拼多多gmv同比变化以及归因",
            "26年7月拼多多GMV同比变化以及归因",
            "26年7月拼多多ＧＭＶ同比变化以及归因",
            "26年7月拼多多 G M V，同比变化以及归因",
        ]
        selected = [select_query_policy(query)["selected_rule_ids"] for query in queries]
        self.assertTrue(all(item == selected[0] for item in selected))
        self.assertIn("gmv-defaults", selected[0])
        self.assertIn("single-platform-payment-gmv-attribution", selected[0])

    def test_generic_gmv_attribution_reaches_payment_attribution_rule(self) -> None:
        packet = select_query_policy("26年7月拼多多GMV同比变化以及归因")
        self.assertEqual(packet["status"], "selected")
        self.assertIn("gmv-defaults", packet["selected_rule_ids"])
        self.assertIn("single-platform-payment-gmv-attribution", packet["selected_rule_ids"])

    def test_vague_performance_phrases_recall_the_same_policy(self) -> None:
        for phrase in ("表现如何", "表现怎么样", "水平如何", "水平怎么样"):
            with self.subTest(phrase=phrase):
                packet = select_query_policy(f"26年7月指标A{phrase}")
                self.assertEqual(packet["status"], "selected")
                self.assertIn("performance-defaults", packet["selected_rule_ids"])

    def test_performance_policy_declares_attribution_and_explicit_output_guards(self) -> None:
        rule = self.rules["performance-defaults"]
        applicability = rule.get("applicability") or {}
        guards = "".join(
            (applicability.get("all") or []) + (applicability.get("none") or [])
        )
        for token in ("归因语义", "显式指标值", "输出限制", "口径语义"):
            self.assertIn(token, guards)
        contract = (
            ROOT / "references" / "query-understanding" / "application-contract.md"
        ).read_text(encoding="utf-8")
        for token in ("原因", "为什么", "贡献", "指标值", "同比", "只看", "口径"):
            self.assertIn(token, contract)
        action = rule["actions"][0]
        instructions = "".join(action.get("instructions") or [])
        self.assertIn("performance_yoy_supplement", instructions)
        self.assertIn("不影响原需求", instructions)

    def test_performance_policy_composes_after_metric_only_expansion(self) -> None:
        packet = select_query_policy("25年下半年拼多多的用户指标表现怎么样")
        self.assertEqual(packet["status"], "selected")
        self.assertIn("user-metric-defaults", packet["selected_rule_ids"])
        self.assertIn("performance-defaults", packet["selected_rule_ids"])
        rule = self.rules["performance-defaults"]
        applicability = "".join(
            (rule.get("applicability") or {}).get("all") or []
        ) + "".join((rule.get("applicability") or {}).get("none") or [])
        instructions = "".join(rule["actions"][0].get("instructions") or [])
        self.assertIn("上游 Policy", applicability)
        self.assertIn("当前目标作用域", applicability)
        self.assertIn("结构化输出槽位", instructions)
        self.assertIn("不使用 analysis_goal", instructions)

    def test_policy_index_covers_rule_level_routes(self) -> None:
        # This assertion protects the generated-index invariant: adding a
        # routing term to a rule must make that rule reachable.
        for rule_id, rule in self.rules.items():
            terms = (rule.get("routing") or {}).get("terms")
            if not isinstance(terms, list):
                continue
            for term in terms:
                normalized = normalize_policy_text(term)
                self.assertTrue(
                    any(
                        rule_id in ids
                        and normalized in {normalize_policy_text(item) for item in str(route).split("|")}
                        for route, ids in self.index["routing"].items()
                    ),
                    f"unreachable route: {rule_id}/{term}",
                )

    def test_policy_validation_rejects_unreachable_rule_route(self) -> None:
        index = deepcopy(self.index)
        rules = deepcopy(self.rules)
        for route, rule_ids in index["routing"].items():
            index["routing"][route] = [
                rule_id for rule_id in rule_ids
                if rule_id != "single-platform-payment-gmv-attribution"
            ]
        index["routing"] = {
            route: rule_ids for route, rule_ids in index["routing"].items() if rule_ids
        }
        policy = refreshed_policy(index, self.manifest, rules)
        with self.assertRaises(QueryPolicyError) as caught:
            validate_policy(*policy)
        self.assertEqual(caught.exception.code, "QP_INDEX_INCOMPLETE")

    def test_unrelated_query_does_not_load_rule_cards(self) -> None:
        packet = select_query_policy("查询昨天的天气")
        self.assertEqual(packet["status"], "no_match")
        self.assertEqual(packet["rules"], [])

    def test_selector_cli_uses_canonical_input_and_output_file_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            input_path = Path(temporary) / "raw-query.json"
            output_path = Path(temporary) / "packet.json"
            input_path.write_text(
                json.dumps({"raw_query": "看一下8月京东闭环电商佣金收入"}, ensure_ascii=False),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "select_query_policy.py"),
                    "--input", str(input_path), "--output", str(output_path),
                ],
                check=False, capture_output=True, text=True,
            )
            packet = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(packet["status"], "selected")
        self.assertIn("jd-commission", packet["selected_rule_ids"])

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
        self.assertEqual(len(fixtures["cases"]), 21)
        for case in fixtures["cases"]:
            references = set(case.get("expected_rules", [])) | set(case.get("forbidden_rules", []))
            self.assertLessEqual(references, set(self.rules), case["case_id"])
            if case.get("kind") == "positive":
                selected = set(select_query_policy(case["query"])["selected_rule_ids"])
                self.assertLessEqual(set(case.get("expected_rules", [])), selected, case["case_id"])
            if case.get("assert_forbidden_rules") and case.get("forbidden_rules"):
                selected = set(select_query_policy(case["query"])["selected_rule_ids"])
                self.assertTrue(
                    selected.isdisjoint(case["forbidden_rules"]),
                    f"forbidden rule selected: {case['case_id']}",
                )


if __name__ == "__main__":
    unittest.main()
