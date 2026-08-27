#!/usr/bin/env python3
"""Materialize bounded, source-revision-scoped dimension sets."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


DEFAULT_MAX_SET_MEMBERS = 200


class SetMaterializationError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def materialize_source_domain_set_spec(
    dimension: str,
    index: dict[str, Any],
    *,
    intent: str = "total",
    max_members: int = DEFAULT_MAX_SET_MEMBERS,
) -> dict[str, Any]:
    """Materialize one live physical dimension domain without IR member lists."""
    by_token = {
        _token(value): str(value)
        for value in (
            ((index.get("dimensions") or {}).get(str(dimension)) or {}).get("values")
            or []
        )
    }
    domain = [by_token[token] for token in sorted(by_token)]
    if not domain:
        raise SetMaterializationError(
            "SET_DOMAIN_UNAVAILABLE", f"维度 {dimension} 缺少可验证枚举"
        )
    if len(domain) > max_members:
        raise SetMaterializationError(
            "SET_DOMAIN_TOO_LARGE",
            f"维度 {dimension} 的物化成员数超过单次上限",
            {"member_count": len(domain), "max_members": max_members},
        )
    source_revision = (index.get("source") or {}).get("revision")
    identity = {
        "dimension_ref": str(dimension),
        "membership_kind": "source_domain",
        "members": domain,
        "source_revision": source_revision,
        "intent": intent,
    }
    return {
        **identity,
        "excluded_members": [],
        "domain_members": list(domain),
        "has_positive_filter": False,
        "set_fingerprint": _fingerprint(identity),
    }


def _token(value: Any) -> str:
    return "".join(str(value or "").strip().lower().split())


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def materialize_set_spec(
    constraints: list[dict[str, Any]],
    index: dict[str, Any],
    *,
    intent: str = "total",
    max_members: int = DEFAULT_MAX_SET_MEMBERS,
) -> dict[str, Any]:
    """Resolve constraints against one live physical dimension domain."""
    dimensions = {
        str(item.get("source_dimension") or "")
        for item in constraints if isinstance(item, dict)
    }
    dimensions.discard("")
    if len(dimensions) != 1:
        raise SetMaterializationError(
            "SET_DIMENSION_UNRESOLVED",
            "集合物化要求且仅允许一个已解析源维度",
            {"source_dimensions": sorted(dimensions)},
        )
    dimension = next(iter(dimensions))
    domain = list(
        ((index.get("dimensions") or {}).get(dimension) or {}).get("values") or []
    )
    if not domain:
        raise SetMaterializationError(
            "SET_DOMAIN_UNAVAILABLE", f"维度 {dimension} 缺少可验证枚举"
        )
    by_token = {_token(value): str(value) for value in domain}
    selected = set(by_token)
    excluded: set[str] = set()
    positive = False
    for constraint in constraints:
        operator = str(constraint.get("operator") or "")
        values = {_token(value) for value in constraint.get("values") or []}
        if not values.issubset(by_token):
            raise SetMaterializationError(
                "SET_MEMBER_UNAVAILABLE",
                f"维度 {dimension} 不包含全部请求成员",
                {"values": sorted(values), "available": domain},
            )
        if operator in {"eq", "in"}:
            selected &= values
            positive = True
        elif operator == "exclude":
            selected -= values
            excluded |= values
        else:
            raise SetMaterializationError(
                "SET_OPERATOR_UNSUPPORTED", f"集合物化不支持操作符 {operator!r}"
            )
    if not selected:
        raise SetMaterializationError("SET_EMPTY", "集合筛选后的成员为空")
    membership_kind = (
        "complement" if excluded and not positive
        else "explicit" if positive or excluded
        else "source_domain"
    )
    materialized_count = (
        len(domain) if membership_kind in {"source_domain", "complement"}
        else len(selected)
    )
    if materialized_count > max_members:
        raise SetMaterializationError(
            "SET_DOMAIN_TOO_LARGE",
            f"维度 {dimension} 的物化成员数超过单次上限",
            {"member_count": materialized_count, "max_members": max_members},
        )
    source_revision = (index.get("source") or {}).get("revision")
    identity = {
        "dimension_ref": dimension,
        "membership_kind": membership_kind,
        "members": [by_token[value] for value in sorted(selected)],
        "source_revision": source_revision,
        "intent": intent,
    }
    return {
        **identity,
        "excluded_members": [by_token[value] for value in sorted(excluded)],
        "domain_members": [by_token[value] for value in sorted(by_token)],
        "has_positive_filter": positive,
        "set_fingerprint": _fingerprint(identity),
    }


def set_aggregate_expression(
    spec: dict[str, Any], metric_ref: str, period_role: str, mode: str
) -> dict[str, Any]:
    dimension = str(spec["dimension_ref"])

    def fact(member: str) -> dict[str, Any]:
        return {"fact": {
            "metric_ref": metric_ref,
            "period_role": period_role,
            "dimensions": {dimension: member},
        }}

    def summed(members: list[str]) -> dict[str, Any]:
        args = [fact(member) for member in members]
        return args[0] if len(args) == 1 else {"op": "sum", "args": args}

    if (
        mode == "same_metric_total_minus_members"
        and spec.get("excluded_members")
        and not spec.get("has_positive_filter")
    ):
        return {"op": "subtract", "args": [
            summed(list(spec["domain_members"])),
            summed(list(spec["excluded_members"])),
        ]}
    return summed(list(spec["members"]))
