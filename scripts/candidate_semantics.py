#!/usr/bin/env python3
"""Pure semantic evidence helpers for constrained metric candidates."""
from __future__ import annotations

import difflib
from typing import Any

from _vendor.ecom_competitor_source import normalize_match_text


def containment_score(requested: Any, candidate: Any) -> float:
    requested_text = normalize_match_text(requested)
    candidate_text = normalize_match_text(candidate)
    if not requested_text or not candidate_text:
        return 0.0
    if requested_text == candidate_text:
        return 1.0
    if len(candidate_text) >= 4 and requested_text.endswith(candidate_text):
        return 1.0
    if len(requested_text) >= 4 and candidate_text.endswith(requested_text):
        return 1.0
    if candidate_text in requested_text or requested_text in candidate_text:
        shorter = min(len(requested_text), len(candidate_text))
        longer = max(len(requested_text), len(candidate_text))
        return min(0.9, 0.5 + 0.5 * shorter / longer)
    return difflib.SequenceMatcher(None, requested_text, candidate_text).ratio()


def core_strip_tokens(policy: dict[str, Any]) -> list[str]:
    semantic = policy.get("semantic_normalization") or {}
    return sorted(
        {
            normalize_match_text(token)
            for group in ("comparison_terms", "measure_terms", "grain_terms")
            for tokens in (semantic.get(group) or {}).values()
            for token in tokens
            if normalize_match_text(token)
        },
        key=len,
        reverse=True,
    )


def operator_terms(policy: dict[str, Any], operator: str | None = None) -> list[str]:
    groups = (
        (policy.get("semantic_normalization") or {}).get("constraint_operator_terms")
        or {}
    )
    selected = [str(operator)] if operator else sorted(groups)
    return sorted(
        {
            normalize_match_text(token)
            for name in selected
            for token in groups.get(name) or []
            if normalize_match_text(token)
        },
        key=len,
        reverse=True,
    )


def strip_core_tokens(value: Any, policy: dict[str, Any]) -> str:
    text = normalize_match_text(value)
    for token in core_strip_tokens(policy):
        text = text.replace(token, "")
    return text


def constrained_core_evidence(
    requested_core: Any,
    candidate_name: str,
    metadata: dict[str, Any],
    constraints: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    requested = strip_core_tokens(requested_core, policy)
    removable = sorted(
        {
            normalize_match_text(value)
            for constraint in constraints
            for value in constraint.get("values") or []
            if normalize_match_text(value)
        }
        | set(operator_terms(policy)),
        key=len,
        reverse=True,
    )
    best_score = 0.0
    best_text = ""
    best_core = ""
    for value in (candidate_name, *(metadata.get("aliases") or [])):
        candidate_text = strip_core_tokens(value, policy)
        for token in removable:
            candidate_text = candidate_text.replace(token, "")
        score = containment_score(requested, candidate_text)
        if score > best_score:
            best_score = score
            best_text = str(value)
            best_core = candidate_text
    return {
        "score": best_score,
        "requested_core": requested,
        "matched_text": best_text,
        "matched_core": best_core,
    }


def breakdown_core_evidence(
    requested_core: Any,
    candidate_name: str,
    metadata: dict[str, Any],
    requested_dimensions: list[str],
    resolved_dimension: str | None,
    dimension_catalogue: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Score the core after separating a proven breakdown from metric labels."""
    base = constrained_core_evidence(
        requested_core, candidate_name, metadata, [], policy
    )
    result = {**base, "mode": "plain", "resolved_dimension": resolved_dimension}
    if not requested_dimensions or not resolved_dimension:
        return result

    dimension_metadata = dimension_catalogue.get(resolved_dimension) or {}
    dimension_terms = {
        normalize_match_text(value)
        for value in [
            *requested_dimensions,
            resolved_dimension,
            *(dimension_metadata.get("aliases") or []),
        ]
        if normalize_match_text(value)
    }
    breakdown_terms = sorted(
        dimension_terms
        | {f"分{value}" for value in dimension_terms}
        | {f"各{value}" for value in dimension_terms},
        key=len,
        reverse=True,
    )
    universe_terms = sorted(
        {
            normalize_match_text(token)
            for token in (
                (policy.get("semantic_normalization") or {})
                .get("scope_terms", {})
                .get("universe", [])
            )
            if normalize_match_text(token)
        },
        key=len,
        reverse=True,
    )
    requested = strip_core_tokens(requested_core, policy)
    for token in universe_terms:
        requested = requested.replace(token, "")
    if not requested:
        return result

    for value in (candidate_name, *(metadata.get("aliases") or [])):
        candidate_core = strip_core_tokens(value, policy)
        for token in breakdown_terms:
            candidate_core = candidate_core.replace(token, "")
        score = containment_score(requested, candidate_core)
        if score > float(result.get("score") or 0.0):
            result = {
                "score": score,
                "requested_core": requested,
                "matched_text": str(value),
                "matched_core": candidate_core,
                "mode": "breakdown_scoped",
                "resolved_dimension": resolved_dimension,
            }
    return result


def full_scope_evidence(
    phrase: str,
    candidate_name: str,
    metadata: dict[str, Any],
    constraints: list[dict[str, Any]],
    core_evidence: dict[str, Any],
    core_floor: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Require one source label to cover every structured scope constraint."""
    phrase_token = normalize_match_text(phrase)
    core_score = float(core_evidence.get("score") or 0.0)
    requested_core = str(core_evidence.get("requested_core") or "")
    base = {
        "status": "no_match",
        "full_scope": False,
        "score": 0.0,
        "value_score": 0.0,
        "operator_score": 0.0,
        "matched_text": None,
        "matched_core": None,
        "core_relation": None,
        "extra_core": None,
        "constraint_checks": [],
    }
    if (
        not phrase_token
        or not constraints
        or not requested_core
        or core_score < core_floor
    ):
        return base

    exclude_terms = operator_terms(policy, "exclude")
    removable = sorted(
        {
            normalize_match_text(value)
            for constraint in constraints
            for value in constraint.get("values") or []
            if normalize_match_text(value)
        }
        | set(operator_terms(policy)),
        key=len,
        reverse=True,
    )
    labels = [candidate_name, *(metadata.get("aliases") or [])]
    matches: list[dict[str, Any]] = []
    for raw_label in labels:
        label = normalize_match_text(raw_label)
        checks: list[dict[str, Any]] = []
        all_values_match = True
        all_operators_match = True
        for constraint in constraints:
            operator = str(constraint.get("operator") or "")
            values = [
                normalize_match_text(value) for value in constraint.get("values") or []
            ]
            values_match = bool(values) and all(
                value in phrase_token and value in label for value in values
            )
            if operator == "exclude":
                operator_match = any(term in phrase_token for term in exclude_terms) and any(
                    term in label for term in exclude_terms
                )
            elif operator in {"eq", "in"}:
                operator_match = not any(term in label for term in exclude_terms)
            else:
                operator_match = False
            checks.append({
                "operator": operator,
                "values": [str(value) for value in constraint.get("values") or []],
                "values_match": values_match,
                "operator_match": operator_match,
            })
            all_values_match = all_values_match and values_match
            all_operators_match = all_operators_match and operator_match
        if all_values_match and all_operators_match:
            label_core = strip_core_tokens(raw_label, policy)
            for token in removable:
                label_core = label_core.replace(token, "")
            label_core_score = containment_score(requested_core, label_core)
            if label_core_score < core_floor:
                continue
            extra_core = None
            if label_core == requested_core:
                core_relation = "exact"
            elif requested_core in label_core:
                extra_core = label_core.replace(requested_core, "", 1)
                core_relation = "overqualified" if extra_core else "exact"
            else:
                core_relation = "fuzzy"
            matches.append({
                "status": (
                    "overqualified"
                    if core_relation == "overqualified"
                    else "exact"
                ),
                "full_scope": True,
                "score": min(label_core_score, 1.0),
                "value_score": 1.0,
                "operator_score": 1.0,
                "matched_text": str(raw_label),
                "matched_core": label_core,
                "core_relation": core_relation,
                "extra_core": extra_core,
                "constraint_checks": checks,
            })
    if not matches:
        return base
    relation_rank = {"exact": 0, "fuzzy": 1, "overqualified": 2}
    return min(
        matches,
        key=lambda item: (
            relation_rank.get(str(item.get("core_relation")), 3),
            -float(item.get("score") or 0.0),
        ),
    )
