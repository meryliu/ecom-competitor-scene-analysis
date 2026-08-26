#!/usr/bin/env python3
"""Fail-fast structural validation for attribution analysis IR."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any


SCENARIO_ROLES = {
    "metric_change": ("analysis", "comparison"),
    "yoy_trend_change": (
        "analysis",
        "analysis_last_year",
        "comparison",
        "comparison_last_year",
    ),
}


class IRContractError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def normalize_attribution_period_roles(ir: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize target-local role aliases without changing derived roles."""
    normalized = deepcopy(ir)
    task = normalized.get("analysis_task")
    if not isinstance(task, dict):
        return normalized
    task_periods = task.setdefault("periods", {})
    if not isinstance(task_periods, dict):
        return normalized
    for index, target in enumerate(normalized.get("attribution_targets") or []):
        if not isinstance(target, dict):
            continue
        declared = target.get("periods")
        periods = deepcopy(declared) if isinstance(declared, dict) else {}
        scenario = str(target.get("scenario") or "")
        if scenario == "metric_change":
            comparison = periods.get("comparison")
            legacy_comparison = periods.get("analysis_last_year")
            if comparison is not None and legacy_comparison is not None:
                if comparison != legacy_comparison:
                    raise IRContractError(
                        "ATTR-IR-006",
                        f"attribution_targets[{index}] has conflicting comparison roles",
                        {
                            "target_id": target.get("target_id"),
                            "comparison": comparison,
                            "analysis_last_year": legacy_comparison,
                        },
                    )
                periods.pop("analysis_last_year", None)
            elif comparison is None and legacy_comparison is not None:
                periods["comparison"] = periods.pop("analysis_last_year")
            if "analysis" not in periods and task_periods.get("analysis") is not None:
                periods["analysis"] = task_periods["analysis"]
            if "comparison" not in periods:
                inherited = task_periods.get("comparison", task_periods.get("analysis_last_year"))
                if inherited is not None:
                    periods["comparison"] = inherited
        else:
            for role in SCENARIO_ROLES.get(scenario, ()):
                if role not in periods and task_periods.get(role) is not None:
                    periods[role] = task_periods[role]
        if periods or isinstance(declared, dict):
            target["periods"] = periods
            for role, value in periods.items():
                inherited = task_periods.get(role)
                if inherited is not None and inherited != value:
                    raise IRContractError(
                        "ATTR-IR-006",
                        f"attribution_targets[{index}].periods.{role} conflicts with analysis_task.periods",
                        {
                            "target_id": target.get("target_id"),
                            "role": role,
                            "target_period": value,
                            "task_period": inherited,
                        },
                    )
                task_periods.setdefault(role, value)
    return normalized


def _formula_factor_refs(expression: Any, path: str) -> list[str]:
    if not isinstance(expression, dict):
        raise IRContractError(
            "ATTR-IR-002", f"{path} must be an object AST", {"path": path}
        )
    if "factor_ref" in expression:
        factor_ref = expression.get("factor_ref")
        if not isinstance(factor_ref, str) or not factor_ref:
            raise IRContractError(
                "ATTR-IR-004", f"{path}.factor_ref must be non-empty", {"path": path}
            )
        return [factor_ref]
    args = expression.get("args")
    if not isinstance(expression.get("op"), str) or not isinstance(args, list) or not args:
        raise IRContractError(
            "ATTR-IR-002", f"{path} must be a formula AST", {"path": path}
        )
    refs: list[str] = []
    for index, arg in enumerate(args):
        refs.extend(_formula_factor_refs(arg, f"{path}.args[{index}]"))
    return refs


def validate_analysis_ir_contract(
    ir: dict[str, Any], *, validate_periods: bool = True
) -> None:
    """Validate source-independent attribution invariants before Provider resolve."""
    if not isinstance(ir, dict) or ir.get("ir_version") != "analysis_ir/1.0":
        raise IRContractError(
            "ATTR-IR-000", "analysis IR must use ir_version='analysis_ir/1.0'"
        )
    if "attribution_targets" not in ir or not isinstance(ir.get("attribution_targets"), list):
        raise IRContractError(
            "ATTR-IR-000", "$.attribution_targets must be an array"
        )
    task = ir.get("analysis_task")
    metrics = task.get("metrics") if isinstance(task, dict) else None
    metric_ids = {
        str(item.get("metric_id"))
        for item in metrics or []
        if isinstance(item, dict) and item.get("metric_id")
    }
    task_periods = task.get("periods") if isinstance(task, dict) else {}
    task_periods = task_periods if isinstance(task_periods, dict) else {}
    for target_index, target in enumerate(ir["attribution_targets"]):
        path = f"attribution_targets[{target_index}]"
        if not isinstance(target, dict):
            raise IRContractError("ATTR-IR-000", f"{path} must be an object")
        metric_ref = target.get("metric_ref")
        if not isinstance(metric_ref, str) or not metric_ref:
            raise IRContractError(
                "ATTR-IR-001", f"{path}.metric_ref is required", {"path": path}
            )
        if metric_ref not in metric_ids:
            raise IRContractError(
                "ATTR-IR-003",
                f"{path}.metric_ref references an unknown metric",
                {"path": path, "metric_ref": metric_ref},
            )
        roles = SCENARIO_ROLES.get(str(target.get("scenario") or ""), ())
        periods = target.get("periods") if isinstance(target.get("periods"), dict) else {}
        effective_periods = {role: periods.get(role, task_periods.get(role)) for role in roles}
        missing_roles = [role for role, value in effective_periods.items() if value is None]
        if validate_periods and missing_roles:
            raise IRContractError(
                "ATTR-IR-005",
                f"{path} is missing attribution period roles: {missing_roles}",
                {"path": path, "missing_roles": missing_roles},
            )
        if validate_periods:
            seen_periods: dict[str, str] = {}
            for role, value in effective_periods.items():
                period = str(value)
                if period in seen_periods:
                    raise IRContractError(
                        "ATTR-IR-006",
                        f"{path} maps multiple roles to period {period}",
                        {"path": path, "roles": [seen_periods[period], role], "period": period},
                    )
                seen_periods[period] = role

        factors = target.get("factors")
        if factors in (None, []):
            if "formula" in target:
                refs = _formula_factor_refs(target.get("formula"), f"{path}.formula")
                if refs:
                    raise IRContractError(
                        "ATTR-IR-004",
                        f"{path}.formula references factors but factors are missing",
                        {"path": path, "unknown_factor_refs": sorted(set(refs))},
                    )
            continue
        if not isinstance(factors, list):
            raise IRContractError("ATTR-IR-000", f"{path}.factors must be an array")
        factor_ids: set[str] = set()
        for factor_index, factor in enumerate(factors):
            factor_path = f"{path}.factors[{factor_index}]"
            if not isinstance(factor, dict):
                raise IRContractError("ATTR-IR-000", f"{factor_path} must be an object")
            factor_id = factor.get("factor_id")
            if not isinstance(factor_id, str) or not factor_id or factor_id in factor_ids:
                raise IRContractError(
                    "ATTR-IR-004", f"{factor_path}.factor_id must be unique and non-empty"
                )
            factor_ids.add(factor_id)
            kind = factor.get("kind") or ("metric" if factor.get("metric_ref") is not None else None)
            if kind == "metric":
                factor_metric_ref = factor.get("metric_ref")
                if not isinstance(factor_metric_ref, str) or not factor_metric_ref:
                    raise IRContractError(
                        "ATTR-IR-001", f"{factor_path}.metric_ref is required"
                    )
                if factor_metric_ref not in metric_ids:
                    raise IRContractError(
                        "ATTR-IR-003",
                        f"{factor_path}.metric_ref references an unknown metric",
                        {"path": factor_path, "metric_ref": factor_metric_ref},
                    )
            if kind == "literal":
                values = factor.get("values_by_period_role")
                if values is None and "literal" in factor:
                    continue
                if not isinstance(values, dict):
                    raise IRContractError(
                        "ATTR-IR-005", f"{factor_path}.values_by_period_role is required"
                    )
                missing = [role for role in roles if role not in values]
                if missing:
                    raise IRContractError(
                        "ATTR-IR-005",
                        f"{factor_path} is missing literal period roles: {missing}",
                        {"path": factor_path, "missing_roles": missing},
                    )
        formula = target.get("formula")
        refs = _formula_factor_refs(formula, f"{path}.formula")
        unresolved = sorted(set(refs) - factor_ids)
        unreferenced = sorted(factor_ids - set(refs))
        duplicate_refs = sorted(
            factor_ref for factor_ref, count in Counter(refs).items() if count > 1
        )
        if unresolved or unreferenced or duplicate_refs:
            raise IRContractError(
                "ATTR-IR-004",
                f"{path}.formula factor set must reference every factor exactly once",
                {
                    "path": path,
                    "unknown_factor_refs": unresolved,
                    "unreferenced_factor_ids": unreferenced,
                    "duplicate_factor_refs": duplicate_refs,
                },
            )


def validate_analysis_input_contract(value: dict[str, Any]) -> None:
    if value.get("ir_version") == "analysis_ir/1.0":
        validate_analysis_ir_contract(value)
        return
    if value.get("schema_version") == "analysis_bundle/1.0":
        for item in value.get("tasks") or []:
            if isinstance(item, dict) and isinstance(item.get("analysis_ir"), dict):
                validate_analysis_ir_contract(item["analysis_ir"])
