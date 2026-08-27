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


def _split_multi_period_compositions(ir: dict[str, Any]) -> None:
    """Expand declared compositions to the compiler's one-period shape."""
    expanded: list[Any] = []
    for requirement in ir.get("metric_compositions") or []:
        if not isinstance(requirement, dict):
            expanded.append(requirement)
            continue
        roles = requirement.get("period_roles")
        if not isinstance(roles, list) or len(roles) <= 1:
            expanded.append(requirement)
            continue
        base_id = str(requirement.get("requirement_id") or "composition")
        for role in roles:
            item = deepcopy(requirement)
            item["period_roles"] = [role]
            item["requirement_id"] = f"{base_id}__{_stable_suffix([base_id, str(role)])}"
            item["normalized_from_requirement_id"] = base_id
            expanded.append(item)
    ir["metric_compositions"] = expanded


def normalize_analysis_ir(ir: dict[str, Any]) -> dict[str, Any]:
    """Return canonical IR without resolving business or source semantics."""
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
    _split_multi_period_compositions(normalized)
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
