#!/usr/bin/env python3
"""Resolve deterministic attribution parameters before strict IR validation."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

from ir_contract_guard import SCENARIO_ROLES, validate_analysis_ir_contract
from time_rollup import normalize_period


BUSINESS_PATCH_KIND = "business_parameter"
PREFLIGHT_VERSION = "business_parameter_preflight/1.0"


class BusinessParameterPreflightError(ValueError):
    code = "BUSINESS_PARAMETER_PATCH_INVALID"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, value: Any, length: int = 16) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _previous_year(period: str) -> str | None:
    parsed = normalize_period(period)
    if parsed is None:
        return None
    _, canonical = parsed
    return f"{int(canonical[:4]) - 1:04d}{canonical[4:]}"


def _previous_period(period: str) -> str | None:
    parsed = normalize_period(period)
    if parsed is None:
        return None
    grain, canonical = parsed
    year = int(canonical[:4])
    if grain == "month":
        month = int(canonical[-2:])
        return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"
    if grain == "quarter":
        quarter = int(canonical[-1])
        return f"{year - 1:04d}-Q4" if quarter == 1 else f"{year:04d}-Q{quarter - 1}"
    if grain == "year":
        return f"{year - 1:04d}"
    week = int(canonical[-2:])
    previous = date.fromisocalendar(year, week, 1) - timedelta(days=7)
    iso = previous.isocalendar()
    return f"{iso.year:04d}-W{iso.week:02d}"


def _query_relation(query: str) -> str | None:
    has_yoy = "同比" in query or "去年同期" in query
    has_previous = any(
        token in query
        for token in ("环比", "上月", "上个月", "上一月", "上期", "上一期")
    )
    if has_yoy and has_previous:
        return "ambiguous"
    if has_yoy:
        return "yoy"
    if has_previous:
        return "previous"
    return None


def _context_fingerprint(ir: dict[str, Any], target: dict[str, Any]) -> str:
    task = ir.get("analysis_task") if isinstance(ir.get("analysis_task"), dict) else {}
    return hashlib.sha256(_canonical_json({
        "task_context": {
            key: task.get(key)
            for key in ("query", "analysis_goal", "periods", "scope", "filters", "assumptions")
        },
        "target": target,
    }).encode("utf-8")).hexdigest()


def _candidate(
    candidate_id: str,
    label: str,
    *,
    value: Any = None,
    requires_value: bool = False,
    patch_field: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"candidate_id": candidate_id, "label": label}
    if value is not None:
        item["value"] = value
    if requires_value:
        item["requires_value"] = True
    if patch_field is not None:
        item["patch_field"] = patch_field
    return item


def _case(
    ir: dict[str, Any],
    target: dict[str, Any],
    *,
    code: str,
    field: str,
    question: str,
    candidates: list[dict[str, Any]],
    impact: str,
) -> dict[str, Any]:
    target_id = str(target.get("target_id") or "")
    fingerprint = _context_fingerprint(ir, target)
    case_id = _stable_id("business_parameter_case", {
        "target_id": target_id,
        "code": code,
        "field": field,
        "context_fingerprint": fingerprint,
    })
    return {
        "case_id": case_id,
        "action": "confirm",
        "kind": BUSINESS_PATCH_KIND,
        "origin": "business_parameter_preflight",
        "parameter_code": code,
        "target_id": target_id,
        "requested_field": field,
        "question": question,
        "impact": impact,
        "context_fingerprint": fingerprint,
        "candidates": candidates[:3],
    }


def _infer_target(ir: dict[str, Any], target: dict[str, Any]) -> None:
    task = ir.get("analysis_task")
    if not isinstance(task, dict):
        return
    task_periods = task.get("periods")
    if not isinstance(task_periods, dict):
        return
    query = str(task.get("query") or "")
    scenario = str(target.get("scenario") or "")

    if not target.get("target_semantics"):
        if scenario == "metric_change":
            target["target_semantics"] = "absolute_delta"
        elif scenario == "yoy_trend_change":
            target["target_semantics"] = "relative_yoy_trend"

    if not target.get("decomposition"):
        if isinstance(target.get("formula"), dict):
            target["decomposition"] = "formula"
        elif isinstance(target.get("group_dimensions"), list) and target["group_dimensions"]:
            target["decomposition"] = "dimension"

    roles = SCENARIO_ROLES.get(scenario)
    if roles is None:
        return
    declared = target.get("periods")
    periods = deepcopy(declared) if isinstance(declared, dict) else {}
    for role in roles:
        if role not in periods and task_periods.get(role) is not None:
            periods[role] = task_periods[role]

    if scenario == "metric_change":
        if "comparison" not in periods and task_periods.get("analysis_last_year") is not None:
            periods["comparison"] = task_periods["analysis_last_year"]
        analysis = periods.get("analysis")
        if "comparison" not in periods and isinstance(analysis, str):
            relation = _query_relation(query)
            inferred = _previous_year(analysis) if relation == "yoy" else (
                _previous_period(analysis) if relation == "previous" else None
            )
            if inferred is not None:
                periods["comparison"] = inferred
    else:
        analysis = periods.get("analysis")
        comparison = periods.get("comparison")
        if "comparison" not in periods and isinstance(analysis, str) and _query_relation(query) == "previous":
            inferred = _previous_period(analysis)
            if inferred is not None:
                periods["comparison"] = inferred
                comparison = inferred
        if "analysis_last_year" not in periods and isinstance(analysis, str):
            inferred = _previous_year(analysis)
            if inferred is not None:
                periods["analysis_last_year"] = inferred
        if "comparison_last_year" not in periods and isinstance(comparison, str):
            inferred = _previous_year(comparison)
            if inferred is not None:
                periods["comparison_last_year"] = inferred

    if periods or isinstance(declared, dict):
        target["periods"] = periods


def _target_cases(ir: dict[str, Any], target: dict[str, Any]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    if (
        ("formula" in target and not isinstance(target.get("formula"), dict))
        or ("factors" in target and not isinstance(target.get("factors"), list))
        or ("periods" in target and not isinstance(target.get("periods"), dict))
    ):
        # Let the existing IR guard preserve its structural error code and
        # fail-fast behavior instead of masking it as a business question.
        return cases
    scenario = str(target.get("scenario") or "")
    if scenario not in SCENARIO_ROLES:
        if not scenario:
            cases.append(_case(
                ir, target,
                code="ATTRIBUTION_SCENARIO_MISSING",
                field="scenario",
                question="请确认需要归因的是指标两期变化，还是两个时期的同比趋势变化",
                candidates=[
                    _candidate("metric_change", "两期指标变化", value="metric_change"),
                    _candidate("yoy_trend_change", "同比趋势变化", value="yoy_trend_change"),
                ],
                impact="归因场景决定所需时期和算子",
            ))
        return cases

    periods = target.get("periods") if isinstance(target.get("periods"), dict) else {}
    task = ir.get("analysis_task") if isinstance(ir.get("analysis_task"), dict) else {}
    task_periods = task.get("periods") if isinstance(task.get("periods"), dict) else {}
    for role in SCENARIO_ROLES[scenario]:
        target_value = periods.get(role)
        task_value = task_periods.get(role)
        if target_value is None or task_value is None:
            continue
        target_period = normalize_period(target_value)
        task_period = normalize_period(task_value)
        if target_period == task_period:
            continue
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_PERIOD_ROLE_CONFLICT",
            field=f"periods.{role}",
            question=f"归因目标与任务对时期角色 {role} 的定义不同，请确认采用哪个时期",
            candidates=[
                _candidate("use_task_period", "采用任务时期", value=task_value),
                _candidate(
                    "use_target_period",
                    "采用归因目标时期",
                    value=target_value,
                    patch_field=f"analysis_task.periods.{role}",
                ),
            ],
            impact="同一时期角色必须在任务和归因目标之间保持一致",
        ))
    missing = [role for role in SCENARIO_ROLES[scenario] if periods.get(role) is None]
    analysis = periods.get("analysis")
    for role in missing:
        candidates: list[dict[str, Any]] = []
        if role == "comparison" and isinstance(analysis, str):
            yoy = _previous_year(analysis)
            previous = _previous_period(analysis)
            if yoy is not None:
                candidates.append(_candidate("yoy", "去年同期", value=yoy))
            if previous is not None and previous != yoy:
                candidates.append(_candidate("previous", "上一期", value=previous))
        if not candidates:
            candidates.append(_candidate("provide_period", "指定时期", requires_value=True))
        cases.append(_case(
            ir, target,
            code=("ATTRIBUTION_ANALYSIS_PERIOD_MISSING" if role == "analysis" else "ATTRIBUTION_COMPARISON_PERIOD_MISSING"),
            field=f"periods.{role}",
            question=f"请确认归因时期角色 {role}",
            candidates=candidates,
            impact="时期角色不完整时无法读取一致的归因输入",
        ))

    seen: dict[str, str] = {}
    for role in SCENARIO_ROLES[scenario]:
        value = periods.get(role)
        if value is None:
            continue
        canonical = normalize_period(value)
        token = canonical[1] if canonical is not None else str(value)
        if token in seen:
            cases.append(_case(
                ir, target,
                code="ATTRIBUTION_PERIOD_ROLE_CONFLICT",
                field=f"periods.{role}",
                question=f"{seen[token]} 与 {role} 指向同一时期，请确认不同的归因时期",
                candidates=[_candidate("provide_period", "指定不同时期", requires_value=True)],
                impact="相同物理时期不能同时承担两个归因角色",
            ))
        else:
            seen[token] = role

    if not target.get("target_semantics"):
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_SEMANTICS_AMBIGUOUS",
            field="target_semantics",
            question="请确认归因目标语义",
            candidates=[_candidate("provide_semantics", "指定目标语义", requires_value=True)],
            impact="目标语义决定算子输出及单位",
        ))

    decomposition = target.get("decomposition")
    has_factors = bool(target.get("factors"))
    formula_declared = "formula" in target
    has_formula = isinstance(target.get("formula"), dict)
    has_formula_inputs = has_factors or has_formula
    has_dimension_inputs = bool(target.get("group_dimensions")) or isinstance(target.get("binding"), dict)
    if not decomposition:
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_DECOMPOSITION_MISSING",
            field="decomposition",
            question="请确认归因采用公式因子拆解还是维度拆解",
            candidates=[
                _candidate("formula", "公式因子拆解", value="formula"),
                _candidate("dimension", "维度拆解", value="dimension"),
            ],
            impact="拆解方式决定归因输入和算子",
        ))
    elif (
        decomposition in {"formula", "addition", "subtraction", "multiplication", "division"}
        and not has_formula_inputs
        and not formula_declared
    ):
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_FACTORS_MISSING",
            field="factors",
            question="请提供完整归因因子",
            candidates=[_candidate("provide_factors", "提供因子", requires_value=True)],
            impact="缺少因子时无法生成可审计的归因计算",
        ))
    elif (
        decomposition in {"formula", "addition", "subtraction", "multiplication", "division"}
        and has_factors
        and not formula_declared
    ):
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_FORMULA_MISSING",
            field="formula",
            question="请确认归因因子之间的计算关系",
            candidates=[_candidate("provide_formula", "提供公式 AST", requires_value=True)],
            impact="只有因子列表时无法唯一确定加减乘除关系",
        ))
    elif (
        decomposition in {"formula", "addition", "subtraction", "multiplication", "division"}
        and has_formula
        and not has_factors
    ):
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_FACTORS_MISSING",
            field="factors",
            question="请提供公式引用的完整归因因子定义",
            candidates=[_candidate("provide_factors", "提供因子", requires_value=True)],
            impact="缺少因子定义时公式引用无法绑定到事实或常量",
        ))
    elif decomposition in {"dimension", "structure"} and not has_dimension_inputs:
        cases.append(_case(
            ir, target,
            code="ATTRIBUTION_GROUP_DIMENSION_MISSING",
            field="group_dimensions",
            question="请确认用于归因拆解的业务维度",
            candidates=[_candidate("provide_group_dimensions", "指定拆解维度", requires_value=True)],
            impact="缺少拆解维度时无法形成分组贡献",
        ))
    return cases


def _set_parameter_field(ir: dict[str, Any], target: dict[str, Any], field: str, value: Any) -> None:
    if field.startswith("analysis_task.periods."):
        role = field.rsplit(".", 1)[1]
        if normalize_period(value) is None:
            raise BusinessParameterPreflightError(
                f"business parameter patch contains an invalid period: {value!r}",
                {"field": field, "value": value},
            )
        task = ir.get("analysis_task")
        if not isinstance(task, dict):
            raise BusinessParameterPreflightError("analysis_task must be an object")
        periods = task.setdefault("periods", {})
        if not isinstance(periods, dict):
            raise BusinessParameterPreflightError("analysis_task.periods must be an object")
        periods[role] = value
        return
    if field.startswith("periods."):
        role = field.split(".", 1)[1]
        if normalize_period(value) is None:
            raise BusinessParameterPreflightError(
                f"business parameter patch contains an invalid period: {value!r}",
                {"field": field, "value": value},
            )
        periods = target.setdefault("periods", {})
        if not isinstance(periods, dict):
            raise BusinessParameterPreflightError("attribution target periods must be an object")
        periods[role] = value
        return
    if field in {"scenario", "target_semantics", "decomposition"}:
        if not isinstance(value, str) or not value:
            raise BusinessParameterPreflightError(f"business parameter patch {field} must be non-empty")
        target[field] = value
        return
    if field == "group_dimensions":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            raise BusinessParameterPreflightError("group_dimensions patch must be a non-empty string array")
        target[field] = deepcopy(value)
        return
    if field == "formula":
        if not isinstance(value, dict):
            raise BusinessParameterPreflightError("formula patch must be an object AST")
        target[field] = deepcopy(value)
        return
    if field == "factors":
        if not isinstance(value, list) or not value or not all(isinstance(item, dict) for item in value):
            raise BusinessParameterPreflightError("factors patch must be a non-empty object array")
        target[field] = deepcopy(value)
        return
    raise BusinessParameterPreflightError(
        f"business parameter patch cannot modify {field!r}", {"field": field}
    )


def _apply_patch(ir: dict[str, Any], patch: dict[str, Any], cases: list[dict[str, Any]]) -> bool:
    case_id = str(patch.get("case_id") or patch.get("clarification_id") or "")
    case = next((item for item in cases if item.get("case_id") == case_id), None)
    if case is None:
        raise BusinessParameterPreflightError(
            "business parameter patch is stale or references an unknown case",
            {"case_id": case_id},
        )
    if patch.get("context_fingerprint") != case.get("context_fingerprint"):
        raise BusinessParameterPreflightError(
            "business parameter patch context fingerprint is stale",
            {"case_id": case_id},
        )
    candidate_id = str(patch.get("candidate_id") or "")
    candidate = next(
        (item for item in case.get("candidates") or [] if item.get("candidate_id") == candidate_id),
        None,
    )
    if candidate is None:
        raise BusinessParameterPreflightError(
            "business parameter patch references an unknown candidate",
            {"case_id": case_id, "candidate_id": candidate_id},
        )
    value = candidate.get("value")
    if candidate.get("requires_value") is True:
        value = patch.get("value")
    targets = [
        item for item in ir.get("attribution_targets") or []
        if isinstance(item, dict) and str(item.get("target_id") or "") == case.get("target_id")
    ]
    if len(targets) != 1:
        raise BusinessParameterPreflightError(
            "business parameter patch target is missing or not unique",
            {"target_id": case.get("target_id")},
        )
    field = str(candidate.get("patch_field") or case["requested_field"])
    _set_parameter_field(ir, targets[0], field, value)
    return True


def preflight_business_parameters(ir: dict[str, Any]) -> dict[str, Any]:
    """Return a task-local decision without consulting source metadata."""
    if not isinstance(ir, dict):
        raise BusinessParameterPreflightError("analysis IR must be an object")
    working = deepcopy(ir)
    raw_patches = working.get("resolution_patches")
    patches = [
        deepcopy(item) for item in raw_patches or []
        if isinstance(item, dict) and item.get("kind") == BUSINESS_PATCH_KIND
    ] if isinstance(raw_patches, list) else []
    if isinstance(raw_patches, list):
        retained = [
            deepcopy(item) for item in raw_patches
            if not isinstance(item, dict) or item.get("kind") != BUSINESS_PATCH_KIND
        ]
        if retained:
            working["resolution_patches"] = retained
        else:
            working.pop("resolution_patches", None)

    # Preserve strict reference and AST failures before business questions.
    # A missing formula with declared factors is intentionally handled below
    # as a business parameter, so the probe supplies a neutral one-use AST.
    structural_probe = deepcopy(working)
    for target in structural_probe.get("attribution_targets") or []:
        if not isinstance(target, dict):
            continue
        factors = target.get("factors")
        if factors in (None, []) and isinstance(target.get("formula"), dict):
            target.pop("formula", None)
            continue
        if "formula" in target or not isinstance(factors, list) or not factors:
            continue
        refs = [
            {"factor_ref": item.get("factor_id")}
            for item in factors
            if isinstance(item, dict)
        ]
        if refs:
            target["formula"] = refs[0] if len(refs) == 1 else {"op": "multiply", "args": refs}
    validate_analysis_ir_contract(structural_probe, validate_periods=False)

    applied_patch_ids: set[str] = set()
    for _ in range(len(patches) + 1):
        for target in working.get("attribution_targets") or []:
            if isinstance(target, dict):
                _infer_target(working, target)
        cases = [
            case
            for target in working.get("attribution_targets") or []
            if isinstance(target, dict)
            for case in _target_cases(working, target)
        ]
        pending = [
            patch for patch in patches
            if str(patch.get("case_id") or patch.get("clarification_id") or "") not in applied_patch_ids
        ]
        if not pending:
            return {
                "schema_version": PREFLIGHT_VERSION,
                "status": "waiting_confirmation" if cases else "continue",
                "analysis_ir": working,
                "resolution_cases": cases,
            }
        patch = pending[0]
        _apply_patch(working, patch, cases)
        applied_patch_ids.add(str(patch.get("case_id") or patch.get("clarification_id") or ""))

    raise BusinessParameterPreflightError("business parameter patch resolution did not converge")
