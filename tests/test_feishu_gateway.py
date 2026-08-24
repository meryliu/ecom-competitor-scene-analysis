from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers.feishu_competitor import FeishuCompetitorGateway  # noqa: E402


class FeishuGatewayTests(unittest.TestCase):
    def test_resolve_and_fetch_reuse_one_pinned_index(self) -> None:
        config = {
            "provider_id": "feishu_competitor",
            "source_id": "competitor_macro_sheet",
            "source_url": "https://source",
            "config_hash": "config-hash",
            "allow_stale_by_default": False,
            "sheet_roles": {
                "metric_metadata": {"allowed_names": ["指标元信息"]},
                "dimension_metadata": {"allowed_names": ["维度元信息"]},
            },
        }
        index = {
            "source": {"revision": 7, "schema_hash": "schema", "spreadsheet_token": "secret"},
            "metrics": {"支付GMV": {"aliases": ["支付成交GMV"], "unit": "亿元", "additive": True}},
            "dimensions": {"平台": {"aliases": [], "values": ["京东"]}},
            "sheets": {"month": {
                "available": True,
                "sheet_id": "physical-sheet",
                "periods": {"2026-05": "C"},
                "blocks": {"支付GMV": {"dimension": "平台", "rows": {"京东": 3}}},
            }},
        }
        with patch(
            "providers.feishu_competitor.ensure_shared_index",
            return_value=(index, "hit", Path("/tmp/index.json")),
        ) as resolve_mock, patch(
            "providers.feishu_competitor.fetch_facts_from_index",
            return_value={"schema_version": "scene_facts/2.0", "facts": [], "bindings": []},
        ) as fetch_mock:
            gateway = FeishuCompetitorGateway(config)
            capability = gateway.resolve({
                "metrics": ["支付成交GMV"], "dimensions": ["平台"], "periods": ["2026-05"],
            })
            result = gateway.fetch({"source_binding": gateway.source_binding})

        self.assertEqual(resolve_mock.call_count, 1)
        self.assertEqual(fetch_mock.call_count, 1)
        self.assertIs(fetch_mock.call_args.args[1], index)
        self.assertEqual(result["schema_version"], "scene_facts/2.0")
        self.assertEqual(capability["metric_bindings"]["支付成交GMV"], "支付GMV")
        serialized = str(capability)
        for physical in ("spreadsheet_token", "sheet_id", "rows", "physical-sheet"):
            self.assertNotIn(physical, serialized)

    def test_resolution_candidates_do_not_expose_physical_coordinates(self) -> None:
        config = {
            "provider_id": "feishu_competitor",
            "source_id": "competitor_macro_sheet",
            "source_url": "https://source",
            "config_hash": "config-hash",
            "sheet_roles": {},
        }
        index = {
            "source": {"revision": 7, "schema_hash": "schema", "spreadsheet_token": "secret"},
            "metrics": {"支付GMV": {"unit": "亿元", "dimensions": ["TOP6平台"]}},
            "dimensions": {
                "TOP6平台": {"values": ["淘系", "拼多多", "京东", "抖音", "快手", "视频号"]},
            },
            "sheets": {"month": {
                "available": False,
                "sheet_id": "physical-sheet",
                "periods": {"2026-07": "B"},
                "blocks": {},
                "unresolved_blocks": [{
                    "header_row": 3,
                    "raw_metric": "支付GMV",
                    "raw_dimension": "平台",
                    "rows": {name: row for row, name in enumerate(
                        ["淘系", "拼多多", "京东", "抖音", "快手", "视频号"], start=4
                    )},
                    "metric_match": {"candidates": [{"name": "支付GMV", "confidence": 1.0, "conflicts": []}]},
                    "dimension_match": {"candidates": [{"name": "TOP6平台", "confidence": 0.5, "conflicts": []}]},
                }],
            }},
        }
        with patch(
            "providers.feishu_competitor.ensure_shared_index",
            return_value=(index, "hit", Path("/tmp/index.json")),
        ):
            capability = FeishuCompetitorGateway(config).resolve({
                "metrics": ["支付GMV"],
                "dimensions": ["平台"],
                "contexts": [{
                    "task_id": "q",
                    "query": "TOP6支付GMV",
                    "dimensions": ["平台"],
                    "metrics": [{"name": "支付GMV", "provenance": "user_explicit"}],
                }],
            })
        serialized = str(capability)
        for physical in ("spreadsheet_token", "sheet_id", "header_row", "rows", "physical-sheet"):
            self.assertNotIn(physical, serialized)

    def test_structural_candidate_filter_uses_one_resolve(self) -> None:
        config = {
            "provider_id": "feishu_competitor",
            "source_id": "competitor_macro_sheet",
            "source_url": "https://source",
            "config_hash": "config-hash",
            "allow_stale_by_default": False,
            "sheet_roles": {},
        }
        index = {
            "source": {"revision": 7, "schema_hash": "schema", "spreadsheet_token": "secret"},
            "metrics": {
                "实物商品网上零售额": {
                    "aliases": ["线上社零"], "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["year"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
                "实物商品网上零售额同比增速": {
                    "aliases": ["线上社零同比增速", "线上社零增速"], "unit": "%",
                    "metric_object": "ratio", "supported_grains": ["month"],
                    "dimensions": ["无"], "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {},
            "sheets": {
                "month": {"available": True, "periods": {"2026-06": "B", "2026-07": "C"}, "blocks": {
                    "实物商品网上零售额同比增速": {"dimension": "无", "rows": {"整体": 2}},
                }},
                "year": {"available": True, "periods": {"2026": "B"}, "blocks": {
                    "实物商品网上零售额": {"dimension": "无", "rows": {"整体": 2}},
                }},
            },
        }
        request = {
            "metrics": ["线上社零"], "dimensions": [], "periods": ["2026-06", "2026-07"],
            "contexts": [{
                "task_id": "q", "query": "线上社零表现怎么样，较上月涨幅变化如何",
                "periods": ["2026-06", "2026-07"], "dimensions": [],
                "metrics": [{
                    "metric_ref": "retail", "name": "线上社零", "metric_object": "volume",
                    "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                    "provenance": "user_explicit",
                    "consumers": [{"requirement_type": "fact_observations"}],
                }],
            }],
        }
        with patch(
            "providers.feishu_competitor.ensure_shared_index",
            return_value=(index, "hit", Path("/tmp/index.json")),
        ) as resolve_mock:
            capability = FeishuCompetitorGateway(config).resolve(request)
        self.assertEqual(resolve_mock.call_count, 1)
        task = capability["task_resolutions"]["q"]
        self.assertNotIn("线上社零", task["metric_bindings"])
        self.assertEqual(task["resolution_cases"][0]["candidates"], [])
        self.assertIn("business_intent_policy_hash", capability["source"])
