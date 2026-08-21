#!/usr/bin/env python3
"""Run an admitted scene-analysis fast query in one deterministic workflow."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compile_plan import compile_and_validate, load_json
from dimension_domain_registry import DEFAULT_REGISTRY_PATH as DEFAULT_DIMENSION_SET_REGISTRY
from execution_runner import execute_plan
from fact_contract import SCENE_FACTS_V1, SCENE_FACTS_V2, project_scene_facts
from source_runtime import shared_index_path
from validate_execution import Validator


class FastQueryFallback(ValueError):
    def __init__(self, trigger: str, detail: str) -> None:
        super().__init__(detail)
        self.trigger = trigger
        self.detail = detail


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _record_adaptation(adaptations: list[str] | None, code: str) -> None:
    if adaptations is not None and code not in adaptations:
        adaptations.append(code)


def _first_non_null(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def canonicalize_response_row(
    row: dict[str, Any],
    adaptations: list[str] | None = None,
) -> dict[str, Any]:
    canonical = dict(row)
    if "value" not in canonical and "metric_value" in canonical:
        canonical["value"] = canonical["metric_value"]
        _record_adaptation(adaptations, "metric_value_to_value")
    if "missing" not in canonical and "is_missing" in canonical:
        canonical["missing"] = canonical["is_missing"]
        _record_adaptation(adaptations, "is_missing_to_missing")
    if "coverage" not in canonical:
        coverage = _first_non_null(canonical, ("coverage_type", "coverage_status"))
        if coverage is not None:
            canonical["coverage"] = coverage
            _record_adaptation(adaptations, "coverage_alias_to_coverage")

    if not isinstance(canonical.get("dimensions"), dict):
        dimension_ref = _first_non_null(canonical, ("dimension_ref", "dimension_name"))
        dimension_value = _first_non_null(
            canonical,
            ("dimension_value", "dimension_value_name", "dimension_value_id"),
        )
        if isinstance(dimension_ref, str) and dimension_ref and dimension_value is not None:
            canonical["dimensions"] = {dimension_ref: dimension_value}
            _record_adaptation(adaptations, "flat_dimension_to_dimensions")
    return canonical


def response_rows(
    payload: Any,
    adaptations: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metadata = payload if isinstance(payload, dict) else {}
    raw_rows: Any = payload
    columns: Any = None
    if isinstance(payload, dict):
        raw_rows = payload.get("facts")
        columns = payload.get("columns")
        if columns is None and payload.get("schema") is not None:
            columns = payload.get("schema")
            _record_adaptation(adaptations, "schema_to_columns")
        if raw_rows is None and isinstance(payload.get("data"), dict):
            raw_rows = payload["data"].get("facts")
            nested_columns = payload["data"].get("columns")
            if nested_columns is None and payload["data"].get("schema") is not None:
                nested_columns = payload["data"].get("schema")
                _record_adaptation(adaptations, "schema_to_columns")
            columns = nested_columns if nested_columns is not None else columns
    if not isinstance(raw_rows, list):
        raise FastQueryFallback("response_not_machine_parseable", "structured response has no facts array")
    if raw_rows and all(isinstance(row, list) for row in raw_rows):
        if not isinstance(columns, list) or not columns or not all(isinstance(item, str) and item for item in columns):
            raise FastQueryFallback("response_not_machine_parseable", "columnar facts require string columns")
        rows = []
        for index, row in enumerate(raw_rows):
            if len(row) != len(columns):
                raise FastQueryFallback(
                    "response_not_machine_parseable",
                    f"facts[{index}] has {len(row)} values for {len(columns)} columns",
                )
            rows.append(canonicalize_response_row(dict(zip(columns, row)), adaptations))
        return rows, metadata
    if not all(isinstance(row, dict) for row in raw_rows):
        raise FastQueryFallback("response_not_machine_parseable", "facts must contain objects or uniform arrays")
    return [canonicalize_response_row(row, adaptations) for row in raw_rows], metadata


def _metric_maps(plan: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    metrics = (plan.get("analysis_task") or {}).get("metrics", [])
    by_id = {
        str(item["metric_id"]): item
        for item in metrics
        if isinstance(item, dict) and item.get("metric_id")
    }
    by_name = {
        str(item["name"]): item
        for item in metrics
        if isinstance(item, dict) and item.get("name")
    }
    return by_id, by_name


def _row_dimensions(row: dict[str, Any], slot: dict[str, Any] | None) -> dict[str, Any]:
    dimensions = row.get("dimensions")
    if isinstance(dimensions, dict):
        return dimensions
    dimension_ref = row.get("dimension_ref")
    if isinstance(dimension_ref, str) and dimension_ref:
        return {dimension_ref: row.get("dimension_value")}
    dimension_refs = slot.get("dimension_refs", []) if isinstance(slot, dict) else []
    dimension_value = _first_non_null(
        row,
        ("dimension_value", "dimension_value_name", "dimension_value_id"),
    )
    if len(dimension_refs) == 1 and dimension_value is not None:
        return {str(dimension_refs[0]): dimension_value}
    return {}


def _row_matches_slot(
    row: dict[str, Any],
    slot: dict[str, Any],
    dimensions: dict[str, Any],
) -> bool:
    if row.get("period") is not None and str(row.get("period")) != str(slot.get("period")):
        return False
    if row.get("period_role") is not None and str(row.get("period_role")) != str(slot.get("period_role")):
        return False
    if row.get("view_id") is not None and row.get("view_id") != slot.get("view_id"):
        return False
    if row.get("metric_ref") is not None and row.get("metric_ref") != slot.get("metric_ref"):
        return False
    if row.get("metric") is not None and row.get("metric") != slot.get("metric"):
        return False
    expected_dimensions = set(slot.get("dimension_refs", []))
    if expected_dimensions != set(dimensions):
        return False
    if expected_dimensions and any(value is None for value in dimensions.values()):
        return False
    return True


def _match_slot(
    row: dict[str, Any],
    slots: dict[str, dict[str, Any]],
    dimensions: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    supplied = row.get("fact_slot_id")
    if isinstance(supplied, str):
        if supplied not in slots:
            raise FastQueryFallback("fact_slot_unbound", f"response references unknown fact slot: {supplied}")
        supplied_slot = slots[supplied]
        supplied_dimensions = _row_dimensions(row, supplied_slot)
        if not _row_matches_slot(row, supplied_slot, supplied_dimensions):
            raise FastQueryFallback(
                "period_view_or_grain_mismatch",
                f"response row does not match supplied fact slot: {supplied}",
            )
        return supplied, supplied_slot
    candidates = []
    for slot_id, slot in slots.items():
        if not _row_matches_slot(row, slot, dimensions):
            continue
        candidates.append((slot_id, slot))
    if len(candidates) != 1:
        raise FastQueryFallback(
            "period_view_or_grain_mismatch",
            f"fact row binds to {len(candidates)} slots instead of exactly one",
        )
    return candidates[0]


def standardize_facts(
    payload: Any,
    plan: dict[str, Any],
    request_id: str,
    adaptations: list[str] | None = None,
) -> list[dict[str, Any]]:
    rows, metadata = response_rows(payload, adaptations)
    admission = plan.get("fast_query_admission") or {}
    max_rows = int((admission.get("limits") or {}).get("result_rows", 1000))
    if len(rows) > max_rows:
        raise FastQueryFallback("result_row_budget_exceeded", f"response has {len(rows)} rows; limit is {max_rows}")
    slots = {
        str(item["fact_slot_id"]): item
        for item in (plan.get("analysis_task") or {}).get("fact_requirements", [])
        if isinstance(item, dict) and item.get("fact_slot_id")
    }
    if not slots:
        raise FastQueryFallback("fact_slot_unbound", "compiled plan has no fact slots")
    by_metric_id, by_metric_name = _metric_maps(plan)
    metric_definition = metadata.get("metric_definition") if isinstance(metadata, dict) else None
    default_definition = metric_definition.get("definition") if isinstance(metric_definition, dict) else None
    standardized: list[dict[str, Any]] = []
    covered: set[str] = set()
    covered_with_value: set[str] = set()
    slot_units: dict[str, set[str]] = {}
    slot_definitions: dict[str, set[str]] = {}
    for index, row in enumerate(rows):
        preliminary_dimensions = _row_dimensions(row, None)
        slot_id, slot = _match_slot(row, slots, preliminary_dimensions)
        dimensions = _row_dimensions(row, slot)
        metric_ref = str(row.get("metric_ref") or slot.get("metric_ref"))
        metric_info = by_metric_id.get(metric_ref) or by_metric_name.get(str(row.get("metric"))) or {}
        metric_name = str(row.get("metric") or slot.get("metric") or metric_info.get("name") or metric_ref)
        value = row["value"] if "value" in row else row.get("metric_value")
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise FastQueryFallback("non_finite_value_or_unsupported_missing_state", f"facts[{index}] value is not finite numeric")
        numerator = row.get("numerator")
        denominator = row.get("denominator")
        complete_components = (
            isinstance(numerator, (int, float))
            and not isinstance(numerator, bool)
            and isinstance(denominator, (int, float))
            and not isinstance(denominator, bool)
            and math.isfinite(float(numerator))
            and math.isfinite(float(denominator))
            and float(denominator) != 0
        )
        missing = row.get("missing") is True or (value is None and not complete_components)
        unit = str(row.get("unit") or slot.get("unit") or metric_info.get("unit") or "unknown")
        slot_units.setdefault(slot_id, set()).add(unit)
        definition = str(row.get("definition") or default_definition or metric_info.get("definition") or "resolved by fetch skill")
        slot_definitions.setdefault(slot_id, set()).add(definition)
        covered.add(slot_id)
        if not missing:
            covered_with_value.add(slot_id)
        standardized.append({
            "fact_slot_id": slot_id,
            "metric": metric_name,
            "metric_ref": metric_ref,
            "view_id": row.get("view_id", slot.get("view_id")),
            "period": str(row.get("period", slot.get("period"))),
            "period_role": str(row.get("period_role", slot.get("period_role"))),
            "dimensions": dimensions,
            "value": value,
            "numerator": numerator,
            "denominator": denominator,
            "unit": unit,
            "definition": definition,
            "raw_missing": missing,
            "missing": missing,
            "normalization_reason": "source_missing" if missing else "unchanged",
            "value_derived_from_components": False,
            "source_request_id": request_id,
            "source_ref": f"structured-response.json#facts[{index}]",
            "coverage": row.get("coverage", "full"),
            "coverage_rate": row.get("coverage_rate"),
        })
    unbound = sorted(set(slots) - covered)
    if unbound:
        raise FastQueryFallback("fact_slot_unbound", f"response does not cover fact slots: {unbound}")
    no_values = sorted(set(slots) - covered_with_value)
    if no_values:
        raise FastQueryFallback("fact_slot_unbound", f"fact slots contain no usable values: {no_values}")
    conflicts = sorted(slot_id for slot_id, units in slot_units.items() if len(units) > 1)
    if conflicts:
        raise FastQueryFallback("unit_or_definition_conflict", f"fact slots contain multiple units: {conflicts}")
    definition_conflicts = sorted(slot_id for slot_id, definitions in slot_definitions.items() if len(definitions) > 1)
    if definition_conflicts:
        raise FastQueryFallback(
            "unit_or_definition_conflict",
            f"fact slots contain multiple definitions: {definition_conflicts}",
        )
    full_required = any(
        phrase in str((plan.get("analysis_task") or {}).get("scope") or "")
        for phrase in ("全量", "全部", "完整覆盖")
    )
    if full_required and metadata_declares_incomplete_coverage(metadata):
        raise FastQueryFallback(
            "incomplete_group_coverage_when_full_required",
            "response declares partial or TopN coverage for a full-coverage request",
        )
    if isinstance(metadata, dict) and metadata.get("additional_fetch_required") is True:
        raise FastQueryFallback("additional_fetch_required", "response requires an additional fetch round")
    return standardized


def metadata_declares_incomplete_coverage(metadata: Any) -> bool:
    pending = [metadata]
    incomplete_values = {"partial", "topn", "top_n", "incomplete"}
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for raw_key, value in item.items():
                key = str(raw_key).lower()
                if key == "topn" and value is True:
                    return True
                if key in {"partial", "incomplete"} and value is True:
                    return True
                if (
                    key == "coverage_rate"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value < 1
                ):
                    return True
                if (
                    key in {"coverage", "coverage_status", "coverage_type"}
                    and isinstance(value, str)
                    and value.lower() in incomplete_values
                ):
                    return True
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
    return False


def validate_scene_facts(payload: Any, plan: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
    """Validate Provider output without transforming it into another response shape."""
    if not isinstance(payload, dict) or payload.get("schema_version") not in {SCENE_FACTS_V1, SCENE_FACTS_V2}:
        raise FastQueryFallback("facts_not_machine_parseable", "Provider output must be scene_facts/1.0 or scene_facts/2.0")
    try:
        rows = project_scene_facts(payload, task_id="default")
    except ValueError as exc:
        raise FastQueryFallback("facts_not_machine_parseable", str(exc)) from exc
    if not isinstance(rows, list):
        raise FastQueryFallback("facts_not_machine_parseable", "scene_facts/1.0 requires a facts array")
    slots = {
        str(item["fact_slot_id"]): item
        for item in (plan.get("analysis_task") or {}).get("fact_requirements", [])
        if isinstance(item, dict) and item.get("fact_slot_id")
    }
    covered_slots: set[str] = set()
    seen_rows: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise FastQueryFallback("facts_not_machine_parseable", f"facts[{index}] must be an object")
        slot_id = row.get("fact_slot_id")
        if slot_id not in slots:
            raise FastQueryFallback("fact_slot_unbound", f"facts[{index}] references unknown fact slot")
        covered_slots.add(str(slot_id))
        for field in ("metric_ref", "metric", "period", "period_role", "view_id", "dimensions", "unit", "definition", "missing", "raw_missing"):
            if field not in row:
                raise FastQueryFallback("facts_not_machine_parseable", f"facts[{index}] missing {field}")
        value = row.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))):
            raise FastQueryFallback("non_finite_value_or_unsupported_missing_state", f"facts[{index}] value is not finite numeric")
        row_identity = json.dumps(
            {
                "fact_slot_id": slot_id,
                "metric_ref": row.get("metric_ref"),
                "period": row.get("period"),
                "period_role": row.get("period_role"),
                "view_id": row.get("view_id"),
                "dimensions": row.get("dimensions"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if row_identity in seen_rows:
            raise FastQueryFallback("fact_slot_unbound", f"facts[{index}] duplicates a fact identity")
        seen_rows.add(row_identity)
    missing_slots = sorted(set(slots) - covered_slots)
    if missing_slots:
        raise FastQueryFallback("fact_slot_unbound", f"scene facts do not cover fact slots: {missing_slots}")
    if metadata_declares_incomplete_coverage(payload):
        scope = str((plan.get("analysis_task") or {}).get("scope") or "")
        if any(phrase in scope for phrase in ("全量", "全部", "完整覆盖")):
            raise FastQueryFallback("incomplete_group_coverage_when_full_required", "Provider declares partial coverage")
    return rows


def answer_payload(manifest: dict[str, Any], profile: str) -> dict[str, Any]:
    facts = manifest.get("normalized_facts") if isinstance(manifest.get("normalized_facts"), list) else []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in facts:
        if not isinstance(row, dict):
            continue
        grouped.setdefault(str(row.get("view_id") or "default"), []).append({
            "metric": row.get("metric"),
            "period": row.get("period"),
            "period_role": row.get("period_role"),
            "dimensions": row.get("dimensions", {}),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "missing": row.get("missing"),
        })
    return {
        "schema_version": "fast_query_answer/1.0",
        "status": manifest.get("status"),
        "execution_profile": profile,
        "views": [
            {"view_id": view_id, "rows": rows}
            for view_id, rows in sorted(grouped.items())
        ],
        "derived_results": manifest.get("derived_results", []),
        "attribution_results": manifest.get("attribution_results", []),
        "model_completion": manifest.get("model_completion"),
        "quality": {
            "logical_facts": len(facts),
            "missing_facts": sum(bool(row.get("missing")) for row in facts if isinstance(row, dict)),
            "fetch_attempts": (manifest.get("performance_metrics") or {}).get("fetch_attempts"),
            "validation_profile": "minimal",
            "response_adaptations": (manifest.get("fast_query_runtime") or {}).get("response_adaptations", []),
        },
        "scope_and_assumptions": {
            "scope": (manifest.get("analysis_task") or {}).get("scope"),
            "assumptions": (manifest.get("analysis_task") or {}).get("assumptions", []),
        },
    }


def finalize_model_nodes(manifest: dict[str, Any]) -> None:
    nodes = manifest.get("nodes") if isinstance(manifest.get("nodes"), list) else []
    by_id = {str(node.get("node_id")): node for node in nodes if isinstance(node, dict) and node.get("node_id")}
    for node in nodes:
        execution = node.get("execution") if isinstance(node, dict) else None
        if not isinstance(execution, dict) or execution.get("handler") != "model_owned":
            continue
        dependency_statuses = [
            by_id.get(str(dependency), {}).get("status")
            for dependency in node.get("depends_on", [])
        ]
        node["status"] = (
            "success"
            if all(status == "success" for status in dependency_statuses)
            else "partial_success"
        )
    succeeded = sorted(node_id for node_id, node in by_id.items() if node.get("status") == "success")
    failed = sorted(node_id for node_id, node in by_id.items() if node.get("status") == "failed")
    partial = sorted(node_id for node_id, node in by_id.items() if node.get("status") == "partial_success")
    skipped = sorted(node_id for node_id, node in by_id.items() if node.get("status") == "skipped")
    blocked = sorted(node_id for node_id, node in by_id.items() if node.get("status") == "blocked")
    summary = manifest.setdefault("execution_summary", {})
    summary.update({
        "succeeded_nodes": succeeded,
        "failed_nodes": failed,
        "partial_nodes": partial,
        "skipped_nodes": skipped,
        "blocked_nodes": blocked,
    })
    successful_result_refs = sorted(
        node_id
        for node_id, node in by_id.items()
        if node.get("status") == "success" and (node.get("execution") or {}).get("handler") != "model_owned"
    )
    incomplete = sorted(
        node_id
        for node_id in set(failed + partial + skipped + blocked)
        if (by_id.get(node_id, {}).get("execution") or {}).get("handler") != "model_owned"
    )
    manifest["conclusions"] = [{
        "conclusion_id": "fast_query_result",
        "status": "partial_success" if incomplete else "success",
        "result_refs": successful_result_refs,
        "statement": (
            "Use validated facts and successful results, then compare them with the original query "
            "and complete any remaining low-risk deterministic analysis."
            if incomplete
            else "Fast query completed from validated facts and deterministic calculations."
        ),
    }]
    manifest["model_completion"] = {
        "required": bool(incomplete),
        "query": (manifest.get("analysis_task") or {}).get("query"),
        "successful_result_refs": successful_result_refs,
        "incomplete_node_ids": incomplete,
        "instruction": (
            "Compare the original query with successful facts/results. Complete only calculations "
            "whose inputs and business definitions are unambiguous; otherwise disclose the gap."
        ),
    }
    if any(
        node.get("criticality", "required") == "core" and node.get("status") != "success"
        for node in nodes
    ):
        final_status = "blocked"
    elif any(
        node.get("criticality", "required") == "required" and node.get("status") != "success"
        for node in nodes
    ):
        final_status = "partial_success"
    elif any(node.get("status") == "partial_success" for node in nodes):
        final_status = "partial_success"
    else:
        final_status = "success"
    manifest["status"] = final_status
    manifest["declared_status"] = final_status
    manifest["computed_status"] = final_status


def fallback_document(
    trigger: str,
    detail: str,
    work_dir: Path,
    plan: dict[str, Any] | None,
    *,
    workflow_duration_ms: float | None = None,
    post_fetch_elapsed_ms: float | None = None,
) -> dict[str, Any]:
    artifact_candidates = {
        "compiled_plan": work_dir / "compiled-plan.json",
        "plan_validation": work_dir / "plan-validation.json",
        "fetch_request": work_dir / "fetch-request.json",
        "fetch_result": work_dir / "fetch-result.json",
        "facts": work_dir / "facts.json",
    }
    artifacts = {key: str(path) for key, path in artifact_candidates.items() if path.exists()}
    successful_fetch_reusable = False
    fetch_result_path = artifact_candidates["fetch_result"]
    if fetch_result_path.exists():
        try:
            successful_fetch_reusable = load_json(fetch_result_path).get("status") == "success"
        except Exception:  # noqa: BLE001 - fallback metadata must remain best-effort.
            successful_fetch_reusable = False
    if trigger in {"fast_query_not_admitted", "ATTRIBUTION_REQUIRES_ORCHESTRATION", "plan_validation_failed"}:
        fallback_class = "admission_or_plan"
    elif trigger in {"fast_query_internal_error", "fetch_failed"}:
        fallback_class = "internal_or_transport"
    else:
        fallback_class = "runtime_semantic_or_data"
    document = {
        "schema_version": "fast_query_fallback/1.0",
        "status": "requires_standard_orchestration",
        "trigger": trigger,
        "detail": detail,
        "fallback_class": fallback_class,
        "execution_profile": (plan or {}).get("execution_profile"),
        "successful_fetch_reusable": successful_fetch_reusable,
        "reuse_policy": (
            "reuse the successful raw/structured response and any existing facts; do not repeat the fetch"
            if successful_fetch_reusable
            else "no successful fetch is available for reuse"
        ),
        "artifacts": artifacts,
    }
    runtime = {}
    if workflow_duration_ms is not None:
        runtime["workflow_duration_ms"] = workflow_duration_ms
    if post_fetch_elapsed_ms is not None:
        runtime["post_fetch_elapsed_ms"] = post_fetch_elapsed_ms
    if runtime:
        document["runtime"] = runtime
    return document


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="analysis_ir/1.0 JSON")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--index", type=Path, help="competitor structure cache path")
    parser.add_argument("--response-file", type=Path, help="offline scene_facts/1.0 response for tests/replay")
    parser.add_argument("--derived-registry", type=Path, default=root / "references" / "derived-metric-registry.json")
    parser.add_argument("--dimension-set-registry", type=Path, default=DEFAULT_DIMENSION_SET_REGISTRY)
    return parser.parse_args()


def legacy_main() -> int:
    args = parse_args()
    started = time.perf_counter()
    fetch_completed: float | None = None
    work_dir = args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)
    plan: dict[str, Any] | None = None
    try:
        ir = load_json(args.input)
        if ir.get("attribution_targets"):
            raise FastQueryFallback("ATTRIBUTION_REQUIRES_ORCHESTRATION", "attribution targets are not eligible for fast execution")
        plan, plan_report = compile_and_validate(
            ir, args.derived_registry, dimension_set_registry_path=args.dimension_set_registry
        )
        plan_path = work_dir / "compiled-plan.json"
        plan_report_path = work_dir / "plan-validation.json"
        write_json(plan_path, plan)
        write_json(plan_report_path, plan_report)
        if not plan_report.get("valid"):
            raise FastQueryFallback("plan_validation_failed", "compiled plan failed validation")
        admission = plan.get("fast_query_admission") or {}
        if not admission.get("eligible"):
            raise FastQueryFallback(
                "fast_query_not_admitted",
                f"execution profile is {plan.get('execution_profile')}: {admission.get('reasons', [])}",
            )
        requests = plan.get("fetch_requests")
        if not isinstance(requests, list) or len(requests) != 1:
            raise FastQueryFallback("additional_fetch_required", "fast execution requires exactly one unified fetch request")
        request = requests[0]
        request_id = str(request["request_id"])
        request_path = work_dir / "fetch-request.json"
        facts_path = work_dir / "facts.json"
        fetch_result_path = work_dir / "fetch-result.json"
        index_path = args.index or shared_index_path()
        write_json(request_path, request)
        if args.response_file is not None:
            payload = load_json(args.response_file)
            if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
                raise FastQueryFallback("facts_not_machine_parseable", "offline response must be scene_facts/1.0")
            write_json(facts_path, payload)
            now = utc_now()
            fetch_result = {
                "request_id": request_id,
                "status": "success",
                "started_at": now,
                "ended_at": now,
                "duration_ms": 0,
                "raw_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                "response_format": "scene_facts/1.0",
                "facts_artifact": str(facts_path),
                "exit_code": 0,
                "replayed": True,
            }
            write_json(fetch_result_path, fetch_result)
        else:
            process = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("run_fetch.py")),
                    "--request-file", str(request_path),
                    "--facts", str(facts_path),
                    "--result", str(fetch_result_path),
                    "--index", str(index_path),
                    "--dimension-set-registry", str(args.dimension_set_registry),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if process.returncode != 0:
                raise FastQueryFallback("fetch_failed", process.stderr.strip() or process.stdout.strip() or "fetch failed")
            fetch_result = load_json(fetch_result_path)
            if fetch_result.get("status") != "success" or not facts_path.exists():
                raise FastQueryFallback("facts_not_machine_parseable", "successful Provider run did not yield facts")
            payload = load_json(facts_path)
        fetch_completed = time.perf_counter()
        facts = validate_scene_facts(payload, plan, request_id)
        write_json(facts_path, {"schema_version": "scene_facts/1.0", "facts": facts, "source": payload.get("source", {})})
        plan["fetch_results"] = [fetch_result]
        plan["validation_reports"] = {
            "plan": {"artifact": str(plan_report_path), "exit_code": 0}
        }
        write_json(plan_path, plan)
        manifest_path = work_dir / "execution-manifest.json"
        events_path = work_dir / "execution-events.jsonl"
        manifest = execute_plan(
            plan,
            facts,
            manifest_path,
            events_path,
            storage_mode="inline",
        )
        finalize_model_nodes(manifest)
        final_report_path = work_dir / "final-validation.json"
        manifest.setdefault("validation_reports", {})["final"] = {
            "artifact": str(final_report_path),
            "exit_code": 0,
        }
        manifest["fast_query_runtime"] = {
            "schema_version": "fast_query_runtime/1.0",
            "admitted_profile": plan["execution_profile"],
            "pre_final_validation_ms": round((time.perf_counter() - started) * 1000, 3),
            "post_fetch_pre_final_validation_ms": round((time.perf_counter() - fetch_completed) * 1000, 3),
            "response_adaptations": [],
            "fallback_used": False,
        }
        write_json(manifest_path, manifest)
        final_report = Validator(manifest, "final", manifest_path.parent).validate()
        write_json(final_report_path, final_report)
        if not final_report.get("valid"):
            raise FastQueryFallback("final_validation_failed", "fast execution failed final validation")
        answer = answer_payload(manifest, str(plan["execution_profile"]))
        answer["workflow_duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        answer["post_fetch_to_answer_ms"] = round((time.perf_counter() - fetch_completed) * 1000, 3)
        answer["artifacts"] = {
            "manifest": str(manifest_path),
            "final_validation": str(final_report_path),
        }
        answer_path = work_dir / "answer-payload.json"
        write_json(answer_path, answer)
        print(json.dumps({
            "status": "success",
            "execution_profile": plan["execution_profile"],
            "workflow_duration_ms": answer["workflow_duration_ms"],
            "answer": str(answer_path),
        }, ensure_ascii=False))
        return 0
    except FastQueryFallback as exc:
        fallback = fallback_document(
            exc.trigger,
            exc.detail,
            work_dir,
            plan,
            workflow_duration_ms=round((time.perf_counter() - started) * 1000, 3),
            post_fetch_elapsed_ms=(
                round((time.perf_counter() - fetch_completed) * 1000, 3)
                if fetch_completed is not None
                else None
            ),
        )
        write_json(work_dir / "fallback.json", fallback)
        print(json.dumps(fallback, ensure_ascii=False), file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - return concise deterministic failure metadata.
        fallback = fallback_document(
            "fast_query_internal_error",
            str(exc),
            work_dir,
            plan,
            workflow_duration_ms=round((time.perf_counter() - started) * 1000, 3),
            post_fetch_elapsed_ms=(
                round((time.perf_counter() - fetch_completed) * 1000, 3)
                if fetch_completed is not None
                else None
            ),
        )
        fallback["error_type"] = exc.__class__.__name__
        write_json(work_dir / "fallback.json", fallback)
        print(json.dumps(fallback, ensure_ascii=False), file=sys.stderr)
        return 2


def main() -> int:
    """Compatibility entrypoint; all profiles now use the unified runner."""
    args = parse_args()
    delegated = [
        "run_analysis.py",
        "--input", str(args.input),
        "--work-dir", str(args.work_dir),
        "--derived-registry", str(args.derived_registry),
        "--dimension-set-registry", str(args.dimension_set_registry),
    ]
    if args.index is not None:
        delegated.extend(["--index", str(args.index)])
    if args.response_file is not None:
        delegated.extend(["--response-file", str(args.response_file)])
    original = sys.argv
    try:
        sys.argv = delegated
        from run_analysis import main as run_unified
        return run_unified()
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
