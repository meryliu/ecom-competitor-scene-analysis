from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_analysis  # noqa: E402
from compile_plan import compile_and_validate  # noqa: E402
from fact_contract import merge_fetch_requests, stable_id  # noqa: E402
from run_fast_query import finalize_model_nodes  # noqa: E402
from validate_execution import Validator  # noqa: E402


def simple_ir() -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "2026年5月支付GMV",
            "analysis_goal": "返回事实",
            "metrics": [{"metric_id": "payment", "name": "支付GMV", "metric_object": "volume", "unit": "亿元"}],
            "periods": {"analysis": "2026-05"},
            "scope": "整体",
            "filters": [],
        },
        "views": [{"view_id": "overall"}],
        "dimension_trees": [],
        "fact_observations": [{
            "requirement_id": "payment_fact",
            "metric_ref": "payment",
            "period_roles": ["analysis"],
            "view_id": "overall",
            "dimension_refs": [],
        }],
        "metric_compositions": [],
        "derived_requirements": [],
        "custom_calculations": [],
        "attribution_targets": [],
        "output_requirements": [],
        "clarifications": [],
    }


def scene_payload(request: dict, *, revision: int = 11) -> dict:
    demand = request["fact_demands"][0]
    fact_id = stable_id("fact", f"retry-{revision}")
    return {
        "schema_version": "scene_facts/2.0",
        "facts": [{
            "fact_id": fact_id,
            "metric_ref": demand["metric_ref"],
            "metric": demand["metric"],
            "metric_object": demand["metric_object"],
            "component": demand.get("component"),
            "period": demand["period"],
            "dimensions": {},
            "value": 100.0,
            "unit": "亿元",
            "definition": "支付GMV",
            "missing": False,
            "raw_missing": False,
            "normalization_reason": "unchanged",
            "value_derived_from_components": False,
            "source_request_id": request["request_id"],
            "source_ref": {"sheet": "月度表", "row": 1, "column": "B", "revision": revision},
            "coverage": "full",
        }],
        "bindings": [
            dict(binding, fact_id=fact_id)
            for binding in demand["consumer_bindings"]
        ],
        "source": {
            "revision": revision,
            "schema_hash": "hash",
            "freshness": "live",
            "cache_status": "hit",
        },
    }


class RunAnalysisTests(unittest.TestCase):
    @staticmethod
    def _gateway_capabilities(cases: list[dict]) -> dict:
        return {
            "schema_version": "resolved_capabilities/1.0",
            "provider": {"provider_id": "test", "contract_version": "1.0"},
            "source": {
                "schema_version": "source_binding/1.0",
                "provider_id": "test",
                "source_id": "source",
                "config_hash": "config",
                "revision": 1106,
                "schema_hash": "schema",
                "freshness": "live",
                "resolution_policy_hash": "policy",
                "resolution_engine_version": "1.0.0",
            },
            "metric_bindings": {"支付GMV": "支付GMV"},
            "dimension_bindings": {},
            "metric_dimension_bindings": {},
            "metrics": {"支付GMV": {"unit": "亿元", "additive": True, "dimensions": ["无"]}},
            "dimensions": {},
            "availability": {
                "month": {
                    "periods": ["2026-05"],
                    "metrics": {"支付GMV": {"dimension": "无"}},
                }
            },
            "resolution_cases": cases,
            "resolution_decisions": cases,
            "resolution_policy": {"sha256": "policy", "engine_version": "1.0.0"},
        }

    def test_all_ambiguous_tasks_wait_for_confirmation_without_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            work_dir = root / "run"
            input_path.write_text(json.dumps(simple_ir(), ensure_ascii=False), encoding="utf-8")
            case = {
                "case_id": "resolution_case_1",
                "action": "confirm",
                "kind": "query_metric",
                "requested_term": "支付GMV",
                "task_ids": ["default"],
                "source_revision": 1106,
                "schema_hash": "schema",
                "resolution_policy_hash": "policy",
                "candidates": [{"candidate_id": "candidate_1", "metric": "支付GMV"}],
            }

            class Gateway:
                def __init__(self, *args, **kwargs):
                    self.fetch_called = False

                def resolve(self, request):
                    return RunAnalysisTests._gateway_capabilities([case])

                def fetch(self, request):
                    self.fetch_called = True
                    raise AssertionError("waiting task must not fetch")

            argv = ["run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir)]
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", Gateway
            ):
                self.assertEqual(run_analysis.main(), 0)
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            self.assertEqual(answer["status"], "waiting_confirmation")
            self.assertEqual(answer["tasks"][0]["status"], "waiting_confirmation")
            self.assertEqual(answer["tasks"][0]["resolution_cases"][0]["case_id"], "resolution_case_1")
            self.assertFalse((work_dir / "fetch-request.json").exists())

    def test_registered_composition_fallback_runs_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            work_dir = root / "run"
            ir = simple_ir()
            ir["analysis_task"].update({
                "query": "2026年5月综合结算TR",
                "analysis_goal": "返回综合结算TR",
                "metrics": [{
                    "metric_id": "target", "name": "综合结算TR",
                    "metric_object": "ratio", "unit": "rate",
                }],
            })
            ir["fact_observations"] = [{
                "requirement_id": "tr_fact", "metric_ref": "target",
                "period_roles": ["analysis"], "view_id": "overall",
                "dimension_refs": [],
            }]
            input_path.write_text(
                json.dumps(ir, ensure_ascii=False), encoding="utf-8"
            )

            leaf_metrics = (
                "闭环电商广告收入", "闭环电商佣金收入", "结算GMV"
            )
            source_binding = {
                "schema_version": "source_binding/1.0",
                "provider_id": "test", "source_id": "source",
                "config_hash": "config", "revision": 1300,
                "schema_hash": "schema", "freshness": "live",
                "resolution_policy_hash": "policy",
                "business_intent_policy_hash": "intent-policy",
                "resolution_engine_version": "2.10.0",
            }
            composition_resolution = {
                "metric_ref": "target", "requested_metric": "综合结算TR",
                "composition_id": "competitor_comprehensive_settlement_tr",
                "direct_status": "composition_deferred",
                "input_bindings": {
                    "ad_revenue": "闭环电商广告收入",
                    "commission_revenue": "闭环电商佣金收入",
                    "settlement_gmv": "结算GMV",
                },
                "input_statuses": {
                    "ad_revenue": {"status": "bound", "binding": "闭环电商广告收入"},
                    "commission_revenue": {"status": "bound", "binding": "闭环电商佣金收入"},
                    "settlement_gmv": {"status": "bound", "binding": "结算GMV"},
                },
                "fallback_status": "ready", "deferred_cases": [],
            }
            capabilities = {
                "schema_version": "resolved_capabilities/1.0",
                "provider": {"provider_id": "test", "contract_version": "1.0"},
                "source": source_binding,
                "metric_bindings": {name: name for name in leaf_metrics},
                "dimension_bindings": {}, "metric_dimension_bindings": {},
                "task_metric_dimension_bindings": {"default": {}},
                "metrics": {
                    name: {
                        "unit": "亿元", "metric_object": "volume",
                        "additive": True, "dimensions": ["无"],
                        "supported_grains": ["month"],
                    }
                    for name in leaf_metrics
                },
                "dimensions": {},
                "availability": {
                    "month": {
                        "periods": ["2026-05"],
                        "metrics": {
                            name: {"dimension": "无"} for name in leaf_metrics
                        },
                    }
                },
                "task_resolutions": {"default": {
                    "metric_bindings": {name: name for name in leaf_metrics},
                    "requirement_bindings": {},
                    "metric_statuses": {"target": {
                        "requested_metric": "综合结算TR",
                        "status": "composition_deferred", "binding": None,
                    }},
                    "intent_resolutions": {"target": {
                        "status": "composition_deferred", "selected_candidate": None,
                    }},
                    "composition_resolutions": [composition_resolution],
                    "resolution_cases": [],
                }},
                "resolution_cases": [], "resolution_decisions": [],
                "resolution_policy": {"sha256": "policy", "engine_version": "2.10.0"},
                "business_intent_policy": {"sha256": "intent-policy"},
            }
            fetched_metrics: set[str] = set()
            values = {
                "闭环电商广告收入": 573.0,
                "闭环电商佣金收入": 87.0,
                "结算GMV": 7734.0,
            }

            class Gateway:
                def __init__(self, *args, **kwargs):
                    pass

                def resolve(self, request):
                    intents = request["contexts"][0]["composition_intents"]
                    assert intents[0]["composition_id"] == "competitor_comprehensive_settlement_tr"
                    return capabilities

                def fetch(self, request):
                    facts = []
                    bindings = []
                    for index, demand in enumerate(request["fact_demands"], start=1):
                        metric = demand["metric"]
                        fetched_metrics.add(metric)
                        fact_id = stable_id("fact", f"composition-{index}")
                        facts.append({
                            "fact_id": fact_id, "metric_ref": demand["metric_ref"],
                            "metric": metric, "metric_object": demand["metric_object"],
                            "component": demand.get("component"),
                            "period": demand["period"], "dimensions": {},
                            "value": values[metric], "unit": "亿元",
                            "definition": metric, "missing": False,
                            "raw_missing": False, "normalization_reason": "unchanged",
                            "value_derived_from_components": False,
                            "source_request_id": request["request_id"],
                            "source_ref": {"revision": 1300}, "coverage": "full",
                        })
                        bindings.extend(
                            dict(binding, fact_id=fact_id)
                            for binding in demand["consumer_bindings"]
                        )
                    return {
                        "schema_version": "scene_facts/2.0",
                        "facts": facts, "bindings": bindings,
                        "source": {
                            "revision": 1300, "schema_hash": "schema",
                            "freshness": "live", "cache_status": "miss",
                        },
                    }

            argv = [
                "run_analysis.py", "--input", str(input_path),
                "--work-dir", str(work_dir),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", Gateway
            ):
                self.assertEqual(run_analysis.main(), 0)

            prepared = json.loads(
                (work_dir / "tasks" / "default" / "prepared-ir.json").read_text(
                    encoding="utf-8"
                )
            )
            request = json.loads(
                (work_dir / "fetch-request.json").read_text(encoding="utf-8")
            )
            answer = json.loads(
                (work_dir / "answer-payload.json").read_text(encoding="utf-8")
            )
            self.assertEqual(prepared["fact_observations"], [])
            self.assertEqual(
                prepared["metric_compositions"][0]["composition_id"],
                "competitor_comprehensive_settlement_tr",
            )
            self.assertEqual(
                {item["metric"] for item in request["fact_demands"]},
                set(leaf_metrics),
            )
            self.assertEqual(fetched_metrics, set(leaf_metrics))
            self.assertEqual(answer["status"], "success")
            derived = answer["tasks"][0]["derived_results"][0]
            self.assertEqual(
                derived["derived_metric_id"],
                "competitor_comprehensive_settlement_tr",
            )
            self.assertAlmostEqual(derived["value"], (573.0 + 87.0) / 7734.0)

    def test_attribution_ir_guard_blocks_before_provider_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            work_dir = root / "run"
            ir = simple_ir()
            ir["analysis_task"]["metrics"].append({
                "metric_id": "input", "name": "MAC", "metric_object": "volume", "unit": "万人"
            })
            ir["analysis_task"]["periods"]["comparison"] = "2025-05"
            ir["attribution_targets"] = [{
                "target_id": "formula", "metric_ref": "payment",
                "scenario": "metric_change", "periods": {
                    "analysis": "2026-05", "comparison": "2025-05"
                },
                "factors": [{"factor_id": "input", "kind": "metric", "metric_ref": "input"}],
                "formula": "MAC",
            }]
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")

            class Gateway:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("Provider must not be initialized for malformed attribution IR")

            argv = ["run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir)]
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", Gateway
            ):
                self.assertEqual(run_analysis.main(), 2)
            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["error"]["code"], "ATTR-IR-002")

    def test_bundle_continues_unaffected_task_while_other_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "bundle.json"
            work_dir = root / "run"
            bundle = {
                "schema_version": "analysis_bundle/1.0",
                "tasks": [
                    {"task_id": "ready", "analysis_ir": simple_ir()},
                    {"task_id": "waiting", "analysis_ir": simple_ir()},
                ],
            }
            input_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            case = {
                "case_id": "resolution_case_1",
                "action": "confirm",
                "kind": "query_metric",
                "requested_term": "支付GMV",
                "task_ids": ["waiting"],
                "source_revision": 1106,
                "schema_hash": "schema",
                "resolution_policy_hash": "policy",
                "candidates": [{"candidate_id": "candidate_1", "metric": "支付GMV"}],
            }

            class Gateway:
                def __init__(self, *args, **kwargs):
                    pass

                def resolve(self, request):
                    return RunAnalysisTests._gateway_capabilities([case])

                def fetch(self, request):
                    return scene_payload(request, revision=1106)

            argv = ["run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir)]
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", Gateway
            ):
                self.assertEqual(run_analysis.main(), 0)
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            self.assertEqual(answer["status"], "partial_success")
            self.assertEqual(
                {item["task_id"]: item["status"] for item in answer["tasks"]},
                {"ready": "success", "waiting": "waiting_confirmation"},
            )

    def test_optional_partial_result_cannot_be_finalized_as_success(self) -> None:
        manifest = {
            "analysis_task": {"query": "attribute optional formula"},
            "status": "partial_success",
            "nodes": [
                {
                    "node_id": "optional_attribution",
                    "criticality": "optional",
                    "status": "partial_success",
                    "execution": {"handler": "attribution"},
                },
                {
                    "node_id": "conclusion_organization",
                    "criticality": "optional",
                    "status": "planned",
                    "depends_on": ["optional_attribution"],
                    "execution": {"handler": "model_owned"},
                },
            ],
            "execution_summary": {},
        }
        finalize_model_nodes(manifest)
        self.assertEqual(manifest["status"], "partial_success")
        report = Validator(manifest, "final").validate()
        self.assertEqual(report["computed_status"], "partial_success")
        self.assertEqual(manifest["conclusions"][0]["status"], "partial_success")
        manifest["status"] = "success"
        report = Validator(manifest, "final").validate()
        self.assertEqual(report["computed_status"], "partial_success")
        self.assertIn("STATUS-001", {issue["rule_id"] for issue in report["issues"]})

    def test_core_partial_result_remains_partial_after_model_finalization(self) -> None:
        manifest = {
            "analysis_task": {"query": "attribute core formula"},
            "nodes": [
                {
                    "node_id": "core_attribution",
                    "criticality": "core",
                    "status": "partial_success",
                    "execution": {"handler": "attribution"},
                },
                {
                    "node_id": "conclusion_organization",
                    "criticality": "core",
                    "status": "planned",
                    "depends_on": ["core_attribution"],
                    "execution": {"handler": "model_owned"},
                },
            ],
            "execution_summary": {},
        }
        finalize_model_nodes(manifest)
        self.assertEqual(manifest["status"], "partial_success")

    def test_model_organization_survives_failed_calculation_node(self) -> None:
        manifest = {
            "analysis_task": {"query": "分析并计算占比"},
            "status": "partial_success",
            "nodes": [
                {
                    "node_id": "fact_artifact",
                    "status": "success",
                    "execution": {"handler": "fact_artifact"},
                },
                {
                    "node_id": "derived_share",
                    "status": "failed",
                    "execution": {"handler": "derived"},
                },
                {
                    "node_id": "conclusion_organization",
                    "status": "planned",
                    "depends_on": ["derived_share"],
                    "execution": {"handler": "model_owned"},
                },
            ],
            "execution_summary": {},
        }
        finalize_model_nodes(manifest)
        conclusion = next(
            node for node in manifest["nodes"]
            if node["node_id"] == "conclusion_organization"
        )
        self.assertEqual(conclusion["status"], "partial_success")
        self.assertIn("conclusion_organization", manifest["execution_summary"]["partial_nodes"])
        self.assertTrue(manifest["model_completion"]["required"])
        self.assertEqual(manifest["model_completion"]["incomplete_node_ids"], ["derived_share"])

    def test_single_ir_rejects_schema_version_with_structured_error(self) -> None:
        invalid = simple_ir()
        invalid["schema_version"] = invalid.pop("ir_version")
        with self.assertRaises(run_analysis.InputProtocolError) as caught:
            run_analysis.normalize_input(invalid)
        self.assertEqual(caught.exception.code, "INPUT_PROTOCOL_INVALID")
        self.assertEqual(caught.exception.details["expected_field"], "ir_version")

    def test_bundle_rejects_inner_ir_without_ir_version(self) -> None:
        invalid_ir = simple_ir()
        invalid_ir["schema_version"] = invalid_ir.pop("ir_version")
        bundle = {
            "schema_version": "analysis_bundle/1.0",
            "tasks": [{"task_id": "task_a", "analysis_ir": invalid_ir}],
        }
        with self.assertRaises(run_analysis.InputProtocolError) as caught:
            run_analysis.normalize_input(bundle)
        self.assertEqual(caught.exception.code, "INPUT_PROTOCOL_INVALID")

    def test_protocol_failure_is_written_to_run_state_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "invalid.json"
            work_dir = root / "run"
            invalid = simple_ir()
            invalid["schema_version"] = invalid.pop("ir_version")
            input_path.write_text(json.dumps(invalid), encoding="utf-8")
            argv = [
                "run_analysis.py",
                "--input", str(input_path),
                "--work-dir", str(work_dir),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 2)
            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["error"]["code"], "INPUT_PROTOCOL_INVALID")
            self.assertEqual(state["stages"]["input"]["status"], "failed")
            self.assertNotIn("fetch", state["stages"])

    def test_successful_fetch_checkpoint_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            response_path = root / "response.json"
            work_dir = root / "run"
            ir = simple_ir()
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            plan, report = compile_and_validate(
                ir,
                ROOT / "references" / "derived-metric-registry.json",
                ROOT / "references" / "metric-composition-registry.json",
            )
            self.assertTrue(report["valid"])
            request = merge_fetch_requests([("default", plan["fetch_requests"][0])])
            demand = request["fact_demands"][0]
            fact_id = stable_id("fact", "offline")
            payload = {
                "schema_version": "scene_facts/2.0",
                "facts": [{
                    "fact_id": fact_id,
                    "metric_ref": demand["metric_ref"],
                    "metric": demand["metric"],
                    "metric_object": demand["metric_object"],
                    "component": demand.get("component"),
                    "period": demand["period"],
                    "dimensions": {},
                    "value": 100.0,
                    "unit": "亿元",
                    "definition": "支付GMV",
                    "missing": False,
                    "raw_missing": False,
                    "normalization_reason": "unchanged",
                    "value_derived_from_components": False,
                    "source_request_id": request["request_id"],
                    "source_ref": {"sheet": "月度", "row": 1, "column": "B", "revision": 7},
                    "coverage": "full",
                }],
                "bindings": [dict(binding, fact_id=fact_id) for binding in demand["consumer_bindings"]],
                "source": {"revision": 7, "schema_hash": "hash", "freshness": "live", "cache_status": "hit"},
            }
            response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
                "--response-file", str(response_path),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            first_state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(first_state["fetch_attempts"]), 1)
            first_state["status"] = "failed"
            first_state["stages"]["execute"] = {"status": "failed"}
            (work_dir / "run-state.json").write_text(json.dumps(first_state, ensure_ascii=False), encoding="utf-8")
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            second_state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            self.assertEqual(len(second_state["fetch_attempts"]), 1)
            self.assertTrue(answer["fetch_reused"])

    def test_fetch_checkpoint_survives_semantically_irrelevant_input_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            response_path = root / "response.json"
            work_dir = root / "run"
            ir = simple_ir()
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            plan, report = compile_and_validate(
                ir,
                ROOT / "references" / "derived-metric-registry.json",
                ROOT / "references" / "metric-composition-registry.json",
            )
            self.assertTrue(report["valid"])
            request = merge_fetch_requests([("default", plan["fetch_requests"][0])])
            demand = request["fact_demands"][0]
            fact_id = stable_id("fact", "semantic-checkpoint")
            payload = {
                "schema_version": "scene_facts/2.0",
                "facts": [{
                    "fact_id": fact_id,
                    "metric_ref": demand["metric_ref"],
                    "metric": demand["metric"],
                    "metric_object": demand["metric_object"],
                    "period": demand["period"],
                    "dimensions": {},
                    "value": 100.0,
                    "unit": "亿元",
                    "definition": "支付GMV",
                    "missing": False,
                    "raw_missing": False,
                    "normalization_reason": "unchanged",
                    "value_derived_from_components": False,
                    "source_request_id": request["request_id"],
                    "source_ref": {"revision": 7},
                    "coverage": "full",
                }],
                "bindings": [dict(binding, fact_id=fact_id) for binding in demand["consumer_bindings"]],
                "source": {"revision": 7, "schema_hash": "hash", "freshness": "live"},
            }
            response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
                "--response-file", str(response_path),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            state["status"] = "failed"
            state["stages"]["execute"] = {"status": "failed"}
            (work_dir / "run-state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            ir["analysis_task"]["query"] = "换一种展示文案，物理事实不变"
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            second_state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertTrue(answer["fetch_reused"])
            self.assertEqual(len(second_state["fetch_attempts"]), 1)

    def test_bundle_isolates_compile_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "bundle.json"
            response_path = root / "response.json"
            work_dir = root / "run"
            valid = simple_ir()
            invalid = simple_ir()
            invalid["fact_observations"][0]["metric_ref"] = "missing_metric"
            bundle = {
                "schema_version": "analysis_bundle/1.0",
                "tasks": [
                    {"task_id": "valid", "analysis_ir": valid},
                    {"task_id": "invalid", "analysis_ir": invalid},
                ],
            }
            input_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            plan, report = compile_and_validate(
                valid,
                ROOT / "references" / "derived-metric-registry.json",
                ROOT / "references" / "metric-composition-registry.json",
            )
            self.assertTrue(report["valid"])
            request = merge_fetch_requests([("valid", plan["fetch_requests"][0])])
            demand = request["fact_demands"][0]
            fact_id = stable_id("fact", "isolated-bundle")
            payload = {
                "schema_version": "scene_facts/2.0",
                "facts": [{
                    "fact_id": fact_id,
                    "metric_ref": demand["metric_ref"],
                    "metric": demand["metric"],
                    "metric_object": demand["metric_object"],
                    "period": demand["period"],
                    "dimensions": {},
                    "value": 100.0,
                    "unit": "亿元",
                    "definition": "支付GMV",
                    "missing": False,
                    "raw_missing": False,
                    "normalization_reason": "unchanged",
                    "value_derived_from_components": False,
                    "source_request_id": request["request_id"],
                    "source_ref": {"revision": 7},
                    "coverage": "full",
                }],
                "bindings": [dict(binding, fact_id=fact_id) for binding in demand["consumer_bindings"]],
                "source": {"revision": 7, "schema_hash": "hash", "freshness": "live"},
            }
            response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
                "--response-file", str(response_path),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            self.assertEqual(answer["status"], "partial_success")
            self.assertEqual(
                {item["task_id"]: item["status"] for item in answer["tasks"]},
                {"valid": "success", "invalid": "blocked"},
            )

    def test_bundle_uses_one_fact_and_one_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "bundle.json"
            response_path = root / "response.json"
            work_dir = root / "run"
            ir = simple_ir()
            bundle = {
                "schema_version": "analysis_bundle/1.0",
                "tasks": [
                    {"task_id": "q1", "analysis_ir": ir},
                    {"task_id": "q2", "analysis_ir": ir},
                ],
            }
            input_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
            requests = []
            for task_id in ("q1", "q2"):
                plan, report = compile_and_validate(
                    ir,
                    ROOT / "references" / "derived-metric-registry.json",
                    ROOT / "references" / "metric-composition-registry.json",
                )
                self.assertTrue(report["valid"])
                requests.append((task_id, plan["fetch_requests"][0]))
            request = merge_fetch_requests(requests)
            self.assertEqual(len(request["fact_demands"]), 1)
            demand = request["fact_demands"][0]
            fact_id = stable_id("fact", "bundle-offline")
            payload = {
                "schema_version": "scene_facts/2.0",
                "facts": [{
                    "fact_id": fact_id,
                    "metric_ref": demand["metric_ref"],
                    "metric": demand["metric"],
                    "metric_object": demand["metric_object"],
                    "component": demand.get("component"),
                    "period": demand["period"],
                    "dimensions": {},
                    "value": 100.0,
                    "unit": "亿元",
                    "definition": "支付GMV",
                    "missing": False,
                    "raw_missing": False,
                    "normalization_reason": "unchanged",
                    "value_derived_from_components": False,
                    "source_request_id": request["request_id"],
                    "source_ref": {"sheet": "月度", "row": 1, "column": "B", "revision": 9},
                    "coverage": "full",
                }],
                "bindings": [dict(binding, fact_id=fact_id) for binding in demand["consumer_bindings"]],
                "source": {"revision": 9, "schema_hash": "hash", "freshness": "live", "cache_status": "hit"},
            }
            response_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
                "--response-file", str(response_path),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_analysis.main(), 0)
            answer = json.loads((work_dir / "answer-payload.json").read_text(encoding="utf-8"))
            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            facts = json.loads((work_dir / "facts.json").read_text(encoding="utf-8"))
            self.assertEqual(answer["source_revision"], 9)
            self.assertEqual({item["source_revision"] for item in answer["tasks"]}, {9})
            self.assertEqual(len(state["fetch_attempts"]), 1)
            self.assertEqual(len(facts["facts"]), 1)

    def test_fetch_fails_closed_on_concurrent_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            work_dir = root / "run"
            ir = simple_ir()
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            failed_record = {
                "attempt_id": "attempt_revision_conflict",
                "request_id": "placeholder",
                "status": "failed",
                "error": {"code": "concurrent_modification", "message": "revision changed"},
            }

            def fetch_side_effect(request: dict, **_: object) -> tuple[dict, dict]:
                failed_record["request_id"] = request["request_id"]
                raise RuntimeError(json.dumps(failed_record, ensure_ascii=False))

            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
            ]
            gateway = unittest.mock.MagicMock()
            gateway.source_binding = {
                "schema_version": "source_binding/1.0", "provider_id": "test",
                "source_id": "test", "config_hash": "config", "revision": 10,
                "schema_hash": "hash", "freshness": "live",
            }
            gateway.resolve.return_value = {"source": gateway.source_binding}
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", return_value=gateway
            ), patch.object(
                run_analysis,
                "prepare_analysis_ir",
                return_value=(ir, []),
            ), patch.object(
                run_analysis, "_fetch", side_effect=fetch_side_effect
            ) as fetch_mock:
                self.assertEqual(run_analysis.main(), 2)

            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual([attempt["status"] for attempt in state["fetch_attempts"]], ["failed"])

    def test_fetch_does_not_retry_semantic_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_path = root / "input.json"
            work_dir = root / "run"
            ir = simple_ir()
            input_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
            failed_record = {
                "attempt_id": "attempt_semantic_error",
                "request_id": "placeholder",
                "status": "failed",
                "error": {"code": "metric_not_found", "message": "unknown metric"},
            }

            def fetch_side_effect(request: dict, **_: object) -> tuple[dict, dict]:
                failed_record["request_id"] = request["request_id"]
                raise RuntimeError(json.dumps(failed_record, ensure_ascii=False))

            argv = [
                "run_analysis.py", "--input", str(input_path), "--work-dir", str(work_dir),
            ]
            gateway = unittest.mock.MagicMock()
            gateway.source_binding = {
                "schema_version": "source_binding/1.0", "provider_id": "test",
                "source_id": "test", "config_hash": "config", "revision": 10,
                "schema_hash": "hash", "freshness": "live",
            }
            gateway.resolve.return_value = {"source": gateway.source_binding}
            with patch.object(sys, "argv", argv), patch.object(
                run_analysis, "FeishuCompetitorGateway", return_value=gateway
            ), patch.object(
                run_analysis,
                "prepare_analysis_ir",
                return_value=(ir, []),
            ), patch.object(
                run_analysis, "_fetch", side_effect=fetch_side_effect
            ) as fetch_mock:
                self.assertEqual(run_analysis.main(), 2)

            state = json.loads((work_dir / "run-state.json").read_text(encoding="utf-8"))
            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual(len(state["fetch_attempts"]), 1)
            self.assertEqual(state["stages"]["fetch"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
