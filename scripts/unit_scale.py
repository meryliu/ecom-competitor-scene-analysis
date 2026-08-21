"""Small, deterministic unit-magnitude algebra for formula materialization."""
from __future__ import annotations

import math
from typing import Any, Callable


class UnitScaleError(ValueError):
    pass


_EXACT_SCALES = {
    "1": 1.0,
    "个": 1.0,
    "人": 1.0,
    "单": 1.0,
    "件": 1.0,
    "次": 1.0,
    "天": 1.0,
    "元": 1.0,
    "万元": 1e4,
    "亿元": 1e8,
    "万人": 1e4,
    "万单": 1e4,
    "万件": 1e4,
    "万次": 1e4,
    "%": 1e-2,
    "pp": 1e-2,
}


def unit_scale(unit: Any) -> float:
    token = str(unit or "").strip().replace(" ", "")
    if token in _EXACT_SCALES:
        return _EXACT_SCALES[token]
    raise UnitScaleError(f"unsupported unit magnitude: {unit!r}")


def formula_scale(
    expression: Any,
    factor_unit: Callable[[str], str | None],
) -> float:
    """Return the base-unit scale of a multiply/divide formula."""
    if not isinstance(expression, dict):
        raise UnitScaleError("formula must be an object")
    if "literal" in expression:
        return 1.0
    if "factor_ref" in expression:
        factor_ref = str(expression["factor_ref"])
        unit = factor_unit(factor_ref)
        if unit is None:
            raise UnitScaleError(f"factor {factor_ref!r} is not a directly unit-scaled metric")
        return unit_scale(unit)
    op = expression.get("op")
    args = expression.get("args")
    if not isinstance(args, list) or not args:
        raise UnitScaleError("formula args must be a non-empty array")
    scales = [formula_scale(arg, factor_unit) for arg in args]
    if op == "multiply":
        return math.prod(scales)
    if op == "divide" and len(scales) == 2:
        return scales[0] / scales[1]
    raise UnitScaleError("formula fallback supports multiply/divide only")


def conversion_factor(expression_scale: float, target_unit: Any) -> float:
    factor = expression_scale / unit_scale(target_unit)
    if not math.isfinite(factor) or factor == 0:
        raise UnitScaleError("unit conversion factor must be finite and non-zero")
    return factor
