#!/usr/bin/env python3
"""Normalize semantic IR and resolve source-backed inputs before compilation."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any

from source_capability import (
    direct_available,
    normalize_match_text,
    normalize_period,
    resolve_dimension,
    resolve_metric,
    validate_capabilities,
)


class PreparationError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


UNRESOLVED_METADATA_VALUES = {"", "unknown", "待元信息解析", "未解析"}


def _is_unresolved_metadata(value: Any) -> bool:
    return value is None or str(value).strip().lower() in UNRESOLVED_METADATA_VALUES


def _canonical_period(value: Any, path: str) -> str:
    parsed = normalize_period(value)
    if parsed is None:
        raise PreparationError("INVALID_PERIOD", f"{path} 无法识别时期：{value}")
    return parsed[1]


def normalize_analysis_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize periods before compilation, hashing, and fact selection."""
    normalized = deepcopy(ir)
    task = normalized.get("analysis_task")
    if not isinstance(task, dict):
        return normalized
    periods = task.get("periods")
    if isinstance(periods, dict):
        task["periods"] = {
            str(role): _canonical_period(value, f"analysis_task.periods.{role}")
            for role, value in periods.items()
        }
    for index, target in enumerate(normalized.get("attribution_targets") or []):
        if not isinstance(target, dict) or not isinstance(target.get("periods"), dict):
            continue
        target["periods"] = {
            str(role): _canonical_period(
                value, f"attribution_targets[{index}].periods.{role}"
            )
            for role, value in target["periods"].items()
        }
        for role, value in target["periods"].items():
            task.setdefault("periods", {}).setdefault(role, value)
    return normalized


def normalize_analysis_input(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("ir_version") == "analysis_ir/1.0":
        return normalize_analysis_ir(value)
    normalized = deepcopy(value)
    if normalized.get("schema_version") == "analysis_bundle/1.0":
        for item in normalized.get("tasks") or []:
            if isinstance(item, dict) and isinstance(item.get("analysis_ir"), dict):
                item["analysis_ir"] = normalize_analysis_ir(item["analysis_ir"])
    return normalized


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
            raise PreparationError(
                "METRIC_METADATA_CONFLICT",
                f"指标 {metric.get('name')} 的声明对象与源表元信息冲突",
                {
                    "metric": metric.get("name"),
                    "field": "metric_object",
                    "declared": declared_object,
                    "source": source_object,
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
                raise PreparationError(
                    "METRIC_METADATA_CONFLICT",
                    f"指标 {metric.get('name')} 的声明单位与源表元信息冲突",
                    {
                        "metric": metric.get("name"),
                        "field": "unit",
                        "declared": declared_unit,
                        "source": source_unit,
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
    return expected_dimension is not None and direct_available(
        index, source_metric, period, expected_dimension
    )


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
    parsed = normalize_period(period)
    if parsed is None:
        return []
    grain, canonical = parsed
    year = int(canonical[:4])
    if grain == "quarter":
        quarter = int(canonical[-1])
        start = (quarter - 1) * 3 + 1
        return [[f"{year:04d}-{month:02d}" for month in range(start, start + 3)]]
    if grain == "year":
        return [
            [f"{year:04d}-Q{quarter}" for quarter in range(1, 5)],
            [f"{year:04d}-{month:02d}" for month in range(1, 13)],
        ]
    # Week-to-month boundaries require an explicit coverage calendar and are not guessed.
    return []


def _aggregation_children(
    index: dict[str, Any],
    metric_name: str,
    target_period: str,
    dimensions: dict[str, Any],
    dimension_refs: list[str],
) -> list[str] | None:
    source_metric = _source_metric(metric_name, index)
    if source_metric is None:
        return None
    metadata = (index.get("metrics") or {}).get(source_metric) or {}
    if metadata.get("additive") is not True:
        return None
    for children in _child_period_candidates(target_period):
        if all(
            _direct_available(index, metric_name, period, dimensions, dimension_refs)
            for period in children
        ):
            return children
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
        for role in roles:
            consumers.append({
                "requirement_id": target.get("target_id"),
                "metric_ref": target.get("metric_ref", target.get("metric")),
                "view_id": target.get("view_id"),
                "dimensions": target_dimensions,
                "dimension_refs": target.get("group_dimensions") or [],
                "criticality": target.get("criticality", "required"),
                "role": role,
            })
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


def prepare_analysis_ir(
    ir: dict[str, Any],
    index: dict[str, Any],
    composition_registry: dict[str, Any],
    derived_registry: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_capabilities(index)
    prepared = normalize_analysis_ir(ir)
    _bind_declared_metric_metadata(prepared, index)
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
            str(item.get("view_id")),
            tuple(sorted(item.get("dimension_refs") or [])),
        )
        for item in prepared.get("input_adaptations") or []
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
        for input_ref in _composition_input_refs(prepared, metrics, definition, index):
            for role in requirement.get("period_roles") or []:
                consumers.append({
                    **requirement,
                    "metric_ref": input_ref,
                    "role": role,
                })

    for consumer in consumers:
        metric_ref = str(consumer.get("metric_ref"))
        metric = metrics.get(metric_ref)
        role = str(consumer.get("role"))
        if metric is None or role not in periods:
            continue
        # Composed metrics are resolved through their leaf inputs, not aggregated as ratios.
        if metric.get("composition_id"):
            composition = (composition_registry.get("definitions") or {}).get(
                metric["composition_id"]
            ) or {}
            for input_ref in _composition_input_refs(prepared, metrics, composition, index):
                consumers.append({**consumer, "metric_ref": input_ref})
            continue
        dimensions = consumer.get("dimensions") or {}
        dimension_refs = consumer.get("dimension_refs") or []
        identity = (
            metric_ref,
            role,
            str(consumer.get("view_id")),
            json.dumps(dimensions, ensure_ascii=False, sort_keys=True),
            tuple(sorted(dimension_refs)),
        )
        relaxed_identity = (
            metric_ref,
            role,
            str(consumer.get("view_id")),
            tuple(sorted(dimension_refs)),
        )
        if (
            identity in existing_targets
            or (not dimensions and relaxed_identity in existing_relaxed_targets)
            or _direct_available(
            index, metric["name"], periods[role], dimensions, dimension_refs
            )
        ):
            continue
        children = _aggregation_children(
            index, metric["name"], periods[role], dimensions, dimension_refs
        )
        if children is None:
            raise PreparationError(
                "SOURCE_PATH_UNAVAILABLE",
                f"指标 {metric['name']} 在 {periods[role]} 没有直接事实或安全聚合方案",
                {"metric": metric["name"], "period": periods[role]},
            )
        suffix = _stable_suffix(identity)
        source_roles: list[str] = []
        for number, period in enumerate(children, start=1):
            source_role = f"__auto_{role}_{suffix}_{number}"
            periods[source_role] = period
            source_roles.append(source_role)
        adaptation_id = f"auto_adapt_{suffix}"
        adaptations.append({
            "requirement_id": adaptation_id,
            "metric_ref": metric_ref,
            "target_period_role": role,
            "view_id": consumer.get("view_id"),
            "dimensions": deepcopy(dimensions),
            "dimension_refs": list(dimension_refs),
            "expression": {
                "op": "sum",
                "args": [
                    {"fact": {"metric_ref": metric_ref, "period_role": source_role}}
                    for source_role in source_roles
                ],
            },
            "rule_source": "source_metric_metadata",
            "validation": ["facts_present", "unit_consistent", "metric_additive"],
            "criticality": consumer.get("criticality", "required"),
            "generated": True,
        })
        existing_targets.add(identity)
        decisions.append({
            "requirement_id": consumer.get("requirement_id"),
            "mode": "aggregate",
            "metric": metric["name"],
            "target_period": periods[role],
            "source_periods": children,
            "adaptation_id": adaptation_id,
        })

    prepared["input_adaptations"] = adaptations
    task["periods"] = periods
    return prepared, decisions
