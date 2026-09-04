#!/usr/bin/env python3
"""Deterministic, source-independent normalization for analysis IR."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from ir_contract_guard import normalize_attribution_period_roles
from source_capability import normalize_period


class AnalysisIRNormalizationError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def validate_period_values(ir: dict[str, Any]) -> None:
    """Preflight period maps before policy commit or source resolution.

    Role labels (for example ``analysis``) are not period values.  This is a
    deliberately narrow protocol check; it does not infer missing periods or
    impose a new period grammar beyond ``normalize_period``.
    """
    task = ir.get("analysis_task") if isinstance(ir, dict) else None
    period_maps: list[tuple[str, Any]] = []
    if isinstance(task, dict) and isinstance(task.get("periods"), dict):
        period_maps.append(("analysis_task.periods", task["periods"]))
    for index, target in enumerate(ir.get("attribution_targets") or []):
        if isinstance(target, dict) and isinstance(target.get("periods"), dict):
            period_maps.append((f"attribution_targets[{index}].periods", target["periods"]))
    role_names = {"analysis", "comparison", "analysis_last_year", "comparison_last_year"}
    for prefix, values in period_maps:
        for role, value in values.items():
            path = f"{prefix}.{role}"
            if value is None or not str(value).strip():
                raise AnalysisIRNormalizationError(
                    "INVALID_PERIOD", f"{path} 不能为空", {"path": path, "value": value}
                )
            if str(value).strip().lower() in role_names:
                raise AnalysisIRNormalizationError(
                    "INVALID_PERIOD",
                    f"{path} 不能使用时期角色名作为时期值：{value}",
                    {"path": path, "value": value},
                )
            parsed = normalize_period(value)
            if parsed is None:
                raise AnalysisIRNormalizationError(
                    "INVALID_PERIOD", f"{path} 无法识别时期：{value}",
                    {"path": path, "value": value},
                )


def _canonical_period(value: Any, path: str) -> str:
    parsed = normalize_period(value)
    if parsed is None:
        raise AnalysisIRNormalizationError(
            "INVALID_PERIOD", f"{path} 无法识别时期：{value}", {"path": path, "value": value}
        )
    return parsed[1]


def _stable_suffix(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:10]


def _normalize_factor_kinds(target: dict[str, Any]) -> None:
    for factor in target.get("factors") or []:
        if not isinstance(factor, dict) or factor.get("kind"):
            continue
        if factor.get("metric_ref") is not None:
            factor["kind"] = "metric"
        elif "literal" in factor or factor.get("values_by_period_role") is not None:
            factor["kind"] = "literal"


_REQUIREMENT_COLLECTIONS = (
    ("input_adaptations", "requirement_id"),
    ("fact_observations", "requirement_id"),
    ("metric_compositions", "requirement_id"),
    ("derived_requirements", "requirement_id"),
    ("custom_calculations", "requirement_id"),
    ("attribution_targets", "target_id"),
)


def _calculation_requirement_ids(ir: dict[str, Any]) -> set[str]:
    return {
        str(item[id_field])
        for collection, id_field in _REQUIREMENT_COLLECTIONS
        for item in ir.get(collection) or []
        if isinstance(item, dict) and item.get(id_field)
    }


def _split_multi_period_compositions(ir: dict[str, Any]) -> dict[str, list[str]]:
    """Expand declared compositions to the compiler's one-period shape."""
    expanded: list[Any] = []
    occupied_ids = _calculation_requirement_ids(ir)
    parent_children: dict[str, list[str]] = {}
    for requirement in ir.get("metric_compositions") or []:
        if not isinstance(requirement, dict):
            expanded.append(requirement)
            continue
        normalized_from = requirement.get("normalized_from_requirement_id")
        requirement_id = requirement.get("requirement_id")
        if normalized_from and requirement_id:
            parent_children.setdefault(str(normalized_from), []).append(
                str(requirement_id)
            )
        roles = requirement.get("period_roles")
        if not isinstance(roles, list) or len(roles) <= 1:
            expanded.append(requirement)
            continue
        base_id = str(requirement.get("requirement_id") or "composition")
        child_ids: list[str] = []
        for role in roles:
            item = deepcopy(requirement)
            item["period_roles"] = [role]
            child_id = f"{base_id}__{_stable_suffix([base_id, str(role)])}"
            if child_id in occupied_ids:
                raise AnalysisIRNormalizationError(
                    "NORMALIZED_REQUIREMENT_ID_COLLISION",
                    f"normalized composition requirement ID collides: {child_id}",
                    {"parent_requirement_id": base_id, "child_requirement_id": child_id},
                )
            occupied_ids.add(child_id)
            child_ids.append(child_id)
            item["requirement_id"] = child_id
            item["normalized_from_requirement_id"] = base_id
            expanded.append(item)
        parent_children[base_id] = child_ids
    ir["metric_compositions"] = expanded
    return parent_children


def _rewrite_output_requirement_refs(
    ir: dict[str, Any], parent_children: dict[str, list[str]]
) -> None:
    available_ids = _calculation_requirement_ids(ir)
    for output_index, output in enumerate(ir.get("output_requirements") or []):
        if not isinstance(output, dict):
            continue
        refs = output.get("source_requirement_refs")
        if not isinstance(refs, list):
            continue
        expanded_refs: list[Any] = []
        seen: set[str] = set()
        for ref in refs:
            replacements = parent_children.get(str(ref), [ref])
            for replacement in replacements:
                token = str(replacement)
                if token in seen:
                    continue
                seen.add(token)
                expanded_refs.append(replacement)
        missing = [
            str(ref) for ref in expanded_refs
            if not isinstance(ref, str) or ref not in available_ids
        ]
        if missing:
            raise AnalysisIRNormalizationError(
                "OUTPUT_REQUIREMENT_REF_UNKNOWN",
                "output requirement references unknown calculation requirements",
                {
                    "path": f"output_requirements[{output_index}].source_requirement_refs",
                    "unknown_requirement_refs": missing,
                },
            )
        output["source_requirement_refs"] = expanded_refs


def normalize_analysis_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Return canonical IR without resolving business or source semantics."""
    normalized = deepcopy(ir)
    task = normalized.get("analysis_task")
    if not isinstance(task, dict):
        return normalized
    validate_period_values(normalized)
    periods = task.get("periods")
    if isinstance(periods, dict):
        task["periods"] = {
            str(role): _canonical_period(value, f"analysis_task.periods.{role}")
            for role, value in periods.items()
        }
    for index, target in enumerate(normalized.get("attribution_targets") or []):
        if not isinstance(target, dict):
            continue
        target_periods = target.get("periods")
        if isinstance(target_periods, dict):
            target["periods"] = {
                str(role): _canonical_period(
                    value, f"attribution_targets[{index}].periods.{role}"
                )
                for role, value in target_periods.items()
            }
        _normalize_factor_kinds(target)
    normalized = normalize_attribution_period_roles(normalized)
    parent_children = _split_multi_period_compositions(normalized)
    _rewrite_output_requirement_refs(normalized, parent_children)
    return normalized


def normalize_analysis_input(value: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(value)
    if normalized.get("ir_version") == "analysis_ir/1.0":
        return normalize_analysis_ir(normalized)
    if normalized.get("schema_version") == "analysis_bundle/1.0":
        for item in normalized.get("tasks") or []:
            if isinstance(item, dict) and isinstance(item.get("analysis_ir"), dict):
                item["analysis_ir"] = normalize_analysis_ir(item["analysis_ir"])
    return normalized
