#!/usr/bin/env python3
"""Provider-neutral Requirement fulfillment candidate ordering."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


FULFILLMENT_TIERS = {
    "direct_fact": 0,
    "member_selector": 1,
    "set_aggregate": 2,
    "registered_composition": 3,
    "registered_derived": 4,
    "safe_inference": 5,
}

PATH_CANDIDATE_TYPES = {
    "direct_fact": "direct_fact",
    "aggregate_fact": "direct_fact",
    "source_scoped_fact": "direct_fact",
    "source_derived_fact": "direct_fact",
    "source_derived_calculation": "registered_derived",
    "member_selector": "member_selector",
    "additive_member_sum": "set_aggregate",
    "same_metric_total_minus_members": "set_aggregate",
    "registered_composition": "registered_composition",
    "registered_derived": "registered_derived",
    "safe_inference": "safe_inference",
}


class FulfillmentCandidateError(ValueError):
    pass


def candidate_type_for_path(path: Any) -> str | None:
    return PATH_CANDIDATE_TYPES.get(str(path or ""))


def fulfillment_tier_for_path(path: Any, default: int = 99) -> int:
    candidate_type = candidate_type_for_path(path)
    return FULFILLMENT_TIERS.get(candidate_type, default)


def rank_fulfillment_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank viable paths without changing semantic candidate gates."""
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        candidate_type = str(candidate.get("candidate_type") or "")
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_type not in FULFILLMENT_TIERS or not candidate_id:
            raise FulfillmentCandidateError("candidate_type and candidate_id are required")
        identity = (candidate_type, candidate_id)
        if identity in seen:
            continue
        seen.add(identity)
        item = deepcopy(candidate)
        item["fulfillment_tier"] = FULFILLMENT_TIERS[candidate_type]
        normalized.append(item)
    return sorted(normalized, key=lambda item: (
        0 if item.get("status") == "viable" else 1,
        int(item["fulfillment_tier"]),
        int(item.get("semantic_tier") or 0),
        int(item.get("constraint_tier") or 0),
        -float(item.get("confidence") or 0.0),
        str(item["candidate_id"]),
    ))


def select_fulfillment_candidate(
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    ranked = rank_fulfillment_candidates(candidates)
    selected = next((item for item in ranked if item.get("status") == "viable"), None)
    return selected, ranked
