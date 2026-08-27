#!/usr/bin/env python3
"""Stable data-access boundary used by the analysis workflow."""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any

from _vendor.ecom_competitor_source import normalize_match_text
from metric_constraints import normalize_metric_constraints
from selector_context import task_filter_selectors


RESOLVED_CAPABILITIES_V1 = "resolved_capabilities/1.0"
SOURCE_BINDING_V1 = "source_binding/1.0"


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_source_config(path: Path, *, source_url: str | None = None) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != "data_source_config/1.0":
        raise ValueError(f"unsupported data source config: {path}")
    required = ("provider_id", "source_id", "source_url", "sheet_roles")
    missing = [key for key in required if not value.get(key)]
    if missing:
        raise ValueError(f"data source config is missing: {missing}")
    period_semantics = value.get("period_semantics") or {}
    week_semantics = period_semantics.get("week") if isinstance(period_semantics, dict) else None
    if week_semantics is not None and week_semantics != {
        "label_format": "YYYY-Www",
        "calendar": "iso8601",
        "week_start": "monday",
        "week_end": "sunday",
    }:
        raise ValueError("unsupported week period semantics")
    effective = deepcopy(value)
    if source_url is not None:
        effective["source_url"] = source_url
    effective["config_hash"] = canonical_hash({
        key: effective.get(key)
        for key in (
            "schema_version", "provider_id", "source_id", "source_url",
            "sheet_roles", "allow_stale_by_default",
            "period_semantics",
        )
    })
    return effective


def build_resolve_request(
    tasks: list[tuple[str, dict[str, Any]]],
    composition_registry: dict[str, Any],
    derived_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect only semantic names and periods needed during preparation."""
    metric_names: set[str] = set()
    dimension_names: set[str] = set()
    periods: set[str] = set()
    contexts: list[dict[str, Any]] = []
    composition_definitions = composition_registry.get("definitions") or {}
    derived_definitions = (derived_registry or {}).get("definitions") or {}

    def composition_for_metric(
        metric: dict[str, Any], explicit_by_ref: dict[str, str]
    ) -> tuple[str, dict[str, Any]] | None:
        metric_ref = str(metric.get("metric_id") or "")
        explicit = metric.get("composition_id") or explicit_by_ref.get(metric_ref)
        if explicit in composition_definitions:
            return str(explicit), composition_definitions[str(explicit)]
        requested = normalize_match_text(metric.get("name"))
        for composition_id, definition in composition_definitions.items():
            if not isinstance(definition, dict):
                continue
            if any(
                normalize_match_text(value) == requested
                for value in definition.get("trigger_phrases") or []
            ):
                return str(composition_id), definition
        return None

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            if key == "dimensions":
                dimension_names.update(str(item) for item in value)
            for child_key, child in value.items():
                if child_key in {"dimension_refs", "group_dimensions"} and isinstance(child, list):
                    dimension_names.update(str(item) for item in child)
                elif child_key == "dimension_ref" and child:
                    dimension_names.add(str(child))
                visit(child, child_key)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)

    for task_id, ir in tasks:
        task = ir.get("analysis_task") or {}
        task_periods = task.get("periods") or {}
        task_metrics_by_ref = {
            str(item.get("metric_id")): item
            for item in task.get("metrics") or []
            if isinstance(item, dict) and item.get("metric_id")
        }
        inherited_dimensions = set(task_filter_selectors(task.get("filters")))
        task_dimensions: set[str] = set()

        def collect_task_dimensions(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                if key == "dimensions":
                    task_dimensions.update(str(item) for item in value)
                for child_key, child in value.items():
                    if child_key in {"dimension_refs", "group_dimensions"} and isinstance(child, list):
                        task_dimensions.update(str(item) for item in child)
                    elif child_key == "dimension_ref" and child:
                        task_dimensions.add(str(child))
                    collect_task_dimensions(child, child_key)
            elif isinstance(value, list):
                for child in value:
                    collect_task_dimensions(child, key)

        collect_task_dimensions(ir)
        query = str(task.get("query") or "")
        query_token = normalize_match_text(query)
        context_metrics: list[dict[str, Any]] = []
        explicit_compositions = {
            str(item.get("metric_ref")): str(item.get("composition_id"))
            for item in ir.get("metric_compositions") or []
            if isinstance(item, dict) and item.get("metric_ref") and item.get("composition_id")
        }
        consumers_by_metric: dict[str, list[dict[str, Any]]] = {}
        for collection, id_field in (
            ("fact_observations", "requirement_id"),
            ("metric_compositions", "requirement_id"),
            ("derived_requirements", "requirement_id"),
            ("attribution_targets", "target_id"),
        ):
            for requirement in ir.get(collection) or []:
                if not isinstance(requirement, dict) or not requirement.get("metric_ref"):
                    continue
                period_roles = list(
                    requirement.get("period_roles")
                    or requirement.get("required_period_roles")
                    or []
                )
                if collection == "derived_requirements" and not period_roles:
                    period_roles = list(
                        (
                            derived_definitions.get(str(requirement.get("derived_metric_id")))
                            or {}
                        ).get("required_period_roles")
                        or []
                    )
                requirement_dimensions = inherited_dimensions | {
                    str(value) for value in (requirement.get("dimensions") or {})
                } | {
                    str(value) for value in (requirement.get("dimension_refs") or [])
                }
                breakdown_dimensions = {
                    str(value) for value in (requirement.get("dimension_refs") or [])
                } | {
                    str(value) for value in (requirement.get("group_dimensions") or [])
                }
                metric_constraints = normalize_metric_constraints(
                    requirement.get("metric_constraints")
                )
                resolution_intent = requirement.get("resolution_intent")
                if isinstance(resolution_intent, dict):
                    operand = resolution_intent.get("operand")
                    scope = operand.get("scope") if isinstance(operand, dict) else None
                    dimension_hint = (
                        scope.get("dimension_hint") if isinstance(scope, dict) else None
                    )
                    if dimension_hint:
                        task_dimensions.add(str(dimension_hint))
                        dimension_names.add(str(dimension_hint))
                constraint_dimensions = {
                    str(item["dimension_hint"])
                    for item in metric_constraints
                    if item.get("dimension_hint")
                }
                task_dimensions.update(constraint_dimensions)
                dimension_names.update(constraint_dimensions)
                consumers_by_metric.setdefault(str(requirement["metric_ref"]), []).append({
                    "requirement_id": str(requirement.get(id_field) or ""),
                    "requirement_type": collection,
                    "criticality": str(requirement.get("criticality") or "required"),
                    "period_roles": period_roles,
                    "periods": [
                        str(task_periods[role]) for role in period_roles if role in task_periods
                    ],
                    "dimensions": sorted(requirement_dimensions),
                    "breakdown_dimensions": sorted(breakdown_dimensions),
                    "derived_metric_id": requirement.get("derived_metric_id"),
                    "semantic_text": requirement.get("semantic_text"),
                    "query_fragment": requirement.get("query_fragment"),
                    "metric_constraints": metric_constraints,
                    "resolution_intent": deepcopy(resolution_intent),
                    "allowed_metric_objects": list(
                        (
                            derived_definitions.get(str(requirement.get("derived_metric_id")))
                            or {}
                        ).get("metric_objects")
                        or []
                    ),
                })
        composition_intents: list[dict[str, Any]] = []
        for metric in task.get("metrics") or []:
            if isinstance(metric, dict) and metric.get("name"):
                name = str(metric["name"])
                metric_names.add(name)
                name_token = normalize_match_text(name)
                metric_ref = str(metric.get("metric_id") or name)
                context_metrics.append({
                    "metric_ref": metric_ref,
                    "name": name,
                    "metric_object": metric.get("metric_object"),
                    "metric_object_provenance": metric.get("metric_object_source") or "model_inferred",
                    "unit": metric.get("unit"),
                    "unit_provenance": metric.get("unit_source") or (
                        "model_inferred"
                        if str(metric.get("unit") or "").strip().lower()
                        in {"", "unknown", "待元信息解析", "未解析"}
                        else "user_explicit"
                    ),
                    "consumers": deepcopy(consumers_by_metric.get(metric_ref) or []),
                    "required_periods": sorted({
                        str(period)
                        for consumer in consumers_by_metric.get(metric_ref) or []
                        for period in consumer.get("periods") or []
                    }),
                    "required_dimensions": sorted({
                        str(dimension)
                        for consumer in consumers_by_metric.get(metric_ref) or []
                        for dimension in consumer.get("dimensions") or []
                    }),
                    "required_breakdown_dimensions": sorted({
                        str(dimension)
                        for consumer in consumers_by_metric.get(metric_ref) or []
                        for dimension in consumer.get("breakdown_dimensions") or []
                    }),
                    "provenance": metric.get("name_source") or (
                        "user_explicit" if name_token and name_token in query_token else "model_inferred"
                    ),
                })
                composition = composition_for_metric(metric, explicit_compositions)
                if composition is not None:
                    composition_id, definition = composition
                    inputs = [
                        deepcopy(item)
                        for item in definition.get("inputs") or []
                        if isinstance(item, dict) and item.get("metric")
                    ]
                    for item in inputs:
                        metric_names.add(str(item["metric"]))
                    composition_intents.append({
                        "metric_ref": metric_ref,
                        "requested_metric": name,
                        "composition_id": composition_id,
                        "direct_preferred": True,
                        "inputs": inputs,
                        "consumers": deepcopy(consumers_by_metric.get(metric_ref) or []),
                    })
        for metric_ref, consumers in consumers_by_metric.items():
            for consumer in consumers:
                resolution_intent = consumer.get("resolution_intent")
                if not isinstance(resolution_intent, dict):
                    continue
                requirement_id = str(consumer.get("requirement_id") or "")
                semantic_text = str(consumer.get("semantic_text") or "")
                if not requirement_id or not semantic_text:
                    continue
                virtual_ref = f"__resolution_{canonical_hash([task_id, requirement_id])[:12]}"
                virtual_consumer = deepcopy(consumer)
                logical_metric = task_metrics_by_ref.get(str(metric_ref)) or {}
                resolution_operation = str(resolution_intent.get("operation") or "")
                provenance = str(
                    resolution_intent.get("provenance") or "model_inferred"
                )
                context_metrics.append({
                    "metric_ref": virtual_ref,
                    "name": semantic_text,
                    "metric_object": resolution_intent.get("output_metric_object") or "ratio",
                    "metric_object_provenance": "user_explicit",
                    "unit": "待元信息解析",
                    "unit_provenance": "model_inferred",
                    "consumers": [virtual_consumer],
                    "required_periods": list(consumer.get("periods") or []),
                    "required_dimensions": list(consumer.get("dimensions") or []),
                    "required_breakdown_dimensions": list(
                        consumer.get("breakdown_dimensions") or []
                    ),
                    "provenance": provenance,
                    "resolution_requirement_id": requirement_id,
                    "resolution_operation": resolution_operation,
                    "resolution_intent": deepcopy(resolution_intent),
                    "logical_metric_ref": metric_ref,
                    "logical_metric_name": logical_metric.get("name"),
                })
                metric_names.add(semantic_text)
                requested = normalize_match_text(semantic_text)
                composition = next((
                    (str(composition_id), definition)
                    for composition_id, definition in composition_definitions.items()
                    if isinstance(definition, dict)
                    and any(
                        normalize_match_text(trigger) == requested
                        for trigger in definition.get("trigger_phrases") or []
                    )
                ), None)
                if composition is None:
                    continue
                composition_id, definition = composition
                inputs = [
                    deepcopy(item)
                    for item in definition.get("inputs") or []
                    if isinstance(item, dict) and item.get("metric")
                ]
                for item in inputs:
                    metric_names.add(str(item["metric"]))
                composition_intents.append({
                    "metric_ref": virtual_ref,
                    "requested_metric": semantic_text,
                    "composition_id": composition_id,
                    "direct_preferred": True,
                    "inputs": inputs,
                    "consumers": [virtual_consumer],
                    "resolution_requirement_id": requirement_id,
                    "logical_metric_ref": metric_ref,
                })
        periods.update(str(item) for item in (task.get("periods") or {}).values())
        for target in ir.get("attribution_targets") or []:
            if isinstance(target, dict):
                periods.update(str(item) for item in (target.get("periods") or {}).values())
        visit(ir)
        contexts.append({
            "task_id": task_id,
            "query": query,
            "scope": task.get("scope"),
            "metrics": context_metrics,
            "composition_intents": composition_intents,
            "dimensions": sorted(task_dimensions),
            "periods": sorted(str(item) for item in (task.get("periods") or {}).values()),
            "resolution_patches": deepcopy(ir.get("resolution_patches") or []),
        })

    return {
        "schema_version": "capability_resolve_request/1.0",
        "composition_registry_hash": canonical_hash(composition_registry),
        "metrics": sorted(metric_names),
        "dimensions": sorted(dimension_names),
        "periods": sorted(periods),
        "contexts": contexts,
    }


def validate_source_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise ValueError("source_binding must be an object")
    required = ("schema_version", "provider_id", "source_id", "config_hash", "revision", "schema_hash")
    missing = [key for key in required if binding.get(key) in (None, "")]
    if binding.get("schema_version") != SOURCE_BINDING_V1 or missing:
        raise ValueError(f"invalid source_binding; missing or invalid fields: {missing}")
    return binding


class DataGateway(ABC):
    """Minimal replaceable boundary for metadata resolution and fact retrieval."""

    @abstractmethod
    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
