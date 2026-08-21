from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fact_contract import build_fact_demands, merge_fetch_requests, project_scene_facts  # noqa: E402
from run_state import value_hash  # noqa: E402


def slot(slot_id: str, selector: dict, *, scope: str = "选择平台") -> dict:
    return {
        "fact_slot_id": slot_id,
        "metric_ref": "payment",
        "metric": "支付GMV",
        "metric_object": "volume",
        "period": "2026-05",
        "period_role": "analysis",
        "view_id": "platform",
        "dimension_refs": ["平台"],
        "selector_dimensions": selector,
        "scope": scope,
        "filters": [],
        "unit": "亿元",
        "requirement_refs": [slot_id],
    }


class FactContractTests(unittest.TestCase):
    def test_merge_preserves_source_binding_and_rejects_mismatch(self) -> None:
        binding = {
            "schema_version": "source_binding/1.0",
            "provider_id": "test",
            "source_id": "source",
            "config_hash": "config",
            "revision": 1,
            "schema_hash": "schema",
        }
        merged = merge_fetch_requests(
            [("q", {"fact_slots": [slot("a", {})]})],
            source_binding=binding,
        )
        self.assertEqual(merged["source_binding"], binding)
        incompatible = dict(binding, revision=2)
        with self.assertRaisesRegex(ValueError, "incompatible source_binding"):
            merge_fetch_requests(
                [("q", {"fact_slots": [slot("a", {})], "source_binding": incompatible})],
                source_binding=binding,
            )

    def test_policy_hash_changes_fetch_checkpoint_identity(self) -> None:
        binding = {
            "schema_version": "source_binding/1.0",
            "provider_id": "test",
            "source_id": "source",
            "config_hash": "config",
            "revision": 1,
            "schema_hash": "schema",
            "resolution_policy_hash": "policy-a",
            "resolution_engine_version": "1.0.0",
        }
        first = merge_fetch_requests(
            [("q", {"fact_slots": [slot("a", {})]})], source_binding=binding
        )
        second = merge_fetch_requests(
            [("q", {"fact_slots": [slot("a", {})]})],
            source_binding=dict(binding, resolution_policy_hash="policy-b"),
        )
        self.assertNotEqual(value_hash(first), value_hash(second))

    def test_selected_domain_and_subset_merge_but_keep_consumer_selectors(self) -> None:
        demands = build_fact_demands([
            slot("share", {"平台": ["淘系", "抖音"]}),
            slot("attribution", {"平台": ["京东", "拼多多"]}),
        ])
        self.assertEqual(len(demands), 1)
        self.assertEqual(
            demands[0]["selector_dimensions"],
            {"平台": ["淘系", "抖音", "京东", "拼多多"]},
        )
        selectors = [item["selector_dimensions"] for item in demands[0]["consumer_bindings"]]
        self.assertIn({"平台": ["京东", "拼多多"]}, selectors)

    def test_different_scope_is_not_merged(self) -> None:
        demands = build_fact_demands([
            slot("selected", {"平台": ["淘系", "抖音"]}),
            slot("all", {}, scope="全部平台"),
        ])
        self.assertEqual(len(demands), 2)

    def test_cross_task_merge_preserves_task_bindings(self) -> None:
        requests = [
            ("q1", {"fact_slots": [slot("share", {"平台": ["淘系", "抖音"]})]}),
            ("q2", {"fact_slots": [slot("attr", {"平台": ["京东"]})]}),
        ]
        merged = merge_fetch_requests(requests)
        self.assertEqual(len(merged["fact_demands"]), 1)
        self.assertEqual(
            {item["task_id"] for item in merged["fact_demands"][0]["consumer_bindings"]},
            {"q1", "q2"},
        )

    def test_different_logical_metric_refs_share_one_source_metric(self) -> None:
        first = slot("a", {"平台": ["京东"]})
        second = slot("b", {"平台": ["京东"]})
        first["metric_ref"] = "payment_gmv_q1"
        second["metric_ref"] = "payment_gmv_q2"
        merged = merge_fetch_requests([
            ("q1", {"fact_slots": [first]}),
            ("q2", {"fact_slots": [second]}),
        ])
        self.assertEqual(len(merged["fact_demands"]), 1)
        self.assertEqual(
            {item["metric_ref"] for item in merged["fact_demands"][0]["consumer_bindings"]},
            {"payment_gmv_q1", "payment_gmv_q2"},
        )

    def test_full_source_dimension_domain_survives_cross_task_merge(self) -> None:
        share = slot("share", {})
        share.update({
            "source_dimension_refs": ["TOP6平台"],
            "source_selector_dimensions": {},
            "dimension_projection": {"TOP6平台": "TOP6平台"},
            "source_dimension_domains": {"TOP6平台": "domain_physical"},
        })
        merged = merge_fetch_requests([("q1", {"fact_slots": [share]})])
        demand = merged["fact_demands"][0]
        self.assertEqual(
            demand["source_dimension_domains"],
            {"TOP6平台": "domain_physical"},
        )
        self.assertEqual(
            demand["consumer_bindings"][0]["source_dimension_domains"],
            {"TOP6平台": "domain_physical"},
        )

    def test_v2_projection_binds_one_fact_to_two_consumers(self) -> None:
        payload = {
            "schema_version": "scene_facts/2.0",
            "facts": [{"fact_id": "f1", "value": 1}],
            "bindings": [
                {"fact_id": "f1", "task_id": "q", "fact_slot_id": "share", "period_role": "analysis", "view_id": "v"},
                {"fact_id": "f1", "task_id": "q", "fact_slot_id": "attr", "period_role": "analysis", "view_id": "v"},
            ],
        }
        rows = project_scene_facts(payload, "q")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["fact_slot_id"] for row in rows}, {"share", "attr"})

    def test_v2_projection_preserves_observed_unit_over_placeholder(self) -> None:
        payload = {
            "schema_version": "scene_facts/2.0",
            "facts": [{"fact_id": "f1", "value": 1, "unit": "亿元"}],
            "bindings": [{
                "fact_id": "f1",
                "task_id": "q",
                "fact_slot_id": "fact",
                "period_role": "analysis",
                "view_id": "v",
                "unit": "待元信息解析",
            }],
        }
        rows = project_scene_facts(payload, "q")
        self.assertEqual(rows[0]["unit"], "亿元")

    def test_physical_dimension_is_projected_back_to_logical_name(self) -> None:
        physical = slot("share", {"平台": ["京东"]})
        physical.update({
            "source_metric_name": "支付GMV",
            "source_dimension_refs": ["TOP6平台"],
            "source_selector_dimensions": {"TOP6平台": ["京东"]},
            "dimension_projection": {"TOP6平台": "平台"},
        })
        demand = build_fact_demands([physical])[0]
        self.assertEqual(demand["dimension_refs"], ["TOP6平台"])
        self.assertEqual(demand["selector_dimensions"], {"TOP6平台": ["京东"]})
        payload = {
            "schema_version": "scene_facts/2.0",
            "facts": [{
                "fact_id": "f1",
                "metric": "支付GMV",
                "dimensions": {"TOP6平台": "京东"},
                "value": 1,
            }],
            "bindings": [dict(demand["consumer_bindings"][0], fact_id="f1")],
        }
        rows = project_scene_facts(payload, "default")
        self.assertEqual(rows[0]["dimensions"], {"平台": "京东"})

    def test_v2_projection_rejects_concrete_unit_conflict(self) -> None:
        payload = {
            "schema_version": "scene_facts/2.0",
            "facts": [{"fact_id": "f1", "value": 1, "unit": "亿元"}],
            "bindings": [{
                "fact_id": "f1",
                "task_id": "q",
                "fact_slot_id": "fact",
                "period_role": "analysis",
                "view_id": "v",
                "unit": "万元",
            }],
        }
        with self.assertRaisesRegex(ValueError, "unit conflict"):
            project_scene_facts(payload, "q")


if __name__ == "__main__":
    unittest.main()
