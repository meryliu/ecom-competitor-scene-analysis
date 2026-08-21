"""Canonical task-level selector propagation shared by Prepare and Compile."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class SelectorContextError(ValueError):
    pass


def _merge(base: dict[str, Any], declared: Any, path: str) -> dict[str, Any]:
    if declared is None:
        return deepcopy(base)
    if not isinstance(declared, dict):
        raise SelectorContextError(f"{path} must be an object")
    merged = deepcopy(base)
    for raw_dimension, value in declared.items():
        dimension = str(raw_dimension)
        if dimension in merged and merged[dimension] != value:
            raise SelectorContextError(
                f"SELECTOR_CONTEXT_CONFLICT: {path}.{dimension} conflicts with analysis_task.filters"
            )
        merged[dimension] = deepcopy(value)
    return merged


def task_filter_selectors(filters: Any) -> dict[str, Any]:
    """Return physical selector constraints from the supported filter contract."""
    if filters in (None, []):
        return {}
    if isinstance(filters, dict):
        return {str(key): deepcopy(value) for key, value in filters.items()}
    if not isinstance(filters, list):
        raise SelectorContextError("analysis_task.filters must be an object or array")
    selectors: dict[str, Any] = {}
    for index, item in enumerate(filters):
        path = f"analysis_task.filters[{index}]"
        if not isinstance(item, dict):
            raise SelectorContextError(f"{path} must be an object")
        dimension = item.get("dimension_ref", item.get("dimension", item.get("field")))
        operator = str(item.get("operator", item.get("op", "eq"))).lower()
        if not isinstance(dimension, str) or not dimension:
            raise SelectorContextError(f"{path} requires dimension_ref")
        if operator in {"eq", "=", "in"}:
            value = item.get("values") if operator == "in" and "values" in item else item.get("value")
        else:
            raise SelectorContextError(
                f"{path} operator {operator!r} cannot be pushed down as a fact selector"
            )
        if value is None:
            raise SelectorContextError(f"{path} requires value or values")
        if dimension in selectors and selectors[dimension] != value:
            raise SelectorContextError(
                f"SELECTOR_CONTEXT_CONFLICT: multiple task filters constrain {dimension!r} differently"
            )
        selectors[dimension] = deepcopy(value)
    return selectors


def apply_task_selector_context(ir: dict[str, Any]) -> dict[str, Any]:
    """Idempotently inherit task selectors into every physical fact consumer."""
    normalized = deepcopy(ir)
    task = normalized.get("analysis_task")
    if not isinstance(task, dict):
        return normalized
    inherited = task_filter_selectors(task.get("filters"))
    task["selector_dimensions"] = deepcopy(inherited)
    if not inherited:
        return normalized

    for collection in (
        "input_adaptations",
        "fact_observations",
        "metric_compositions",
        "derived_requirements",
        "custom_calculations",
    ):
        for index, requirement in enumerate(normalized.get(collection) or []):
            if isinstance(requirement, dict):
                requirement["dimensions"] = _merge(
                    inherited, requirement.get("dimensions"), f"{collection}[{index}].dimensions"
                )

    for index, target in enumerate(normalized.get("attribution_targets") or []):
        if not isinstance(target, dict):
            continue
        target["dimensions"] = _merge(
            inherited, target.get("dimensions"), f"attribution_targets[{index}].dimensions"
        )
        # Factor dimensions remain local additions. Compile/Prepare merge them with
        # the now-canonical target dimensions and detect conflicts there.
    return normalized
