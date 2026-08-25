from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from data_gateway import build_resolve_request, load_source_config  # noqa: E402


class DataGatewayContractTests(unittest.TestCase):
    def test_resolve_request_preserves_requirement_metric_constraints(self) -> None:
        tasks = [("q", {
            "analysis_task": {
                "query": "京东闭环电商佣金收入",
                "metrics": [{
                    "metric_id": "commission", "name": "闭环电商佣金收入",
                    "metric_object": "volume", "unit": "亿元",
                    "unit_source": "model_inferred",
                }],
                "periods": {"analysis": "2026-06"},
            },
            "fact_observations": [{
                "requirement_id": "scoped", "metric_ref": "commission",
                "period_roles": ["analysis"], "semantic_text": "京东闭环电商佣金收入",
                "metric_constraints": [{
                    "kind": "dimension_filter", "operator": "eq", "values": ["京东"],
                    "dimension_hint": "平台", "provenance": "model_inferred",
                }],
            }],
        })]
        request = build_resolve_request(tasks, {"definitions": {}})
        metric = request["contexts"][0]["metrics"][0]
        self.assertEqual(metric["unit_provenance"], "model_inferred")
        self.assertEqual(metric["consumers"][0]["metric_constraints"][0]["operator"], "eq")
        self.assertIn("平台", request["dimensions"])

    def test_effective_config_hash_includes_source_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source-config.json"
            path.write_text(json.dumps({
                "schema_version": "data_source_config/1.0",
                "provider_id": "test",
                "source_id": "source",
                "source_url": "https://default",
                "allow_stale_by_default": False,
                "sheet_roles": {"month": {"allowed_names": ["month"]}},
            }), encoding="utf-8")
            default = load_source_config(path)
            overridden = load_source_config(path, source_url="https://override")
        self.assertNotEqual(default["config_hash"], overridden["config_hash"])

    def test_resolve_request_scopes_composition_leaves_to_task_intent(self) -> None:
        tasks = [("q", {
            "analysis_task": {
                "metrics": [{"metric_id": "rate", "name": "结算率"}],
                "periods": {"analysis": "2026-05"},
            },
            "fact_observations": [{"dimensions": {"平台": "京东"}}],
        })]
        registry = {"definitions": {"rate": {"trigger_phrases": ["结算率"], "inputs": [
            {"metric": "支付GMV"}, {"metric": "结算GMV"},
        ]}}}
        request = build_resolve_request(tasks, registry)
        self.assertEqual(request["metrics"], ["支付GMV", "结算GMV", "结算率"])
        self.assertEqual(request["contexts"][0]["composition_intents"][0]["composition_id"], "rate")
        self.assertEqual(request["dimensions"], ["平台"])

    def test_resolve_request_collects_all_comprehensive_tr_leaves_once(self) -> None:
        registry = json.loads(
            (ROOT / "references" / "metric-composition-registry.json").read_text(
                encoding="utf-8"
            )
        )
        tasks = [("q", {
            "analysis_task": {
                "query": "2026年5月综合支付TR",
                "metrics": [{
                    "metric_id": "tr", "name": "综合支付TR", "metric_object": "ratio"
                }],
                "periods": {"analysis": "2026-05"},
            },
            "fact_observations": [{
                "requirement_id": "tr_fact", "metric_ref": "tr",
                "period_roles": ["analysis"],
            }],
        })]
        request = build_resolve_request(tasks, registry)
        self.assertEqual(
            request["metrics"],
            ["支付GMV", "综合支付TR", "闭环电商佣金收入", "闭环电商广告收入"],
        )
        intent = request["contexts"][0]["composition_intents"][0]
        self.assertEqual(intent["composition_id"], "competitor_comprehensive_payment_tr")
        self.assertEqual(
            {item["role"] for item in intent["inputs"]},
            {"ad_revenue", "commission_revenue", "payment_gmv"},
        )

    def test_resolve_request_marks_metric_object_as_model_inferred_by_default(self) -> None:
        tasks = [("q", {
            "analysis_task": {
                "query": "业务涨幅",
                "metrics": [{"metric_id": "m", "name": "业务", "metric_object": "volume"}],
                "periods": {"analysis": "2026-07"},
            },
            "fact_observations": [{"requirement_id": "f", "metric_ref": "m", "period_roles": ["analysis"]}],
        })]
        request = build_resolve_request(tasks, {"definitions": {}})
        metric = request["contexts"][0]["metrics"][0]
        self.assertEqual(metric["metric_object_provenance"], "model_inferred")
        self.assertEqual(metric["consumers"][0]["requirement_type"], "fact_observations")
        self.assertEqual(metric["required_periods"], ["2026-07"])

    def test_resolve_request_separates_selectors_from_breakdown_dimensions(self) -> None:
        tasks = [("q", {
            "analysis_task": {
                "metrics": [
                    {"metric_id": "selected", "name": "筛选指标"},
                    {"metric_id": "grouped", "name": "拆解指标"},
                ],
                "periods": {"analysis": "2026-07"},
            },
            "fact_observations": [
                {
                    "requirement_id": "selected_fact", "metric_ref": "selected",
                    "period_roles": ["analysis"], "dimensions": {"平台": "京东"},
                    "dimension_refs": [],
                },
                {
                    "requirement_id": "grouped_fact", "metric_ref": "grouped",
                    "period_roles": ["analysis"], "dimensions": {},
                    "dimension_refs": ["平台"],
                },
            ],
        })]
        request = build_resolve_request(tasks, {"definitions": {}})
        metrics = {
            item["metric_ref"]: item for item in request["contexts"][0]["metrics"]
        }
        self.assertEqual(metrics["selected"]["required_dimensions"], ["平台"])
        self.assertEqual(metrics["selected"]["required_breakdown_dimensions"], [])
        self.assertEqual(metrics["grouped"]["required_breakdown_dimensions"], ["平台"])

    def test_resolve_request_uses_registered_derived_roles_and_object_capability(self) -> None:
        tasks = [("q", {
            "analysis_task": {
                "query": "业务涨幅较上月变化",
                "metrics": [{"metric_id": "m", "name": "业务", "metric_object": "volume"}],
                "periods": {"analysis": "2026-07", "comparison": "2026-06"},
            },
            "derived_requirements": [{
                "requirement_id": "d", "metric_ref": "m", "derived_metric_id": "mom",
                "semantic_text": "京东业务涨幅较上月变化",
                "metric_constraints": [{
                    "kind": "dimension_filter", "operator": "eq", "values": ["京东"],
                    "dimension_hint": "平台", "provenance": "user_explicit",
                }],
            }],
        })]
        request = build_resolve_request(
            tasks,
            {"definitions": {}},
            {"definitions": {"mom": {
                "required_period_roles": ["analysis", "comparison"],
                "metric_objects": ["volume"],
            }}},
        )
        metric = request["contexts"][0]["metrics"][0]
        self.assertEqual(metric["required_periods"], ["2026-06", "2026-07"])
        consumer = metric["consumers"][0]
        self.assertEqual(consumer["allowed_metric_objects"], ["volume"])
        self.assertEqual(consumer["derived_metric_id"], "mom")
        self.assertEqual(consumer["semantic_text"], "京东业务涨幅较上月变化")
        self.assertEqual(consumer["metric_constraints"][0]["operator"], "eq")
