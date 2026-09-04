"""Small, shared helpers for request-side metadata provenance.

The resolver must not treat a value merely emitted in an IR as user-confirmed
evidence.  This module deliberately contains no catalogue lookup or ranking
logic; it only classifies the trust level of request-side unit/object fields.
"""
from __future__ import annotations

from typing import Any


MODEL_PROVENANCE = "model_inferred"
FORMULA_PROVENANCE = "user_formula"
EXPLICIT_PROVENANCE = "user_explicit"
AUTHORITATIVE_PROVENANCE = {
    "registered_definition",
    "source_metadata",
    "source_metric_metadata",
    "business_intent_policy",
}


def _has_evidence(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def effective_constraint_provenance(
    requested: dict[str, Any], field: str
) -> str:
    """Return the effective trust level for a request-side metadata field.

    ``user_explicit`` is accepted only when an evidence field is present.  A
    formula describes a relationship between inputs, not their units or
    metric objects, so ``user_formula`` intentionally degrades to inference.
    Registered/source/policy metadata remain authoritative.
    """
    source = str(
        requested.get(f"{field}_provenance")
        or requested.get(f"{field}_source")
        or ""
    ).strip()
    if source in AUTHORITATIVE_PROVENANCE:
        return source
    if source == EXPLICIT_PROVENANCE:
        return EXPLICIT_PROVENANCE if _has_evidence(
            requested.get(f"{field}_evidence")
        ) else MODEL_PROVENANCE
    if source == FORMULA_PROVENANCE:
        return MODEL_PROVENANCE

    # Legacy callers put the generic provenance on composition leaves.  Keep
    # registered definitions strong while avoiding an implicit user-explicit
    # upgrade for ordinary metrics.
    generic = str(requested.get("provenance") or "").strip()
    if generic in AUTHORITATIVE_PROVENANCE:
        return generic
    if generic == EXPLICIT_PROVENANCE and _has_evidence(
        requested.get(f"{field}_evidence")
    ):
        return EXPLICIT_PROVENANCE
    return MODEL_PROVENANCE


def is_hard_constraint_provenance(provenance: str) -> bool:
    return provenance in AUTHORITATIVE_PROVENANCE or provenance == EXPLICIT_PROVENANCE

