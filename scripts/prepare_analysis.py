#!/usr/bin/env python3
"""Normalize semantic IR and resolve source-backed inputs before compilation."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from analysis_ir_normalizer import (
    AnalysisIRNormalizationError,
    normalize_analysis_input,
    normalize_analysis_ir,
)
from ir_contract_guard import (
    IRContractError,
    validate_analysis_ir_contract,
)
from source_capability import (
    evaluate_direct_capability,
    metric_aggregation_eligibility,
    normalize_match_text,
    normalize_period,
    resolve_dimension,
    resolve_metric,
    validate_capabilities,
)
from selector_context import SelectorContextError, apply_task_selector_context
from set_materialization import (
    SetMaterializationError,
    materialize_source_domain_set_spec,
    materialize_set_spec,
    set_aggregate_expression,
)
from time_rollup import iso_weeks_covering
from unit_scale import UnitScaleError, conversion_factor, formula_scale


class PreparationError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


UNRESOLVED_METADATA_VALUES = {"", "unknown", "待元信息解析", "未解析"}


def _is_unresolved_metadata(value: Any) -> bool:
    return value is None or str(value).strip().lower() in UNRESOLVED_METADATA_VALUES


def _canonical_period(value: Any, path: str) -> str:
    """Compatibility helper for internal rollup materialization."""
    parsed = normalize_period(value)
    if parsed is None:
        raise PreparationError("INVALID_PERIOD", f"{path} 无法识别时期：{value}")
    return parsed[1]


def _stable_suffix(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:10]


def _merge_dimensions(
    inherited: Any,
    declared: Any,
    path: str,
) -> dict[str, Any]:
    if not isinstance(inherited, dict) or (
        declared is not None and not isinstance(declared, dict)
    ):
        raise PreparationError(
            "DIMENSION_CONFLICT",
            f"{path}.dimensions 必须是对象",
        )
    merged = deepcopy(inherited)
    for dimension, value in (declared or {}).items():
        if (
            dimension in merged
            and json.dumps(merged[dimension], ensure_ascii=False, sort_keys=True)
            != json.dumps(value, ensure_ascii=False, sort_keys=True)
        ):
            raise PreparationError(
                "DIMENSION_CONFLICT",
                f"{path}.dimensions 与继承维度 {dimension!r} 冲突",
                {
                    "dimension": str(dimension),
                    "inherited": deepcopy(merged[dimension]),
                    "declared": deepcopy(value),
                },
            )
        merged[str(dimension)] = deepcopy(value)
    return merged


def _metric_map(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    task = ir["analysis_task"]
    return {
        str(metric["metric_id"]): metric
        for metric in task.get("metrics") or []
        if isinstance(metric, dict) and metric.get("metric_id")
    }


def _source_metric(name: str, index: dict[str, Any]) -> str | None:
    return resolve_metric(name, index)


def _source_dimension(
    name: str,
    index: dict[str, Any],
    source_metric: str | None = None,
) -> str | None:
    if source_metric:
        metric_bindings = (index.get("metric_dimension_bindings") or {}).get(source_metric) or {}
        if metric_bindings.get(name):
            return str(metric_bindings[name])
    return resolve_dimension(name, index)


def _apply_business_intent_selection(ir: dict[str, Any], index: dict[str, Any]) -> None:
    """Apply one audited intent selection before source metadata conflict checks."""
    resolutions = index.get("intent_resolutions") or {}
    metrics = _metric_map(ir)
    for metric_ref, resolution in resolutions.items():
        if not isinstance(resolution, dict):
            continue
        selected = resolution.get("selected_candidate") or {}
        if not isinstance(selected, dict) or not selected.get("metric"):
            continue
        metric = metrics.get(str(metric_ref))
        if metric is None:
            continue
        selected_object = selected.get("metric_object")
        if selected.get("object_override_allowed") and selected_object in {"volume", "ratio"}:
            metric["metric_object"] = selected_object
            metric["metric_object_source"] = "business_intent_policy"
            for requirement in ir.get("derived_requirements") or []:
                if (
                    isinstance(requirement, dict)
                    and str(requirement.get("metric_ref")) == str(metric_ref)
                    and requirement.get("metric_object") in {"volume", "ratio"}
                ):
                    requirement["metric_object"] = selected_object
                    requirement["metric_object_source"] = "business_intent_policy"
        metric["business_intent_id"] = selected.get("intent_id")
        metric["business_intent_candidate_id"] = selected.get("candidate_id")


def _materialize_composition_input_binding(
    ir: dict[str, Any],
    index: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    source_catalogue: dict[str, Any],
    materialized_source_refs: dict[tuple[str, str, str, str], str],
    requirement_id: str,
    output_metric_ref: str,
    role: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Materialize one already-resolved composition leaf without re-resolving it."""
    mode = str(binding.get("mode") or "")
    source_metric = str(binding.get("source_metric") or "")
    metadata = source_catalogue.get(source_metric) or {}
    if mode not in {"member_selector", "source_scoped_fact"} or not source_metric or not metadata:
        raise PreparationError(
            "COMPOSITION_INPUT_BINDING_INVALID",
            f"需求 {requirement_id} 的组合输入 {role!r} 绑定不可物化",
            {"role": role, "binding": deepcopy(binding)},
        )
    binding_fingerprint = _stable_suffix(binding)
    materialization_key = (
        output_metric_ref,
        source_metric,
        mode,
        f"{role}:{binding_fingerprint}",
    )
    source_metric_ref = materialized_source_refs.get(materialization_key)
    if source_metric_ref is None:
        source_metric_ref = f"__source_requirement_{_stable_suffix(materialization_key)}"
        materialized_source_refs[materialization_key] = source_metric_ref
    source_object = metadata.get("metric_object")
    if source_object not in {"volume", "ratio"}:
        source_object = "ratio" if str(metadata.get("unit") or "").lower() in {
            "%", "rate", "ratio", "share", "pp"
        } else "volume"
    if source_metric_ref not in metrics:
        source_declaration = {
            "metric_id": source_metric_ref,
            "name": source_metric,
            "metric_object": source_object,
            "unit": metadata.get("unit") or "待元信息解析",
            "definition": metadata.get("notes") or "source metric for registered composition",
            "source_metric_name": source_metric,
            "source_metric_object": source_object,
            "source_dimension_bindings": deepcopy(
                (index.get("metric_dimension_bindings") or {}).get(source_metric) or {}
            ),
            "aggregation": metadata.get("aggregation"),
            "additive": metadata.get("additive"),
            "generated_from": "requirement_composition_binding",
            "logical_metric_ref": output_metric_ref,
            "composition_input_role": role,
        }
        ir["analysis_task"].setdefault("metrics", []).append(source_declaration)
        metrics[source_metric_ref] = source_declaration
    index.setdefault("metric_bindings", {})[source_metric] = source_metric

    dimensions: dict[str, Any] = {}
    for constraint in binding.get("metric_constraints") or []:
        if not isinstance(constraint, dict) or not constraint.get("source_dimension"):
            continue
        source_dimension = str(constraint["source_dimension"])
        index.setdefault("dimension_bindings", {}).setdefault(
            source_dimension, source_dimension
        )
        index.setdefault("metric_dimension_bindings", {}).setdefault(
            source_metric, {}
        ).setdefault(source_dimension, source_dimension)
        if mode != "member_selector":
            continue
        values = list(constraint.get("values") or [])
        if constraint.get("operator") not in {"eq", "in"} or len(values) != 1:
            raise PreparationError(
                "CONSTRAINT_PATH_INVALID",
                "member_selector 仅支持单成员正向筛选",
                {"requirement_id": requirement_id, "input_role": role},
            )
        if source_dimension in dimensions and dimensions[source_dimension] != values[0]:
            raise PreparationError(
                "DIMENSION_CONFLICT",
                f"需求 {requirement_id} 的组合输入 {role!r} 存在维度冲突",
            )
        dimensions[source_dimension] = values[0]
    return {
        "metric_ref": source_metric_ref,
        "dimensions": dimensions,
        "dimension_refs": [],
        "fulfillment_mode": mode,
        "constraint_binding": deepcopy(binding),
    }


def _apply_requirement_bindings(ir: dict[str, Any], index: dict[str, Any]) -> None:
    """Materialize requirement-scoped source bindings without changing the core metric."""
    bindings = index.get("requirement_bindings") or {}
    if not bindings:
        return
    metrics = _metric_map(ir)
    requirements: dict[str, dict[str, Any]] = {}
    for collection, id_field in (
        ("fact_observations", "requirement_id"),
        ("metric_compositions", "requirement_id"),
        ("derived_requirements", "requirement_id"),
        ("attribution_targets", "target_id"),
    ):
        for item in ir.get(collection) or []:
            if isinstance(item, dict) and item.get(id_field):
                requirements[str(item[id_field])] = item
    source_catalogue = index.get("metrics") or {}
    materialized_source_refs: dict[tuple[str, str, str, str], str] = {}
    for requirement_id, binding in bindings.items():
        if not isinstance(binding, dict):
            continue
        requirement = requirements.get(str(requirement_id))
        if requirement is None:
            continue
        mode = str(binding.get("mode") or "")
        if mode == "unavailable":
            blocks = ir.setdefault("resolution_blocks", [])
            if not any(
                str(item.get("requirement_id") or "") == str(requirement_id)
                for item in blocks if isinstance(item, dict)
            ):
                blocks.append({
                    "requirement_id": str(requirement_id),
                    "criticality": requirement.get("criticality", "required"),
                    "reason_code": "SOURCE_REQUIREMENT_UNAVAILABLE",
                    "details": deepcopy(binding),
                })
            requirement.pop("resolution_intent", None)
            continue
        if mode == "registered_composition":
            logical_metric_ref = str(requirement.get("metric_ref") or "")
            logical_metric = metrics.get(logical_metric_ref)
            composition_id = str(binding.get("composition_id") or "")
            output_metric_ref = str(
                binding.get("output_metric_ref")
                or f"__resolution_{_stable_suffix((requirement_id, composition_id))}"
            )
            if logical_metric is None or not composition_id:
                continue
            if output_metric_ref not in metrics:
                output_metric = {
                    "metric_id": output_metric_ref,
                    "name": requirement.get("semantic_text") or logical_metric.get("name"),
                    "metric_object": "ratio",
                    "unit": "share",
                    "definition": "registered metric composition",
                    "composition_id": composition_id,
                    "generated_from": "requirement_binding",
                    "logical_metric_ref": logical_metric_ref,
                }
                ir["analysis_task"].setdefault("metrics", []).append(output_metric)
                metrics[output_metric_ref] = output_metric
            metrics[output_metric_ref]["composition_id"] = composition_id
            input_bindings = binding.get("input_bindings") or {}
            if not isinstance(input_bindings, dict):
                raise PreparationError(
                    "COMPOSITION_INPUT_BINDING_INVALID",
                    f"需求 {requirement_id} 的组合输入绑定必须是对象",
                )
            structured_bindings = {
                str(role): leaf
                for role, leaf in input_bindings.items()
                if isinstance(leaf, dict)
            }
            if structured_bindings:
                if len(structured_bindings) != len(input_bindings):
                    raise PreparationError(
                        "COMPOSITION_INPUT_BINDING_INVALID",
                        f"需求 {requirement_id} 的组合输入绑定格式不一致",
                    )
                requirement["composition_input_bindings"] = {
                    role: _materialize_composition_input_binding(
                        ir,
                        index,
                        metrics,
                        source_catalogue,
                        materialized_source_refs,
                        str(requirement_id),
                        output_metric_ref,
                        role,
                        leaf,
                    )
                    for role, leaf in structured_bindings.items()
                }
            requirement["metric_ref"] = output_metric_ref
            requirement["fulfillment_mode"] = mode
            requirement["constraint_binding"] = deepcopy(binding)
            requirement.pop("resolution_intent", None)
            continue
        if mode not in {
            "source_derived_fact",
            "source_derived_calculation",
            "source_scoped_fact",
            "member_selector",
            "additive_member_sum",
            "same_metric_total_minus_members",
            "source_dimension_all_sum",
        }:
            continue
        output_metric_ref = str(requirement.get("metric_ref") or "")
        output_metric = metrics.get(output_metric_ref)
        source_metric = str(binding.get("source_metric") or "")
        metadata = source_catalogue.get(source_metric) or {}
        if output_metric is None or not source_metric or not metadata:
            continue
        materialization_key = (
            output_metric_ref,
            source_metric,
            mode,
            str(binding.get("constraints_fingerprint") or requirement_id),
        )
        source_metric_ref = materialized_source_refs.get(materialization_key)
        if source_metric_ref is None:
            source_metric_ref = f"__source_requirement_{_stable_suffix(materialization_key)}"
            materialized_source_refs[materialization_key] = source_metric_ref
        logical_source_name = (
            source_metric
            if mode not in {"source_derived_fact", "source_derived_calculation"}
            else f"{output_metric.get('name')}::{requirement.get('derived_metric_id')}"
        )
        source_object = metadata.get("metric_object")
        if source_object not in {"volume", "ratio"}:
            source_object = "ratio" if str(metadata.get("unit") or "").lower() in {
                "%", "rate", "ratio", "share", "pp"
            } else "volume"
        if source_metric_ref not in metrics:
            source_declaration = {
                "metric_id": source_metric_ref,
                "name": logical_source_name,
                "metric_object": source_object,
                "unit": metadata.get("unit") or "待元信息解析",
                "definition": metadata.get("notes") or "source precomputed derived metric",
                "source_metric_name": source_metric,
                "source_metric_object": source_object,
                "source_dimension_bindings": deepcopy(
                    (index.get("metric_dimension_bindings") or {}).get(source_metric) or {}
                ),
                "aggregation": metadata.get("aggregation"),
                "additive": metadata.get("additive"),
                "generated_from": "requirement_binding",
                "logical_metric_ref": output_metric_ref,
                "logical_metric_name": output_metric.get("name"),
            }
            ir["analysis_task"].setdefault("metrics", []).append(source_declaration)
            metrics[source_metric_ref] = source_declaration
        index.setdefault("metric_bindings", {})[logical_source_name] = source_metric
        for constraint in binding.get("metric_constraints") or []:
            if not isinstance(constraint, dict) or not constraint.get("source_dimension"):
                continue
            source_dimension = str(constraint["source_dimension"])
            index.setdefault("dimension_bindings", {}).setdefault(
                source_dimension, source_dimension
            )
            index.setdefault("metric_dimension_bindings", {}).setdefault(
                source_metric, {}
            ).setdefault(source_dimension, source_dimension)
        if binding.get("source_dimension"):
            source_dimension = str(binding["source_dimension"])
            index.setdefault("dimension_bindings", {}).setdefault(
                source_dimension, source_dimension
            )
            index.setdefault("metric_dimension_bindings", {}).setdefault(
                source_metric, {}
            ).setdefault(source_dimension, source_dimension)
        requirement["fulfillment_mode"] = mode
        requirement["source_metric_ref"] = source_metric_ref
        requirement["constraint_binding"] = deepcopy(binding)
        requirement["source_binding_candidate_id"] = binding.get("candidate_id")
        requirement.pop("resolution_intent", None)
        if mode == "source_derived_fact":
            requirement["source_period_role"] = str(
                binding.get("source_period_role") or "analysis"
            )
        elif mode == "source_derived_calculation":
            requirement["metric_ref"] = source_metric_ref
            requirement["metric_object"] = source_object
            requirement["derived_metric_id"] = str(
                binding.get("execution_derived_metric_id") or "period_change"
            )
            requirement["required_period_roles"] = list(
                binding.get("source_period_roles") or ["analysis", "comparison"]
            )
            requirement["definition_status"] = "registered"
        elif mode in {"source_scoped_fact", "member_selector"}:
            if mode == "member_selector":
                dimensions = requirement.setdefault("dimensions", {})
                for constraint in binding.get("metric_constraints") or []:
                    values = list(constraint.get("values") or [])
                    if constraint.get("operator") not in {"eq", "in"} or len(values) != 1:
                        raise PreparationError(
                            "CONSTRAINT_PATH_INVALID",
                            "member_selector 仅支持单成员正向筛选",
                            {"requirement_id": requirement_id},
                        )
                    dimension = str(constraint.get("source_dimension") or "")
                    if dimension in dimensions and dimensions[dimension] != values[0]:
                        raise PreparationError(
                            "DIMENSION_CONFLICT",
                            f"需求 {requirement_id} 的约束与已有维度冲突",
                        )
                    dimensions[dimension] = values[0]
            requirement["metric_ref"] = source_metric_ref


def _bind_declared_metric_metadata(ir: dict[str, Any], index: dict[str, Any]) -> None:
    """Resolve source-backed metadata once before compilation and adaptation."""
    metrics = (ir.get("analysis_task") or {}).get("metrics") or []
    catalogue = index.get("metrics") or {}
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        source_name = _source_metric(str(metric.get("name") or ""), index)
        if source_name is None:
            continue
        metric["source_metric_name"] = source_name
        metric["source_dimension_bindings"] = deepcopy(
            (index.get("metric_dimension_bindings") or {}).get(source_name) or {}
        )
        metadata = catalogue.get(source_name) or {}
        source_object = metadata.get("metric_object")
        if source_object not in {"volume", "ratio"}:
            unit_token = str(metadata.get("unit") or "").strip().lower()
            source_object = (
                "ratio"
                if unit_token in {"%", "rate", "ratio", "share", "pp"}
                else "volume"
                if unit_token
                else None
            )
        declared_object = metric.get("metric_object")
        if source_object and declared_object and declared_object != source_object:
            object_source = str(
                metric.get("metric_object_source") or "model_inferred"
            )
            if object_source == "model_inferred":
                metric.setdefault("metadata_corrections", []).append({
                    "field": "metric_object",
                    "declared": declared_object,
                    "source": source_object,
                    "reason": "model_inferred_overridden_by_source_metadata",
                })
                metric["metric_object"] = source_object
                metric["metric_object_source"] = "source_metric_metadata"
            else:
                raise PreparationError(
                    "METRIC_METADATA_CONFLICT",
                    f"指标 {metric.get('name')} 的声明对象与源表元信息冲突",
                    {
                        "metric": metric.get("name"),
                        "field": "metric_object",
                        "declared": declared_object,
                        "source": source_object,
                        "provenance": object_source,
                    },
                )
        if source_object:
            metric["source_metric_object"] = source_object
        source_unit = metadata.get("unit")
        declared_unit = metric.get("unit")
        if not _is_unresolved_metadata(source_unit):
            if _is_unresolved_metadata(declared_unit):
                metric["unit"] = source_unit
                metric["unit_source"] = "source_metric_metadata"
            elif str(declared_unit).strip() != str(source_unit).strip():
                unit_source = str(metric.get("unit_source") or "user_explicit")
                if unit_source == "model_inferred":
                    metric.setdefault("metadata_corrections", []).append({
                        "field": "unit",
                        "declared": declared_unit,
                        "source": source_unit,
                        "reason": "model_inferred_overridden_by_source_metadata",
                    })
                    metric["unit"] = source_unit
                    metric["unit_source"] = "source_metric_metadata"
                else:
                    raise PreparationError(
                        "METRIC_METADATA_CONFLICT",
                        f"指标 {metric.get('name')} 的声明单位与源表元信息冲突",
                        {
                            "metric": metric.get("name"),
                            "field": "unit",
                            "declared": declared_unit,
                            "source": source_unit,
                            "provenance": unit_source,
                        },
                    )
        source_definition = metadata.get("notes")
        if _is_unresolved_metadata(metric.get("definition")) and source_definition:
            metric["definition"] = source_definition
            metric["definition_source"] = "source_metric_metadata"
        for field in ("aggregation", "additive"):
            if metric.get(field) is None and metadata.get(field) is not None:
                metric[field] = metadata[field]


def _expected_dimension(
    dimensions: dict[str, Any], dimension_refs: list[str], index: dict[str, Any],
    source_metric: str | None = None,
) -> str | None:
    names = {str(value) for value in dimension_refs} | {
        str(value) for value in dimensions
    }
    if not names:
        return "无"
    resolved = {_source_dimension(name, index, source_metric) for name in names}
    resolved.discard(None)
    if len(resolved) != 1:
        return None
    return next(iter(resolved))


def _direct_available(
    index: dict[str, Any],
    metric_name: str,
    period: str,
    dimensions: dict[str, Any],
    dimension_refs: list[str],
) -> bool:
    source_metric = _source_metric(metric_name, index)
    if source_metric is None:
        return False
    expected_dimension = _expected_dimension(
        dimensions, dimension_refs, index, source_metric
    )
    if expected_dimension is None or evaluate_direct_capability(
        index, source_metric, period, expected_dimension
    )["status"] != "available":
        return False
    parsed = normalize_period(period)
    if parsed is None:
        return False
    grain, _ = parsed
    member_coverage = (
        (((index.get("availability") or {}).get(grain) or {}).get("metrics") or {})
        .get(source_metric, {})
        .get("members")
    )
    if member_coverage is None or expected_dimension == "无":
        return True
    requested_members: set[str] = set()
    value = dimensions.get(expected_dimension)
    if isinstance(value, list):
        requested_members.update(str(item) for item in value)
    elif value is not None:
        requested_members.add(str(value))
    return requested_members.issubset({str(item) for item in member_coverage})


def _direct_capability(
    index: dict[str, Any],
    metric_name: str,
    period: str,
    dimensions: dict[str, Any],
    dimension_refs: list[str],
) -> dict[str, Any]:
    source_metric = _source_metric(metric_name, index)
    if source_metric is None:
        return {"status": "blocked", "reason": "metric_unresolved"}
    expected_dimension = _expected_dimension(
        dimensions, dimension_refs, index, source_metric
    )
    if expected_dimension is None:
        return {"status": "blocked", "reason": "dimension_ambiguous"}
    result = evaluate_direct_capability(
        index, source_metric, period, expected_dimension
    )
    result.update({
        "source_metric_name": source_metric,
        "source_dimension": expected_dimension,
    })
    return result


def _composition_for_metric(
    metric: dict[str, Any], composition_registry: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    requested = normalize_match_text(metric.get("name"))
    explicit = metric.get("composition_id")
    definitions = composition_registry.get("definitions") or {}
    if explicit in definitions:
        return str(explicit), definitions[str(explicit)]
    for composition_id, definition in definitions.items():
        triggers = definition.get("trigger_phrases") or []
        if any(normalize_match_text(value) == requested for value in triggers):
            return str(composition_id), definition
    return None


def _composition_resolution(
    capabilities: dict[str, Any], metric_ref: str, composition_id: str
) -> dict[str, Any] | None:
    for item in capabilities.get("composition_resolutions") or []:
        if not isinstance(item, dict):
            continue
        if (
            str(item.get("metric_ref")) == str(metric_ref)
            and str(item.get("composition_id")) == str(composition_id)
        ):
            return item
    return None


def _activate_composition_cases(
    resolution: dict[str, Any] | None,
    requirement: dict[str, Any],
) -> list[dict[str, Any]]:
    if not resolution or resolution.get("fallback_status") == "ready":
        return []
    active: list[dict[str, Any]] = []
    for deferred in resolution.get("deferred_cases") or []:
        if not isinstance(deferred, dict):
            continue
        case = deepcopy(deferred)
        case["activation"] = "active"
        case["requirement_id"] = requirement.get("requirement_id") or requirement.get("target_id")
        case["criticality"] = requirement.get("criticality", "required")
        case["period_roles"] = list(
            requirement.get("period_roles")
            or requirement.get("required_period_roles")
            or []
        )
        active.append(case)
    return active


def _ensure_metric(
    ir: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    name: str,
    index: dict[str, Any],
) -> str:
    requested = normalize_match_text(name)
    for metric_ref, metric in metrics.items():
        if normalize_match_text(metric.get("name")) == requested:
            return metric_ref
    source_name = _source_metric(name, index) or name
    metadata = (index.get("metrics") or {}).get(source_name) or {}
    metric_ref = f"auto_metric_{_stable_suffix(source_name)}"
    metric = {
        "metric_id": metric_ref,
        "name": source_name,
        "metric_object": "volume",
        "unit": metadata.get("unit") or "待元信息解析",
        "definition": metadata.get("notes") or "待元信息解析",
        "generated_from": "metric_composition_registry",
        "source_metric_name": source_name,
        "source_dimension_bindings": deepcopy(
            (index.get("metric_dimension_bindings") or {}).get(source_name) or {}
        ),
    }
    ir["analysis_task"].setdefault("metrics", []).append(metric)
    metrics[metric_ref] = metric
    return metric_ref


def _composition_input_refs(
    ir: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    definition: dict[str, Any],
    index: dict[str, Any],
) -> list[str]:
    refs: list[str] = []
    for item in definition.get("inputs") or []:
        if not isinstance(item, dict):
            continue
        metric_ref = item.get("metric_ref")
        if metric_ref is None:
            metric_ref = _ensure_metric(ir, metrics, str(item.get("metric") or ""), index)
        refs.append(str(metric_ref))
    return refs


def _child_period_candidates(period: str) -> list[list[str]]:
    """Compatibility view of candidate paths without rollup metadata."""
    return [
        [str(item["period"]) for item in path]
        for path in _aggregation_candidate_paths(period)
    ]


def _aggregation_candidate_paths(period: str) -> list[list[dict[str, Any]]]:
    parsed = normalize_period(period)
    if parsed is None:
        return []
    grain, canonical = parsed
    year = int(canonical[:4])
    if grain == "month":
        return [iso_weeks_covering(canonical)]
    if grain == "quarter":
        quarter = int(canonical[-1])
        start = (quarter - 1) * 3 + 1
        return [
            [
                {"period": f"{year:04d}-{month:02d}", "overlap_days": None, "weight": 1.0}
                for month in range(start, start + 3)
            ],
            iso_weeks_covering(canonical),
        ]
    if grain == "year":
        return [
            [
                {"period": f"{year:04d}-Q{quarter}", "overlap_days": None, "weight": 1.0}
                for quarter in range(1, 5)
            ],
            [
                {"period": f"{year:04d}-{month:02d}", "overlap_days": None, "weight": 1.0}
                for month in range(1, 13)
            ],
            iso_weeks_covering(canonical),
        ]
    return []


def _intern_physical_period_role(periods: dict[str, str], period: str) -> str:
    """Return one stable runtime role for each canonical physical period."""
    canonical = _canonical_period(period, "aggregate source period")
    for role, value in periods.items():
        if value == canonical:
            return str(role)
    base = "__fact_" + re.sub(r"[^0-9A-Za-z]+", "_", canonical).strip("_")
    role = base
    if role in periods and periods[role] != canonical:
        role = f"{base}_{_stable_suffix(canonical)}"
    periods[role] = canonical
    return role


def _aggregation_children(
    index: dict[str, Any],
    metric_name: str,
    target_period: str,
    dimensions: dict[str, Any],
    dimension_refs: list[str],
) -> list[str] | None:
    components = _aggregation_components(
        index, metric_name, target_period, dimensions, dimension_refs
    )
    return [str(item["period"]) for item in components] if components else None


def _aggregation_components(
    index: dict[str, Any],
    metric_name: str,
    target_period: str,
    dimensions: dict[str, Any],
    dimension_refs: list[str],
) -> list[dict[str, Any]] | None:
    source_metric = _source_metric(metric_name, index)
    if source_metric is None:
        return None
    metadata = (index.get("metrics") or {}).get(source_metric) or {}
    if not metric_aggregation_eligibility(metadata)["allowed"]:
        return None
    for components in _aggregation_candidate_paths(target_period):
        if all(
            _direct_available(index, metric_name, str(item["period"]), dimensions, dimension_refs)
            for item in components
        ):
            return components
    return None


def _requirement_roles(
    ir: dict[str, Any],
    derived_registry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return leaf metric consumers that may require a source-grain adaptation."""
    metrics = _metric_map(ir)
    blocked = {
        str(item.get("requirement_id"))
        for item in ir.get("resolution_blocks") or []
        if isinstance(item, dict) and item.get("requirement_id")
    }
    consumers: list[dict[str, Any]] = []
    for requirement in ir.get("fact_observations") or []:
        if str(requirement.get("requirement_id")) in blocked:
            continue
        for role in requirement.get("period_roles") or []:
            consumers.append({**requirement, "role": role})
    for requirement in ir.get("derived_requirements") or []:
        if str(requirement.get("requirement_id")) in blocked:
            continue
        if requirement.get("fulfillment_mode") == "source_derived_fact":
            consumers.append({
                **requirement,
                "metric_ref": requirement.get("source_metric_ref"),
                "role": requirement.get("source_period_role", "analysis"),
            })
            continue
        definition = (derived_registry.get("definitions") or {}).get(
            requirement.get("derived_metric_id")
        ) or {}
        roles = requirement.get("required_period_roles") or definition.get(
            "required_period_roles"
        ) or []
        for role in roles:
            consumers.append({**requirement, "role": role})
    for target in ir.get("attribution_targets") or []:
        if str(target.get("target_id")) in blocked:
            continue
        roles = list((target.get("periods") or {}).keys())
        if not roles:
            roles = (
                ["analysis", "comparison"]
                if target.get("scenario") == "metric_change"
                else ["analysis", "analysis_last_year", "comparison", "comparison_last_year"]
                if target.get("scenario") == "yoy_trend_change"
                else []
            )
        target_dimensions = target.get("dimensions") or {}
        target_periods = target.get("periods") or {}
        group_dimensions = target.get("group_dimensions") or []
        include_overall = (
            not group_dimensions
            or target.get("partial_coverage") is True
            or target.get("include_overall_metric") is True
            or (
                isinstance(target.get("coverage"), dict)
                and (
                    target["coverage"].get("mode") == "auto_residual"
                    or isinstance(target["coverage"].get("parent_selector"), dict)
                )
            )
        )
        for role in roles:
            grouped_consumer = {
                "requirement_id": target.get("target_id"),
                "metric_ref": target.get("metric_ref", target.get("metric")),
                "view_id": target.get("view_id"),
                "dimensions": target_dimensions,
                "dimension_refs": group_dimensions,
                "criticality": target.get("criticality", "required"),
                "role": role,
                "period": target_periods.get(role),
            }
            consumers.append(grouped_consumer)
            if group_dimensions and include_overall:
                consumers.append({**grouped_consumer, "dimension_refs": []})
        for factor in target.get("factors") or []:
            if not isinstance(factor, dict):
                continue
            factor_path = f"attribution target {target.get('target_id')}.factor {factor.get('factor_id')}"
            factor_dimensions = _merge_dimensions(
                target_dimensions,
                factor.get("dimensions"),
                factor_path,
            )
            factor_kind = factor.get("kind") or (
                "metric" if factor.get("metric_ref") is not None else None
            )
            if factor_kind == "metric":
                for role in roles:
                    consumers.append({
                        "requirement_id": target.get("target_id"),
                        "metric_ref": factor.get("metric_ref"),
                        "view_id": target.get("view_id"),
                        "dimensions": deepcopy(factor_dimensions),
                        "dimension_refs": list(factor_dimensions),
                        "criticality": target.get("criticality", "required"),
                        "role": role,
                        "period": target_periods.get(role),
                    })

            def visit_expression(expression: Any) -> None:
                if not isinstance(expression, dict):
                    return
                fact = expression.get("fact")
                if isinstance(fact, dict) and fact.get("metric_ref") and fact.get("period_role"):
                    expression_dimension_refs = fact.get("dimension_refs", [])
                    if not isinstance(expression_dimension_refs, list):
                        raise PreparationError(
                            "DIMENSION_CONFLICT",
                            f"{factor_path}.expression.fact.dimension_refs 必须是数组",
                        )
                    expression_dimensions = _merge_dimensions(
                        factor_dimensions,
                        fact.get("dimensions"),
                        f"{factor_path}.expression.fact",
                    )
                    consumers.append({
                        "requirement_id": target.get("target_id"),
                        "metric_ref": fact.get("metric_ref"),
                        "view_id": target.get("view_id"),
                        "dimensions": deepcopy(expression_dimensions),
                        "dimension_refs": sorted({
                            *expression_dimensions,
                            *(str(item) for item in expression_dimension_refs if isinstance(item, str)),
                        }),
                        "criticality": target.get("criticality", "required"),
                        "role": fact.get("period_role"),
                    })
                for arg in expression.get("args") or []:
                    visit_expression(arg)

            expressions = factor.get("expressions_by_period_role")
            if isinstance(expressions, dict):
                for expression in expressions.values():
                    visit_expression(expression)
    return [item for item in consumers if item.get("metric_ref") in metrics]


def _materialize_set_adaptations(
    ir: dict[str, Any], index: dict[str, Any], derived_registry: dict[str, Any]
) -> None:
    """Turn proven same-metric member sets into safe requirement-local ASTs."""
    periods = (ir.get("analysis_task") or {}).get("periods") or {}
    adaptations = ir.setdefault("input_adaptations", [])

    for collection in ("fact_observations", "derived_requirements"):
        for requirement in ir.get(collection) or []:
            if not isinstance(requirement, dict) or requirement.get("fulfillment_mode") not in {
                "additive_member_sum", "same_metric_total_minus_members",
                "source_dimension_all_sum",
            }:
                continue
            binding = requirement.get("constraint_binding") or {}
            constraints = [
                item for item in binding.get("metric_constraints") or []
                if isinstance(item, dict)
            ]
            source_metric = str(binding.get("source_metric") or "")
            source_metric_ref = str(requirement.get("source_metric_ref") or "")
            metadata = (index.get("metrics") or {}).get(source_metric) or {}
            if not metric_aggregation_eligibility(metadata)["allowed"]:
                raise PreparationError(
                    "CONSTRAINT_PATH_UNAVAILABLE",
                    f"指标 {source_metric} 不可按维度成员聚合",
                    {"reason": "metric_non_additive"},
                )
            try:
                if requirement.get("fulfillment_mode") == "source_dimension_all_sum":
                    source_dimension = str(binding.get("source_dimension") or "")
                    set_spec = materialize_source_domain_set_spec(
                        source_dimension, index, intent="total"
                    )
                else:
                    set_spec = materialize_set_spec(constraints, index, intent="total")
            except SetMaterializationError as exc:
                raise PreparationError(
                    "CONSTRAINT_PATH_UNAVAILABLE", str(exc), exc.details
                ) from exc
            source_dimension = str(set_spec["dimension_ref"])
            roles = list(requirement.get("period_roles") or [])
            if collection == "derived_requirements":
                definition = (derived_registry.get("definitions") or {}).get(
                    requirement.get("derived_metric_id")
                ) or {}
                roles = list(
                    requirement.get("required_period_roles")
                    or definition.get("required_period_roles") or []
                )
            for role in roles:
                if role not in periods:
                    continue
                needed_members = (
                    set(set_spec["domain_members"])
                    if requirement.get("fulfillment_mode")
                    == "same_metric_total_minus_members"
                    and not set_spec.get("has_positive_filter")
                    else set(set_spec["members"])
                )
                unavailable = [
                    member
                    for member in sorted(needed_members)
                    if not _direct_available(
                        index, source_metric, periods[role],
                        {source_dimension: member}, []
                    )
                ]
                if unavailable:
                    raise PreparationError(
                        "CONSTRAINT_PATH_UNAVAILABLE",
                        f"指标 {source_metric} 在 {periods[role]} 缺少成员事实",
                        {"members": unavailable},
                    )
                expression = set_aggregate_expression(
                    set_spec, source_metric_ref, role,
                    str(requirement.get("fulfillment_mode") or ""),
                )
                adaptation_id = "constraint_adapt_" + _stable_suffix((
                    requirement.get("requirement_id"), role,
                    binding.get("constraints_fingerprint")
                    or set_spec.get("set_fingerprint"),
                ))
                if any(
                    isinstance(item, dict)
                    and item.get("requirement_id") == adaptation_id
                    for item in adaptations
                ):
                    continue
                adaptations.append({
                    "requirement_id": adaptation_id,
                    "metric_ref": requirement.get("metric_ref"),
                    "target_period_role": role,
                    "view_id": requirement.get("view_id"),
                    "dimensions": deepcopy(requirement.get("dimensions") or {}),
                    "dimension_refs": list(requirement.get("dimension_refs") or []),
                    "expression": expression,
                    "rule_source": "source_metric_metadata",
                    "validation": ["facts_present", "unit_consistent", "metric_additive"],
                    "criticality": requirement.get("criticality", "required"),
                    "generated": True,
                    "constraint_fulfillment": requirement.get("fulfillment_mode"),
                    "set_fulfillment": requirement.get("fulfillment_mode"),
                    "set_spec": deepcopy(set_spec),
                })


def _formula_fallback_expression(
    expression: Any,
    factors: dict[str, dict[str, Any]],
    role: str,
) -> dict[str, Any]:
    """Expand attribution factor references into a materializable fact expression."""
    if not isinstance(expression, dict):
        raise PreparationError("FORMULA_SHAPE_INVALID", "attribution formula must be an object")
    if "literal" in expression:
        return {"literal": expression["literal"]}
    if "factor_ref" in expression:
        factor = factors.get(str(expression["factor_ref"]))
        if factor is None:
            raise PreparationError("FORMULA_SHAPE_INVALID", "formula references an unknown factor")
        kind = factor.get("kind")
        if kind == "metric":
            fact = {"metric_ref": factor.get("metric_ref"), "period_role": role}
            if factor.get("dimensions"):
                fact["dimensions"] = deepcopy(factor["dimensions"])
            return {"fact": fact}
        if kind == "literal":
            values = factor.get("values_by_period_role") or {}
            if role not in values:
                raise PreparationError("FORMULA_SHAPE_INVALID", f"literal factor has no value for {role}")
            return {"literal": values[role]}
        expressions = factor.get("expressions_by_period_role") or {}
        if role not in expressions:
            raise PreparationError("FORMULA_SHAPE_INVALID", f"derived factor has no expression for {role}")
        return _formula_fallback_expression(expressions[role], factors, role)
    op = expression.get("op")
    args = expression.get("args")
    if op not in {"add", "subtract", "multiply", "divide", "negate"} or not isinstance(args, list):
        raise PreparationError("FORMULA_SHAPE_INVALID", "unsupported formula expression")
    return {"op": op, "args": [_formula_fallback_expression(arg, factors, role) for arg in args]}


def _formula_unit_conversion(
    expression: dict[str, Any],
    factors: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    target_unit: Any,
) -> dict[str, Any]:
    expected: dict[str, str] = {}

    def factor_unit(factor_ref: str) -> str | None:
        factor = factors.get(factor_ref)
        if factor is None:
            raise UnitScaleError(f"unknown formula factor: {factor_ref!r}")
        kind = factor.get("kind") or (
            "metric" if factor.get("metric_ref") is not None else None
        )
        if kind == "literal":
            return "1"
        if kind != "metric":
            raise UnitScaleError(
                f"formula fallback does not support {kind or 'unknown'} factor {factor_ref!r}"
            )
        metric_ref = str(factor.get("metric_ref") or "")
        metric = metrics.get(metric_ref) or {}
        unit = metric.get("unit")
        if not metric_ref or _is_unresolved_metadata(unit):
            raise UnitScaleError(f"formula factor {factor_ref!r} has no resolved unit")
        expected[metric_ref] = str(unit)
        return str(unit)

    expression_scale = formula_scale(expression, factor_unit)
    return {
        "expected_input_units": [
            {"metric_ref": metric_ref, "unit": unit}
            for metric_ref, unit in sorted(expected.items())
        ],
        "target_unit": str(target_unit),
        "scale_factor": conversion_factor(expression_scale, target_unit),
    }


def _prepare_formula_target_fallbacks(
    prepared: dict[str, Any],
    index: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
) -> set[tuple[str, str]]:
    """Materialize a target from the user formula only when its target fact is absent."""
    periods = (prepared.get("analysis_task") or {}).get("periods") or {}
    fallback_targets: set[tuple[str, str]] = set()
    adaptations = prepared.setdefault("input_adaptations", [])
    existing = {str(item.get("requirement_id")) for item in adaptations if isinstance(item, dict)}
    for target in prepared.get("attribution_targets") or []:
        if not isinstance(target, dict) or not target.get("factors") or not target.get("formula"):
            continue
        target_id = str(target.get("target_id") or "")
        metric = metrics.get(str(target.get("metric_ref"))) or {}
        scenario_roles = {
            "metric_change": ["analysis", "comparison"],
            "yoy_trend_change": ["analysis", "analysis_last_year", "comparison", "comparison_last_year"],
        }
        roles = list((target.get("periods") or {}).keys()) or list(scenario_roles.get(str(target.get("scenario")), []))
        if not target_id or not metric or not roles:
            continue
        target_periods = target.get("periods") or periods
        dimensions = target.get("dimensions") or {}
        dimension_refs = target.get("group_dimensions") or []
        target_available = all(
            role in target_periods and _direct_available(
                index, str(metric.get("name") or ""), target_periods[role],
                dimensions, dimension_refs,
            )
            for role in roles
        )
        if target_available:
            continue
        target_aggregatable = all(
            role in target_periods and _aggregation_children(
                index, str(metric.get("name") or ""), target_periods[role], dimensions, dimension_refs
            ) is not None
            for role in roles
        )
        if target_aggregatable:
            continue
        factors = {str(item.get("factor_id")): item for item in target.get("factors") if isinstance(item, dict)}
        try:
            unit_conversion = _formula_unit_conversion(
                target["formula"], factors, metrics, metric.get("unit")
            )
        except UnitScaleError:
            # An unprovable unit conversion is not an executable fallback path.
            continue
        eligible_roles: list[str] = []
        role_expressions: dict[str, dict[str, Any]] = {}
        for role in roles:
            if role not in target_periods:
                continue
            source_role = _intern_physical_period_role(periods, target_periods[role])
            factor_ok = True
            for factor in factors.values():
                if factor.get("kind") != "metric":
                    continue
                factor_metric = metrics.get(str(factor.get("metric_ref"))) or {}
                factor_dimensions = _merge_dimensions(dimensions, factor.get("dimensions"), f"attribution target {target_id}")
                factor_direct = factor_metric and _direct_available(
                    index, str(factor_metric.get("name") or ""), target_periods[role],
                    factor_dimensions, list(factor_dimensions)
                )
                if not factor_direct:
                    factor_ok = False
                    break
            if not factor_ok:
                break
            eligible_roles.append(role)
            role_expressions[role] = _formula_fallback_expression(
                target["formula"], factors, source_role
            )
        if len(eligible_roles) != len(roles):
            continue
        for role in eligible_roles:
            adaptation_id = f"formula_target_{_stable_suffix([target_id, role])}"
            if adaptation_id not in existing:
                adaptations.append({
                    "requirement_id": adaptation_id,
                    "metric_ref": target.get("metric_ref"),
                    "target_period_role": role,
                    "target_period": target_periods[role],
                    "view_id": target.get("view_id"),
                    "dimensions": deepcopy(dimensions),
                    "dimension_refs": list(dimension_refs),
                    "expression": role_expressions[role],
                    "rule_source": "user_query_formula",
                    "validation": ["facts_present", "unit_scale_verified"],
                    "unit_conversion": deepcopy(unit_conversion),
                    "criticality": target.get("criticality", "required"),
                    "generated": True,
                })
                existing.add(adaptation_id)
            fallback_targets.add((target_id, role))
        if all((target_id, role) in fallback_targets for role in roles):
            target["target_fact_source"] = "formula_computed"
    return fallback_targets


def prepare_analysis_ir(
    ir: dict[str, Any],
    index: dict[str, Any],
    composition_registry: dict[str, Any],
    derived_registry: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_capabilities(index)
    raw_periods = (ir.get("analysis_task") or {}).get("periods") or {}
    reserved_roles = sorted(
        str(role) for role in raw_periods if str(role).startswith("__fact_")
    ) if isinstance(raw_periods, dict) else []
    if reserved_roles:
        raise PreparationError(
            "RESERVED_PERIOD_ROLE",
            f"analysis_task.periods uses reserved internal roles: {reserved_roles}",
        )
    try:
        prepared = normalize_analysis_ir(ir)
        validate_analysis_ir_contract(prepared)
        prepared = apply_task_selector_context(prepared)
    except (IRContractError, AnalysisIRNormalizationError) as exc:
        raise PreparationError(exc.code, str(exc), exc.details) from exc
    except SelectorContextError as exc:
        raise PreparationError("SELECTOR_CONTEXT_INVALID", str(exc)) from exc
    _apply_business_intent_selection(prepared, index)
    _apply_requirement_bindings(prepared, index)
    unresolved_intents = [
        str(item.get("requirement_id") or item.get("target_id") or "")
        for collection in (
            "fact_observations", "metric_compositions", "derived_requirements",
            "attribution_targets",
        )
        for item in prepared.get(collection) or []
        if isinstance(item, dict) and item.get("resolution_intent") is not None
    ]
    if unresolved_intents:
        raise PreparationError(
            "RESOLUTION_INTENT_UNRESOLVED",
            "path-neutral resolution intents were not materialized",
            {"requirement_ids": sorted(unresolved_intents)},
        )
    _bind_declared_metric_metadata(prepared, index)
    _materialize_set_adaptations(prepared, index, derived_registry)
    task = prepared.get("analysis_task") or {}
    metrics = _metric_map(prepared)
    periods = task.get("periods") or {}
    decisions: list[dict[str, Any]] = []
    active_resolution_cases: list[dict[str, Any]] = []

    # A declared composition is a fallback. Replace it with a direct fact when the
    # source has the requested metric at every requested period.
    remaining_compositions: list[dict[str, Any]] = []
    for requirement in prepared.get("metric_compositions") or []:
        metric = metrics.get(str(requirement.get("metric_ref"))) or {}
        roles = requirement.get("period_roles") or []
        dimensions = requirement.get("dimensions") or {}
        dimension_refs = requirement.get("dimension_refs") or []
        if roles and all(
            role in periods and _direct_available(
                index, str(metric.get("name") or ""), periods[role], dimensions, dimension_refs
            )
            for role in roles
        ):
            direct = deepcopy(requirement)
            direct.pop("composition_id", None)
            prepared.setdefault("fact_observations", []).append(direct)
            decisions.append({
                "requirement_id": requirement.get("requirement_id"),
                "mode": "direct",
                "metric": metric.get("name"),
            })
            continue
        composition_id = requirement.get("composition_id") or metric.get("composition_id")
        definition = (composition_registry.get("definitions") or {}).get(composition_id) or {}
        resolution = _composition_resolution(
            index, str(requirement.get("metric_ref") or ""), str(composition_id or "")
        )
        activated = _activate_composition_cases(resolution, requirement)
        if activated:
            active_resolution_cases.extend(activated)
            remaining_compositions.append(requirement)
            continue
        _composition_input_refs(prepared, metrics, definition, index)
        remaining_compositions.append(requirement)
        decisions.append({
            "requirement_id": requirement.get("requirement_id"),
            "mode": "derived",
            "composition_id": composition_id,
        })
    prepared["metric_compositions"] = remaining_compositions

    # A simple fact request for a metric absent from the source can use a registered
    # composition without requiring the model to restate its inputs.
    remaining_facts: list[dict[str, Any]] = []
    generated_compositions: list[dict[str, Any]] = []
    for requirement in prepared.get("fact_observations") or []:
        metric = metrics.get(str(requirement.get("metric_ref"))) or {}
        roles = requirement.get("period_roles") or []
        dimensions = requirement.get("dimensions") or {}
        dimension_refs = requirement.get("dimension_refs") or []
        direct = roles and all(
            role in periods and _direct_available(
                index, str(metric.get("name") or ""), periods[role], dimensions, dimension_refs
            )
            for role in roles
        )
        composition = _composition_for_metric(metric, composition_registry)
        if not direct and composition and len(roles) == 1:
            composition_id, definition = composition
            resolution = _composition_resolution(
                index, str(requirement.get("metric_ref") or ""), composition_id
            )
            activated = _activate_composition_cases(resolution, requirement)
            if activated:
                active_resolution_cases.extend(activated)
                remaining_facts.append(requirement)
                continue
            _composition_input_refs(prepared, metrics, definition, index)
            generated = deepcopy(requirement)
            generated["composition_id"] = composition_id
            generated_compositions.append(generated)
            decisions.append({
                "requirement_id": requirement.get("requirement_id"),
                "mode": "derived",
                "composition_id": composition_id,
            })
        else:
            remaining_facts.append(requirement)
    prepared["fact_observations"] = remaining_facts
    prepared["metric_compositions"].extend(generated_compositions)

    capability_plan: list[dict[str, Any]] = []
    canonical_selectors: list[dict[str, Any]] = []
    planned_identities: set[tuple[Any, ...]] = set()
    formula_fallback_targets = _prepare_formula_target_fallbacks(
        prepared, index, metrics
    )

    # Derived metrics over a composed business metric reuse the same automatic inputs.
    for requirement in prepared.get("derived_requirements") or []:
        metric = metrics.get(str(requirement.get("metric_ref"))) or {}
        composition = _composition_for_metric(metric, composition_registry)
        if not composition:
            continue
        definition = (derived_registry.get("definitions") or {}).get(
            requirement.get("derived_metric_id")
        ) or {}
        roles = requirement.get("required_period_roles") or definition.get(
            "required_period_roles"
        ) or []
        dimensions = requirement.get("dimensions") or {}
        dimension_refs = requirement.get("dimension_refs") or []
        if not roles or not all(
            role in periods and _direct_available(
                index, str(metric.get("name") or ""), periods[role], dimensions, dimension_refs
            )
            for role in roles
        ):
            composition_id, composition_definition = composition
            resolution = _composition_resolution(
                index, str(requirement.get("metric_ref") or ""), composition_id
            )
            activated = _activate_composition_cases(resolution, requirement)
            if activated:
                active_resolution_cases.extend(activated)
                continue
            metric["composition_id"] = composition_id
            _composition_input_refs(
                prepared, metrics, composition_definition, index
            )

    if active_resolution_cases:
        core_cases = [
            case for case in active_resolution_cases
            if case.get("criticality") == "core"
        ]
        if core_cases:
            prepared["resolution_cases"] = core_cases
            decisions.extend({"mode": "resolution_case", "case": case} for case in core_cases)
            return prepared, decisions
        blocks_by_requirement: dict[str, dict[str, Any]] = {}
        for case in active_resolution_cases:
            requirement_id = str(case.get("requirement_id") or "")
            if not requirement_id:
                continue
            block = blocks_by_requirement.setdefault(requirement_id, {
                "requirement_id": requirement_id,
                "criticality": case.get("criticality", "required"),
                "reason_code": "SOURCE_RESOLUTION_REQUIRED",
                "resolution_cases": [],
            })
            block["resolution_cases"].append(case)
        prepared["resolution_blocks"] = list(blocks_by_requirement.values())
        decisions.extend(
            {"mode": "resolution_block", "block": block}
            for block in prepared["resolution_blocks"]
        )

    existing_targets = {
        (
            str(item.get("metric_ref")),
            str(item.get("target_period_role")),
            str(
                item.get("target_period")
                or periods.get(str(item.get("target_period_role")))
                or ""
            ),
            str(item.get("view_id")),
            json.dumps(item.get("dimensions") or {}, ensure_ascii=False, sort_keys=True),
            tuple(sorted(item.get("dimension_refs") or [])),
        )
        for item in prepared.get("input_adaptations") or []
    }
    existing_relaxed_targets = {
        (
            str(item.get("metric_ref")),
            str(item.get("target_period_role")),
            str(
                item.get("target_period")
                or periods.get(str(item.get("target_period_role")))
                or ""
            ),
            str(item.get("view_id")),
            tuple(sorted(item.get("dimension_refs") or [])),
        )
        for item in prepared.get("input_adaptations") or []
    }
    constraint_adaptation_paths = {
        (
            str(item.get("metric_ref")),
            str(item.get("target_period_role")),
            str(
                item.get("target_period")
                or periods.get(str(item.get("target_period_role")))
                or ""
            ),
            str(item.get("view_id")),
            json.dumps(item.get("dimensions") or {}, ensure_ascii=False, sort_keys=True),
            tuple(sorted(item.get("dimension_refs") or [])),
        ): str(item.get("constraint_fulfillment"))
        for item in prepared.get("input_adaptations") or []
        if item.get("constraint_fulfillment")
    }
    adaptations = list(prepared.get("input_adaptations") or [])

    # Composition inputs are leaf source consumers too.
    consumers = _requirement_roles(prepared, derived_registry)
    for requirement in prepared.get("metric_compositions") or []:
        if any(
            str(item.get("requirement_id")) == str(requirement.get("requirement_id"))
            for item in prepared.get("resolution_blocks") or []
            if isinstance(item, dict)
        ):
            continue
        metric = metrics.get(str(requirement.get("metric_ref"))) or {}
        composition_id = requirement.get("composition_id") or metric.get("composition_id")
        definition = (composition_registry.get("definitions") or {}).get(composition_id) or {}
        prepared_inputs = requirement.get("composition_input_bindings")
        if isinstance(prepared_inputs, dict) and prepared_inputs:
            input_consumers = [
                {
                    "metric_ref": str(binding.get("metric_ref") or ""),
                    "dimensions": deepcopy(binding.get("dimensions") or {}),
                    "dimension_refs": list(binding.get("dimension_refs") or []),
                }
                for binding in prepared_inputs.values()
                if isinstance(binding, dict) and binding.get("metric_ref")
            ]
        else:
            input_consumers = [
                {"metric_ref": input_ref}
                for input_ref in _composition_input_refs(
                    prepared, metrics, definition, index
                )
            ]
        for input_consumer in input_consumers:
            for role in requirement.get("period_roles") or []:
                consumers.append({
                    **requirement,
                    **input_consumer,
                    "role": role,
                })

    for consumer in consumers:
        metric_ref = str(consumer.get("metric_ref"))
        metric = metrics.get(metric_ref)
        role = str(consumer.get("role"))
        actual_period = consumer.get("period")
        if actual_period is None:
            actual_period = periods.get(role)
        if metric is None or not isinstance(actual_period, str) or not actual_period:
            continue
        if (str(consumer.get("requirement_id")), role) in formula_fallback_targets:
            capability_plan_item = {
                "requirement_id": consumer.get("requirement_id"),
                "metric_ref": metric_ref,
                "source_metric_name": metric.get("source_metric_name") or metric.get("name"),
                "period_role": role,
                "period": actual_period,
                "grain": normalize_period(actual_period)[0] if normalize_period(actual_period) else None,
                "selector_dimensions": deepcopy(consumer.get("dimensions") or {}),
                "group_dimensions": list(consumer.get("group_dimensions") or []),
                "path": "formula_computed",
                "direct": {"status": "unavailable", "reason": "target_fact_unavailable"},
            }
            capability_plan.append(capability_plan_item)
            continue
        # Composed metrics are resolved through their leaf inputs, not aggregated as ratios.
        if metric.get("composition_id"):
            composition = (composition_registry.get("definitions") or {}).get(
                metric["composition_id"]
            ) or {}
            prepared_inputs = consumer.get("composition_input_bindings")
            if isinstance(prepared_inputs, dict) and prepared_inputs:
                for binding in prepared_inputs.values():
                    if not isinstance(binding, dict) or not binding.get("metric_ref"):
                        continue
                    consumers.append({
                        **consumer,
                        "metric_ref": str(binding["metric_ref"]),
                        "dimensions": deepcopy(binding.get("dimensions") or {}),
                        "dimension_refs": list(binding.get("dimension_refs") or []),
                    })
            else:
                for input_ref in _composition_input_refs(
                    prepared, metrics, composition, index
                ):
                    consumers.append({**consumer, "metric_ref": input_ref})
            continue
        dimensions = consumer.get("dimensions") or {}
        dimension_refs = sorted({
            *(str(item) for item in consumer.get("dimension_refs") or []),
            *(str(item) for item in dimensions),
        })
        identity = (
            metric_ref,
            role,
            actual_period,
            str(consumer.get("view_id")),
            json.dumps(dimensions, ensure_ascii=False, sort_keys=True),
            tuple(sorted(dimension_refs)),
        )
        relaxed_identity = (
            metric_ref,
            role,
            actual_period,
            str(consumer.get("view_id")),
            tuple(sorted(dimension_refs)),
        )
        direct_capability = _direct_capability(
            index, metric["name"], actual_period, dimensions, dimension_refs
        )
        if identity not in planned_identities:
            planned_identities.add(identity)
            plan_item = {
                "requirement_id": consumer.get("requirement_id"),
                "metric_ref": metric_ref,
                "source_metric_name": metric.get("source_metric_name") or metric.get("name"),
                "period_role": role,
                "period": actual_period,
                "grain": normalize_period(actual_period)[0] if normalize_period(actual_period) else None,
                "selector_dimensions": deepcopy(dimensions),
                "group_dimensions": list(consumer.get("group_dimensions") or []),
                "direct": direct_capability,
                "path": (
                    "direct_fact"
                    if direct_capability.get("status") == "available"
                    else constraint_adaptation_paths.get(identity)
                ),
            }
            capability_plan.append(plan_item)
            if direct_capability.get("status") == "available":
                canonical_selectors.append({
                    "metric_ref": metric_ref,
                    "source_metric_name": plan_item["source_metric_name"],
                    "period_role": role,
                    "period": actual_period,
                    "view_id": consumer.get("view_id"),
                    "grain": plan_item["grain"],
                    "selector_dimensions": deepcopy(dimensions),
                    "dimension_refs": list(dimension_refs),
                    "group_dimensions": list(dimension_refs),
                    "component": consumer.get("component"),
                    "capability_path": "direct_fact",
                    "source_binding": deepcopy(index.get("source") or {}),
                })
        if (
            identity in existing_targets
            or (not dimensions and relaxed_identity in existing_relaxed_targets)
            or _direct_available(
            index, metric["name"], actual_period, dimensions, dimension_refs
            )
        ):
            continue
        components = _aggregation_components(
            index, metric["name"], actual_period, dimensions, dimension_refs
        )
        if components is None:
            raise PreparationError(
                "SOURCE_PATH_UNAVAILABLE",
                f"指标 {metric['name']} 在 {actual_period} 没有直接事实或安全聚合方案",
                {"metric": metric["name"], "period": actual_period},
            )
        children = [str(item["period"]) for item in components]
        for item in capability_plan:
            if (
                item.get("metric_ref") == metric_ref
                and item.get("period_role") == role
                and item.get("selector_dimensions") == dimensions
            ):
                item["path"] = "aggregate_fact"
                item["aggregate_source_periods"] = list(children)
                item["aggregate_components"] = deepcopy(components)
                break
        suffix = _stable_suffix(identity)
        source_roles: list[str] = []
        for period in children:
            source_roles.append(_intern_physical_period_role(periods, period))
        adaptation_id = f"auto_adapt_{suffix}"
        adaptations.append({
            "requirement_id": adaptation_id,
            "metric_ref": metric_ref,
            "target_period_role": role,
            "target_period": actual_period,
            "view_id": consumer.get("view_id"),
            "dimensions": deepcopy(dimensions),
            "dimension_refs": list(dimension_refs),
            "expression": {
                "op": "sum",
                "args": [
                    (
                        {"fact": {"metric_ref": metric_ref, "period_role": source_role}}
                        if float(component.get("weight", 1.0)) == 1.0
                        else {
                            "op": "multiply",
                            "args": [
                                {"fact": {"metric_ref": metric_ref, "period_role": source_role}},
                                {"literal": float(component.get("weight", 1.0))},
                            ],
                        }
                    )
                    for source_role, component in zip(source_roles, components)
                ],
            },
            "rule_source": "source_metric_metadata",
            "validation": ["facts_present", "unit_consistent", "metric_additive"],
            "rollup": {
                "calendar": "iso8601",
                "target_period": actual_period,
                "components": deepcopy(components),
            },
            "criticality": consumer.get("criticality", "required"),
            "generated": True,
        })
        existing_targets.add(identity)
        decisions.append({
            "requirement_id": consumer.get("requirement_id"),
            "mode": "aggregate",
            "metric": metric["name"],
            "target_period": actual_period,
            "source_periods": children,
            "adaptation_id": adaptation_id,
        })

    prepared["input_adaptations"] = adaptations
    prepared["fact_capability_plan"] = capability_plan
    prepared["canonical_fact_selectors"] = canonical_selectors
    task["periods"] = periods
    return prepared, decisions
