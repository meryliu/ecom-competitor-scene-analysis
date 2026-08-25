#!/usr/bin/env python3
"""Validation and canonical identity for requirement-local metric constraints."""
from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from typing import Any

from _vendor.ecom_competitor_source import normalize_match_text


ALLOWED_OPERATORS = {"eq", "in", "exclude"}
ALLOWED_PROVENANCE = {
    "model_inferred",
    "user_explicit",
    "user_formula",
    "registered_definition",
    "source_metadata",
}


class MetricConstraintError(ValueError):
    pass


def _display_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def normalize_metric_constraints(value: Any) -> list[dict[str, Any]]:
    """Validate, normalize and deduplicate AND-combined dimension filters."""
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise MetricConstraintError("metric_constraints must be an array")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise MetricConstraintError(
                f"metric_constraints[{position}] must be an object"
            )
        unknown = set(raw) - {
            "constraint_id",
            "kind",
            "operator",
            "values",
            "dimension_hint",
            "provenance",
        }
        if unknown:
            raise MetricConstraintError(
                f"metric_constraints[{position}] has unsupported fields: {sorted(unknown)}"
            )
        kind = str(raw.get("kind") or "dimension_filter")
        if kind != "dimension_filter":
            raise MetricConstraintError(
                f"metric_constraints[{position}].kind must be dimension_filter"
            )
        operator = str(raw.get("operator") or "")
        if operator not in ALLOWED_OPERATORS:
            raise MetricConstraintError(
                f"metric_constraints[{position}].operator must be eq, in or exclude"
            )
        raw_values = raw.get("values")
        if not isinstance(raw_values, list) or not raw_values:
            raise MetricConstraintError(
                f"metric_constraints[{position}].values must be a non-empty array"
            )
        values: list[str] = []
        value_tokens: set[str] = set()
        for raw_value in raw_values:
            display = _display_text(raw_value)
            token = normalize_match_text(display)
            if not display or not token:
                raise MetricConstraintError(
                    f"metric_constraints[{position}].values contains an empty value"
                )
            if token not in value_tokens:
                value_tokens.add(token)
                values.append(display)
        if operator == "eq" and len(values) != 1:
            raise MetricConstraintError(
                f"metric_constraints[{position}].eq requires exactly one value"
            )
        dimension_hint = _display_text(raw.get("dimension_hint")) or None
        provenance = str(raw.get("provenance") or "model_inferred")
        if provenance not in ALLOWED_PROVENANCE:
            raise MetricConstraintError(
                f"metric_constraints[{position}].provenance is unsupported"
            )
        identity = {
            "kind": kind,
            "operator": operator,
            "values": sorted(values, key=normalize_match_text),
            "dimension_hint": dimension_hint,
            "provenance": provenance,
        }
        identity_json = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if identity_json in seen:
            continue
        seen.add(identity_json)
        normalized.append({
            "constraint_id": str(raw.get("constraint_id") or "").strip()
            or "constraint_" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()[:16],
            **identity,
        })
    return sorted(
        normalized,
        key=lambda item: (
            str(item.get("dimension_hint") or ""),
            str(item["operator"]),
            tuple(normalize_match_text(value) for value in item["values"]),
            str(item["constraint_id"]),
        ),
    )


def metric_constraints_fingerprint(value: Any) -> str:
    constraints = normalize_metric_constraints(value)
    canonical = [
        {
            key: deepcopy(item[key])
            for key in ("kind", "operator", "values", "dimension_hint", "provenance")
        }
        for item in constraints
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
