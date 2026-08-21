#!/usr/bin/env python3
"""Deterministically select the scene-analysis execution profile."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_LIMITS = {
    "metrics": 3,
    "views": 3,
    "dimension_levels": 3,
    "max_dimension_depth": 2,
    "fact_slots": 16,
    "calculation_nodes": 4,
    "expression_depth": 4,
    "result_rows": 1000,
}


@dataclass(frozen=True)
class ExpressionStats:
    depth: int = 0
    operations: int = 0
    result_refs: int = 0


def expression_stats(expression: Any) -> ExpressionStats:
    if not isinstance(expression, dict):
        return ExpressionStats()
    if "result" in expression:
        return ExpressionStats(depth=1, result_refs=1)
    if "fact" in expression or "fact_role" in expression or "literal" in expression:
        return ExpressionStats(depth=1)
    args = expression.get("args")
    if not isinstance(args, list):
        return ExpressionStats(depth=1)
    children = [expression_stats(arg) for arg in args]
    return ExpressionStats(
        depth=1 + max((child.depth for child in children), default=0),
        operations=1 + sum(child.operations for child in children),
        result_refs=sum(child.result_refs for child in children),
    )


def _limits(ir: dict[str, Any]) -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    runtime = ir.get("runtime")
    configured = runtime.get("fast_query_limits") if isinstance(runtime, dict) else None
    if isinstance(configured, dict):
        for key, default in DEFAULT_LIMITS.items():
            value = configured.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                limits[key] = value
            else:
                limits[key] = default
    return limits


def _clarification_is_blocking(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    return item.get("severity") in {"blocking", "capability", "operator_input"}


def assess_query(ir: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Return a traceable fast-query admission decision.

    Admission is intentionally optimistic for bounded deterministic work. Runtime
    response checks remain responsible for escalating malformed or unexpectedly
    large results without discarding a successful fetch.
    """

    limits = _limits(ir)
    task = ir.get("analysis_task") if isinstance(ir.get("analysis_task"), dict) else {}
    metrics = task.get("metrics") if isinstance(task.get("metrics"), list) else []
    views = ir.get("views") if isinstance(ir.get("views"), list) else []
    trees = ir.get("dimension_trees") if isinstance(ir.get("dimension_trees"), list) else []
    levels_per_tree = [len(tree.get("levels", [])) for tree in trees if isinstance(tree, dict)]
    fact_slots = (plan.get("analysis_task") or {}).get("fact_requirements", [])
    if not isinstance(fact_slots, list):
        fact_slots = []
    derived = ir.get("derived_requirements") if isinstance(ir.get("derived_requirements"), list) else []
    custom = ir.get("custom_calculations") if isinstance(ir.get("custom_calculations"), list) else []
    targets = ir.get("attribution_targets") if isinstance(ir.get("attribution_targets"), list) else []
    clarifications = ir.get("clarifications") if isinstance(ir.get("clarifications"), list) else []
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []

    inferred_ambiguities = 0
    for requirement in derived:
        if not isinstance(requirement, dict):
            inferred_ambiguities += 1
            continue
        if requirement.get("definition_status") == "inferred":
            definition = requirement.get("definition")
            alternatives = requirement.get("alternative_candidates", [])
            if (
                not isinstance(definition, dict)
                or not isinstance(requirement.get("inference_basis"), str)
                or not requirement.get("inference_basis")
                or (isinstance(alternatives, list) and bool(alternatives))
                or requirement.get("definition_unique") is False
            ):
                inferred_ambiguities += 1
    expressions: list[Any] = []
    for node in nodes:
        execution = node.get("execution") if isinstance(node, dict) else None
        if not isinstance(execution, dict) or execution.get("handler") != "derived":
            continue
        if "expression" in execution:
            expressions.append(execution.get("expression"))
        if isinstance(execution.get("expressions"), dict):
            expressions.extend(execution["expressions"].values())
        if isinstance(execution.get("intermediate_expressions"), dict):
            expressions.extend(execution["intermediate_expressions"].values())
    stats = [expression_stats(expression) for expression in expressions]
    max_expression_depth = max((item.depth for item in stats), default=0)
    result_refs = sum(item.result_refs for item in stats)
    calculation_nodes = len(derived) + len(custom)

    runtime = ir.get("runtime") if isinstance(ir.get("runtime"), dict) else {}
    estimated_rows = runtime.get("estimated_result_rows")
    estimated_rows_valid = isinstance(estimated_rows, int) and not isinstance(estimated_rows, bool) and estimated_rows >= 0
    adaptive_fetch = runtime.get("adaptive_fetch") is True
    blocked_nodes = [str(node.get("node_id")) for node in nodes if isinstance(node, dict) and node.get("status") == "blocked"]
    blocking_clarifications = [
        str(item.get("id") or item.get("clarification_id") or index)
        for index, item in enumerate(clarifications)
        if _clarification_is_blocking(item)
    ]

    features = {
        "metrics": len(metrics),
        "views": len(views),
        "dimension_levels": sum(levels_per_tree),
        "max_dimension_depth": max(levels_per_tree, default=0),
        "fact_slots": len(fact_slots),
        "calculation_nodes": calculation_nodes,
        "max_expression_depth": max_expression_depth,
        "result_refs": result_refs,
        "attribution_targets": len(targets),
        "blocking_clarifications": len(blocking_clarifications),
        "blocked_nodes": len(blocked_nodes),
        "inferred_ambiguities": inferred_ambiguities,
        "estimated_result_rows": estimated_rows if estimated_rows_valid else None,
        "adaptive_fetch": adaptive_fetch,
    }

    reasons: list[str] = []
    profile = "fast_derived" if calculation_nodes else "fast_fact"
    if targets or adaptive_fetch:
        profile = "orchestrated"
        if targets:
            reasons.append("ATTRIBUTION_REQUIRES_ORCHESTRATION")
        if adaptive_fetch:
            reasons.append("ADAPTIVE_FETCH_REQUIRES_ORCHESTRATION")
    else:
        hard_failures = {
            "NO_FACT_SLOTS": len(fact_slots) == 0,
            "BLOCKING_CLARIFICATION": bool(blocking_clarifications),
            "BLOCKED_PLAN_NODE": bool(blocked_nodes),
            "INFERRED_DEFINITION_AMBIGUOUS": inferred_ambiguities > 0,
            "RESULT_DEPENDENCY_CHAIN": result_refs > 0,
        }
        budget_failures = {
            "METRIC_BUDGET_EXCEEDED": len(metrics) > limits["metrics"],
            "VIEW_BUDGET_EXCEEDED": len(views) > limits["views"],
            "DIMENSION_LEVEL_BUDGET_EXCEEDED": sum(levels_per_tree) > limits["dimension_levels"],
            "DIMENSION_DEPTH_BUDGET_EXCEEDED": max(levels_per_tree, default=0) > limits["max_dimension_depth"],
            "FACT_SLOT_BUDGET_EXCEEDED": len(fact_slots) > limits["fact_slots"],
            "CALCULATION_BUDGET_EXCEEDED": calculation_nodes > limits["calculation_nodes"],
            "EXPRESSION_DEPTH_BUDGET_EXCEEDED": max_expression_depth > limits["expression_depth"],
            "RESULT_ROW_BUDGET_EXCEEDED": estimated_rows_valid and estimated_rows > limits["result_rows"],
        }
        reasons.extend(code for code, failed in hard_failures.items() if failed)
        reasons.extend(code for code, failed in budget_failures.items() if failed)
        if reasons:
            profile = "standard"

    eligible = profile in {"fast_fact", "fast_derived"}
    return {
        "schema_version": "fast_query_admission/1.0",
        "eligible": eligible,
        "execution_profile": profile,
        "validation_profile": "minimal" if eligible else "full",
        "features": features,
        "limits": limits,
        "reasons": reasons,
        "evidence": {
            "blocking_clarifications": blocking_clarifications,
            "blocked_nodes": blocked_nodes,
        },
        "fallback_triggers": [
            "response_not_machine_parseable",
            "fact_slot_unbound",
            "period_view_or_grain_mismatch",
            "unit_or_definition_conflict",
            "non_finite_value_or_unsupported_missing_state",
            "derived_binding_or_denominator_failure",
            "result_row_budget_exceeded",
            "incomplete_group_coverage_when_full_required",
            "additional_fetch_required",
        ],
    }
