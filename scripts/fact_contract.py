#!/usr/bin/env python3
"""Canonical physical-fact demands and consumer bindings."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


SCENE_FACTS_V1 = "scene_facts/1.0"
SCENE_FACTS_V2 = "scene_facts/2.0"
UNRESOLVED_UNIT_VALUES = {"", "unknown", "待元信息解析", "未解析"}


def _is_unresolved_unit(value: Any) -> bool:
    return value is None or str(value).strip().lower() in UNRESOLVED_UNIT_VALUES


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any, length: int = 24) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _selector_values(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value]


def merge_selectors(selectors: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the smallest simple selector union; an empty selector means all values."""
    if not selectors or any(not selector for selector in selectors):
        return {}
    dimensions = sorted({key for selector in selectors for key in selector})
    merged: dict[str, Any] = {}
    for dimension in dimensions:
        if any(dimension not in selector for selector in selectors):
            continue
        values: list[Any] = []
        for selector in selectors:
            for value in _selector_values(selector[dimension]):
                if value not in values:
                    values.append(value)
        merged[dimension] = values
    return merged


def merge_source_domains(slots: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for slot in slots:
        for dimension, domain_ref in (slot.get("source_dimension_domains") or {}).items():
            previous = merged.get(str(dimension))
            if previous is not None and previous != domain_ref:
                raise ValueError(
                    f"physical dimension {dimension!r} has conflicting source domains"
                )
            merged[str(dimension)] = deepcopy(domain_ref)
    return merged


def build_fact_demands(
    slots: list[dict[str, Any]],
    *,
    task_id: str = "default",
) -> list[dict[str, Any]]:
    """Merge compatible slots while preserving every consumer binding."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    bases: dict[str, dict[str, Any]] = {}
    for slot in slots:
        base = {
            "source_metric_name": slot.get("source_metric_name"),
            "period": slot.get("period"),
            "dimension_refs": sorted(set(
                slot.get("source_dimension_refs") or slot.get("dimension_refs") or []
            )),
            "component": slot.get("component"),
            "scope": slot.get("scope"),
            "filters": slot.get("filters") or [],
        }
        key = canonical_json(base)
        bases[key] = base
        grouped.setdefault(key, []).append(slot)

    demands: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = grouped[key]
        selector = merge_selectors([
            deepcopy(
                slot.get("source_selector_dimensions")
                or slot.get("selector_dimensions")
                or {}
            ) for slot in group
        ])
        representative = group[0]
        source_domains = merge_source_domains(group)
        demand_identity = {
            **bases[key],
            "selector_dimensions": selector,
            "source_dimension_domains": source_domains,
        }
        bindings = []
        for slot in group:
            binding = {
                "binding_id": stable_id(
                    "binding",
                    [task_id, slot.get("fact_slot_id"), slot.get("period_role"), slot.get("view_id")],
                ),
                "task_id": task_id,
                "fact_slot_id": slot.get("fact_slot_id"),
                "period_role": slot.get("period_role"),
                "view_id": slot.get("view_id"),
                "selector_dimensions": deepcopy(slot.get("selector_dimensions") or {}),
                "source_selector_dimensions": deepcopy(
                    slot.get("source_selector_dimensions")
                    or slot.get("selector_dimensions")
                    or {}
                ),
                "dimension_projection": deepcopy(slot.get("dimension_projection") or {}),
                "source_dimension_domains": deepcopy(slot.get("source_dimension_domains") or {}),
                "requirement_refs": sorted(set(slot.get("requirement_refs") or [])),
                "metric_ref": slot.get("metric_ref"),
                "metric": slot.get("metric"),
                "metric_object": slot.get("metric_object"),
                "unit": slot.get("unit"),
            }
            bindings.append(binding)
        demands.append({
            "fact_demand_id": stable_id("demand", demand_identity),
            **demand_identity,
            "dimension_projection": deepcopy(
                representative.get("dimension_projection") or {}
            ),
            "metric_ref": representative.get("metric_ref"),
            "metric": representative.get("metric"),
            "metric_object": representative.get("metric_object"),
            "unit": representative.get("unit"),
            "consumer_bindings": bindings,
        })
    return demands


def merge_fetch_requests(
    requests: list[tuple[str, dict[str, Any]]],
    *,
    request_id: str = "fetch_bundle_1",
    source_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slots: list[dict[str, Any]] = []
    covered: set[str] = set()
    scopes: list[Any] = []
    filters: list[Any] = []
    registry_hashes: set[str] = set()
    for task_id, request in requests:
        request_binding = request.get("source_binding")
        if request_binding is not None and request_binding != source_binding:
            raise ValueError("fetch requests use an incompatible source_binding")
        scopes.append(request.get("scope"))
        filters.append(request.get("filters") or [])
        covered.update(request.get("covered_requirement_refs") or [])
        if request.get("dimension_set_registry_hash"):
            registry_hashes.add(str(request["dimension_set_registry_hash"]))
        task_slots = request.get("fact_slots") or []
        for slot in task_slots:
            copied = deepcopy(slot)
            copied["_task_id"] = task_id
            slots.append(copied)

    # Build per task first so task identity remains attached, then merge compatible demands.
    initial: list[dict[str, Any]] = []
    for task_id, _ in requests:
        initial.extend(build_fact_demands(
            [slot for slot in slots if slot.get("_task_id") == task_id],
            task_id=task_id,
        ))
    synthetic_slots: list[dict[str, Any]] = []
    for demand in initial:
        for binding in demand["consumer_bindings"]:
            synthetic_slots.append({
                key: deepcopy(value)
                for key, value in demand.items()
                if key not in {"fact_demand_id", "consumer_bindings"}
            } | {
                "fact_slot_id": binding["fact_slot_id"],
                "period_role": binding["period_role"],
                "view_id": binding["view_id"],
                "selector_dimensions": binding["selector_dimensions"],
                "source_selector_dimensions": deepcopy(
                    binding.get("source_selector_dimensions")
                    or demand.get("selector_dimensions")
                    or {}
                ),
                "source_dimension_refs": deepcopy(demand.get("dimension_refs") or []),
                "dimension_projection": deepcopy(binding.get("dimension_projection") or {}),
                "source_dimension_domains": deepcopy(
                    binding.get("source_dimension_domains")
                    or demand.get("source_dimension_domains")
                    or {}
                ),
                "requirement_refs": binding["requirement_refs"],
                "metric_ref": binding.get("metric_ref"),
                "metric": binding.get("metric"),
                "metric_object": binding.get("metric_object"),
                "unit": binding.get("unit"),
                "_task_id": binding["task_id"],
            })

    # Cross-task merge while retaining each task in its binding.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for slot in synthetic_slots:
        base = {
            key: slot.get(key)
            for key in (
                "source_metric_name", "period", "dimension_refs",
                "component", "scope", "filters",
            )
        }
        grouped.setdefault(canonical_json(base), []).append(slot)
    demands: list[dict[str, Any]] = []
    for key in sorted(grouped):
        group = grouped[key]
        base = json.loads(key)
        selector = merge_selectors([
            slot.get("source_selector_dimensions")
            or slot.get("selector_dimensions")
            or {}
            for slot in group
        ])
        source_domains = merge_source_domains(group)
        identity = {
            **base,
            "selector_dimensions": selector,
            "source_dimension_domains": source_domains,
        }
        representative = group[0]
        bindings = []
        for slot in group:
            task_id = str(slot.get("_task_id") or "default")
            bindings.append({
                "binding_id": stable_id(
                    "binding",
                    [task_id, slot.get("fact_slot_id"), slot.get("period_role"), slot.get("view_id")],
                ),
                "task_id": task_id,
                "fact_slot_id": slot.get("fact_slot_id"),
                "period_role": slot.get("period_role"),
                "view_id": slot.get("view_id"),
                "selector_dimensions": deepcopy(slot.get("selector_dimensions") or {}),
                "source_selector_dimensions": deepcopy(
                    slot.get("source_selector_dimensions")
                    or slot.get("selector_dimensions")
                    or {}
                ),
                "dimension_projection": deepcopy(slot.get("dimension_projection") or {}),
                "source_dimension_domains": deepcopy(slot.get("source_dimension_domains") or {}),
                "requirement_refs": sorted(set(slot.get("requirement_refs") or [])),
                "metric_ref": slot.get("metric_ref"),
                "metric": slot.get("metric"),
                "metric_object": slot.get("metric_object"),
                "unit": slot.get("unit"),
            })
        demands.append({
            "fact_demand_id": stable_id("demand", identity),
            **identity,
            "dimension_projection": deepcopy(
                representative.get("dimension_projection") or {}
            ),
            "metric_ref": representative.get("metric_ref"),
            "metric": representative.get("metric"),
            "metric_object": representative.get("metric_object"),
            "unit": representative.get("unit"),
            "consumer_bindings": bindings,
        })

    if len(registry_hashes) > 1:
        raise ValueError("fetch requests use different dimension set registries")
    merged = {
        "request_id": request_id,
        "dimension_set_registry_hash": next(iter(registry_hashes), None),
        "purpose": "bundle_unified_fetch",
        "scope": scopes[0] if scopes and all(item == scopes[0] for item in scopes) else "multiple_scopes",
        "filters": filters[0] if filters and all(item == filters[0] for item in filters) else [],
        "fact_demands": demands,
        "covered_requirement_refs": sorted(covered),
    }
    if source_binding is not None:
        from data_gateway import validate_source_binding

        validate_source_binding(source_binding)
        merged["source_binding"] = deepcopy(source_binding)
    return merged


def project_scene_facts(payload: dict[str, Any], task_id: str | None = None) -> list[dict[str, Any]]:
    """Project canonical v2 facts into logical rows consumed by the v1 executor."""
    facts = payload.get("facts")
    if not isinstance(facts, list):
        raise ValueError("scene facts require a facts array")
    if payload.get("schema_version") != SCENE_FACTS_V2:
        return facts
    bindings = payload.get("bindings")
    if not isinstance(bindings, list):
        raise ValueError("scene_facts/2.0 requires a bindings array")
    fact_ids: set[str] = set()
    physical: dict[str, dict[str, Any]] = {}
    for fact in facts:
        if not isinstance(fact, dict) or not fact.get("fact_id"):
            raise ValueError("scene_facts/2.0 contains an invalid fact")
        fact_id = str(fact["fact_id"])
        if fact_id in fact_ids:
            raise ValueError(f"scene_facts/2.0 contains duplicate fact_id: {fact_id}")
        fact_ids.add(fact_id)
        source_ref = fact.get("source_ref") if isinstance(fact.get("source_ref"), dict) else {}
        identity = canonical_json({
            "sheet_id": source_ref.get("sheet_id") or source_ref.get("sheet"),
            "row": source_ref.get("row"),
            "column": source_ref.get("column"),
            "revision": source_ref.get("revision"),
            "source_metric_name": fact.get("source_metric_name") or fact.get("metric"),
            "component": fact.get("component"),
            "scope": fact.get("scope"),
            "filters": fact.get("filters") or [],
            "period": fact.get("period"),
            "dimensions": fact.get("dimensions"),
        })
        previous = physical.get(identity)
        if previous is not None:
            if previous.get("value") != fact.get("value") or previous.get("missing") != fact.get("missing"):
                raise ValueError("scene_facts/2.0 contains conflicting physical facts")
            raise ValueError("scene_facts/2.0 contains duplicate physical facts")
        physical[identity] = fact

    by_fact: dict[str, list[dict[str, Any]]] = {}
    binding_ids: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or not binding.get("fact_id"):
            raise ValueError("scene_facts/2.0 contains an invalid binding")
        if str(binding["fact_id"]) not in fact_ids:
            raise ValueError(f"binding references unknown fact_id: {binding['fact_id']}")
        binding_id = binding.get("binding_id")
        if not isinstance(binding_id, str) or not binding_id:
            raise ValueError("scene_facts/2.0 binding requires a non-empty binding_id")
        if binding_id in binding_ids:
            raise ValueError(f"scene_facts/2.0 contains duplicate binding_id: {binding_id}")
        binding_ids.add(binding_id)
        if task_id is not None and binding.get("task_id") != task_id:
            continue
        by_fact.setdefault(str(binding["fact_id"]), []).append(binding)
    rows: list[dict[str, Any]] = []
    for fact in facts:
        for binding in by_fact.get(str(fact["fact_id"]), []):
            row = deepcopy(fact)
            physical_fact_id = str(fact["fact_id"])
            binding_id = str(binding["binding_id"])
            row.update({
                "fact_id": stable_id("fact", [physical_fact_id, binding_id]),
                "physical_fact_id": physical_fact_id,
                "binding_id": binding_id,
                "fact_slot_id": binding.get("fact_slot_id"),
                "period_role": binding.get("period_role"),
                "view_id": binding.get("view_id"),
                "consumer_task_id": binding.get("task_id"),
                "requirement_refs": deepcopy(binding.get("requirement_refs") or []),
            })
            for field in ("metric_ref", "metric", "metric_object"):
                if binding.get(field) is not None:
                    row[field] = binding[field]
            projection = binding.get("dimension_projection") or {}
            if projection and isinstance(row.get("dimensions"), dict):
                row["dimensions"] = {
                    str(projection.get(name) or name): deepcopy(value)
                    for name, value in row["dimensions"].items()
                }
            observed_unit = fact.get("unit")
            expected_unit = binding.get("unit")
            if not _is_unresolved_unit(observed_unit):
                if (
                    not _is_unresolved_unit(expected_unit)
                    and str(observed_unit).strip() != str(expected_unit).strip()
                ):
                    raise ValueError(
                        "scene_facts/2.0 unit conflict for "
                        f"fact_id={fact.get('fact_id')}: observed={observed_unit!r}, "
                        f"expected={expected_unit!r}"
                    )
                row["unit"] = observed_unit
            elif not _is_unresolved_unit(expected_unit):
                row["unit"] = expected_unit
            rows.append(row)
    return rows
