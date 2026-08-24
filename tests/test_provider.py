from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import competitor_fact_provider as provider  # noqa: E402
from run_fast_query import validate_scene_facts  # noqa: E402


def sample_index() -> dict:
    return {
        "source": {
            "url": "source",
            "title": "source",
            "spreadsheet_token": "token",
            "revision": 7,
            "schema_hash": "hash",
        },
        "metrics": {
            "支付GMV": {"unit": "亿元", "notes": "支付GMV", "aggregation": "可聚合", "additive": True},
            "结算GMV": {"unit": "亿元", "notes": "结算GMV", "aggregation": "可聚合", "additive": True},
        },
        "dimensions": {"平台": {"values": ["淘系", "京东"]}},
        "sheets": {
            "month": {
                "available": True,
                "sheet_id": "sheet",
                "sheet_name": "月度表",
                "periods": {"2026-05": "B"},
                "blocks": {
                    "支付GMV": {
                        "dimension": "平台",
                        "rows": {"淘系": 4, "京东": 5},
                    },
                    "结算GMV": {
                        "dimension": "平台",
                        "rows": {"淘系": 8, "京东": 9},
                    },
                },
            }
        },
        "warnings": [],
    }


class ProviderTests(unittest.TestCase):
    def test_percentage_display_value_keeps_declared_unit_magnitude(self) -> None:
        self.assertEqual(provider._parse_number("28.1%"), 28.1)
        self.assertEqual(provider._parse_number("1,234.5"), 1234.5)

    def test_scalar_dimension_value_is_confirmed_by_source_metadata(self) -> None:
        resolution = provider._resolve_dimension_from_value(
            {"selector_dimensions": {"平台": "京东"}},
            "平台",
            sample_index(),
            provider.load_dimension_set_registry(),
        )
        self.assertEqual(resolution["resolved_dimension"], "平台")
        self.assertEqual(resolution["members"], ["京东"])
        self.assertEqual(resolution["selection_basis"], "source_metadata_unique")

    def test_ambiguous_dimension_value_requires_clarification(self) -> None:
        index = sample_index()
        index["dimensions"]["渠道"] = {"values": ["京东"]}
        with self.assertRaises(provider.SkillError) as raised:
            provider._resolve_dimension_from_value(
                {"selector_dimensions": {"平台": "京东"}},
                "平台",
                index,
                provider.load_dimension_set_registry(),
            )
        self.assertEqual(raised.exception.code, "ambiguous_dimension_value")
        self.assertEqual(set(raised.exception.details["candidates"]), {"平台", "渠道"})

    def test_unique_value_in_different_dimension_returns_resolution_patch(self) -> None:
        index = sample_index()
        index["dimensions"]["平台"]["values"] = ["淘系"]
        index["dimensions"]["渠道"] = {"values": ["京东"]}
        with self.assertRaises(provider.SkillError) as raised:
            provider._resolve_dimension_from_value(
                {"selector_dimensions": {"平台": "京东"}},
                "平台",
                index,
                provider.load_dimension_set_registry(),
            )
        self.assertEqual(raised.exception.code, "dimension_resolution_required")
        self.assertEqual(
            raised.exception.details["resolution_patch"]["to_dimension"],
            "渠道",
        )

    def test_physical_dimension_full_domain_uses_current_members(self) -> None:
        index = sample_index()
        index["dimensions"]["TOP6平台"] = {
            "values": ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"]
        }
        domain_ref = provider.source_dimension_domain_ref("TOP6平台")
        resolution = provider._selector_resolution(
            {"source_dimension_domains": {"TOP6平台": domain_ref}},
            "TOP6平台",
            index,
            provider.load_dimension_set_registry(),
        )
        self.assertEqual(resolution["domain_id"], domain_ref)
        self.assertEqual(resolution["domain_kind"], "source_dimension_all")
        self.assertEqual(len(resolution["members"]), 6)

    def test_physical_domain_members_can_change_without_registry_update(self) -> None:
        index = sample_index()
        index["dimensions"] = {"TOP6平台": {"values": ["淘系", "京东"]}}
        domain_ref = provider.source_dimension_domain_ref("TOP6平台")
        slot = {"source_dimension_domains": {"TOP6平台": domain_ref}}
        first = provider._selector_resolution(
            slot, "TOP6平台", index, provider.load_dimension_set_registry()
        )
        index["dimensions"]["TOP6平台"]["values"].append("新增平台")
        second = provider._selector_resolution(
            slot, "TOP6平台", index, provider.load_dimension_set_registry()
        )
        self.assertEqual(first["domain_id"], second["domain_id"])
        self.assertEqual(second["members"], ["淘系", "京东", "新增平台"])

    def test_top4_and_top6_physical_dimensions_coexist(self) -> None:
        index = sample_index()
        index["dimensions"] = {
            "TOP4平台": {"values": ["淘系", "抖音", "拼多多", "京东"]},
            "TOP6平台": {"values": ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"]},
        }
        top4 = provider.source_dimension_domain_ref("TOP4平台")
        top6 = provider.source_dimension_domain_ref("TOP6平台")
        self.assertNotEqual(top4, top6)
        with self.assertRaises(provider.SkillError):
            provider._source_dimension("平台", index)

    def test_provider_emits_facts_not_results(self) -> None:
        index = sample_index()
        request = {
            "request_id": "r1",
            "fact_slots": [{
                "fact_slot_id": "slot1",
                "metric_ref": "payment_gmv",
                "metric": "支付GMV",
                "period": "2026-05",
                "period_role": "analysis",
                "view_id": "platform",
                "dimension_refs": ["平台"],
                "selector_dimensions": {"平台": ["京东"]},
            }],
        }
        with patch.object(provider, "ensure_shared_index", return_value=(index, "hit", Path("/tmp/index.json"))), patch.object(
            provider,
            "_read_cells",
            return_value={("sheet", 5, 2): "2605"},
        ), patch.object(provider.LarkClient, "revision", return_value=7):
            payload = provider.fetch_facts(request, Path("/tmp/provider-test-index.json"))
        self.assertEqual(len(payload["facts"]), 1)
        self.assertEqual(payload["schema_version"], "scene_facts/2.0")
        self.assertNotIn("results", payload)
        self.assertEqual(payload["facts"][0]["metric_ref"], "payment_gmv")
        self.assertEqual(payload["facts"][0]["dimensions"], {"平台": "京东"})
        self.assertEqual(payload["facts"][0]["value"], 2605.0)
        self.assertEqual(payload["facts"][0]["source_ref"]["revision"], 7)
        self.assertTrue(payload["facts"][0]["additive"])
        self.assertEqual(payload["bindings"][0]["fact_slot_id"], "slot1")

    def test_provider_emits_percentage_in_declared_unit_magnitude(self) -> None:
        index = sample_index()
        index["metrics"]["支付GMV"]["unit"] = "%"
        request = {
            "request_id": "r1",
            "fact_slots": [{
                "fact_slot_id": "slot1",
                "metric_ref": "online_retail_rate",
                "metric": "支付GMV",
                "period": "2026-05",
                "period_role": "analysis",
                "view_id": "platform",
                "dimension_refs": ["平台"],
                "selector_dimensions": {"平台": ["京东"]},
            }],
        }
        with patch.object(
            provider,
            "ensure_shared_index",
            return_value=(index, "hit", Path("/tmp/index.json")),
        ), patch.object(
            provider,
            "_read_cells",
            return_value={("sheet", 5, 2): "28.1%"},
        ), patch.object(provider.LarkClient, "revision", return_value=7):
            payload = provider.fetch_facts(request)
        self.assertEqual(payload["facts"][0]["value"], 28.1)
        self.assertEqual(payload["facts"][0]["unit"], "%")

    def test_overlapping_consumers_share_physical_facts(self) -> None:
        index = sample_index()
        request = {
            "request_id": "r1",
            "fact_slots": [
                {
                    "fact_slot_id": "share",
                    "metric_ref": "payment_gmv",
                    "metric": "支付GMV",
                    "period": "2026-05",
                    "period_role": "analysis",
                    "view_id": "share_view",
                    "dimension_refs": ["平台"],
                    "selector_dimensions": {"平台": ["淘系", "京东"]},
                },
                {
                    "fact_slot_id": "attribution",
                    "metric_ref": "payment_gmv",
                    "metric": "支付GMV",
                    "period": "2026-05",
                    "period_role": "analysis",
                    "view_id": "attribution_view",
                    "dimension_refs": ["平台"],
                    "selector_dimensions": {"平台": ["京东"]},
                },
            ],
        }
        with patch.object(provider, "ensure_shared_index", return_value=(index, "hit", Path("/tmp/index.json"))), patch.object(
            provider,
            "_read_cells",
            return_value={("sheet", 4, 2): "100", ("sheet", 5, 2): "200"},
        ), patch.object(provider.LarkClient, "revision", return_value=7):
            payload = provider.fetch_facts(request)
        self.assertEqual(len(payload["facts"]), 2)
        jd_fact = next(item for item in payload["facts"] if item["dimensions"] == {"平台": "京东"})
        bound_slots = {
            item["fact_slot_id"] for item in payload["bindings"] if item["fact_id"] == jd_fact["fact_id"]
        }
        self.assertEqual(bound_slots, {"share", "attribution"})

    def test_provider_emits_resolved_domain_for_physical_dimension(self) -> None:
        index = sample_index()
        platforms = ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"]
        index["dimensions"] = {"TOP6平台": {"values": platforms}}
        index["sheets"]["month"]["blocks"]["支付GMV"]["rows"] = {
            platform: row for row, platform in enumerate(platforms, start=4)
        }
        index["sheets"]["month"]["blocks"]["支付GMV"]["dimension"] = "TOP6平台"
        domain_ref = provider.source_dimension_domain_ref("TOP6平台")
        request = {
            "request_id": "r1",
            "fact_slots": [{
                "fact_slot_id": "share",
                "metric_ref": "payment_gmv",
                "metric": "支付GMV",
                "period": "2026-05",
                "period_role": "analysis",
                "view_id": "share_view",
                "dimension_refs": ["TOP6平台"],
                "selector_dimensions": {},
                "source_dimension_domains": {"TOP6平台": domain_ref},
            }],
        }
        raw = {("sheet", row, 2): str(row) for row in range(4, 10)}
        with patch.object(provider, "ensure_shared_index", return_value=(index, "hit", Path("/tmp/index.json"))), patch.object(
            provider, "_read_cells", return_value=raw
        ), patch.object(provider.LarkClient, "revision", return_value=7):
            payload = provider.fetch_facts(request)
        self.assertEqual(len(payload["facts"]), 6)
        self.assertEqual(len(payload["resolved_dimension_domains"]), 1)
        domain_id, domain = next(iter(payload["resolved_dimension_domains"].items()))
        self.assertEqual(domain_id, domain_ref)
        self.assertEqual(domain["domain_kind"], "source_dimension_all")
        self.assertEqual(domain["members"], platforms)
        self.assertEqual(
            {binding["dimension_domain_refs"]["TOP6平台"] for binding in payload["bindings"]},
            {domain_id},
        )

    def test_fast_path_reuses_standard_facts_without_remapping(self) -> None:
        row = {
            "fact_slot_id": "slot1",
            "metric_ref": "payment_gmv",
            "metric": "支付GMV",
            "period": "2026-05",
            "period_role": "analysis",
            "view_id": "platform",
            "dimensions": {"平台": "京东"},
            "value": 2605.0,
            "unit": "亿元",
            "definition": "支付GMV",
            "missing": False,
            "raw_missing": False,
            "normalization_reason": "unchanged",
            "value_derived_from_components": False,
            "source_request_id": "r1",
            "source_ref": {"revision": 7},
        }
        payload = {"schema_version": "scene_facts/1.0", "facts": [row], "source": {"revision": 7}}
        plan = {
            "analysis_task": {
                "scope": "京东",
                "fact_requirements": [{"fact_slot_id": "slot1"}],
            }
        }
        facts = validate_scene_facts(payload, plan, "r1")
        self.assertIs(facts, payload["facts"])
        self.assertIs(facts[0], row)

    def test_fast_path_allows_dimension_rows_for_one_slot(self) -> None:
        base = {
            "fact_slot_id": "slot1",
            "metric_ref": "payment_gmv",
            "metric": "支付GMV",
            "period": "2026-05",
            "period_role": "analysis",
            "view_id": "platform",
            "value": 1.0,
            "unit": "亿元",
            "definition": "支付GMV",
            "missing": False,
            "raw_missing": False,
        }
        rows = [
            {**base, "dimensions": {"平台": "京东"}},
            {**base, "dimensions": {"平台": "拼多多"}},
        ]
        payload = {"schema_version": "scene_facts/1.0", "facts": rows}
        plan = {"analysis_task": {"scope": "平台", "fact_requirements": [{"fact_slot_id": "slot1"}]}}
        self.assertEqual(validate_scene_facts(payload, plan, "r1"), rows)


    def test_no_dimension_block_uses_single_data_row_as_overall(self) -> None:
        index = sample_index()
        index["metrics"]["实物商品网上零售额增速"] = {
            "unit": "%",
            "notes": "线上社零增速",
            "aggregation": "不可聚合",
            "additive": False,
        }
        index["sheets"]["month"]["blocks"]["实物商品网上零售额增速"] = {
            "dimension": "无",
            "header_row": 61,
            "rows": {"实物商品网上零售额-当月增速": 62},
        }
        request = {
            "request_id": "r1",
            "fact_slots": [{
                "fact_slot_id": "slot1",
                "metric_ref": "online_retail_growth",
                "metric": "实物商品网上零售额增速",
                "period": "2026-05",
                "period_role": "analysis",
                "view_id": "overall",
                "dimension_refs": [],
                "selector_dimensions": {},
            }],
        }
        cells = provider._requested_cells(request, index, provider.load_dimension_set_registry())
        self.assertEqual(cells[0]["row"], 62)
        self.assertEqual(cells[0]["dimension"], None)
        self.assertEqual(cells[0]["dimension_value"], None)

    def test_no_dimension_block_falls_back_to_header_row_when_rows_empty(self) -> None:
        block = {"dimension": "无", "header_row": 61, "rows": {}}
        self.assertEqual(provider._overall_row_for_no_dimension_block(block, "指标A"), 61)

    def test_no_dimension_block_rejects_ambiguous_rows_without_overall_label(self) -> None:
        block = {"dimension": "无", "header_row": 61, "rows": {"A": 62, "B": 63}}
        with self.assertRaises(provider.SkillError) as raised:
            provider._overall_row_for_no_dimension_block(block, "指标A")
        self.assertEqual(raised.exception.code, "ambiguous_no_dimension_rows")



if __name__ == "__main__":
    unittest.main()
