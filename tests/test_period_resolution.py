from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compile_plan import compile_and_validate  # noqa: E402
from prepare_analysis import PreparationError, prepare_analysis_ir  # noqa: E402


def load(name: str) -> dict:
    return json.loads((ROOT / "references" / name).read_text(encoding="utf-8"))


def capabilities() -> dict:
    month_periods = [
        f"{year}-{month:02d}"
        for year in (2025, 2026)
        for month in range(1, 7)
    ]
    metrics = {
        "支付GMV": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
        "结算GMV": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
        "闭环电商广告收入": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
        "闭环电商佣金收入": {"unit": "亿元", "additive": True, "dimensions": ["平台"]},
        "类目同比增速": {"unit": "%", "additive": False, "dimensions": ["类目"]},
    }
    return {
        "schema_version": "resolved_capabilities/1.0",
        "provider": {"provider_id": "test", "contract_version": "1.0"},
        "source": {"revision": 1, "schema_hash": "test"},
        "metric_bindings": {name: name for name in metrics},
        "dimension_bindings": {"平台": "平台", "类目": "类目"},
        "metrics": metrics,
        "dimensions": {
            "平台": {"values": ["京东"]},
            "类目": {"values": ["家电", "服装"]},
        },
        "availability": {
            "month": {
                "periods": month_periods,
                "metrics": {
                    name: {"dimension": metadata["dimensions"][0]}
                    for name, metadata in metrics.items()
                },
            },
            "quarter": {"periods": ["2026-Q2"], "metrics": {}},
        },
    }


def base_ir(metric_name: str, metric_object: str, dimension: str) -> dict:
    return {
        "ir_version": "analysis_ir/1.0",
        "analysis_task": {
            "query": "test",
            "analysis_goal": "test",
            "metrics": [{
                "metric_id": "target",
                "name": metric_name,
                "metric_object": metric_object,
                "unit": "待元信息解析",
            }],
            "periods": {},
            "scope": "test",
            "filters": [],
        },
        "views": [{"view_id": "v"}],
        "dimension_trees": [],
        "input_adaptations": [],
        "fact_observations": [],
        "metric_compositions": [],
        "derived_requirements": [],
        "custom_calculations": [],
        "attribution_targets": [],
        "output_requirements": [],
        "clarifications": [],
    }


def h1_request(year: int, *, grain: str | None = None) -> dict:
    value = {
        "type": "bounded_span",
        "label": f"{year}年上半年",
        "start": f"{year}-01-01",
        "end": f"{year}-06-30",
    }
    if grain:
        value.update({"requested_grain": grain, "grain_source": "user_explicit"})
    return value


class PeriodResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compositions = load("metric-composition-registry.json")
        self.derived = load("derived-metric-registry.json")

    def prepare(self, ir: dict, index: dict | None = None) -> tuple[dict, list[dict]]:
        return prepare_analysis_ir(
            ir, index or capabilities(), self.compositions, self.derived
        )

    def compile(self, prepared: dict) -> dict:
        plan, report = compile_and_validate(
            prepared,
            ROOT / "references" / "derived-metric-registry.json",
            ROOT / "references" / "metric-composition-registry.json",
        )
        self.assertTrue(report["valid"], report)
        return plan

    def test_existing_native_period_path_is_unchanged(self) -> None:
        ir = base_ir("支付GMV", "volume", "平台")
        ir["analysis_task"]["periods"] = {"analysis": "2026-Q2"}
        ir["fact_observations"] = [{
            "requirement_id": "fact", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {"平台": "京东"}, "dimension_refs": [],
        }]
        index = capabilities()
        index["availability"]["quarter"]["metrics"]["支付GMV"] = {
            "dimension": "平台"
        }
        prepared, decisions = self.prepare(ir, index)
        self.assertEqual(prepared["analysis_task"]["periods"], {"analysis": "2026-Q2"})
        self.assertEqual(prepared["input_adaptations"], [])
        self.assertFalse(any(item.get("mode") in {"sum", "period_only"} for item in decisions))

    def test_additive_h1_is_summed_without_changing_metric_selection(self) -> None:
        ir = base_ir("支付GMV", "volume", "平台")
        ir["analysis_task"]["period_requests"] = {"analysis": h1_request(2026)}
        ir["fact_observations"] = [{
            "requirement_id": "fact", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {"平台": "京东"}, "dimension_refs": [],
        }]
        prepared, _ = self.prepare(ir)
        self.assertEqual(prepared["analysis_task"]["metrics"][0]["name"], "支付GMV")
        self.assertEqual(prepared["analysis_task"]["period_resolutions"][0]["mode"], "sum")
        self.assertEqual(
            prepared["analysis_task"]["period_resolutions"][0]["source_periods"],
            [f"2026-{month:02d}" for month in range(1, 7)],
        )
        self.assertEqual(len(prepared["input_adaptations"]), 1)
        self.compile(prepared)

    def test_non_additive_h1_expands_to_complete_supported_months(self) -> None:
        ir = base_ir("类目同比增速", "ratio", "类目")
        ir["analysis_task"]["period_requests"] = {"analysis": h1_request(2026)}
        ir["fact_observations"] = [{
            "requirement_id": "fact", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {}, "dimension_refs": ["类目"],
        }]
        prepared, _ = self.prepare(ir)
        resolution = prepared["analysis_task"]["period_resolutions"][0]
        self.assertEqual(resolution["mode"], "period_only")
        self.assertEqual(resolution["output_grain"], "month")
        self.assertEqual(resolution["source_periods"], [f"2026-{m:02d}" for m in range(1, 7)])
        self.assertEqual(len(prepared["fact_observations"][0]["period_roles"]), 6)
        self.assertEqual(prepared["input_adaptations"], [])
        self.compile(prepared)

    def test_explicit_week_uses_intersecting_iso_weeks_and_discloses_spill(self) -> None:
        ir = base_ir("类目同比增速", "ratio", "类目")
        ir["analysis_task"]["period_requests"] = {"analysis": {
            "type": "bounded_span", "label": "2026年5月",
            "start": "2026-05-01", "end": "2026-05-31",
            "requested_grain": "week", "grain_source": "user_explicit",
        }}
        ir["fact_observations"] = [{
            "requirement_id": "fact", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {}, "dimension_refs": ["类目"],
        }]
        index = capabilities()
        index["availability"]["week"] = {
            "periods": [f"2026-W{week:02d}" for week in range(18, 23)],
            "metrics": {"类目同比增速": {"dimension": "类目"}},
        }
        prepared, _ = self.prepare(ir, index)
        resolution = prepared["analysis_task"]["period_resolutions"][0]
        self.assertEqual(resolution["source_periods"], [f"2026-W{w:02d}" for w in range(18, 23)])
        self.assertTrue(resolution["boundary_spill"])

    def test_registered_ratio_recomputes_from_summed_formula_inputs(self) -> None:
        ir = base_ir("综合结算TR", "ratio", "平台")
        ir["analysis_task"]["period_requests"] = {"analysis": h1_request(2026)}
        ir["fact_observations"] = [{
            "requirement_id": "tr", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {"平台": "京东"}, "dimension_refs": [],
        }]
        prepared, _ = self.prepare(ir)
        resolution = prepared["analysis_task"]["period_resolutions"][0]
        self.assertEqual(resolution["mode"], "recompute")
        self.assertEqual(len(prepared["input_adaptations"]), 3)
        self.assertEqual(len(resolution["input_adaptations"]), 3)
        plan = self.compile(prepared)
        node = next(item for item in plan["nodes"] if item["type"] == "metric_composition")
        self.assertEqual(node["execution"]["composition_id"], "competitor_comprehensive_settlement_tr")

    def test_registered_derived_series_binds_each_clone_to_physical_roles(self) -> None:
        ir = base_ir("类目同比增速", "ratio", "类目")
        ir["analysis_task"]["period_requests"] = {
            "analysis": h1_request(2026),
            "analysis_last_year": h1_request(2025),
        }
        ir["derived_requirements"] = [{
            "requirement_id": "yoy_delta", "derived_metric_id": "yoy_growth",
            "definition_status": "registered", "metric_ref": "target",
            "metric_object": "ratio", "required_period_roles": [
                "analysis", "analysis_last_year"
            ],
            "view_id": "v", "dimensions": {}, "dimension_refs": ["类目"],
        }]
        prepared, _ = self.prepare(ir)
        self.assertEqual(len(prepared["derived_requirements"]), 6)
        for requirement in prepared["derived_requirements"]:
            self.assertEqual(set(requirement["period_role_bindings"]), {
                "analysis", "analysis_last_year"
            })
        plan = self.compile(prepared)
        derived_nodes = [item for item in plan["nodes"] if item["type"] == "derived_metric"]
        self.assertEqual(len(derived_nodes), 6)
        self.assertTrue(all(
            all("__period_" in role for role in node["execution"]["period_roles"])
            for node in derived_nodes
        ))

    def test_optional_yoy_series_uses_selected_physical_month_roles(self) -> None:
        ir = base_ir("月度客单价", "volume", "平台")
        ir["analysis_task"]["period_requests"] = {
            "analysis": {
                "type": "bounded_span", "label": "2025年下半年",
                "start": "2025-07-01", "end": "2025-12-31",
            },
            "analysis_last_year": {
                "type": "bounded_span", "label": "2024年下半年",
                "start": "2024-07-01", "end": "2024-12-31",
            },
        }
        common = {
            "metric_ref": "target", "view_id": "v",
            "dimensions": {"平台": "京东"}, "dimension_refs": [],
        }
        ir["fact_observations"] = [{
            **common, "requirement_id": "level", "period_roles": ["analysis"],
            "criticality": "core",
        }]
        ir["derived_requirements"] = [{
            **common, "requirement_id": "yoy", "derived_metric_id": "yoy_growth",
            "definition_status": "registered", "metric_object": "volume",
            "criticality": "optional",
            "default_output_role": "performance_yoy_supplement",
            "provenance": "business_policy",
        }]
        ir["output_requirements"] = [{
            "requirement_id": "answer", "source_requirement_refs": ["level", "yoy"],
            "criticality": "core",
        }]
        index = capabilities()
        index["metrics"]["月度客单价"] = {
            "unit": "元", "additive": False, "dimensions": ["平台"],
        }
        index["metric_bindings"]["月度客单价"] = "月度客单价"
        index["availability"]["month"]["metrics"]["月度客单价"] = {
            "dimension": "平台"
        }
        index["availability"]["month"]["periods"] = [
            f"{year}-{month:02d}"
            for year in (2024, 2025)
            for month in range(7, 13)
        ]

        prepared, _ = self.prepare(ir, index)

        resolutions = prepared["analysis_task"]["period_resolutions"]
        self.assertTrue(all(
            item["mode"] == "period_only" and item["output_grain"] == "month"
            for item in resolutions
        ))
        derived = prepared["derived_requirements"]
        self.assertEqual(len(derived), 6)
        for month, requirement in enumerate(derived, start=7):
            self.assertEqual(requirement["period_role_bindings"], {
                "analysis": f"analysis__period_2025_{month:02d}",
                "analysis_last_year": (
                    f"analysis_last_year__period_2024_{month:02d}"
                ),
            })
            self.assertEqual(
                requirement["default_output_role"], "performance_yoy_supplement"
            )
        self.assertEqual(
            ir["analysis_task"]["period_requests"]["analysis"]["start"],
            "2025-07-01",
        )
        plan = self.compile(prepared)
        derived_nodes = [
            node for node in plan["nodes"] if node["type"] == "derived_metric"
        ]
        self.assertEqual(len(derived_nodes), 6)
        fetched = {
            (slot["metric"], slot["period"])
            for request in plan["fetch_requests"]
            for slot in request["fact_slots"]
        }
        self.assertEqual(fetched, {
            ("月度客单价", f"{year}-{month:02d}")
            for year in (2024, 2025)
            for month in range(7, 13)
        })

    def test_explicit_unsupported_grain_fails_instead_of_substituting(self) -> None:
        ir = base_ir("类目同比增速", "ratio", "类目")
        ir["analysis_task"]["period_requests"] = {
            "analysis": h1_request(2026, grain="quarter")
        }
        ir["fact_observations"] = [{
            "requirement_id": "fact", "metric_ref": "target",
            "period_roles": ["analysis"], "view_id": "v",
            "dimensions": {}, "dimension_refs": ["类目"],
        }]
        with self.assertRaises(PreparationError) as caught:
            self.prepare(ir)
        self.assertEqual(caught.exception.code, "SOURCE_PATH_UNAVAILABLE")

    def test_two_metrics_can_expand_one_span_at_different_grains(self) -> None:
        ir = base_ir("季度比例", "ratio", "平台")
        ir["analysis_task"]["metrics"].append({
            "metric_id": "monthly", "name": "月度比例",
            "metric_object": "ratio", "unit": "待元信息解析",
        })
        ir["analysis_task"]["period_requests"] = {"analysis": h1_request(2026)}
        ir["fact_observations"] = [
            {
                "requirement_id": "quarterly", "metric_ref": "target",
                "period_roles": ["analysis"], "view_id": "v",
                "dimensions": {"平台": "京东"}, "dimension_refs": [],
            },
            {
                "requirement_id": "monthly", "metric_ref": "monthly",
                "period_roles": ["analysis"], "view_id": "v",
                "dimensions": {"平台": "京东"}, "dimension_refs": [],
            },
        ]
        index = capabilities()
        for name in ("季度比例", "月度比例"):
            index["metrics"][name] = {
                "unit": "%", "additive": False, "dimensions": ["平台"]
            }
            index["metric_bindings"][name] = name
        index["availability"]["quarter"] = {
            "periods": ["2026-Q1", "2026-Q2"],
            "metrics": {"季度比例": {"dimension": "平台"}},
        }
        index["availability"]["month"]["metrics"]["月度比例"] = {
            "dimension": "平台"
        }
        prepared, _ = self.prepare(ir, index)
        grains = {
            item["requirement_id"]: item["output_grain"]
            for item in prepared["analysis_task"]["period_resolutions"]
        }
        self.assertEqual(grains, {"quarterly": "quarter", "monthly": "month"})
        roles = [
            role
            for requirement in prepared["fact_observations"]
            for role in requirement["period_roles"]
        ]
        self.assertEqual(len(roles), len(set(roles)))
        self.compile(prepared)


if __name__ == "__main__":
    unittest.main()
