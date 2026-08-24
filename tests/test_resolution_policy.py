from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from resolution_policy import (  # noqa: E402
    ResolutionPolicyError,
    load_resolution_policy,
    resolve_request_overlay,
    validate_resolution_policy,
)


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

    def test_policy_rejects_unbounded_or_mapping_fields(self) -> None:
        invalid = dict(self.policy)
        invalid["mappings"] = {"平台": "TOP6平台"}
        with self.assertRaises(ResolutionPolicyError):
            validate_resolution_policy(invalid)

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

    def test_derived_only_alias_does_not_replace_core_fact(self) -> None:
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
            "task_id": "q", "query": "线上社零表现和涨幅变化", "periods": ["2026-04", "2026-05"],
            "dimensions": [], "metrics": [{
                "metric_ref": "m", "name": "线上社零", "metric_object": "volume",
                "metric_object_provenance": "model_inferred", "unit": "待元信息解析",
                "consumers": [{"requirement_type": "fact_observations"}],
            }],
        }]}
        result = resolve_request_overlay(index, request, self.policy)
        task = result["task_resolutions"]["q"]
        self.assertNotIn("线上社零", task["metric_bindings"])
        self.assertEqual(task["resolution_cases"][0]["candidates"], [])

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
        composition = result["task_resolutions"]["q"]["composition_resolutions"][0]
        self.assertEqual(composition["fallback_status"], "ready")
        for status in composition["input_statuses"].values():
            self.assertEqual(status["status"], "bound")
            self.assertEqual(status["structural_capability"][0]["path"], "aggregate_fact")

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


if __name__ == "__main__":
    unittest.main()
