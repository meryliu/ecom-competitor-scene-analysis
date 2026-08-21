#!/usr/bin/env python3
"""Attribution calculation engine.

Input: JSON file with operator/scenario/metric/factors/groups.
Output: JSON with summary, row-level contribution and warnings.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .identity import identity
from .registry import REGISTRY_NAME, REGISTRY_VERSION, get_operator, route_operator
from .semantics import add_ranking, add_semantic_labels

Number = Optional[float]
COVERAGE_TOLERANCE = 1e-10


class AttributionError(ValueError):
    pass


def to_float(value: Any, field: str) -> float:
    if value is None:
        raise AttributionError(f"missing required field: {field}")
    try:
        if isinstance(value, str):
            raw = value.strip().replace(",", "")
            lowered = raw.lower().replace(" ", "")
            for suffix in ("percentagepoints", "percentagepoint", "pct", "pp"):
                if lowered.endswith(suffix):
                    return float(lowered[: -len(suffix)]) / 100
            if lowered.endswith("%"):
                return float(lowered[:-1]) / 100
            return float(raw)
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AttributionError(f"field {field} must be numeric or percentage-like string, got {value!r}") from exc


def get_num(obj: Dict[str, Any], key: str, prefix: str) -> float:
    return to_float(obj.get(key), f"{prefix}.{key}")


def safe_div(numerator: float, denominator: float) -> Number:
    if denominator == 0:
        return None
    return numerator / denominator


def change_rate(analysis: float, comparison: float) -> Number:
    if comparison == 0:
        return None
    return analysis / comparison - 1


def pct_or_none(value: Number) -> Number:
    return None if value is None else value


def ensure_positive(value: float, field: str) -> None:
    if value <= 0:
        raise AttributionError(f"{field} must be > 0 for logarithmic decomposition, got {value}")


def log_ratio(analysis: float, comparison: float, field: str) -> float:
    ensure_positive(analysis, f"{field}.analysis_value")
    ensure_positive(comparison, f"{field}.comparison_value")
    return math.log(analysis / comparison)


def sum_values(items: Iterable[Dict[str, Any]], key: str) -> float:
    total = 0.0
    for idx, item in enumerate(items):
        total += get_num(item, key, f"items[{idx}]")
    return total


def round_float(value: Any, digits: int = 12) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, digits)
    if isinstance(value, list):
        return [round_float(v, digits) for v in value]
    if isinstance(value, dict):
        return {k: round_float(v, digits) for k, v in value.items()}
    return value


def infer_operator(payload: Dict[str, Any]) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    explicit = payload.get("operator")
    if explicit:
        return str(explicit), warnings

    scenario = payload.get("scenario")
    metric_object = payload.get("metric_object")
    decomposition = payload.get("decomposition")
    if not scenario or not decomposition:
        raise AttributionError("operator is missing; scenario and decomposition are required for routing")

    try:
        operator = route_operator(str(scenario), str(metric_object), str(decomposition))
    except ValueError as exc:
        raise AttributionError(str(exc)) from exc
    if scenario == "metric_change" and metric_object == "ratio" and decomposition == "dimension":
        warnings.append("metric_object=ratio with decomposition=dimension is routed to structure_change by default")
    if scenario == "yoy_trend_change" and metric_object == "ratio" and decomposition == "dimension":
        warnings.append("metric_object=ratio with decomposition=dimension is routed to structure_yoy_trend by default")
    return operator, warnings


def metric_values(payload: Dict[str, Any], items: Optional[List[Dict[str, Any]]] = None) -> Tuple[float, float, Dict[str, Any]]:
    metric = payload.get("metric") or {}
    meta: Dict[str, Any] = {}
    if metric.get("analysis_value") is None:
        if items is None:
            raise AttributionError("missing required field: metric.analysis_value")
        analysis = sum_values(items, "analysis_value")
        meta["analysis_value_source"] = "sum(items.analysis_value)"
    else:
        analysis = get_num(metric, "analysis_value", "metric")
    if metric.get("comparison_value") is None:
        if items is None:
            raise AttributionError("missing required field: metric.comparison_value")
        comparison = sum_values(items, "comparison_value")
        meta["comparison_value_source"] = "sum(items.comparison_value)"
    else:
        comparison = get_num(metric, "comparison_value", "metric")
    return analysis, comparison, meta


def make_summary(metric_name: str, analysis: float, comparison: float, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    delta = analysis - comparison
    contribution_sum = sum(float(r.get("contribution_value") or 0.0) for r in rows)
    return {
        "metric_name": metric_name,
        "analysis_value": analysis,
        "comparison_value": comparison,
        "change_value": delta,
        "change_rate": change_rate(analysis, comparison),
        "contribution_sum": contribution_sum,
        "residual": delta - contribution_sum,
        "contribution_rate_available": delta != 0,
    }


def factor_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("factors")
    if not isinstance(items, list) or not items:
        raise AttributionError("factors must be a non-empty list")
    return items


def group_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("groups")
    if not isinstance(items, list) or not items:
        raise AttributionError("groups must be a non-empty list")
    return items


def coverage_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    value = payload.get("coverage")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise AttributionError("coverage must be an object")
    return value


def residual_name(payload: Dict[str, Any]) -> str:
    sparse = payload.get("sparse_policy") or {}
    value = sparse.get("other_name", coverage_config(payload).get("residual_name", "其他/未覆盖")) if isinstance(sparse, dict) else "其他/未覆盖"
    return str(value) if value is not None and str(value) else "其他/未覆盖"


def has_any_metric_fields(metric: Dict[str, Any], fields: Iterable[str]) -> bool:
    return any(metric.get(field) is not None for field in fields)


def require_parent_metric(metric: Dict[str, Any], fields: List[str], operator: str) -> None:
    missing = [field for field in fields if metric.get(field) is None]
    if missing:
        raise AttributionError(
            f"partial coverage for {operator} requires complete parent metric fields: {', '.join(missing)}"
        )


def nonnegative_residual(parent: float, selected: float, field: str) -> float:
    residual = parent - selected
    tolerance = COVERAGE_TOLERANCE * max(1.0, abs(parent), abs(selected))
    if residual < -tolerance:
        raise AttributionError(
            f"selected groups exceed parent total for {field}: parent={parent}, selected={selected}"
        )
    return 0.0 if abs(residual) <= tolerance else residual


def coverage_summary(
    payload: Dict[str, Any],
    parent: Dict[str, float],
    selected: Dict[str, float],
    residual: Dict[str, float],
) -> Dict[str, Any]:
    fields = list(parent)
    retained = {
        field: safe_div(selected[field], parent[field]) if parent[field] != 0 else None
        for field in fields
    }
    return {
        "mode": "auto_residual",
        "residual_added": any(abs(value) > COVERAGE_TOLERANCE for value in residual.values()),
        "residual_name": residual_name(payload),
        "retained_coverage": retained,
        "residual_totals": residual,
    }


def sparse_policy_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw = payload.get("sparse_policy")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AttributionError("sparse_policy must be an object")
    if isinstance(raw.get("epsilon", payload.get("epsilon", 1e-9)), bool):
        raise AttributionError("sparse_policy.epsilon must be numeric")
    try:
        epsilon = float(raw.get("epsilon", payload.get("epsilon", 1e-9)))
    except (TypeError, ValueError) as exc:
        raise AttributionError("sparse_policy.epsilon must be numeric") from exc
    if not math.isfinite(epsilon) or epsilon <= 0:
        raise AttributionError("sparse_policy.epsilon must be finite and > 0")
    merge_rules = raw.get("merge_rules", [])
    rollup_path = raw.get("rollup_path", [])
    if not isinstance(merge_rules, list):
        raise AttributionError("sparse_policy.merge_rules must be an array")
    if not isinstance(rollup_path, list):
        raise AttributionError("sparse_policy.rollup_path must be an array")
    for rule_index, rule in enumerate(merge_rules):
        if not isinstance(rule, dict):
            raise AttributionError(f"sparse_policy.merge_rules[{rule_index}] must be an object")
        members = rule.get("members")
        if not isinstance(members, list) or not members:
            raise AttributionError(f"sparse_policy.merge_rules[{rule_index}].members must be non-empty")
    for level_index, level in enumerate(rollup_path):
        if (
            not isinstance(level, list)
            or not level
            or not all(isinstance(key, str) and key for key in level)
        ):
            raise AttributionError(
                f"sparse_policy.rollup_path[{level_index}] must be a non-empty string array"
            )
    strategy = raw.get("strategy", "merge_other_then_epsilon")
    if strategy != "merge_other_then_epsilon":
        raise AttributionError("sparse_policy.strategy must be 'merge_other_then_epsilon'")
    reference_rate_policy = raw.get("reference_rate_policy", "paired_observed_self_rate")
    if reference_rate_policy != "paired_observed_self_rate":
        raise AttributionError(
            "sparse_policy.reference_rate_policy must be 'paired_observed_self_rate'"
        )
    parent_dimensions = raw.get("parent_dimensions", [])
    if (
        not isinstance(parent_dimensions, list)
        or not all(isinstance(item, str) and item for item in parent_dimensions)
    ):
        raise AttributionError("sparse_policy.parent_dimensions must be a string array")
    other_name = raw.get("other_name", residual_name(payload))
    if not isinstance(other_name, str) or not other_name:
        raise AttributionError("sparse_policy.other_name must be a non-empty string")
    return {
        **raw,
        "strategy": strategy,
        "other_name": other_name,
        "epsilon": epsilon,
        "reference_rate_policy": reference_rate_policy,
        "merge_rules": merge_rules,
        "rollup_path": rollup_path,
        "parent_dimensions": parent_dimensions,
    }


def ratio_roles(fields: List[str]) -> List[str]:
    return [field[: -len("_denominator")] for field in fields if field.endswith("_denominator")]


def ratio_field(role: str, component: str) -> str:
    return f"{role}_{component}"


def ratio_group_sparse(group: Dict[str, Any], roles: List[str], prefix: str = "groups") -> bool:
    return any(get_num(group, ratio_field(role, "denominator"), prefix) == 0 for role in roles)


def validate_ratio_group(group: Dict[str, Any], roles: List[str], prefix: str) -> None:
    for role in roles:
        numerator = get_num(group, ratio_field(role, "numerator"), prefix)
        denominator = get_num(group, ratio_field(role, "denominator"), prefix)
        if denominator < 0:
            raise AttributionError(f"{prefix}.{role}_denominator must not be negative")
        if denominator == 0 and numerator != 0:
            raise AttributionError(
                f"{prefix}.{role}_numerator must be 0 when {role}_denominator is 0, got {numerator}"
            )


def group_dimensions(group: Dict[str, Any]) -> Dict[str, Any]:
    value = group.get("dimensions") or {}
    if not isinstance(value, dict):
        raise AttributionError("groups[].dimensions must be an object")
    return value


def validate_single_parent_partition(groups: List[Dict[str, Any]], parent_dimensions: List[str]) -> None:
    if not parent_dimensions:
        return
    parents: set[str] = set()
    for index, group in enumerate(groups):
        dimensions = group_dimensions(group)
        missing = [key for key in parent_dimensions if key not in dimensions]
        if missing:
            raise AttributionError(
                f"groups[{index}] is missing sparse_policy.parent_dimensions: {missing}"
            )
        parent = {key: dimensions[key] for key in parent_dimensions}
        parents.add(json.dumps(parent, ensure_ascii=False, sort_keys=True))
    if len(parents) > 1:
        raise AttributionError(
            "ratio structure payload contains multiple parents; execute one payload per parent group"
        )


def validate_ratio_parent(parent: Dict[str, float], roles: List[str], operator: str) -> None:
    for role in roles:
        denominator = parent[ratio_field(role, "denominator")]
        if denominator <= 0:
            raise AttributionError(
                f"overall {role}_denominator must be > 0 for {operator}, got {denominator}"
            )


def parent_dimensions_for_groups(
    groups: List[Dict[str, Any]], policy: Dict[str, Any]
) -> Dict[str, Any]:
    keys = policy.get("parent_dimensions") or []
    if not keys or not groups:
        return {}
    dimensions = group_dimensions(groups[0])
    return {key: dimensions[key] for key in keys if key in dimensions}


def aggregate_ratio_groups(
    groups: List[Dict[str, Any]],
    fields: List[str],
    name: str,
    dimensions: Optional[Dict[str, Any]] = None,
    *,
    is_other: bool = False,
) -> Dict[str, Any]:
    output: Dict[str, Any] = {"name": name, "dimensions": dict(dimensions or {})}
    for field in fields:
        output[field] = sum(get_num(group, field, f"groups[{idx}]") for idx, group in enumerate(groups))
    members: List[str] = []
    reasons: List[str] = []
    for group in groups:
        nested = group.get("merged_members")
        members.extend(str(value) for value in nested if value is not None) if isinstance(nested, list) else members.append(str(group.get("name", "group")))
        nested_reasons = group.get("merge_reasons")
        if isinstance(nested_reasons, list):
            reasons.extend(str(value) for value in nested_reasons)
    output["merged_members"] = sorted(set(members))
    if reasons:
        output["merge_reasons"] = sorted(set(reasons))
    if is_other:
        output["is_other"] = True
    return output


def merge_rule_matches(group: Dict[str, Any], member: Any) -> bool:
    if isinstance(member, str):
        return str(group.get("name")) == member
    if not isinstance(member, dict):
        raise AttributionError("sparse_policy.merge_rules[].members must contain strings or objects")
    if member.get("name") is not None and str(group.get("name")) != str(member["name"]):
        return False
    selected_dimensions = member.get("dimensions") or {}
    if not isinstance(selected_dimensions, dict):
        raise AttributionError("sparse_policy merge member dimensions must be an object")
    dimensions = group_dimensions(group)
    return all(dimensions.get(key) == value for key, value in selected_dimensions.items())


def apply_explicit_merge_rules(
    groups: List[Dict[str, Any]], fields: List[str], policy: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    output = [dict(group) for group in groups]
    applied: List[Dict[str, Any]] = []
    consumed_names: set[int] = set()
    for rule_index, rule in enumerate(policy["merge_rules"]):
        if not isinstance(rule, dict):
            raise AttributionError("sparse_policy.merge_rules must contain objects")
        target_name = str(rule.get("target_name", policy["other_name"]))
        members = rule.get("members", [])
        if not isinstance(members, list) or not members:
            raise AttributionError(f"sparse_policy.merge_rules[{rule_index}].members must be non-empty")
        selected_indexes = {
            index for index, group in enumerate(output)
            if any(merge_rule_matches(group, member) for member in members)
        }
        selected_indexes.update(
            index for index, group in enumerate(output) if str(group.get("name")) == target_name
        )
        if selected_indexes & consumed_names:
            raise AttributionError("a group cannot be consumed by multiple sparse merge rules")
        if not selected_indexes:
            raise AttributionError(f"sparse merge rule matched no groups: {target_name}")
        selected = [output[index] for index in sorted(selected_indexes)]
        dimensions = rule.get("target_dimensions")
        if dimensions is None:
            existing = next((group for group in selected if str(group.get("name")) == target_name), None)
            dimensions = (
                group_dimensions(existing)
                if existing is not None
                else parent_dimensions_for_groups(selected, policy)
            )
        if not isinstance(dimensions, dict):
            raise AttributionError("sparse merge rule target_dimensions must be an object")
        merged = aggregate_ratio_groups(
            selected,
            fields,
            target_name,
            dimensions,
            is_other=bool(rule.get("is_other", target_name in {"其他", "其他/未覆盖", policy["other_name"]})),
        )
        merged["merge_reasons"] = sorted(set(merged.get("merge_reasons", []) + ["query_defined_merge"]))
        output = [group for index, group in enumerate(output) if index not in selected_indexes] + [merged]
        consumed_names = set()
        applied.append({"target_name": target_name, "members": merged["merged_members"]})
    return output, applied


def rollup_ratio_groups(
    groups: List[Dict[str, Any]], fields: List[str], roles: List[str], policy: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Optional[List[str]]]:
    if not policy["rollup_path"] or not any(ratio_group_sparse(group, roles) for group in groups):
        return groups, None
    current = groups
    selected_level: Optional[List[str]] = None
    for level_index, level in enumerate(policy["rollup_path"]):
        if not isinstance(level, list) or not all(isinstance(key, str) and key for key in level):
            raise AttributionError(f"sparse_policy.rollup_path[{level_index}] must be a string array")
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        bucket_dimensions: Dict[str, Dict[str, Any]] = {}
        for group in groups:
            dimensions = group_dimensions(group)
            missing = [key for key in level if key not in dimensions]
            if missing:
                raise AttributionError(f"rollup dimensions are missing from groups: {missing}")
            selected = {key: dimensions[key] for key in level}
            encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True)
            buckets.setdefault(encoded, []).append(group)
            bucket_dimensions[encoded] = selected
        current = []
        for encoded in sorted(buckets):
            dimensions = bucket_dimensions[encoded]
            name = "全部" if not level else " | ".join(f"{key}={dimensions[key]}" for key in level)
            merged = aggregate_ratio_groups(buckets[encoded], fields, name, dimensions)
            merged["merge_reasons"] = sorted(set(merged.get("merge_reasons", []) + ["query_defined_rollup"]))
            current.append(merged)
        selected_level = list(level)
        if not any(ratio_group_sparse(group, roles) for group in current):
            break
    return current, selected_level


def is_other_group(group: Dict[str, Any], other_name: str) -> bool:
    return bool(group.get("is_other")) or str(group.get("name")) in {other_name, "其他", "其他/未覆盖"}


def merge_sparse_to_other(
    groups: List[Dict[str, Any]], fields: List[str], roles: List[str], policy: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    sparse = [group for group in groups if ratio_group_sparse(group, roles)]
    if not sparse:
        return groups, []
    existing_other = [group for group in groups if is_other_group(group, policy["other_name"])]
    selected_ids = {id(group) for group in sparse + existing_other}
    selected = [group for group in groups if id(group) in selected_ids]
    if not selected:
        return groups, []
    stable = [group for group in groups if id(group) not in selected_ids]
    output_name = str(existing_other[0].get("name")) if existing_other else policy["other_name"]
    merged = aggregate_ratio_groups(
        selected,
        fields,
        output_name,
        parent_dimensions_for_groups(selected, policy),
        is_other=True,
    )
    merged["merge_reasons"] = sorted(set(merged.get("merge_reasons", []) + ["zero_denominator_other_merge"]))
    return stable + [merged], merged["merged_members"]


def merge_residual_into_other(
    groups: List[Dict[str, Any]], fields: List[str], residual: Dict[str, float], policy: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], bool]:
    if not any(abs(value) > COVERAGE_TOLERANCE for value in residual.values()):
        return groups, False
    residual_group: Dict[str, Any] = {
        "name": policy["other_name"],
        "dimensions": parent_dimensions_for_groups(groups, policy),
        "is_other": True,
        "merged_members": ["其他/未覆盖残差"],
        "merge_reasons": ["uncovered_residual"],
        **residual,
    }
    other = [group for group in groups if is_other_group(group, policy["other_name"])]
    remaining = [group for group in groups if not is_other_group(group, policy["other_name"])]
    output_name = str(other[0].get("name")) if other else policy["other_name"]
    merge_inputs = other + [residual_group]
    merged = aggregate_ratio_groups(
        merge_inputs,
        fields,
        output_name,
        parent_dimensions_for_groups(merge_inputs, policy),
        is_other=True,
    )
    merged["merge_reasons"] = sorted(set(merged.get("merge_reasons", []) + ["uncovered_residual"]))
    return remaining + [merged], True


def prepare_effective_ratio_rates(
    group: Dict[str, Any], pairs: List[Tuple[str, str]], policy: Dict[str, Any]
) -> Dict[str, Any]:
    effective: Dict[str, Optional[float]] = {}
    adjustments: Dict[str, Dict[str, Any]] = {}
    inactive_pairs: List[List[str]] = []
    for left, right in pairs:
        left_n = get_num(group, ratio_field(left, "numerator"), "group")
        left_p = get_num(group, ratio_field(left, "denominator"), "group")
        right_n = get_num(group, ratio_field(right, "numerator"), "group")
        right_p = get_num(group, ratio_field(right, "denominator"), "group")
        if left_p > 0 and right_p > 0:
            effective[left], effective[right] = left_n / left_p, right_n / right_p
            continue
        if left_p == 0 and right_p == 0:
            effective[left], effective[right] = None, None
            inactive_pairs.append([left, right])
            continue
        observed_role, missing_role = (left, right) if left_p > 0 else (right, left)
        observed_n, observed_p = (left_n, left_p) if left_p > 0 else (right_n, right_p)
        reference_rate = observed_n / observed_p
        effective[observed_role] = reference_rate
        effective[missing_role] = reference_rate
        adjustments[missing_role] = {
            "epsilon": policy["epsilon"],
            "effective_numerator": policy["epsilon"] * reference_rate,
            "effective_denominator": policy["epsilon"],
            "reference_role": observed_role,
            "reference_rate": reference_rate,
        }
    return {
        "rates": effective,
        "epsilon_adjustments": adjustments,
        "inactive_pairs": inactive_pairs,
    }
def complete_volume_groups(
    payload: Dict[str, Any],
    groups: List[Dict[str, Any]],
    fields: List[str],
    operator: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, float], Dict[str, Any] | None]:
    metric = payload.get("metric") or {}
    parent_supplied = all(metric.get(field) is not None for field in fields)
    parent_partially_supplied = has_any_metric_fields(metric, fields)
    coverage = coverage_config(payload)
    explicit_coverage = coverage.get("mode") == "auto_residual" or payload.get("partial_coverage") is True
    wants_residual = parent_supplied or explicit_coverage
    if wants_residual:
        if not parent_supplied:
            require_parent_metric(metric, fields, operator)
    elif parent_partially_supplied:
        return [dict(group) for group in groups], {
            field: get_num(metric, field, "metric") if metric.get(field) is not None else sum_values(groups, field)
            for field in fields
        }, None
    if not parent_supplied:
        return [dict(group) for group in groups], {
            field: sum_values(groups, field) for field in fields
        }, None
    parent = {field: get_num(metric, field, "metric") for field in fields}
    selected = {field: sum_values(groups, field) for field in fields}
    residual = {field: nonnegative_residual(parent[field], selected[field], f"metric.{field}") for field in fields}
    output = [dict(group) for group in groups]
    if any(abs(value) > COVERAGE_TOLERANCE for value in residual.values()):
        item = {"name": residual_name(payload)}
        item.update(residual)
        output.append(item)
    return output, parent, coverage_summary(payload, parent, selected, residual)


def complete_ratio_groups(
    payload: Dict[str, Any],
    groups: List[Dict[str, Any]],
    fields: List[str],
    operator: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, float], Dict[str, Any] | None, Dict[str, Any]]:
    roles = ratio_roles(fields)
    pairs = (
        [("analysis", "comparison")]
        if operator == "structure_change"
        else [("analysis", "analysis_last_year"), ("comparison", "comparison_last_year")]
    )
    policy = sparse_policy_config(payload)
    prepared = [dict(group) for group in groups]
    for idx, group in enumerate(prepared):
        validate_ratio_group(group, roles, f"groups[{idx}]")
        group.setdefault("dimensions", {})
    validate_single_parent_partition(prepared, policy["parent_dimensions"])
    prepared, explicit_merges = apply_explicit_merge_rules(prepared, fields, policy)
    prepared, rollup_level = rollup_ratio_groups(prepared, fields, roles, policy)
    prepared, sparse_members = merge_sparse_to_other(prepared, fields, roles, policy)

    metric = payload.get("metric") or {}
    parent_supplied = all(metric.get(field) is not None for field in fields)
    coverage = coverage_config(payload)
    explicit_coverage = coverage.get("mode") == "auto_residual" or payload.get("partial_coverage") is True
    wants_residual = parent_supplied or explicit_coverage
    if wants_residual:
        require_parent_metric(metric, fields, operator)
    if parent_supplied:
        parent = {field: get_num(metric, field, "metric") for field in fields}
        validate_ratio_parent(parent, roles, operator)
        selected = {field: sum_values(prepared, field) for field in fields}
        residual = {
            field: nonnegative_residual(parent[field], selected[field], f"metric.{field}")
            for field in fields
        }
        prepared, residual_merged = merge_residual_into_other(prepared, fields, residual, policy)
        coverage_output: Dict[str, Any] | None = coverage_summary(payload, parent, selected, residual)
    else:
        parent = {field: sum_values(prepared, field) for field in fields}
        validate_ratio_parent(parent, roles, operator)
        residual = {field: 0.0 for field in fields}
        residual_merged = False
        coverage_output = None

    output: List[Dict[str, Any]] = []
    epsilon_periods: List[str] = []
    inactive_pairs: List[List[str]] = []
    for idx, group in enumerate(prepared):
        validate_ratio_group(group, roles, f"groups[{idx}]")
        if all(get_num(group, ratio_field(role, "denominator"), f"groups[{idx}]") == 0 for role in roles):
            continue
        effective = prepare_effective_ratio_rates(group, pairs, policy)
        current = dict(group)
        current["_effective_rates"] = effective["rates"]
        current["_inactive_pairs"] = effective["inactive_pairs"]
        if effective["epsilon_adjustments"]:
            current["epsilon_adjustments"] = effective["epsilon_adjustments"]
            current["epsilon_applied"] = True
            current["approximation"] = True
            epsilon_periods.extend(effective["epsilon_adjustments"].keys())
        inactive_pairs.extend(effective["inactive_pairs"])
        output.append(current)
    if not output:
        raise AttributionError(f"{operator} has no groups with a positive denominator in any required period")

    sparse_meta = {
        "strategy": policy["strategy"],
        "other_name": policy["other_name"],
        "parent_dimensions": policy["parent_dimensions"],
        "explicit_merges": explicit_merges,
        "rollup_level": rollup_level,
        "sparse_members_merged": sparse_members,
        "residual_merged": residual_merged,
        "epsilon_applied": bool(epsilon_periods),
        "epsilon": policy["epsilon"] if epsilon_periods else None,
        "epsilon_periods": sorted(set(epsilon_periods)),
        "reference_rate_policy": policy["reference_rate_policy"],
        "inactive_pairs": inactive_pairs,
        "approximation": bool(epsilon_periods),
    }
    if coverage_output is None:
        coverage_output = {"mode": "complete_groups"}
    coverage_output["sparse_processing"] = sparse_meta
    return output, parent, coverage_output, sparse_meta


def additive_like(payload: Dict[str, Any], subtractive: bool = False) -> Dict[str, Any]:
    warnings: List[str] = []
    factors = factor_items(payload)
    analysis, comparison, meta = metric_values(payload)
    total_delta = analysis - comparison
    rows: List[Dict[str, Any]] = []
    for idx, factor in enumerate(factors):
        name = str(factor.get("name", f"factor_{idx + 1}"))
        a = get_num(factor, "analysis_value", f"factors[{idx}]")
        c = get_num(factor, "comparison_value", f"factors[{idx}]")
        if subtractive:
            if factor.get("sign") is None:
                warnings.append(f"factor {name} missing sign; default +1 used")
            sign = float(factor.get("sign", 1))
            if sign not in (-1.0, 1.0):
                raise AttributionError(f"factors[{idx}].sign must be +1 or -1")
        else:
            sign = float(factor.get("sign", 1))
        raw_delta = a - c
        contribution = sign * raw_delta
        rows.append({
            "name": name,
            "sign": sign,
            "analysis_value": a,
            "comparison_value": c,
            "change_value": raw_delta,
            "diff_value": raw_delta,
            "change_rate": change_rate(a, c),
            "mom_value": change_rate(a, c),
            "contribution_value": contribution,
            "contribution_rate": safe_div(contribution, total_delta),
        })
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    summary = make_summary(metric_name, analysis, comparison, rows)
    summary.update(meta)
    return {"summary": summary, "rows": rows, "warnings": warnings}


def multiplicative_change(payload: Dict[str, Any], division: bool = False) -> Dict[str, Any]:
    warnings: List[str] = []
    factors = factor_items(payload)
    analysis, comparison, meta = metric_values(payload)
    ensure_positive(analysis, "metric.analysis_value")
    ensure_positive(comparison, "metric.comparison_value")
    total_delta = analysis - comparison
    denom_log = math.log(analysis / comparison)
    rows: List[Dict[str, Any]] = []
    if denom_log == 0:
        warnings.append("metric log ratio is 0; contribution_rate is not available")
    for idx, factor in enumerate(factors):
        name = str(factor.get("name", f"factor_{idx + 1}"))
        a = get_num(factor, "analysis_value", f"factors[{idx}]")
        c = get_num(factor, "comparison_value", f"factors[{idx}]")
        role = str(factor.get("role", "multiplier"))
        if division and role not in ("numerator", "multiplier", "denominator", "divisor"):
            raise AttributionError(f"factors[{idx}].role must be numerator/multiplier/denominator/divisor")
        ensure_positive(a, f"factors[{idx}].analysis_value")
        ensure_positive(c, f"factors[{idx}].comparison_value")
        if role in ("denominator", "divisor"):
            component_log = math.log(c / a)
        else:
            component_log = math.log(a / c)
        share = safe_div(component_log, denom_log)
        contribution = None if share is None else share * total_delta
        rows.append({
            "name": name,
            "role": role,
            "analysis_value": a,
            "comparison_value": c,
            "change_value": a - c,
            "diff_value": a - c,
            "change_rate": change_rate(a, c),
            "mom_value": change_rate(a, c),
            "log_change": component_log,
            "contribution_value": contribution,
            "contribution_rate": share,
        })
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    summary = make_summary(metric_name, analysis, comparison, rows)
    summary.update(meta)
    summary["log_ratio"] = denom_log
    return {"summary": summary, "rows": rows, "warnings": warnings}


def dimension_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    groups = group_items(payload)
    groups, totals, coverage = complete_volume_groups(
        payload, groups, ["analysis_value", "comparison_value"], "dimension_change"
    )
    metric = payload.get("metric") or {}
    if coverage is None:
        analysis, comparison, meta = metric_values(payload, groups)
    else:
        analysis, comparison = totals["analysis_value"], totals["comparison_value"]
        meta = {}
    total_delta = analysis - comparison
    rows: List[Dict[str, Any]] = []
    for idx, group in enumerate(groups):
        name = str(group.get("name", f"group_{idx + 1}"))
        a = get_num(group, "analysis_value", f"groups[{idx}]")
        c = get_num(group, "comparison_value", f"groups[{idx}]")
        contribution = a - c
        rows.append({
            "name": name,
            "analysis_value": a,
            "comparison_value": c,
            "change_value": contribution,
            "diff_value": contribution,
            "change_rate": change_rate(a, c),
            "mom_value": change_rate(a, c),
            "contribution_value": contribution,
            "contribution_rate": safe_div(contribution, total_delta),
        })
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    summary = make_summary(metric_name, analysis, comparison, rows)
    summary.update(meta)
    if coverage is not None:
        summary["coverage"] = coverage
    return {"summary": summary, "rows": rows, "warnings": []}


def structure_change(payload: Dict[str, Any]) -> Dict[str, Any]:
    groups = group_items(payload)
    ratio_fields = [
        "analysis_numerator", "analysis_denominator",
        "comparison_numerator", "comparison_denominator",
    ]
    groups, totals, coverage, sparse_meta = complete_ratio_groups(payload, groups, ratio_fields, "structure_change")
    total_s1 = totals["analysis_numerator"]
    total_p1 = totals["analysis_denominator"]
    total_s0 = totals["comparison_numerator"]
    total_p0 = totals["comparison_denominator"]
    if total_p1 <= 0 or total_p0 <= 0:
        raise AttributionError("overall denominator must be > 0 for structure_change")
    y1 = total_s1 / total_p1
    y0 = total_s0 / total_p0
    total_delta = y1 - y0
    rows: List[Dict[str, Any]] = []
    for idx, group in enumerate(groups):
        name = str(group.get("name", f"group_{idx + 1}"))
        s1 = get_num(group, "analysis_numerator", f"groups[{idx}]")
        p1 = get_num(group, "analysis_denominator", f"groups[{idx}]")
        s0 = get_num(group, "comparison_numerator", f"groups[{idx}]")
        p0 = get_num(group, "comparison_denominator", f"groups[{idx}]")
        effective_rates = group.get("_effective_rates") or {}
        yi1 = effective_rates.get("analysis")
        yi0 = effective_rates.get("comparison")
        if yi1 is None or yi0 is None:
            raise AttributionError(f"groups[{idx}] has no effective paired rate for structure_change")
        share1 = p1 / total_p1
        share0 = p0 / total_p0
        level_effect = (yi1 - yi0) * share0
        mix_effect = (share1 - share0) * (yi1 - y0)
        contribution = level_effect + mix_effect
        rows.append({
            "name": name,
            "analysis_numerator": s1,
            "analysis_denominator": p1,
            "comparison_numerator": s0,
            "comparison_denominator": p0,
            "analysis_value": yi1,
            "analysis_raw_value": safe_div(s1, p1),
            "baseline_value": yi0,
            "comparison_value": yi0,
            "comparison_raw_value": safe_div(s0, p0),
            "change_value": yi1 - yi0,
            "diff_value": yi1 - yi0,
            "change_rate": change_rate(yi1, yi0),
            "analysis_denominator_share": share1,
            "comparison_denominator_share": share0,
            "level_effect": level_effect,
            "level_effect_rate": safe_div(level_effect, total_delta),
            "mix_effect": mix_effect,
            "mix_effect_rate": safe_div(mix_effect, total_delta),
            "contribution_value": contribution,
            "contribution_rate": safe_div(contribution, total_delta),
        })
        for key in ("dimensions", "is_other", "merged_members", "merge_reasons", "epsilon_adjustments", "epsilon_applied", "approximation"):
            if key in group:
                rows[-1][key] = group[key]
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    summary = make_summary(metric_name, y1, y0, rows)
    summary.update({
        "analysis_numerator": total_s1,
        "analysis_denominator": total_p1,
        "comparison_numerator": total_s0,
        "comparison_denominator": total_p0,
    })
    if coverage is not None:
        summary["coverage"] = coverage
    warnings = []
    if sparse_meta["epsilon_applied"]:
        warnings.append("其他/未覆盖在部分周期使用 epsilon 平滑近似；贡献主要解释为结构占比变化")
    return {"summary": summary, "rows": rows, "warnings": warnings}


def yoy_items(payload: Dict[str, Any], prefer: str) -> List[Dict[str, Any]]:
    if prefer == "factors":
        return factor_items(payload)
    if prefer == "groups":
        return group_items(payload)
    return payload.get("groups") or payload.get("factors") or []


def yoy_totals(payload: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, float]:
    metric = payload.get("metric") or {}
    mapping = {
        "analysis_value": "analysis_value",
        "analysis_last_year_value": "analysis_last_year_value",
        "comparison_value": "comparison_value",
        "comparison_last_year_value": "comparison_last_year_value",
    }
    totals: Dict[str, float] = {}
    for metric_key, item_key in mapping.items():
        if metric.get(metric_key) is None:
            totals[metric_key] = sum_values(items, item_key)
        else:
            totals[metric_key] = get_num(metric, metric_key, "metric")
    return totals


def dimension_yoy_trend(payload: Dict[str, Any], prefer: str = "groups") -> Dict[str, Any]:
    items = yoy_items(payload, prefer)
    if not items:
        raise AttributionError("groups or factors must be a non-empty list")
    yoy_fields = [
        "analysis_value", "analysis_last_year_value",
        "comparison_value", "comparison_last_year_value",
    ]
    coverage = None
    if prefer == "groups":
        items, parent_totals, coverage = complete_volume_groups(
            payload, items, yoy_fields, "dimension_yoy_trend"
        )
        totals = parent_totals if coverage is not None else yoy_totals(payload, items)
    else:
        totals = yoy_totals(payload, items)
    ly1 = totals["analysis_last_year_value"]
    ly0 = totals["comparison_last_year_value"]
    if ly1 == 0 or ly0 == 0:
        raise AttributionError("overall last year values must not be 0 for yoy trend attribution")
    y1 = (totals["analysis_value"] - ly1) / ly1
    y0 = (totals["comparison_value"] - ly0) / ly0
    delta_y = y1 - y0
    rows: List[Dict[str, Any]] = []
    negative_delta_abs_sum = 0.0
    tmp: List[Dict[str, Any]] = []
    for idx, item in enumerate(items):
        name = str(item.get("name", f"item_{idx + 1}"))
        g1 = get_num(item, "analysis_value", f"items[{idx}]")
        l1 = get_num(item, "analysis_last_year_value", f"items[{idx}]")
        g0 = get_num(item, "comparison_value", f"items[{idx}]")
        l0 = get_num(item, "comparison_last_year_value", f"items[{idx}]")
        q1 = (g1 - l1) / ly1
        q0 = (g0 - l0) / ly0
        delta_i = q1 - q0
        if delta_i < 0:
            negative_delta_abs_sum += abs(delta_i)
        tmp.append({
            "name": name,
            "analysis_value": g1,
            "analysis_last_year_value": l1,
            "analysis_yoy": safe_div(g1, l1) - 1 if l1 != 0 else None,
            "analysis_yoy_point_contribution": q1,
            "comparison_value": g0,
            "comparison_last_year_value": l0,
            "comparison_yoy": safe_div(g0, l0) - 1 if l0 != 0 else None,
            "comparison_yoy_point_contribution": q0,
            "yoy_change": (safe_div(g1, l1) - 1 if l1 != 0 else 0) - (safe_div(g0, l0) - 1 if l0 != 0 else 0) if l1 != 0 and l0 != 0 else None,
            "contribution_value": delta_i,
        })
    for row in tmp:
        delta_i = row["contribution_value"]
        row["contribution_rate"] = safe_div(delta_i, delta_y)
        row["deterioration_mix"] = safe_div(abs(delta_i), negative_delta_abs_sum) if delta_i < 0 else None
        rows.append(row)
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    summary = {
        "metric_name": metric_name,
        "analysis_value": totals["analysis_value"],
        "analysis_last_year_value": ly1,
        "analysis_yoy": y1,
        "comparison_value": totals["comparison_value"],
        "comparison_last_year_value": ly0,
        "comparison_yoy": y0,
        "yoy_trend_delta": delta_y,
        "contribution_sum": sum(r["contribution_value"] for r in rows),
        "residual": delta_y - sum(r["contribution_value"] for r in rows),
        "contribution_rate_available": delta_y != 0,
    }
    if coverage is not None:
        summary["coverage"] = coverage
    return {"summary": summary, "rows": rows, "warnings": []}


def structure_yoy_trend(payload: Dict[str, Any]) -> Dict[str, Any]:
    groups = group_items(payload)
    ratio_fields = [
        "analysis_numerator", "analysis_denominator",
        "analysis_last_year_numerator", "analysis_last_year_denominator",
        "comparison_numerator", "comparison_denominator",
        "comparison_last_year_numerator", "comparison_last_year_denominator",
    ]
    groups, totals, coverage, sparse_meta = complete_ratio_groups(payload, groups, ratio_fields, "structure_yoy_trend")
    total_s1 = totals["analysis_numerator"]
    total_p1 = totals["analysis_denominator"]
    total_s1_ly = totals["analysis_last_year_numerator"]
    total_p1_ly = totals["analysis_last_year_denominator"]
    total_s0 = totals["comparison_numerator"]
    total_p0 = totals["comparison_denominator"]
    total_s0_ly = totals["comparison_last_year_numerator"]
    total_p0_ly = totals["comparison_last_year_denominator"]
    for value, field in [
        (total_p1, "analysis_denominator"),
        (total_p1_ly, "analysis_last_year_denominator"),
        (total_p0, "comparison_denominator"),
        (total_p0_ly, "comparison_last_year_denominator"),
    ]:
        if value <= 0:
            raise AttributionError(f"overall {field} must be > 0 for structure_yoy_trend")
    y1 = total_s1 / total_p1
    y1_ly = total_s1_ly / total_p1_ly
    y0 = total_s0 / total_p0
    y0_ly = total_s0_ly / total_p0_ly
    analysis_yoy = y1 - y1_ly
    comparison_yoy = y0 - y0_ly
    delta_yoy = analysis_yoy - comparison_yoy
    rows: List[Dict[str, Any]] = []
    for idx, group in enumerate(groups):
        name = str(group.get("name", f"group_{idx + 1}"))
        s1 = get_num(group, "analysis_numerator", f"groups[{idx}]")
        p1 = get_num(group, "analysis_denominator", f"groups[{idx}]")
        s1_ly = get_num(group, "analysis_last_year_numerator", f"groups[{idx}]")
        p1_ly = get_num(group, "analysis_last_year_denominator", f"groups[{idx}]")
        s0 = get_num(group, "comparison_numerator", f"groups[{idx}]")
        p0 = get_num(group, "comparison_denominator", f"groups[{idx}]")
        s0_ly = get_num(group, "comparison_last_year_numerator", f"groups[{idx}]")
        p0_ly = get_num(group, "comparison_last_year_denominator", f"groups[{idx}]")
        effective_rates = group.get("_effective_rates") or {}
        yi1 = effective_rates.get("analysis")
        yi1_ly = effective_rates.get("analysis_last_year")
        yi0 = effective_rates.get("comparison")
        yi0_ly = effective_rates.get("comparison_last_year")
        inactive = {tuple(pair) for pair in group.get("_inactive_pairs", [])}
        share1 = p1 / total_p1
        share1_ly = p1_ly / total_p1_ly
        share0 = p0 / total_p0
        share0_ly = p0_ly / total_p0_ly
        if ("analysis", "analysis_last_year") in inactive:
            analysis_level_effect = 0.0
            analysis_mix_effect = 0.0
            analysis_group_yoy = None
        else:
            if yi1 is None or yi1_ly is None:
                raise AttributionError(f"groups[{idx}] has no effective analysis yoy paired rate")
            analysis_level_effect = (yi1 - yi1_ly) * share1_ly
            analysis_mix_effect = (share1 - share1_ly) * (yi1 - y1_ly)
            analysis_group_yoy = yi1 - yi1_ly
        if ("comparison", "comparison_last_year") in inactive:
            comparison_level_effect = 0.0
            comparison_mix_effect = 0.0
            comparison_group_yoy = None
        else:
            if yi0 is None or yi0_ly is None:
                raise AttributionError(f"groups[{idx}] has no effective comparison yoy paired rate")
            comparison_level_effect = (yi0 - yi0_ly) * share0_ly
            comparison_mix_effect = (share0 - share0_ly) * (yi0 - y0_ly)
            comparison_group_yoy = yi0 - yi0_ly
        level_effect = analysis_level_effect - comparison_level_effect
        mix_effect = analysis_mix_effect - comparison_mix_effect
        contribution = level_effect + mix_effect
        rows.append({
            "name": name,
            "analysis_numerator": s1,
            "analysis_denominator": p1,
            "analysis_value": yi1,
            "analysis_raw_value": safe_div(s1, p1),
            "analysis_last_year_numerator": s1_ly,
            "analysis_last_year_denominator": p1_ly,
            "analysis_last_year_value": yi1_ly,
            "analysis_last_year_raw_value": safe_div(s1_ly, p1_ly),
            "analysis_yoy": analysis_group_yoy,
            "comparison_numerator": s0,
            "comparison_denominator": p0,
            "comparison_value": yi0,
            "comparison_raw_value": safe_div(s0, p0),
            "comparison_last_year_numerator": s0_ly,
            "comparison_last_year_denominator": p0_ly,
            "comparison_last_year_value": yi0_ly,
            "comparison_last_year_raw_value": safe_div(s0_ly, p0_ly),
            "comparison_yoy": comparison_group_yoy,
            "yoy_change": (
                analysis_group_yoy - comparison_group_yoy
                if analysis_group_yoy is not None and comparison_group_yoy is not None
                else None
            ),
            "analysis_denominator_share": share1,
            "analysis_last_year_denominator_share": share1_ly,
            "comparison_denominator_share": share0,
            "comparison_last_year_denominator_share": share0_ly,
            "analysis_level_effect": analysis_level_effect,
            "analysis_mix_effect": analysis_mix_effect,
            "comparison_level_effect": comparison_level_effect,
            "comparison_mix_effect": comparison_mix_effect,
            "level_effect": level_effect,
            "level_effect_rate": safe_div(level_effect, delta_yoy),
            "mix_effect": mix_effect,
            "mix_effect_rate": safe_div(mix_effect, delta_yoy),
            "contribution_value": contribution,
            "contribution_rate": safe_div(contribution, delta_yoy),
        })
        for key in ("dimensions", "is_other", "merged_members", "merge_reasons", "epsilon_adjustments", "epsilon_applied", "approximation"):
            if key in group:
                rows[-1][key] = group[key]
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    contribution_sum = sum(float(r.get("contribution_value") or 0.0) for r in rows)
    summary = {
        "metric_name": metric_name,
        "analysis_value": y1,
        "analysis_last_year_value": y1_ly,
        "analysis_yoy": analysis_yoy,
        "comparison_value": y0,
        "comparison_last_year_value": y0_ly,
        "comparison_yoy": comparison_yoy,
        "yoy_trend_delta": delta_yoy,
        "contribution_sum": contribution_sum,
        "residual": delta_yoy - contribution_sum,
        "contribution_rate_available": delta_yoy != 0,
        "analysis_numerator": total_s1,
        "analysis_denominator": total_p1,
        "analysis_last_year_numerator": total_s1_ly,
        "analysis_last_year_denominator": total_p1_ly,
        "comparison_numerator": total_s0,
        "comparison_denominator": total_p0,
        "comparison_last_year_numerator": total_s0_ly,
        "comparison_last_year_denominator": total_p0_ly,
    }
    if coverage is not None:
        summary["coverage"] = coverage
    warnings = []
    if sparse_meta["epsilon_applied"]:
        warnings.append("其他/未覆盖在部分同比配对周期使用 epsilon 平滑近似；贡献主要解释为结构占比变化")
    return {"summary": summary, "rows": rows, "warnings": warnings}


def signed_yoy_totals(payload: Dict[str, Any], items: List[Dict[str, Any]]) -> Dict[str, float]:
    metric = payload.get("metric") or {}
    keys = ["analysis_value", "analysis_last_year_value", "comparison_value", "comparison_last_year_value"]
    totals: Dict[str, float] = {}
    for key in keys:
        if metric.get(key) is None:
            total = 0.0
            for idx, item in enumerate(items):
                sign = float(item.get("sign", 1))
                if sign not in (-1.0, 1.0):
                    raise AttributionError(f"items[{idx}].sign must be +1 or -1")
                total += sign * get_num(item, key, f"items[{idx}]")
            totals[key] = total
        else:
            totals[key] = get_num(metric, key, "metric")
    return totals


def subtractive_yoy_trend(payload: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = []
    factors = factor_items(payload)
    totals = signed_yoy_totals(payload, factors)
    ly1 = totals["analysis_last_year_value"]
    ly0 = totals["comparison_last_year_value"]
    if ly1 == 0 or ly0 == 0:
        raise AttributionError("overall last year values must not be 0 for subtractive yoy trend attribution")
    y1 = (totals["analysis_value"] - ly1) / ly1
    y0 = (totals["comparison_value"] - ly0) / ly0
    delta_y = y1 - y0
    rows: List[Dict[str, Any]] = []
    for idx, factor in enumerate(factors):
        name = str(factor.get("name", f"factor_{idx + 1}"))
        if factor.get("sign") is None:
            warnings.append(f"factor {name} missing sign; default +1 used")
        sign = float(factor.get("sign", 1))
        if sign not in (-1.0, 1.0):
            raise AttributionError(f"factors[{idx}].sign must be +1 or -1")
        g1 = get_num(factor, "analysis_value", f"factors[{idx}]")
        l1 = get_num(factor, "analysis_last_year_value", f"factors[{idx}]")
        g0 = get_num(factor, "comparison_value", f"factors[{idx}]")
        l0 = get_num(factor, "comparison_last_year_value", f"factors[{idx}]")
        q1 = sign * (g1 - l1) / ly1
        q0 = sign * (g0 - l0) / ly0
        delta_i = q1 - q0
        rows.append({
            "name": name,
            "sign": sign,
            "analysis_value": g1,
            "analysis_last_year_value": l1,
            "analysis_yoy": safe_div(g1, l1) - 1 if l1 != 0 else None,
            "analysis_yoy_point_contribution": q1,
            "comparison_value": g0,
            "comparison_last_year_value": l0,
            "comparison_yoy": safe_div(g0, l0) - 1 if l0 != 0 else None,
            "comparison_yoy_point_contribution": q0,
            "yoy_change": (safe_div(g1, l1) - 1 if l1 != 0 else 0) - (safe_div(g0, l0) - 1 if l0 != 0 else 0) if l1 != 0 and l0 != 0 else None,
            "contribution_value": delta_i,
            "contribution_rate": safe_div(delta_i, delta_y),
        })
    metric_name = (payload.get("metric") or {}).get("name", "metric")
    contribution_sum = sum(r["contribution_value"] for r in rows)
    summary = {
        "metric_name": metric_name,
        "analysis_value": totals["analysis_value"],
        "analysis_last_year_value": ly1,
        "analysis_yoy": y1,
        "comparison_value": totals["comparison_value"],
        "comparison_last_year_value": ly0,
        "comparison_yoy": y0,
        "yoy_trend_delta": delta_y,
        "contribution_sum": contribution_sum,
        "residual": delta_y - contribution_sum,
        "contribution_rate_available": delta_y != 0,
    }
    return {"summary": summary, "rows": rows, "warnings": warnings}


def multiplicative_yoy_trend(payload: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = []
    factors = factor_items(payload)
    factor_rows: List[Dict[str, Any]] = []
    r_analysis_product = 1.0
    r_comparison_product = 1.0
    for idx, factor in enumerate(factors):
        name = str(factor.get("name", f"factor_{idx + 1}"))
        a = get_num(factor, "analysis_value", f"factors[{idx}]")
        aly = get_num(factor, "analysis_last_year_value", f"factors[{idx}]")
        c = get_num(factor, "comparison_value", f"factors[{idx}]")
        cly = get_num(factor, "comparison_last_year_value", f"factors[{idx}]")
        role = str(factor.get("role", "multiplier"))
        for value, field in [(a, "analysis_value"), (aly, "analysis_last_year_value"), (c, "comparison_value"), (cly, "comparison_last_year_value")]:
            ensure_positive(value, f"factors[{idx}].{field}")
        r1_raw = a / aly
        r0_raw = c / cly
        if role in ("denominator", "divisor"):
            r1 = 1 / r1_raw
            r0 = 1 / r0_raw
        else:
            r1 = r1_raw
            r0 = r0_raw
        r_analysis_product *= r1
        r_comparison_product *= r0
        d = math.log(r1) - math.log(r0)
        factor_rows.append({
            "name": name,
            "role": role,
            "analysis_value": a,
            "analysis_last_year_value": aly,
            "analysis_yoy_multiple": r1_raw,
            "analysis_yoy": r1_raw - 1,
            "comparison_value": c,
            "comparison_last_year_value": cly,
            "comparison_yoy_multiple": r0_raw,
            "comparison_yoy": r0_raw - 1,
            "yoy_change": r1_raw - r0_raw,
            "log_trend_delta": d,
        })
    metric = payload.get("metric") or {}
    if all(metric.get(k) is not None for k in ["analysis_value", "analysis_last_year_value", "comparison_value", "comparison_last_year_value"]):
        ma = get_num(metric, "analysis_value", "metric")
        maly = get_num(metric, "analysis_last_year_value", "metric")
        mc = get_num(metric, "comparison_value", "metric")
        mcly = get_num(metric, "comparison_last_year_value", "metric")
        ensure_positive(ma, "metric.analysis_value")
        ensure_positive(maly, "metric.analysis_last_year_value")
        ensure_positive(mc, "metric.comparison_value")
        ensure_positive(mcly, "metric.comparison_last_year_value")
        r_analysis = ma / maly
        r_comparison = mc / mcly
    else:
        r_analysis = r_analysis_product
        r_comparison = r_comparison_product
        warnings.append("metric yoy multiples were inferred from product of factor yoy multiples")
    y1 = r_analysis - 1
    y0 = r_comparison - 1
    delta_y = y1 - y0
    delta_ln_r = math.log(r_analysis) - math.log(r_comparison)
    rows: List[Dict[str, Any]] = []
    if delta_ln_r == 0:
        warnings.append("overall log yoy trend delta is 0; contribution_rate is not available")
    for row in factor_rows:
        share = safe_div(row["log_trend_delta"], delta_ln_r)
        row["contribution_rate"] = share
        row["contribution_value"] = None if share is None else share * delta_y
        rows.append(row)
    metric_name = metric.get("name", "metric")
    contribution_sum = sum(float(r.get("contribution_value") or 0.0) for r in rows)
    summary = {
        "metric_name": metric_name,
        "analysis_yoy_multiple": r_analysis,
        "analysis_yoy": y1,
        "comparison_yoy_multiple": r_comparison,
        "comparison_yoy": y0,
        "yoy_trend_delta": delta_y,
        "log_yoy_trend_delta": delta_ln_r,
        "contribution_sum": contribution_sum,
        "residual": delta_y - contribution_sum,
        "contribution_rate_available": delta_ln_r != 0,
    }
    return {"summary": summary, "rows": rows, "warnings": warnings}


def division_yoy_trend(payload: Dict[str, Any]) -> Dict[str, Any]:
    factors = factor_items(payload)
    roles = {str(f.get("role", "multiplier")) for f in factors}
    invalid = roles - {"numerator", "multiplier", "denominator", "divisor"}
    if invalid:
        raise AttributionError(f"division_yoy_trend factor roles must be numerator/multiplier/denominator/divisor, got {sorted(invalid)}")
    if not roles.intersection({"denominator", "divisor"}):
        raise AttributionError("division_yoy_trend requires at least one denominator/divisor factor")
    result = multiplicative_yoy_trend(payload)
    result.setdefault("warnings", []).append("division_yoy_trend uses log-space contribution rates and maps them back to yoy point contribution_value")
    return result


OPERATORS = {
    "additive_change": lambda p: additive_like(p, subtractive=False),
    "subtractive_change": lambda p: additive_like(p, subtractive=True),
    "multiplicative_change": lambda p: multiplicative_change(p, division=False),
    "division_change": lambda p: multiplicative_change(p, division=True),
    "dimension_change": dimension_change,
    "structure_change": structure_change,
    "dimension_yoy_trend": lambda p: dimension_yoy_trend(p, prefer="groups"),
    "structure_yoy_trend": structure_yoy_trend,
    "additive_yoy_trend": lambda p: dimension_yoy_trend(p, prefer="factors"),
    "subtractive_yoy_trend": subtractive_yoy_trend,
    "multiplicative_yoy_trend": multiplicative_yoy_trend,
    "division_yoy_trend": division_yoy_trend,
}


def run(payload: Dict[str, Any], explain_routing: bool = False) -> Dict[str, Any]:
    operator, route_warnings = infer_operator(payload)
    if operator not in OPERATORS:
        raise AttributionError(f"unsupported operator: {operator}")
    if get_operator(operator) is None:
        raise AttributionError(f"operator is not registered: {operator}")
    result = OPERATORS[operator](payload)
    semantic_warnings = payload.get("semantic_warnings", [])
    if not isinstance(semantic_warnings, list):
        semantic_warnings = ["semantic_warnings must be a list; ignored"]
    warnings = route_warnings + result.get("warnings", []) + [str(item) for item in semantic_warnings]
    scenario = payload.get("scenario") or ("yoy_trend_change" if "yoy_trend" in operator else "metric_change")
    output = {
        "ok": True,
        "operator": operator,
        "engine_identity": identity(),
        "registry": {"name": REGISTRY_NAME, "version": REGISTRY_VERSION},
        "scenario": scenario,
        "metric_object": payload.get("metric_object"),
        "decomposition": payload.get("decomposition"),
        "periods": payload.get("periods", {}),
        "summary": result.get("summary", {}),
        "rows": result.get("rows", []),
        "warnings": warnings,
        "boundary_cases": [],
    }
    if explain_routing:
        output["routing"] = {
            "operator_explicit": bool(payload.get("operator")),
            "scenario": payload.get("scenario"),
            "metric_object": payload.get("metric_object"),
            "decomposition": payload.get("decomposition"),
        }
    if not output["summary"].get("contribution_rate_available", True):
        output["boundary_cases"].append("overall change is 0; contribution_rate is null or not meaningful")
    if abs(float(output["summary"].get("residual") or 0.0)) > 1e-8:
        output["boundary_cases"].append("contribution sum differs from overall change; check coverage, rounding or formula consistency")
    output["warnings"].extend(add_semantic_labels(payload, output))
    output["warnings"].extend(add_ranking(payload, output))
    return round_float(output)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Attribution calculation engine")
    parser.add_argument("--input", required=True, help="input JSON file")
    parser.add_argument("--output", help="output JSON file; stdout if omitted")
    parser.add_argument("--explain-routing", action="store_true", help="include routing diagnostics")
    args = parser.parse_args(argv)

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            payload = json.load(f)
        result = run(payload, explain_routing=args.explain_routing)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        else:
            print(text)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should return structured errors.
        error = {"ok": False, "error": str(exc), "error_type": exc.__class__.__name__}
        text = json.dumps(error, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        else:
            print(text, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
