from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from resolution_policy import (  # noqa: E402
    ResolutionPolicyError,
    _derived_semantic_score,
    _grain_signature,
    _implicit_fallback_selects_composition_leaf,
    _strip_derived_tokens,
    load_resolution_policy,
    resolve_request_overlay,
    validate_resolution_policy,
)
from semantic_context_guard import extract_current_core_hint  # noqa: E402


def ambiguous_platform_index() -> dict:
    dimensions = {
        "TOP4平台": {"aliases": [], "values": ["淘系", "拼多多", "京东", "抖音"]},
        "TOP6平台": {"aliases": [], "values": ["淘系", "拼多多", "京东", "抖音", "快手", "视频号"]},
    }
    unresolved = {
        "decision": "reject",
        "header_row": 3,
        "raw_metric": "支付GMV",
        "raw_dimension": "平台",
        "rows": {name: row for row, name in enumerate(dimensions["TOP6平台"]["values"], start=4)},
        "metric_match": {
            "candidates": [{"name": "支付GMV", "confidence": 1.0, "conflicts": []}],
        },
        "dimension_match": {
            "candidates": [
                {"name": "TOP4平台", "confidence": 0.5, "conflicts": []},
                {"name": "TOP6平台", "confidence": 0.5, "conflicts": []},
            ],
        },
    }
    return {
        "source": {"url": "source", "revision": 1106, "schema_hash": "schema"},
        "metrics": {"支付GMV": {"unit": "亿元", "dimensions": ["TOP6平台"]}},
        "dimensions": dimensions,
        "sheets": {"month": {
            "available": True,
            "periods": {"2026-07": "B"},
            "blocks": {},
            "unresolved_blocks": [unresolved],
        }},
    }


def aggregate_level_index(*, direct: bool = False, additive: bool = True) -> dict:
    metrics = {
        "支付GMV": {
            "aliases": [], "unit": "亿元", "metric_object": "volume",
            "supported_grains": ["month"], "dimensions": ["TOP6平台"],
            "additive": additive,
        },
    }
    blocks = {
        "支付GMV": {
            "dimension": "TOP6平台",
            "rows": {"淘系": 4, "拼多多": 5, "京东": 6},
        },
    }
    dimensions = {
        "TOP6平台": {
            "aliases": ["平台"], "values": ["淘系", "拼多多", "京东"],
        },
    }
    if direct:
        metrics["TOP6大盘支付GMV"] = {
            "aliases": ["TOP6平台合计支付GMV"], "unit": "亿元",
            "metric_object": "volume", "supported_grains": ["month"],
            "dimensions": ["无"], "additive": True,
        }
        blocks["TOP6大盘支付GMV"] = {"dimension": "无", "rows": {"无": 8}}
    return {
        "source": {"url": "source", "revision": 3, "schema_hash": "schema"},
        "metrics": metrics,
        "dimensions": dimensions,
        "sheets": {"month": {
            "available": True, "periods": {"2026-07": "B"}, "blocks": blocks,
        }},
    }


def aggregate_level_request() -> dict:
    consumer = {
        "requirement_id": "market_total", "requirement_type": "fact_observations",
        "periods": ["2026-07"], "period_roles": ["analysis"],
        "breakdown_dimensions": [], "semantic_text": "TOP6大盘支付GMV",
        "resolution_intent": {
            "operation": "aggregate_level", "output_metric_object": "volume",
            "operand": {
                "concept_ref": "payment",
                "scope": {
                    "scope_kind": "source_dimension_all",
                    "dimension_hint": "TOP6平台",
                },
            },
            "provenance": "business_policy",
        },
    }
    return {
        "metrics": ["支付GMV", "TOP6大盘支付GMV"],
        "contexts": [{
            "task_id": "q", "query": "26年7月大盘支付GMV表现",
            "periods": ["2026-07"], "dimensions": ["TOP6平台"],
            "metrics": [{
                "metric_ref": "__resolution_total", "name": "TOP6大盘支付GMV",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "unit_provenance": "model_inferred",
                "consumers": [consumer], "required_periods": ["2026-07"],
                "required_dimensions": [], "required_breakdown_dimensions": [],
                "resolution_requirement_id": "market_total",
                "resolution_operation": "aggregate_level",
                "resolution_intent": consumer["resolution_intent"],
                "logical_metric_ref": "payment", "logical_metric_name": "支付GMV",
                "provenance": "business_policy",
            }],
        }],
    }
class ResolutionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_resolution_policy()

    def test_policy_rejects_unknown_operator(self) -> None:
        invalid = dict(self.policy)
        invalid["rules"] = dict(self.policy["rules"])
        invalid["rules"]["fact_block_joint_resolution"] = dict(
            self.policy["rules"]["fact_block_joint_resolution"]
        )
        invalid["rules"]["fact_block_joint_resolution"]["hard_gates"] = ["eval"]
        with self.assertRaises(ResolutionPolicyError):
            validate_resolution_policy(invalid)

    def test_aggregate_level_prefers_complete_direct_fact(self) -> None:
        result = resolve_request_overlay(
            aggregate_level_index(direct=True), aggregate_level_request(), self.policy
        )
        task = result["task_resolutions"]["q"]
        binding = task["requirement_bindings"]["market_total"]
        self.assertEqual(binding["mode"], "source_scoped_fact")
        self.assertEqual(binding["source_metric"], "TOP6大盘支付GMV")

    def test_aggregate_level_uses_safe_source_domain_when_direct_is_absent(self) -> None:
        result = resolve_request_overlay(
            aggregate_level_index(), aggregate_level_request(), self.policy
        )
        task = result["task_resolutions"]["q"]
        binding = task["requirement_bindings"]["market_total"]
        self.assertEqual(binding["mode"], "source_dimension_all_sum")
        self.assertEqual(binding["source_metric"], "支付GMV")
        self.assertEqual(binding["source_dimension"], "TOP6平台")

    def test_non_additive_aggregate_candidate_does_not_poison_direct_fact(self) -> None:
        result = resolve_request_overlay(
            aggregate_level_index(direct=True, additive=False),
            aggregate_level_request(), self.policy,
        )
        binding = result["task_resolutions"]["q"]["requirement_bindings"][
            "market_total"
        ]
        self.assertEqual(binding["mode"], "source_scoped_fact")

    def test_non_additive_aggregate_blocks_only_when_no_other_path_exists(self) -> None:
        result = resolve_request_overlay(
            aggregate_level_index(additive=False), aggregate_level_request(), self.policy
        )
        task = result["task_resolutions"]["q"]
        self.assertNotIn("market_total", task["requirement_bindings"])
        self.assertEqual(task["metric_statuses"]["__resolution_total"]["status"], "not_executable")
        case = next(
            item for item in task["resolution_cases"]
            if item.get("kind") == "aggregate_level"
        )
        self.assertIn(
            "metric_non_additive",
            {reason for item in case["rejected_candidates"] for reason in item["conflicts"]},
        )

    def test_aggregate_level_rejects_incomplete_live_domain(self) -> None:
        index = aggregate_level_index()
        index["sheets"]["month"]["blocks"]["支付GMV"]["rows"].pop("拼多多")
        result = resolve_request_overlay(index, aggregate_level_request(), self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("market_total", task["requirement_bindings"])
        case = next(
            item for item in task["resolution_cases"]
            if item.get("kind") == "aggregate_level"
        )
        self.assertIn(
            "aggregate_domain_incomplete",
            {reason for item in case["rejected_candidates"] for reason in item["conflicts"]},
        )

    def test_aggregate_level_is_generic_across_physical_dimensions(self) -> None:
        for dimension, hint, members in (
            ("经营区域", "区域", ["华东", "华南"]),
            ("销售渠道", "渠道", ["线上", "线下"]),
        ):
            with self.subTest(dimension=dimension):
                index = aggregate_level_index()
                index["metrics"]["支付GMV"]["dimensions"] = [dimension]
                block = index["sheets"]["month"]["blocks"]["支付GMV"]
                block["dimension"] = dimension
                block["rows"] = {
                    member: number for number, member in enumerate(members)
                }
                index["dimensions"] = {
                    dimension: {"aliases": [hint], "values": members}
                }
                request = aggregate_level_request()
                request["contexts"][0]["metrics"][0]["resolution_intent"][
                    "operand"
                ]["scope"]["dimension_hint"] = hint
                binding = resolve_request_overlay(
                    index, request, self.policy
                )["task_resolutions"]["q"]["requirement_bindings"]["market_total"]
                self.assertEqual(binding["source_dimension"], dimension)

    def test_policy_rejects_unbounded_or_mapping_fields(self) -> None:
        invalid = dict(self.policy)
        invalid["mappings"] = {"平台": "TOP6平台"}
        with self.assertRaises(ResolutionPolicyError):
            validate_resolution_policy(invalid)

    def test_core_semantics_keep_measure_nouns_and_strip_grain_terms(self) -> None:
        self.assertEqual(_strip_derived_tokens("MAC订单量", self.policy), "mac订单量")
        self.assertEqual(_strip_derived_tokens("MAC订单价", self.policy), "mac订单价")
        self.assertEqual(_strip_derived_tokens("支付GMV", self.policy), "支付gmv")
        for label, grain in (
            ("月度支付GMV", "month"),
            ("周度支付GMV", "week"),
            ("季度支付GMV", "quarter"),
            ("年度支付GMV", "year"),
        ):
            self.assertEqual(_strip_derived_tokens(label, self.policy), "支付gmv")
            self.assertEqual(_grain_signature(label, self.policy), grain)

    def test_source_alias_is_equivalent_core_evidence_with_breakdown(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "限额以上企业零售额增速": {
                    "aliases": ["社零大盘"], "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["类目"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"类目": {"aliases": [], "values": []}},
            "sheets": {},
        }
        request = {"metrics": ["社零大盘"], "contexts": [{
            "task_id": "q", "query": "社零大盘中哪些类目表现较好",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "retail", "name": "社零大盘",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [{
                    "requirement_id": "category", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": ["类目"],
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(
            task["metric_bindings"]["社零大盘"], "限额以上企业零售额增速"
        )
        selected = task["intent_resolutions"]["retail"]["selected_candidate"]
        self.assertEqual(selected["confidence_detail"]["core"], 1.0)

    def test_contextual_breakdown_recall_cannot_rescue_wrong_core_metric(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "订单量": {
                    "aliases": ["各类目表现"], "unit": "万单", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["类目"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {"类目": {"aliases": [], "values": []}},
            "sheets": {},
        }
        request = {"metrics": ["广告收入"], "contexts": [{
            "task_id": "q", "query": "广告收入中哪些类目表现较好",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "revenue", "name": "广告收入", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{
                    "requirement_id": "category", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": ["类目"],
                    "semantic_text": "广告收入中各类目表现",
                }],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        self.assertNotEqual(
            (task["metric_statuses"].get("revenue") or {}).get("binding"), "订单量"
        )

    def test_single_breakdown_performance_requirement_uses_scoped_ratio_fact(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "限额以上企业零售额增速": {
                    "aliases": ["社零分类目同比增速", "分类目同比"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["社零类目"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {
                "社零类目": {"aliases": ["类目", "品类"], "values": ["家电"]},
            },
            "sheets": {},
        }
        request = {"metrics": ["社零大盘"], "contexts": [{
            "task_id": "q", "query": "社零大盘中哪些类目表现较好",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "retail", "name": "社零大盘",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [{
                    "requirement_id": "category", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": ["类目"],
                    "semantic_text": "社零大盘中各类目表现",
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(
            task["metric_bindings"]["社零大盘"], "限额以上企业零售额增速"
        )
        selected = task["intent_resolutions"]["retail"]["selected_candidate"]
        self.assertEqual(selected["semantic_role"], "compatible_alternative")
        self.assertEqual(selected["metric_object"], "ratio")
        self.assertEqual(selected["confidence_detail"]["core"], 1.0)

    def test_primary_breakdown_fact_outranks_compatible_ratio_fact(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "社零大盘": {
                    "aliases": ["社零"], "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["社零类目"],
                    "aggregation_mode": "additive",
                },
                "限额以上企业零售额增速": {
                    "aliases": ["社零分类目同比增速", "分类目同比"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["社零类目"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"社零类目": {"aliases": ["类目"], "values": ["家电"]}},
            "sheets": {},
        }
        request = {"metrics": ["社零大盘"], "contexts": [{
            "task_id": "q", "query": "社零大盘中哪些类目表现较好",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "retail", "name": "社零大盘",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [{
                    "requirement_id": "category", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": ["类目"],
                    "semantic_text": "社零大盘中各类目表现",
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["社零大盘"], "社零大盘")
        decision = next(
            item for item in result["resolution_decisions"]
            if item.get("kind") == "interpretation"
        )
        by_metric = {item["metric"]: item for item in decision["candidates"]}
        self.assertEqual(set(by_metric), {"社零大盘", "限额以上企业零售额增速"})
        self.assertLess(
            by_metric["社零大盘"]["semantic_tier"],
            by_metric["限额以上企业零售额增速"]["semantic_tier"],
        )

    def test_category_scoped_growth_does_not_replace_market_total_without_breakdown(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"限额以上企业零售额增速": {
                "aliases": ["社零分类目同比增速"], "unit": "%",
                "metric_object": "ratio", "supported_grains": ["month"],
                "dimensions": ["社零类目"], "aggregation_mode": "non_additive",
            }},
            "dimensions": {"社零类目": {"aliases": ["类目"], "values": ["家电"]}},
            "sheets": {},
        }
        request = {"metrics": ["社零大盘"], "contexts": [{
            "task_id": "q", "query": "社零大盘表现怎么样",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "retail", "name": "社零大盘",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [{
                    "requirement_id": "level", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": [],
                    "semantic_text": "社零大盘表现",
                }],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        self.assertNotIn("社零大盘", task["metric_bindings"])

    def test_complete_scoped_fact_stays_above_generic_selector_candidate(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "京东闭环电商佣金收入（3P佣金）": {
                    "aliases": [], "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
                "闭环电商佣金收入": {
                    "aliases": [], "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["平台"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {
                "无": {"aliases": [], "values": []},
                "平台": {"aliases": [], "values": ["京东"]},
            },
            "sheets": {},
        }
        request = {"metrics": ["京东闭环电商佣金收入（3P佣金）"], "contexts": [{
            "task_id": "q", "query": "京东闭环电商佣金收入（3P佣金）",
            "periods": ["2026-07"], "metrics": [{
                "metric_ref": "commission", "name": "京东闭环电商佣金收入（3P佣金）",
                "metric_object": "volume", "metric_object_provenance": "user_explicit",
                "unit": "亿元", "consumers": [{
                    "requirement_id": "level", "requirement_type": "fact_observations",
                    "periods": ["2026-07"], "breakdown_dimensions": [],
                }],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        self.assertEqual(
            task["metric_bindings"]["京东闭环电商佣金收入（3P佣金）"],
            "京东闭环电商佣金收入（3P佣金）",
        )

    def test_requirement_local_share_does_not_poison_market_level(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "邮政快递揽收量": {
                    "aliases": ["大盘快递", "快递总量"], "unit": "亿件",
                    "metric_object": "volume", "supported_grains": ["week"],
                    "dimensions": ["无"], "aggregation_mode": "additive",
                },
                "抖音包裹量": {
                    "aliases": ["抖音快递量"], "unit": "亿件",
                    "metric_object": "volume", "supported_grains": ["week"],
                    "dimensions": ["无"], "aggregation_mode": "additive",
                },
                "抖音包裹市占率-同比增速": {
                    "unit": "pp", "metric_object": "ratio",
                    "supported_grains": ["week"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"无": {"aliases": [], "values": []}},
            "sheets": {},
        }
        share_intent = {
            "operation": "share_level", "output_metric_object": "ratio",
            "operands": {
                "numerator": {"concept_ref": "express", "metric_constraints": [{
                    "kind": "dimension_filter", "operator": "eq",
                    "dimension_hint": "平台", "values": ["抖音"],
                    "provenance": "user_explicit",
                }]},
                "denominator": {"concept_ref": "express", "scope_kind": "market_total"},
            },
        }
        level = {
            "requirement_id": "market_level", "requirement_type": "fact_observations",
            "periods": ["2026-W33"], "breakdown_dimensions": [],
        }
        trend = {
            "requirement_id": "market_trend", "requirement_type": "derived_requirements",
            "derived_metric_id": "yoy_trend_change",
            "allowed_metric_objects": ["volume", "ratio"],
            "periods": ["2026-W33", "2025-W33", "2026-07", "2025-07"],
            "breakdown_dimensions": [],
        }
        share = {
            "requirement_id": "douyin_share", "requirement_type": "fact_observations",
            "periods": ["2026-W33"], "semantic_text": "抖音快递占比",
            "resolution_intent": share_intent,
        }
        request = {
            "metrics": ["大盘快递量", "抖音快递占比", "抖音包裹量", "邮政快递揽收量"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "大盘快递量，其中抖音快递占比",
                "periods": ["2026-W33"], "metrics": [
                    {
                        "metric_ref": "express", "name": "大盘快递量",
                        "metric_object": "volume", "unit": "待元信息解析",
                        "consumers": [level, trend, share],
                    },
                    {
                        "metric_ref": "__share", "name": "抖音快递占比",
                        "metric_object": "ratio", "unit": "待元信息解析",
                        "consumers": [share], "resolution_requirement_id": "douyin_share",
                    },
                ],
                "composition_intents": [{
                    "metric_ref": "__share", "requested_metric": "抖音快递占比",
                    "composition_id": "douyin_express_market_share",
                    "inputs": [
                        {"role": "numerator", "metric": "抖音包裹量"},
                        {"role": "denominator", "metric": "邮政快递揽收量"},
                    ],
                    "consumers": [share], "resolution_requirement_id": "douyin_share",
                    "logical_metric_ref": "express",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["大盘快递量"], "邮政快递揽收量")
        self.assertNotEqual(
            (task["metric_statuses"].get("__share") or {}).get("binding"),
            "抖音包裹市占率-同比增速",
        )
        binding = task["requirement_bindings"]["douyin_share"]
        self.assertEqual(binding["mode"], "registered_composition")
        self.assertEqual(binding["composition_id"], "douyin_express_market_share")
        self.assertFalse(any(
            item.get("metric_ref") == "express"
            for item in task["resolution_cases"]
        ))

    def test_precomputed_performance_fact_fulfills_compatible_sibling_requirements(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "限额以上企业零售额增速": {
                    "aliases": ["社零分类目同比增速", "分类目同比"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["社零类目"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {
                "社零类目": {"aliases": ["类目"], "values": []},
            },
            "sheets": {},
        }
        level = {
            "requirement_id": "category_level", "requirement_type": "fact_observations",
            "periods": ["2026-07"], "breakdown_dimensions": ["类目"],
            "semantic_text": "社零大盘中各类目表现",
        }
        yoy = {
            "requirement_id": "category_yoy", "requirement_type": "derived_requirements",
            "derived_metric_id": "yoy_growth", "allowed_metric_objects": ["volume", "ratio"],
            "periods": ["2026-07", "2025-07"], "breakdown_dimensions": ["类目"],
            "semantic_text": "社零大盘中各类目同比表现",
        }
        request = {"metrics": ["社零大盘"], "contexts": [{
            "task_id": "q", "query": "社零大盘中哪些类目表现较好",
            "periods": ["2026-07", "2025-07"], "metrics": [{
                "metric_ref": "retail", "name": "社零大盘",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [level, yoy],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        self.assertEqual(task["resolution_cases"], [])
        self.assertEqual(
            task["requirement_bindings"]["category_yoy"]["mode"],
            "source_derived_fact",
        )
        self.assertEqual(
            task["requirement_bindings"]["category_level"]["mode"],
            "source_scoped_fact",
        )

    def test_yoy_trend_can_use_registered_precomputed_series_variant(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "实物商品网上零售额同比增速": {
                    "aliases": ["线上社零同比", "线上社零增速"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"无": {"aliases": [], "values": []}},
            "sheets": {},
        }
        trend = {
            "requirement_id": "trend", "requirement_type": "derived_requirements",
            "derived_metric_id": "yoy_trend_change",
            "allowed_metric_objects": ["volume", "ratio"],
            "periods": ["2026-07", "2025-07", "2026-06", "2025-06"],
            "breakdown_dimensions": [],
        }
        request = {"metrics": ["线上社零"], "contexts": [{
            "task_id": "q", "query": "线上社零相比上个月涨幅变化",
            "periods": trend["periods"], "metrics": [{
                "metric_ref": "online", "name": "线上社零",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [trend],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        binding = task["requirement_bindings"]["trend"]
        self.assertEqual(binding["mode"], "source_derived_calculation")
        self.assertEqual(binding["source_period_roles"], ["analysis", "comparison"])
        self.assertEqual(binding["execution_derived_metric_id"], "period_change")

    def test_grain_terms_do_not_count_as_derived_semantics(self) -> None:
        metadata = {"aliases": ["月度支付GMV"]}
        self.assertEqual(
            _derived_semantic_score("yoy_growth", "月度支付GMV", metadata, self.policy),
            0.0,
        )

    @staticmethod
    def _constraint_consumer(operator: str = "eq") -> dict:
        return {
            "requirement_id": "scoped",
            "requirement_type": "fact_observations",
            "periods": ["2026-06"],
            "semantic_text": (
                "剔除抖音全国业务量" if operator == "exclude" else "京东全国业务量"
            ),
            "metric_constraints": [{
                "kind": "dimension_filter",
                "operator": operator,
                "values": ["抖音" if operator == "exclude" else "京东"],
                "dimension_hint": "平台",
                "provenance": "model_inferred",
            }],
        }

    def test_metadata_conditioned_recall_surfaces_candidate_outside_lexical_top3(self) -> None:
        metrics = {
            name: {
                "unit": "亿件", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["无"],
                "aggregation_mode": "additive",
            }
            for name in ("业务量甲", "业务量乙", "业务量丙")
        }
        metrics["全国业务量"] = {
            "unit": "亿件", "metric_object": "volume",
            "supported_grains": ["month"], "dimensions": ["平台"],
            "aggregation_mode": "additive",
        }
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": metrics,
            "dimensions": {"平台": {"aliases": [], "values": ["京东", "抖音"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer()
        request = {"metrics": ["业务量"], "contexts": [{
            "task_id": "q", "query": "京东业务量", "periods": ["2026-06"],
            "dimensions": ["平台"],
            "metrics": [{
                "metric_ref": "m", "name": "业务量", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["scoped"]
        self.assertEqual(binding["source_metric"], "全国业务量")
        self.assertEqual(binding["mode"], "member_selector")

    def test_full_scoped_fact_outranks_dimension_fallback(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "全国业务量": {
                    "unit": "亿件", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["平台"],
                    "aggregation_mode": "additive",
                },
                "京东全国业务量": {
                    "unit": "亿件", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {"平台": {"aliases": [], "values": ["京东", "抖音"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer()
        request = {"metrics": ["全国业务量"], "contexts": [{
            "task_id": "q", "query": "京东全国业务量", "periods": ["2026-06"],
            "dimensions": ["平台"], "metrics": [{
                "metric_ref": "m", "name": "全国业务量", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["scoped"]
        self.assertEqual(binding["source_metric"], "京东全国业务量")
        self.assertEqual(binding["mode"], "source_scoped_fact")

    @staticmethod
    def _commission_constraint_request(metric_name: str = "闭环电商佣金收入") -> dict:
        consumer = {
            "requirement_id": "scoped",
            "requirement_type": "fact_observations",
            "periods": ["2026-06"],
            "semantic_text": f"京东{metric_name}",
            "metric_constraints": [{
                "kind": "dimension_filter",
                "operator": "eq",
                "values": ["京东"],
                "dimension_hint": "平台",
                "provenance": "user_explicit",
            }],
        }
        return {"metrics": [metric_name], "contexts": [{
            "task_id": "q", "query": f"京东{metric_name}", "periods": ["2026-06"],
            "dimensions": ["平台"], "metrics": [{
                "metric_ref": "commission", "name": metric_name,
                "metric_object": "volume", "metric_object_provenance": "user_explicit",
                "unit": "亿元", "unit_provenance": "user_explicit",
                "consumers": [consumer],
            }],
        }]}

    @staticmethod
    def _commission_metric(*, dimensions: list[str]) -> dict:
        return {
            "aliases": [], "unit": "亿元", "metric_object": "volume",
            "supported_grains": ["month"], "dimensions": dimensions,
            "aggregation_mode": "additive",
        }

    def test_exact_member_selector_outranks_overqualified_scoped_fact(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "京东闭环电商佣金收入（3P佣金）": self._commission_metric(
                    dimensions=["无"]
                ),
                "闭环电商佣金收入": self._commission_metric(dimensions=["TOP6平台"]),
            },
            "dimensions": {
                "无": {"aliases": [], "values": []},
                "TOP6平台": {"aliases": ["平台"], "values": ["京东", "拼多多"]},
            },
            "sheets": {},
        }
        result = resolve_request_overlay(
            index, self._commission_constraint_request(), self.policy
        )
        task = result["task_resolutions"]["q"]
        binding = task["requirement_bindings"]["scoped"]
        self.assertEqual(binding["source_metric"], "闭环电商佣金收入")
        self.assertEqual(binding["mode"], "member_selector")
        self.assertEqual(
            binding["metric_constraints"][0]["source_dimension"], "TOP6平台"
        )
        decision = next(
            item for item in result["resolution_decisions"]
            if item["kind"] == "metric_constraint"
        )
        overqualified = next(
            item for item in decision["candidates"]
            if item["metric"] == "京东闭环电商佣金收入（3P佣金）"
        )
        self.assertEqual(overqualified["semantic_tier"], 1)
        self.assertTrue(overqualified["requires_confirmation"])
        self.assertEqual(
            overqualified["match_evidence"]["constraint"]["core_relation"],
            "overqualified",
        )

    def test_only_overqualified_scoped_fact_requires_confirmation(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "京东闭环电商佣金收入（3P佣金）": self._commission_metric(
                    dimensions=["无"]
                ),
            },
            "dimensions": {
                "无": {"aliases": [], "values": []},
                "TOP6平台": {"aliases": ["平台"], "values": ["京东", "拼多多"]},
            },
            "sheets": {},
        }
        result = resolve_request_overlay(
            index, self._commission_constraint_request(), self.policy
        )
        task = result["task_resolutions"]["q"]
        self.assertNotIn("scoped", task["requirement_bindings"])
        case = task["resolution_cases"][0]
        self.assertEqual(case["action"], "confirm")
        self.assertEqual(len(case["candidates"]), 1)
        candidate = case["candidates"][0]
        self.assertEqual(candidate["path"], "source_scoped_fact")
        self.assertEqual(candidate["semantic_tier"], 1)
        self.assertTrue(candidate["requires_confirmation"])

    def test_explicit_extra_core_keeps_scoped_fact_auto_binding(self) -> None:
        metric_name = "闭环电商佣金收入（3P佣金）"
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "京东闭环电商佣金收入（3P佣金）": self._commission_metric(
                    dimensions=["无"]
                ),
            },
            "dimensions": {
                "无": {"aliases": [], "values": []},
                "TOP6平台": {"aliases": ["平台"], "values": ["京东", "拼多多"]},
            },
            "sheets": {},
        }
        result = resolve_request_overlay(
            index, self._commission_constraint_request(metric_name), self.policy
        )
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["scoped"]
        self.assertEqual(
            binding["source_metric"], "京东闭环电商佣金收入（3P佣金）"
        )
        self.assertEqual(binding["mode"], "source_scoped_fact")

    def test_generic_dimension_hint_resolves_unique_physical_value_domain(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"全国业务量": {
                "unit": "亿件", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {
                "TOP4平台": {"aliases": [], "values": ["京东"]},
                "TOP6平台": {"aliases": [], "values": ["京东", "拼多多"]},
            },
            "sheets": {},
        }
        request = {"metrics": ["全国业务量"], "contexts": [{
            "task_id": "q", "query": "拼多多全国业务量", "periods": ["2026-06"],
            "metrics": [{
                "metric_ref": "m", "name": "全国业务量", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [self._constraint_consumer()],
            }],
        }]}
        consumer = request["contexts"][0]["metrics"][0]["consumers"][0]
        consumer["semantic_text"] = "拼多多全国业务量"
        consumer["metric_constraints"][0]["values"] = ["拼多多"]
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["scoped"]
        self.assertEqual(binding["metric_constraints"][0]["source_dimension"], "TOP6平台")
        decision = next(item for item in result["resolution_decisions"] if item["kind"] == "metric_constraint")
        self.assertEqual(
            decision["candidates"][0]["dimension_resolution"][0]["method"],
            "value_domain_unique_after_metric_capability",
        )

    def test_ambiguous_physical_dimensions_require_confirmation(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"全国业务量": {
                "unit": "亿件", "metric_object": "volume",
                "supported_grains": ["month"],
                "dimensions": ["TOP4平台", "TOP6平台"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {
                "TOP4平台": {"aliases": [], "values": ["拼多多"]},
                "TOP6平台": {"aliases": [], "values": ["拼多多"]},
            },
            "sheets": {},
        }
        consumer = self._constraint_consumer()
        consumer["semantic_text"] = "拼多多全国业务量"
        consumer["metric_constraints"][0]["values"] = ["拼多多"]
        request = {"metrics": ["全国业务量"], "contexts": [{
            "task_id": "q", "query": "拼多多全国业务量", "periods": ["2026-06"],
            "metrics": [{
                "metric_ref": "m", "name": "全国业务量", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        case = result["task_resolutions"]["q"]["resolution_cases"][0]
        self.assertEqual(case["action"], "confirm")
        self.assertEqual(
            {item["constraints"][0]["source_dimension"] for item in case["candidates"]},
            {"TOP4平台", "TOP6平台"},
        )

    def test_unique_domain_inference_is_generic_for_non_platform_dimension(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"成交额": {
                "unit": "亿元", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["销售大区"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {"销售大区": {"aliases": [], "values": ["华东", "华南"]}},
            "sheets": {},
        }
        consumer = {
            "requirement_id": "regional", "requirement_type": "fact_observations",
            "periods": ["2026-06"], "semantic_text": "华东成交额",
            "metric_constraints": [{
                "kind": "dimension_filter", "operator": "eq", "values": ["华东"],
                "dimension_hint": "地区", "provenance": "model_inferred",
            }],
        }
        request = {"metrics": ["成交额"], "contexts": [{
            "task_id": "q", "query": "华东成交额", "periods": ["2026-06"],
            "metrics": [{
                "metric_ref": "m", "name": "成交额", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["regional"]
        self.assertEqual(binding["metric_constraints"][0]["source_dimension"], "销售大区")

    def test_value_outside_supported_dimension_domains_blocks(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"成交额": {
                "unit": "亿元", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["销售大区"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {"销售大区": {"aliases": [], "values": ["华东"]}},
            "sheets": {},
        }
        consumer = {
            "requirement_id": "regional", "requirement_type": "fact_observations",
            "periods": ["2026-06"], "semantic_text": "海外成交额",
            "metric_constraints": [{
                "kind": "dimension_filter", "operator": "eq", "values": ["海外"],
                "dimension_hint": "地区", "provenance": "model_inferred",
            }],
        }
        request = {"metrics": ["成交额"], "contexts": [{
            "task_id": "q", "query": "海外成交额", "periods": ["2026-06"],
            "metrics": [{
                "metric_ref": "m", "name": "成交额", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        case = result["task_resolutions"]["q"]["resolution_cases"][0]
        self.assertEqual(case["action"], "block")
        self.assertIn(
            "constraint_dimension_unavailable", case["rejected_candidates"][0]["conflicts"]
        )

    def test_full_scoped_alias_strips_constraint_terms_before_core_scoring(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "剔除抖音包裹后邮政行业快递业务量完成量": {
                    "aliases": ["剔抖音大盘快递"],
                    "unit": "亿件", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
                "邮政行业快递业务量完成量": {
                    "aliases": ["大盘快递"],
                    "unit": "亿件", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {"TOP6平台": {"aliases": [], "values": ["抖音", "京东"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer("exclude")
        consumer["semantic_text"] = "2026年7月剔除抖音的大盘快递量"
        request = {"metrics": ["大盘快递量"], "contexts": [{
            "task_id": "q", "query": consumer["semantic_text"], "periods": ["2026-07"],
            "dimensions": ["平台"], "metrics": [{
                "metric_ref": "m", "name": "大盘快递量", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["scoped"]
        self.assertEqual(
            binding["source_metric"], "剔除抖音包裹后邮政行业快递业务量完成量"
        )
        self.assertEqual(binding["mode"], "source_scoped_fact")
        candidate = result["resolution_decisions"][0]["candidates"][0]
        self.assertEqual(candidate["confidence"], 0.9)
        self.assertEqual(candidate["lexical_confidence"], 0.666667)
        self.assertTrue(candidate["match_evidence"]["constraint"]["full_scope"])
        self.assertEqual(
            set(candidate["match_evidence"]["recall_channels"]),
            {"full_phrase", "core_metric"},
        )

    def test_constrained_source_derived_binds_only_derived_requirement(self) -> None:
        constraints = [{
            "kind": "dimension_filter", "operator": "eq", "values": ["京东"],
            "dimension_hint": "平台", "provenance": "user_explicit",
        }]
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "京东闭环电商佣金收入": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
                "京东闭环电商佣金收入同比增速": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"平台": {"aliases": [], "values": ["京东", "抖音"]}},
            "sheets": {},
        }
        request = {"metrics": ["闭环电商佣金收入"], "contexts": [{
            "task_id": "q", "query": "京东闭环电商佣金收入及同比",
            "periods": ["2025-07", "2026-07"], "dimensions": ["平台"],
            "metrics": [{
                "metric_ref": "commission", "name": "闭环电商佣金收入",
                "metric_object": "volume", "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [
                    {
                        "requirement_id": "level", "requirement_type": "fact_observations",
                        "periods": ["2026-07"], "semantic_text": "京东闭环电商佣金收入",
                        "metric_constraints": constraints,
                    },
                    {
                        "requirement_id": "yoy", "requirement_type": "derived_requirements",
                        "derived_metric_id": "yoy_growth", "period_roles": ["analysis"],
                        "periods": ["2026-07"], "semantic_text": "京东闭环电商佣金收入同比",
                        "metric_constraints": constraints,
                    },
                ],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        bindings = result["task_resolutions"]["q"]["requirement_bindings"]
        self.assertEqual(bindings["level"]["source_metric"], "京东闭环电商佣金收入")
        self.assertEqual(bindings["level"]["mode"], "source_scoped_fact")
        self.assertEqual(
            bindings["yoy"]["source_metric"], "京东闭环电商佣金收入同比增速"
        )
        self.assertEqual(bindings["yoy"]["mode"], "source_derived_fact")
        yoy_case = next(
            item for item in result["resolution_decisions"]
            if item.get("requirement_id") == "yoy"
        )
        selected = next(
            item for item in yoy_case["candidates"]
            if item["candidate_id"] == yoy_case["selected_candidate_id"]
        )
        self.assertEqual(
            selected["match_evidence"]["derived"]["status"],
            "source_derived_exact",
        )

    def test_dimension_match_cannot_rescue_wrong_core_or_independent_subtraction(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "广告收入": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["平台"],
                    "aggregation_mode": "additive",
                },
                "独立抖音佣金收入": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {"平台": {"aliases": [], "values": ["抖音", "京东"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer("exclude")
        consumer["semantic_text"] = "剔除抖音佣金收入"
        request = {"metrics": ["佣金收入"], "contexts": [{
            "task_id": "q", "query": "剔除抖音佣金收入", "periods": ["2026-06"],
            "dimensions": ["平台"], "metrics": [{
                "metric_ref": "m", "name": "佣金收入", "metric_object": "volume",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(
            result["task_resolutions"]["q"]["requirement_bindings"], {}
        )
        case = result["resolution_decisions"][0]
        self.assertTrue(case["rejected_candidates"])
        self.assertTrue(
            any(
                "constraint_dimension_unavailable" in item.get("conflicts", [])
                for item in case["rejected_candidates"]
            )
        )

    def test_context_guard_retries_only_core_failure_with_current_query_hint(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "剔除抖音包裹后邮政快递揽收量-同比增速": {
                    "aliases": ["剔抖音快递增速"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["week"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"平台": {"aliases": [], "values": ["抖音", "京东"]}},
            "sheets": {},
        }
        constraint = {
            "kind": "dimension_filter", "operator": "exclude", "values": ["抖音"],
            "dimension_hint": "平台", "provenance": "user_explicit",
        }
        consumer = {
            "requirement_id": "yoy", "requirement_type": "derived_requirements",
            "derived_metric_id": "yoy_growth", "periods": ["2026-W33"],
            "semantic_text": "2026年第33周剔除抖音的大盘快递量同比增速",
            "metric_constraints": [constraint],
        }
        request = {"metrics": ["大盘快递量"], "contexts": [{
            "task_id": "q", "query": "最新一周剔除抖音快递同比增速多少？",
            "periods": ["2026-W33"], "metrics": [{
                "metric_ref": "m", "name": "大盘快递量", "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        binding = result["task_resolutions"]["q"]["requirement_bindings"]["yoy"]
        self.assertEqual(
            binding["source_metric"], "剔除抖音包裹后邮政快递揽收量-同比增速"
        )
        case = result["resolution_decisions"][0]
        self.assertEqual(case["context_guard"]["mode"], "failure_fallback")
        self.assertEqual(case["context_guard"]["core_hint"], "快递")

    def test_context_hint_does_not_inherit_without_explicit_core(self) -> None:
        hint = extract_current_core_hint(
            "这个指标同比呢", "大盘快递量", self.policy,
            [{"operator": "exclude", "values": ["抖音"]}],
        )
        self.assertEqual(hint["hint"], "")
        self.assertTrue(hint["explicit_reference"])

    def test_context_guard_does_not_retry_dimension_or_grain_conflicts(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"错误指标": {
                "aliases": ["错误快递"], "unit": "亿件", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["无"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {"平台": {"aliases": [], "values": ["抖音"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer("exclude")
        consumer["semantic_text"] = "2026年第33周剔除抖音快递"
        request = {"metrics": ["大盘快递量"], "contexts": [{
            "task_id": "q", "query": "最新一周剔除抖音快递", "periods": ["2026-W33"],
            "metrics": [{
                "metric_ref": "m", "name": "大盘快递量", "metric_object": "volume",
                "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(
            result["task_resolutions"]["q"]["requirement_bindings"], {}
        )
        self.assertNotIn("context_guard", result["resolution_decisions"][0])

    def test_non_additive_exclude_is_not_auto_bound(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"全国业务量": {
                "unit": "%", "metric_object": "ratio", "supported_grains": ["month"],
                "dimensions": ["平台"], "aggregation_mode": "non_additive",
            }},
            "dimensions": {"平台": {"aliases": [], "values": ["抖音", "京东"]}},
            "sheets": {},
        }
        consumer = self._constraint_consumer("exclude")
        request = {"metrics": ["全国业务量"], "contexts": [{
            "task_id": "q", "query": "剔除抖音全国业务量", "periods": ["2026-06"],
            "dimensions": ["平台"], "metrics": [{
                "metric_ref": "m", "name": "全国业务量", "metric_object": "ratio",
                "unit": "待元信息解析", "consumers": [consumer],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(result["task_resolutions"]["q"]["requirement_bindings"], {})

    def test_model_inferred_unit_mismatch_is_soft_for_exact_metric(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"支付GMV": {"unit": "亿元", "dimensions": ["平台"]}},
            "dimensions": {"平台": {"values": ["京东"]}}, "sheets": {},
        }
        request = {"metrics": ["支付GMV"], "contexts": [{
            "task_id": "q", "query": "支付GMV", "metrics": [{
                "metric_ref": "m", "name": "支付GMV", "metric_object": "volume",
                "unit": "万元", "unit_provenance": "model_inferred",
                "provenance": "user_explicit",
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(
            result["task_resolutions"]["q"]["metric_bindings"]["支付GMV"], "支付GMV"
        )

    def test_joint_domain_evidence_auto_resolves_top6_without_enumerated_mapping(self) -> None:
        request = {
            "metrics": ["支付GMV"],
            "dimensions": ["平台"],
            "contexts": [{
                "task_id": "q1",
                "query": "TOP6支付GMV",
                "metrics": [{"name": "支付GMV", "metric_object": "volume", "unit": "待元信息解析", "provenance": "user_explicit"}],
                "resolution_patches": [],
            }],
        }
        result = resolve_request_overlay(ambiguous_platform_index(), request, self.policy)
        block = result["index"]["sheets"]["month"]["blocks"]["支付GMV"]
        self.assertEqual(block["dimension"], "TOP6平台")
        self.assertEqual(result["dimension_bindings"]["平台"], "TOP6平台")
        self.assertEqual(result["resolution_cases"], [])
        self.assertEqual(result["resolution_decisions"][0]["action"], "auto")

    def test_overlay_recovers_periodic_sheet_with_only_unresolved_blocks(self) -> None:
        index = ambiguous_platform_index()
        index["sheets"]["month"]["available"] = False
        index["sheets"]["month"]["reason"] = "unresolved"
        request = {
            "metrics": ["支付GMV"],
            "dimensions": ["平台"],
            "contexts": [{
                "task_id": "q1",
                "query": "TOP6支付GMV",
                "metrics": [{
                    "name": "支付GMV",
                    "metric_object": "volume",
                    "unit": "待元信息解析",
                    "provenance": "user_explicit",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        self.assertTrue(result["index"]["sheets"]["month"]["available"])
        self.assertEqual(result["index"]["sheets"]["month"]["periods"], {"2026-07": "B"})

    def test_nonstandard_metric_auto_binding_is_audited_without_confirmation(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1106, "schema_hash": "schema"},
            "metrics": {
                "限额以上企业零售额增速": {"unit": "%", "dimensions": ["社零类目"]},
                "实物商品网上零售额增速": {"unit": "%", "dimensions": ["社零类目"]},
            },
            "dimensions": {"社零类目": {"values": ["限额以上企业零售额"]}},
            "sheets": {},
        }
        request = {
            "metrics": ["限额以上企业零售额-当月增速"],
            "dimensions": ["社零类目"],
            "contexts": [{
                "task_id": "q4",
                "query": "2026年5月分社零类目的限额以上企业零售额同比增速是多少？",
                "metrics": [{
                    "name": "限额以上企业零售额-当月增速",
                    "metric_object": "ratio",
                    "unit": "待元信息解析",
                    "provenance": "user_explicit",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(
            result["metric_bindings"]["限额以上企业零售额-当月增速"],
            "限额以上企业零售额增速",
        )
        self.assertEqual(result["resolution_cases"], [])
        self.assertEqual(result["resolution_decisions"][0]["action"], "auto")

    def test_exact_metric_object_conflict_is_blocked(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"结算率": {"unit": "亿元", "dimensions": ["平台"]}},
            "dimensions": {"平台": {"values": ["京东"]}},
            "sheets": {},
        }
        request = {
            "metrics": ["结算率"],
            "dimensions": [],
            "contexts": [{
                "task_id": "q",
                "query": "结算率",
                "metrics": [{
                    "metric_ref": "rate",
                    "name": "结算率",
                    "metric_object": "ratio",
                    "unit": "待元信息解析",
                    "provenance": "user_explicit",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        self.assertNotIn("结算率", result["metric_bindings"])
        case = result["task_resolutions"]["q"]["resolution_cases"][0]
        self.assertEqual(case["action"], "block")
        self.assertIn("metric_object_mismatch", case["candidates"][0]["conflicts"])

    def test_exact_unit_conflict_is_blocked(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"支付GMV": {"unit": "亿元", "dimensions": ["平台"]}},
            "dimensions": {"平台": {"values": ["京东"]}},
            "sheets": {},
        }
        request = {
            "metrics": ["支付GMV"],
            "dimensions": [],
            "contexts": [{
                "task_id": "q",
                "query": "支付GMV",
                "metrics": [{
                    "metric_ref": "gmv",
                    "name": "支付GMV",
                    "metric_object": "volume",
                    "unit": "万元",
                    "provenance": "user_explicit",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        case = result["task_resolutions"]["q"]["resolution_cases"][0]
        self.assertIn("unit_mismatch", case["candidates"][0]["conflicts"])

    def test_conflicting_exact_alias_requires_confirmation(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "成交额A": {"aliases": ["交易额"], "unit": "亿元"},
                "成交额B": {"aliases": ["交易额"], "unit": "亿元"},
            },
            "dimensions": {},
            "sheets": {},
        }
        request = {
            "metrics": ["交易额"],
            "dimensions": [],
            "contexts": [{
                "task_id": "q",
                "query": "交易额",
                "metrics": [{
                    "metric_ref": "gmv",
                    "name": "交易额",
                    "metric_object": "volume",
                    "unit": "待元信息解析",
                    "provenance": "user_explicit",
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        case = result["task_resolutions"]["q"]["resolution_cases"][0]
        self.assertEqual(case["action"], "confirm")
        self.assertEqual(len(case["candidates"]), 2)

    def test_same_requested_name_is_bound_per_task_without_global_override(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "规模指标": {"aliases": ["业务指标"], "unit": "亿元"},
                "比例指标": {"aliases": ["业务指标"], "unit": "%"},
            },
            "dimensions": {},
            "sheets": {},
        }
        request = {
            "metrics": ["业务指标"],
            "dimensions": [],
            "contexts": [
                {"task_id": "volume", "query": "业务指标", "metrics": [{
                    "metric_ref": "m", "name": "业务指标", "metric_object": "volume",
                    "unit": "待元信息解析", "provenance": "user_explicit",
                }]},
                {"task_id": "ratio", "query": "业务指标", "metrics": [{
                    "metric_ref": "m", "name": "业务指标", "metric_object": "ratio",
                    "unit": "待元信息解析", "provenance": "user_explicit",
                }]},
            ],
        }
        result = resolve_request_overlay(index, request, self.policy)
        self.assertNotIn("业务指标", result["metric_bindings"])
        self.assertEqual(
            result["task_resolutions"]["volume"]["metric_bindings"]["业务指标"],
            "规模指标",
        )
        self.assertEqual(
            result["task_resolutions"]["ratio"]["metric_bindings"]["业务指标"],
            "比例指标",
        )

    def test_resolved_blocks_keep_top4_and_top6_bindings_metric_scoped(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1137, "schema_hash": "schema"},
            "metrics": {
                "DAC": {"unit": "万人", "dimensions": ["TOP4平台"]},
                "支付GMV": {"unit": "亿元", "dimensions": ["TOP6平台"]},
            },
            "dimensions": {
                "TOP4平台": {"values": ["淘系", "拼多多", "京东", "抖音"]},
                "TOP6平台": {"values": ["淘系", "拼多多", "京东", "抖音", "快手", "视频号"]},
            },
            "sheets": {"month": {
                "available": True,
                "periods": {"2026-07": "B"},
                "blocks": {
                    "DAC": {"dimension": "TOP4平台", "rows": {}},
                    "支付GMV": {"dimension": "TOP6平台", "rows": {}},
                },
            }},
        }
        request = {
            "metrics": ["DAC", "支付GMV"],
            "dimensions": ["平台"],
            "contexts": [
                {"task_id": "dac", "query": "分平台DAC", "dimensions": ["平台"], "metrics": [{"name": "DAC"}]},
                {"task_id": "gmv", "query": "分平台支付GMV", "dimensions": ["平台"], "metrics": [{"name": "支付GMV"}]},
            ],
        }
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(result["metric_dimension_bindings"]["DAC"]["平台"], "TOP4平台")
        self.assertEqual(result["metric_dimension_bindings"]["支付GMV"]["平台"], "TOP6平台")
        self.assertNotIn("平台", result["dimension_bindings"])

    def test_patch_is_scoped_to_revision_and_policy(self) -> None:
        index = ambiguous_platform_index()
        index["dimensions"]["TOP6平台"]["values"].append("其他")
        request = {
            "metrics": ["支付GMV"],
            "dimensions": ["平台"],
            "contexts": [{
                "task_id": "q1",
                "query": "平台支付GMV",
                "metrics": [{"name": "支付GMV", "metric_object": "volume", "unit": "待元信息解析"}],
                "resolution_patches": [],
            }],
        }
        first = resolve_request_overlay(index, request, self.policy)
        case = first["resolution_cases"][0]
        self.assertEqual(case["action"], "confirm")
        patch = {
            "case_id": case["case_id"],
            "candidate_id": case["candidates"][0]["candidate_id"],
            "source_revision": case["source_revision"],
            "schema_hash": case["schema_hash"],
            "resolution_policy_hash": case["resolution_policy_hash"],
        }
        request["contexts"][0]["resolution_patches"] = [patch]
        resolved = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(resolved["resolution_cases"], [])
        index["source"]["revision"] += 1
        stale = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(stale["resolution_cases"][0]["case_id"], case["case_id"])
        self.assertEqual(stale["resolution_cases"][0]["patch_status"], "stale_patch")

    def test_query_wide_modifier_does_not_rescue_structurally_infeasible_fact(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
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
                "month": {
                    "available": True,
                    "periods": {"2026-06": "B", "2026-07": "C"},
                    "blocks": {"实物商品网上零售额同比增速": {"dimension": "无"}},
                },
                "year": {
                    "available": True,
                    "periods": {"2026": "B"},
                    "blocks": {"实物商品网上零售额": {"dimension": "无"}},
                },
            },
        }
        request = {
            "metrics": ["线上社零"],
            "dimensions": [],
            "contexts": [{
                "task_id": "q", "query": "7月线上社零表现怎么样，较上月涨幅变化如何",
                "periods": ["2026-06", "2026-07"], "dimensions": [],
                "metrics": [{
                    "metric_ref": "retail", "name": "线上社零", "metric_object": "volume",
                    "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                    "provenance": "user_explicit",
                    "consumers": [{"requirement_type": "fact_observations"}],
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("线上社零", task["metric_bindings"])
        case = task["resolution_cases"][0]
        self.assertEqual(case["action"], "block")
        self.assertEqual(case["candidates"], [])

    def test_query_wide_modifier_does_not_create_fact_candidate_ambiguity(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "业务规模": {"aliases": ["业务"], "unit": "亿元", "metric_object": "volume", "supported_grains": ["month"], "dimensions": ["无"]},
                "业务同比增速": {"aliases": ["业务同比增速", "业务增速"], "unit": "%", "metric_object": "ratio", "supported_grains": ["month"], "dimensions": ["无"]},
            },
            "dimensions": {},
            "sheets": {"month": {
                "available": True, "periods": {"2026-07": "B"},
                "blocks": {"业务规模": {"dimension": "无"}, "业务同比增速": {"dimension": "无"}},
            }},
        }
        request = {"metrics": ["业务"], "contexts": [{
            "task_id": "q", "query": "业务表现和涨幅", "periods": ["2026-07"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "业务", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{"requirement_type": "fact_observations"}],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        self.assertEqual(
            result["task_resolutions"]["q"]["metric_bindings"]["业务"],
            "业务规模",
        )

    def test_intent_candidate_respects_registered_operation_object_capability(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "业务规模": {"aliases": ["业务"], "unit": "亿元", "metric_object": "volume", "supported_grains": ["month"], "dimensions": ["无"]},
                "业务同比增速": {"aliases": ["业务同比增速", "业务增速"], "unit": "%", "metric_object": "ratio", "supported_grains": ["month"], "dimensions": ["无"]},
            },
            "dimensions": {},
            "sheets": {"month": {"available": True, "periods": {"2026-07": "B"}, "blocks": {
                "业务规模": {"dimension": "无"}, "业务同比增速": {"dimension": "无"},
            }}},
        }
        request = {"metrics": ["业务"], "contexts": [{
            "task_id": "q", "query": "业务表现和涨幅", "periods": ["2026-07"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "业务", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{
                    "requirement_type": "derived_requirements",
                    "allowed_metric_objects": ["volume"],
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["业务"], "业务规模")
        candidates = next(
            item for item in result["resolution_decisions"]
            if item.get("kind") == "interpretation"
        )["candidates"]
        self.assertEqual([item["metric"] for item in candidates], ["业务规模"])

    def test_vague_performance_uses_unique_growth_alias_when_base_is_absent(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "实物商品网上零售额同比增速": {
                    "aliases": ["线上社零同比", "增速"], "unit": "%",
                    "metric_object": "ratio", "supported_grains": ["month"],
                    "dimensions": ["无"],
                },
                "社会消费品零售增速": {
                    "aliases": ["社零总额同比", "增速"], "unit": "%",
                    "metric_object": "ratio", "supported_grains": ["month"],
                    "dimensions": ["无"],
                },
            },
            "dimensions": {},
            "sheets": {"month": {
                "available": True, "periods": {"2026-04": "B", "2026-05": "C"},
                "blocks": {"实物商品网上零售额同比增速": {"dimension": "无"}},
            }},
        }
        request = {"metrics": ["线上社零"], "contexts": [{
            "task_id": "q", "query": "线上社零表现怎么样", "periods": ["2026-05"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "线上社零", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "线上社零表现怎么样",
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(
            task["metric_bindings"]["线上社零"], "实物商品网上零售额同比增速"
        )
        selected = task["intent_resolutions"]["m"]["selected_candidate"]
        self.assertEqual(selected["semantic_role"], "compatible_alternative")
        self.assertEqual(selected["metric_object"], "ratio")

    def test_primary_fact_outranks_growth_for_vague_performance_without_breakdown(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "线上社零": {
                    "aliases": [], "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                },
                "实物商品网上零售额同比增速": {
                    "aliases": ["线上社零同比"], "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                },
            },
            "dimensions": {},
            "sheets": {},
        }
        request = {"metrics": ["线上社零"], "contexts": [{
            "task_id": "q", "query": "线上社零表现怎么样", "periods": ["2026-05"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "线上社零", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "线上社零表现怎么样",
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["线上社零"], "线上社零")
        selected = task["intent_resolutions"]["m"]["selected_candidate"]
        self.assertEqual(selected["semantic_role"], "primary")
        decision = next(
            item for item in result["resolution_decisions"]
            if item.get("kind") == "interpretation"
        )
        by_metric = {item["metric"]: item for item in decision["candidates"]}
        self.assertLess(
            by_metric["线上社零"]["semantic_tier"],
            by_metric["实物商品网上零售额同比增速"]["semantic_tier"],
        )

    def test_explicit_volume_performance_rejects_growth_only_alias(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"实物商品网上零售额同比增速": {
                "aliases": ["线上社零同比"], "unit": "%", "metric_object": "ratio",
                "supported_grains": ["month"], "dimensions": ["无"],
            }},
            "dimensions": {},
            "sheets": {},
        }
        request = {"metrics": ["线上社零"], "contexts": [{
            "task_id": "q", "query": "线上社零规模表现", "periods": ["2026-05"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "线上社零", "metric_object": "volume",
                "metric_object_provenance": "user_explicit", "unit": "待元信息解析",
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "semantic_text": "线上社零规模表现",
                }],
            }],
        }]}
        task = resolve_request_overlay(index, request, self.policy)["task_resolutions"]["q"]
        self.assertNotIn("线上社零", task["metric_bindings"])

    def test_core_online_rate_outranks_derived_word_overlap(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "线上化率": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
                "社会消费品零售总额:商品零售增速": {
                    "aliases": ["社零商品同比增速", "社零商品同比"],
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {},
            "sheets": {},
        }
        request = {"metrics": ["社零商品线上化率"], "contexts": [{
            "task_id": "q",
            "query": "26年7月，社零商品线上化率水平？线上化率同比变化如何？",
            "periods": ["2025-07", "2026-07"],
            "dimensions": [],
            "metrics": [{
                "metric_ref": "online_rate",
                "name": "社零商品线上化率",
                "metric_object": "ratio",
                "metric_object_provenance": "model_inferred",
                "unit": "待元信息解析",
                "consumers": [
                    {"requirement_type": "fact_observations"},
                    {
                        "requirement_type": "derived_requirements",
                        "derived_metric_id": "yoy_growth",
                        "allowed_metric_objects": ["ratio"],
                    },
                ],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["社零商品线上化率"], "线上化率")
        self.assertEqual(task["requirement_bindings"], {})
        selected = task["intent_resolutions"]["online_rate"]["selected_candidate"]
        self.assertEqual(selected["intent_id"], "declared_metric")
        self.assertEqual(selected["confidence_detail"]["core"], 1.0)

    def test_core_and_derived_source_metric_binds_only_derived_requirement(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "线上化率": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
                "线上化率同比增速": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {}, "sheets": {},
        }
        request = {"metrics": ["线上化率"], "contexts": [{
            "task_id": "q", "query": "线上化率水平和同比", "periods": ["2025-07", "2026-07"],
            "metrics": [{
                "metric_ref": "rate", "name": "线上化率", "metric_object": "ratio",
                "unit": "待元信息解析", "consumers": [
                    {"requirement_id": "level", "requirement_type": "fact_observations"},
                    {
                        "requirement_id": "yoy", "requirement_type": "derived_requirements",
                        "derived_metric_id": "yoy_growth", "period_roles": ["analysis", "analysis_last_year"],
                        "allowed_metric_objects": ["ratio"],
                    },
                ],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["线上化率"], "线上化率")
        self.assertEqual(task["requirement_bindings"]["yoy"]["source_metric"], "线上化率同比增速")
        self.assertEqual(task["requirement_bindings"]["yoy"]["mode"], "source_derived_fact")

    def test_equal_strength_core_candidates_require_confirmation(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                name: {
                    "aliases": ["线上化率"], "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                }
                for name in ("商品线上化率", "服务线上化率")
            },
            "dimensions": {}, "sheets": {},
        }
        request = {"metrics": ["线上化率"], "contexts": [{
            "task_id": "q", "query": "7月线上化率", "periods": ["2026-07"],
            "metrics": [{
                "metric_ref": "rate", "name": "线上化率", "metric_object": "ratio",
                "unit": "待元信息解析",
                "consumers": [{"requirement_type": "fact_observations"}],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("线上化率", task["metric_bindings"])
        case = task["resolution_cases"][0]
        self.assertEqual(case["action"], "confirm")
        self.assertEqual(
            {item["metric"] for item in case["candidates"]},
            {"商品线上化率", "服务线上化率"},
        )

    def test_equal_strength_source_derived_candidates_do_not_bind_requirement(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "线上化率": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
                **{
                    name: {
                        "aliases": ["线上化率同比增速"], "unit": "%",
                        "metric_object": "ratio", "supported_grains": ["month"],
                        "dimensions": ["无"], "aggregation_mode": "non_additive",
                    }
                    for name in ("商品线上化率同比增速", "服务线上化率同比增速")
                },
            },
            "dimensions": {}, "sheets": {},
        }
        request = {"metrics": ["线上化率"], "contexts": [{
            "task_id": "q", "query": "线上化率水平和同比", "periods": ["2025-07", "2026-07"],
            "metrics": [{
                "metric_ref": "rate", "name": "线上化率", "metric_object": "ratio",
                "unit": "待元信息解析", "consumers": [
                    {"requirement_id": "level", "requirement_type": "fact_observations"},
                    {
                        "requirement_id": "yoy", "requirement_type": "derived_requirements",
                        "derived_metric_id": "yoy_growth",
                        "period_roles": ["analysis", "analysis_last_year"],
                        "allowed_metric_objects": ["ratio"],
                    },
                ],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_bindings"]["线上化率"], "线上化率")
        self.assertNotIn("yoy", task["requirement_bindings"])

    def test_additive_finer_grain_is_structurally_viable_without_period_expansion(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"支付GMV": {
                "unit": "亿元", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["无"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {},
            "sheets": {},
        }
        request = {"metrics": ["支付GMV"], "contexts": [{
            "task_id": "q", "query": "季度支付GMV", "periods": ["2026-Q2"],
            "metrics": [{
                "metric_ref": "gmv", "name": "支付GMV", "metric_object": "volume",
                "unit": "待元信息解析",
                "consumers": [{"requirement_type": "fact_observations"}],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        selected = result["task_resolutions"]["q"]["intent_resolutions"]["gmv"]["selected_candidate"]
        self.assertEqual(selected["path"], "aggregate_fact")
        self.assertEqual(selected["capability"]["grains"][0]["source_grain"], "month")

    def test_no_breakdown_does_not_reject_dimensioned_metric(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"支付GMV": {
                "unit": "亿元", "metric_object": "volume",
                "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                "aggregation_mode": "additive",
            }},
            "dimensions": {"TOP6平台": {"aliases": ["平台"], "values": []}},
            "sheets": {},
        }
        request = {"metrics": ["支付GMV"], "contexts": [{
            "task_id": "q", "query": "7月支付GMV", "periods": ["2026-07"],
            "dimensions": ["平台"],
            "metrics": [{
                "metric_ref": "gmv", "name": "支付GMV", "metric_object": "volume",
                "unit": "待元信息解析", "required_breakdown_dimensions": [],
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "dimensions": ["平台"], "breakdown_dimensions": [],
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        selected = result["task_resolutions"]["q"]["intent_resolutions"]["gmv"]["selected_candidate"]
        self.assertEqual(selected["metric"], "支付GMV")
        self.assertEqual(selected["capability"]["dimension"]["status"], "deferred")
        self.assertEqual(
            selected["capability"]["dimension"]["reason"], "no_breakdown_requested"
        )

    def test_explicit_unsupported_breakdown_rejects_metric_candidate(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {"线上化率": {
                "unit": "%", "metric_object": "ratio",
                "supported_grains": ["month"], "dimensions": ["无"],
                "aggregation_mode": "non_additive",
            }},
            "dimensions": {
                "平台": {"aliases": [], "values": []},
                "无": {"aliases": [], "values": []},
            },
            "sheets": {},
        }
        request = {"metrics": ["线上化率"], "contexts": [{
            "task_id": "q", "query": "分平台线上化率", "periods": ["2026-07"],
            "metrics": [{
                "metric_ref": "rate", "name": "线上化率", "metric_object": "ratio",
                "unit": "待元信息解析", "required_breakdown_dimensions": ["平台"],
                "consumers": [{
                    "requirement_type": "fact_observations",
                    "breakdown_dimensions": ["平台"],
                }],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("线上化率", task["metric_bindings"])
        self.assertEqual(task["resolution_cases"][0]["candidates"], [])

    def test_registered_composition_is_structurally_viable_from_additive_inputs(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                name: {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                }
                for name in ("结算GMV", "支付GMV")
            },
            "dimensions": {},
            "sheets": {},
        }
        consumers = [{
            "requirement_id": "settlement_rate",
            "requirement_type": "metric_compositions",
            "periods": ["2026-Q2"],
        }]
        request = {
            "metrics": ["结算率", "结算GMV", "支付GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "季度结算率", "periods": ["2026-Q2"],
                "metrics": [{
                    "metric_ref": "rate", "name": "结算率", "metric_object": "ratio",
                    "unit": "待元信息解析", "consumers": consumers,
                }],
                "composition_intents": [{
                    "metric_ref": "rate", "requested_metric": "结算率",
                    "composition_id": "competitor_settlement_rate",
                    "inputs": [
                        {"role": "numerator", "metric": "结算GMV"},
                        {"role": "denominator", "metric": "支付GMV"},
                    ],
                    "consumers": consumers,
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        composition = task["composition_resolutions"][0]
        self.assertEqual(composition["fallback_status"], "ready")
        self.assertEqual(task["resolution_cases"], [])
        self.assertEqual(task["metric_statuses"]["rate"]["status"], "composition_deferred")
        self.assertEqual(
            task["intent_resolutions"]["rate"]["status"], "composition_deferred"
        )
        for status in composition["input_statuses"].values():
            self.assertEqual(status["status"], "bound")
            self.assertEqual(status["structural_capability"][0]["path"], "aggregate_fact")

    def test_direct_metric_still_wins_over_registered_composition(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "综合结算TR": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
                **{
                    name: {
                        "unit": "亿元", "metric_object": "volume",
                        "supported_grains": ["month"], "dimensions": ["无"],
                        "aggregation_mode": "additive",
                    }
                    for name in ("闭环电商广告收入", "闭环电商佣金收入", "结算GMV")
                },
            },
            "dimensions": {},
            "sheets": {},
        }
        consumers = [{
            "requirement_id": "comprehensive_tr",
            "requirement_type": "metric_compositions",
            "periods": ["2026-07"],
        }]
        request = {
            "metrics": ["综合结算TR", "闭环电商广告收入", "闭环电商佣金收入", "结算GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "综合结算TR", "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "tr", "name": "综合结算TR",
                    "metric_object": "ratio", "unit": "待元信息解析",
                    "consumers": consumers,
                }],
                "composition_intents": [{
                    "metric_ref": "tr", "requested_metric": "综合结算TR",
                    "composition_id": "competitor_comprehensive_settlement_tr",
                    "inputs": [
                        {"role": "ad_revenue", "metric": "闭环电商广告收入"},
                        {"role": "commission_revenue", "metric": "闭环电商佣金收入"},
                        {"role": "settlement_gmv", "metric": "结算GMV"},
                    ],
                    "consumers": consumers,
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_statuses"]["tr"]["status"], "bound")
        self.assertEqual(task["metric_bindings"]["综合结算TR"], "综合结算TR")
        self.assertEqual(task["resolution_cases"], [])
        self.assertEqual(
            task["composition_resolutions"][0]["selected_fulfillment"]["candidate_type"],
            "direct_fact",
        )

    def test_implicit_query_fallback_leaf_does_not_fulfill_composed_metric(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                **{
                    name: {
                        "unit": "亿元", "metric_object": "volume",
                        "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                        "aggregation_mode": "additive",
                    }
                    for name in ("结算GMV", "支付GMV")
                },
                "淘系结算率同比增速": {
                    "unit": "%", "metric_object": "ratio",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "non_additive",
                },
            },
            "dimensions": {"TOP6平台": {"aliases": ["平台"], "values": ["淘系"]}},
            "sheets": {},
        }
        constraint = {
            "constraint_id": "platform", "kind": "dimension_filter",
            "operator": "eq", "values": ["淘系"],
            "dimension_hint": "TOP6平台", "provenance": "user_explicit",
        }
        consumer = {
            "requirement_id": "rate_level", "requirement_type": "fact_observations",
            "periods": ["2026-07"], "period_roles": ["analysis"],
            "semantic_text": "淘系结算率指标值",
            "metric_constraints": [constraint],
        }
        yoy_consumer = {
            "requirement_id": "rate_yoy", "requirement_type": "derived_requirements",
            "derived_metric_id": "yoy_growth", "periods": ["2026-07"],
            "period_roles": ["analysis"], "semantic_text": "淘系结算率同比变化",
            "metric_constraints": [constraint], "allowed_metric_objects": ["ratio"],
        }
        request = {
            "metrics": ["结算率", "结算GMV", "支付GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "26年7月淘系结算GMV表现及同比归因",
                "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "rate", "name": "结算率",
                    "metric_object": "ratio", "metric_object_provenance": "model_inferred",
                    "unit": "待元信息解析", "consumers": [consumer, yoy_consumer],
                }],
                "composition_intents": [{
                    "metric_ref": "rate", "requested_metric": "结算率",
                    "composition_id": "registered_rate",
                    "inputs": [
                        {"role": "numerator", "metric": "结算GMV"},
                        {"role": "denominator", "metric": "支付GMV"},
                    ],
                    "consumers": [consumer, yoy_consumer],
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(
            task["requirement_bindings"]["rate_level"]["mode"],
            "registered_composition",
        )
        self.assertIn("rate_yoy", task["requirement_bindings"], task)
        self.assertEqual(
            task["requirement_bindings"]["rate_yoy"]["source_metric"],
            "淘系结算率同比增速",
        )
        self.assertEqual(
            task["requirement_bindings"]["rate_yoy"]["mode"], "source_derived_fact"
        )
        self.assertEqual(task["metric_statuses"]["rate"]["status"], "composition_deferred")
        self.assertEqual(
            task["composition_resolutions"][0]["selected_fulfillment"]["candidate_type"],
            "registered_composition",
        )
        decision = next(
            item for item in result["resolution_decisions"]
            if item.get("requirement_id") == "rate_level"
        )
        self.assertEqual(decision["activation"], "deferred_to_registered_composition")

    def test_explicit_fallback_reference_is_not_suppressed_by_composition(self) -> None:
        resolution = {"context_guard": {
            "mode": "failure_fallback",
            "reason": "legacy_core_semantic_gate_failed",
            "hint_source": "query",
            "explicit_reference": True,
        }}
        selected = {"metric": "履约金额"}
        intent = {"inputs": [{"role": "numerator", "metric": "履约金额"}]}
        self.assertFalse(
            _implicit_fallback_selects_composition_leaf(resolution, selected, intent)
        )

    def test_direct_ambiguity_is_not_suppressed_by_registered_composition(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                **{
                    name: {
                        "aliases": ["综合结算TR"], "unit": "%",
                        "metric_object": "ratio", "supported_grains": ["month"],
                        "dimensions": ["无"], "aggregation_mode": "non_additive",
                    }
                    for name in ("平台综合结算TR", "行业综合结算TR")
                },
                **{
                    name: {
                        "unit": "亿元", "metric_object": "volume",
                        "supported_grains": ["month"], "dimensions": ["无"],
                        "aggregation_mode": "additive",
                    }
                    for name in ("闭环电商广告收入", "闭环电商佣金收入", "结算GMV")
                },
            },
            "dimensions": {},
            "sheets": {},
        }
        consumers = [{
            "requirement_id": "comprehensive_tr",
            "requirement_type": "metric_compositions",
            "periods": ["2026-07"],
        }]
        request = {
            "metrics": ["综合结算TR"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "综合结算TR", "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "tr", "name": "综合结算TR",
                    "metric_object": "ratio", "unit": "待元信息解析",
                    "consumers": consumers,
                }],
                "composition_intents": [{
                    "metric_ref": "tr", "requested_metric": "综合结算TR",
                    "composition_id": "competitor_comprehensive_settlement_tr",
                    "inputs": [
                        {"role": "ad_revenue", "metric": "闭环电商广告收入"},
                        {"role": "commission_revenue", "metric": "闭环电商佣金收入"},
                        {"role": "settlement_gmv", "metric": "结算GMV"},
                    ],
                    "consumers": consumers,
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["resolution_cases"][0]["action"], "confirm")
        self.assertEqual(task["resolution_cases"][0]["kind"], "interpretation")
        composition = task["composition_resolutions"][0]
        self.assertIsNone(composition["selected_fulfillment"])
        self.assertEqual(
            composition["fulfillment_candidates"][0]["status"], "infeasible"
        )

    def test_unregistered_metric_without_direct_candidate_still_blocks(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {}, "dimensions": {}, "sheets": {},
        }
        request = {"metrics": ["自定义经营率"], "contexts": [{
            "task_id": "q", "query": "自定义经营率", "periods": ["2026-07"],
            "metrics": [{
                "metric_ref": "rate", "name": "自定义经营率",
                "metric_object": "ratio", "unit": "待元信息解析",
                "consumers": [{"requirement_type": "fact_observations"}],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["metric_statuses"]["rate"]["status"], "not_executable")
        self.assertEqual(task["resolution_cases"][0]["action"], "block")

    def test_multi_input_composition_requires_every_leaf_to_bind(self) -> None:
        metric_names = ("闭环电商广告收入", "闭环电商佣金收入", "支付GMV")
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                name: {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                }
                for name in metric_names
            },
            "dimensions": {},
            "sheets": {},
        }
        consumers = [{
            "requirement_id": "comprehensive_tr",
            "requirement_type": "metric_compositions",
            "periods": ["2026-07"],
        }]
        intent = {
            "metric_ref": "tr", "requested_metric": "综合支付TR",
            "composition_id": "competitor_comprehensive_payment_tr",
            "inputs": [
                {"role": "ad_revenue", "metric": "闭环电商广告收入"},
                {"role": "commission_revenue", "metric": "闭环电商佣金收入"},
                {"role": "payment_gmv", "metric": "支付GMV"},
            ],
            "consumers": consumers,
        }
        request = {
            "metrics": ["综合支付TR", *metric_names],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "综合支付TR", "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "tr", "name": "综合支付TR", "metric_object": "ratio",
                    "unit": "待元信息解析", "consumers": consumers,
                }],
                "composition_intents": [intent],
            }],
        }
        ready = resolve_request_overlay(index, request, self.policy)
        ready_task = ready["task_resolutions"]["q"]
        resolution = ready_task["composition_resolutions"][0]
        self.assertEqual(resolution["fallback_status"], "ready")
        self.assertEqual(ready_task["resolution_cases"], [])
        self.assertEqual(set(resolution["input_bindings"]), {
            "ad_revenue", "commission_revenue", "payment_gmv"
        })

        index["metrics"].pop("闭环电商佣金收入")
        blocked = resolve_request_overlay(index, request, self.policy)
        blocked_task = blocked["task_resolutions"]["q"]
        resolution = blocked_task["composition_resolutions"][0]
        self.assertIn(resolution["fallback_status"], {"blocked", "confirm"})
        self.assertEqual(blocked_task["resolution_cases"], [])
        self.assertTrue(resolution["deferred_cases"])
        self.assertNotEqual(
            resolution["input_statuses"]["commission_revenue"]["status"], "bound"
        )

    def test_registered_composition_rejects_explicitly_unsupported_leaf_breakdown(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "结算GMV": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                    "aggregation_mode": "additive",
                },
                "支付GMV": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {
                "TOP6平台": {"aliases": ["平台"], "values": []},
                "无": {"aliases": [], "values": []},
            },
            "sheets": {},
        }
        consumers = [{
            "requirement_id": "settlement_rate",
            "requirement_type": "metric_compositions",
            "periods": ["2026-07"],
            "breakdown_dimensions": ["平台"],
        }]
        request = {
            "metrics": ["结算率", "结算GMV", "支付GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "分平台结算率", "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "rate", "name": "结算率", "metric_object": "ratio",
                    "unit": "待元信息解析", "consumers": consumers,
                }],
                "composition_intents": [{
                    "metric_ref": "rate", "requested_metric": "结算率",
                    "composition_id": "competitor_settlement_rate",
                    "inputs": [
                        {"role": "numerator", "metric": "结算GMV"},
                        {"role": "denominator", "metric": "支付GMV"},
                    ],
                    "consumers": consumers,
                }],
            }],
        }
        result = resolve_request_overlay(index, request, self.policy)
        composition = result["task_resolutions"]["q"]["composition_resolutions"][0]
        self.assertEqual(composition["fallback_status"], "blocked")
        self.assertEqual(
            composition["input_statuses"]["denominator"]["status"], "not_executable"
        )
        self.assertEqual(
            composition["input_statuses"]["denominator"]["dimension_capability"]["reason"],
            "metadata_dimension_unsupported",
        )

    def test_registered_composition_propagates_requirement_constraints_to_every_leaf(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                name: {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                    "aggregation_mode": "additive",
                }
                for name in ("结算GMV", "支付GMV")
            },
            "dimensions": {
                "TOP6平台": {
                    "aliases": ["平台"], "values": ["淘系", "拼多多", "京东"],
                },
            },
            "sheets": {},
        }
        constraint = {
            "constraint_id": "pdd", "kind": "dimension_filter", "operator": "eq",
            "values": ["拼多多"], "dimension_hint": "平台", "provenance": "user_explicit",
        }
        consumers = [
            {
                "requirement_id": "rate_level", "requirement_type": "fact_observations",
                "periods": ["2026-Q2"], "period_roles": ["analysis"],
                "semantic_text": "拼多多结算率", "metric_constraints": [constraint],
            },
            {
                "requirement_id": "rate_yoy", "requirement_type": "derived_requirements",
                "derived_metric_id": "yoy_growth", "periods": ["2026-Q2", "2025-Q2"],
                "period_roles": ["analysis", "comparison"],
                "semantic_text": "拼多多结算率同比", "metric_constraints": [constraint],
                "allowed_metric_objects": ["ratio"],
            },
        ]
        request = {
            "metrics": ["结算率", "结算GMV", "支付GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "26Q2拼多多结算率和同比", "periods": ["2026-Q2", "2025-Q2"],
                "metrics": [{
                    "metric_ref": "rate", "name": "结算率", "metric_object": "ratio",
                    "unit": "待元信息解析", "consumers": consumers,
                }],
                "composition_intents": [{
                    "metric_ref": "rate", "requested_metric": "结算率",
                    "composition_id": "competitor_settlement_rate",
                    "inputs": [
                        {"role": "numerator", "metric": "结算GMV"},
                        {"role": "denominator", "metric": "支付GMV"},
                    ],
                    "consumers": consumers,
                }],
            }],
        }

        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertEqual(task["resolution_cases"], [])
        self.assertEqual(set(task["requirement_bindings"]), {"rate_level", "rate_yoy"})
        for requirement_id in ("rate_level", "rate_yoy"):
            binding = task["requirement_bindings"][requirement_id]
            self.assertEqual(binding["mode"], "registered_composition")
            self.assertEqual(set(binding["input_bindings"]), {"numerator", "denominator"})
            for leaf in binding["input_bindings"].values():
                self.assertEqual(leaf["mode"], "member_selector")
                self.assertEqual(
                    leaf["metric_constraints"][0]["source_dimension"], "TOP6平台"
                )
                self.assertEqual(leaf["metric_constraints"][0]["values"], ["拼多多"])
        composition = task["composition_resolutions"][0]
        self.assertEqual(composition["fallback_status"], "ready")
        self.assertEqual(
            composition["selected_fulfillment"]["candidate_type"],
            "registered_composition",
        )

    def test_registered_composition_does_not_hide_an_unfulfillable_leaf_constraint(self) -> None:
        index = {
            "source": {"url": "source", "revision": 1, "schema_hash": "schema"},
            "metrics": {
                "结算GMV": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["TOP6平台"],
                    "aggregation_mode": "additive",
                },
                "支付GMV": {
                    "unit": "亿元", "metric_object": "volume",
                    "supported_grains": ["month"], "dimensions": ["无"],
                    "aggregation_mode": "additive",
                },
            },
            "dimensions": {
                "TOP6平台": {"aliases": ["平台"], "values": ["拼多多"]},
                "无": {"aliases": [], "values": []},
            },
            "sheets": {},
        }
        constraint = {
            "kind": "dimension_filter", "operator": "eq", "values": ["拼多多"],
            "dimension_hint": "平台", "provenance": "user_explicit",
        }
        consumer = {
            "requirement_id": "rate_level", "requirement_type": "fact_observations",
            "periods": ["2026-07"], "period_roles": ["analysis"],
            "semantic_text": "拼多多结算率", "metric_constraints": [constraint],
        }
        request = {
            "metrics": ["结算率", "结算GMV", "支付GMV"],
            "composition_registry_hash": "registry",
            "contexts": [{
                "task_id": "q", "query": "拼多多结算率", "periods": ["2026-07"],
                "metrics": [{
                    "metric_ref": "rate", "name": "结算率", "metric_object": "ratio",
                    "unit": "待元信息解析", "consumers": [consumer],
                }],
                "composition_intents": [{
                    "metric_ref": "rate", "requested_metric": "结算率",
                    "composition_id": "competitor_settlement_rate",
                    "inputs": [
                        {"role": "numerator", "metric": "结算GMV"},
                        {"role": "denominator", "metric": "支付GMV"},
                    ],
                    "consumers": [consumer],
                }],
            }],
        }

        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("rate_level", task["requirement_bindings"])
        self.assertTrue(task["resolution_cases"])
        self.assertEqual(
            task["composition_resolutions"][0]["fallback_status"], "blocked"
        )


if __name__ == "__main__":
    unittest.main()
