#!/usr/bin/env python3
"""Deterministic business resolution policy and query-scoped source overlay."""
from __future__ import annotations

import hashlib
import json
import re
import difflib
from copy import deepcopy
from pathlib import Path
from typing import Any

from _vendor.ecom_competitor_source import (
    match_catalogue_name,
    normalize_match_text,
    resolve_catalogue_name,
)
from business_intent_policy import (
    business_intent_policy_hash,
    generate_metric_hypotheses,
    load_business_intent_policy,
)
from candidate_semantics import (
    breakdown_core_evidence,
    constrained_core_evidence,
    full_scope_evidence,
)
from fulfillment_candidates import (
    candidate_type_for_path,
    fulfillment_tier_for_path,
    select_fulfillment_candidate,
)
from metric_constraints import (
    MetricConstraintError,
    metric_constraints_fingerprint,
    normalize_metric_constraints,
)
from semantic_context_guard import extract_current_core_hint
from source_capability import evaluate_structural_grain_capability
from time_rollup import normalize_period as _normalize_time_period


POLICY_SCHEMA = "resolution_policy/2.0"
ENGINE_VERSION = "2.15.0"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "resolution-policy-registry.json"
)
DEFAULT_BUSINESS_INTENT_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "business-intent-policy-registry.json"
)
ALLOWED_GATES = {
    "metric_dimension_compatible",
    "source_domain_subset",
    "protected_semantics_compatible",
    "metric_object_compatible",
    "unit_compatible",
}
ALLOWED_EVIDENCE = {
    "dimension_domain_exact",
    "dimension_name_exact",
    "confirmed_patch",
}
UNRESOLVED_UNITS = {"", "unknown", "待元信息解析", "未解析"}
ALLOWED_POLICY_KEYS = {
    "schema_version",
    "policy_version",
    "limits",
    "rules",
    "semantic_normalization",
    "candidate_evaluation",
    "grain_rollup",
}
ALLOWED_RULE_KEYS = {"hard_gates", "strong_evidence", "auto", "confirm"}
ALLOWED_AUTO_KEYS = {
    "require_unique_viable",
    "minimum_strong_evidence",
    "minimum_confidence",
    "minimum_candidate_margin",
    "require_strong_provenance_or_semantics",
}
ALLOWED_CONFIRM_KEYS = {"when_viable_candidates_exist"}


class ResolutionPolicyError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, value: Any, length: int = 24) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def resolution_policy_hash(policy: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def load_resolution_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = path or DEFAULT_POLICY_PATH
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    validate_resolution_policy(policy)
    return policy


def validate_resolution_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict) or policy.get("schema_version") != POLICY_SCHEMA:
        raise ResolutionPolicyError("INVALID_RESOLUTION_POLICY", f"策略必须使用 {POLICY_SCHEMA}")
    unknown_top = set(policy) - ALLOWED_POLICY_KEYS
    if unknown_top:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD",
            "策略包含未允许的顶层字段",
            {"fields": sorted(unknown_top)},
        )
    limits = policy.get("limits") or {}
    if set(limits) - {"max_candidates_per_case"}:
        raise ResolutionPolicyError("INVALID_RESOLUTION_POLICY_FIELD", "limits 包含未允许字段")
    candidate_limit = limits.get("max_candidates_per_case", 3)
    if not isinstance(candidate_limit, int) or not 1 <= candidate_limit <= 10:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_LIMIT",
            "max_candidates_per_case 必须是 1 到 10 的整数",
        )
    semantic_normalization = policy.get("semantic_normalization") or {}
    if not isinstance(semantic_normalization, dict):
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY", "semantic_normalization 必须是对象"
        )
    allowed_semantic_keys = {
        "comparison_terms",
        "measure_terms",
        "core_measure_terms",
        "grain_terms",
        "scope_terms",
        "constraint_operator_terms",
        "equivalence_rules",
    }
    unknown_semantic = set(semantic_normalization) - allowed_semantic_keys
    if unknown_semantic:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD",
            "semantic_normalization 包含未允许字段",
            {"fields": sorted(unknown_semantic)},
        )
    for field in (
        "comparison_terms",
        "measure_terms",
        "core_measure_terms",
        "grain_terms",
        "scope_terms",
        "constraint_operator_terms",
    ):
        values = semantic_normalization.get(field) or {}
        if not isinstance(values, dict) or any(
            not isinstance(key, str)
            or not key
            or not isinstance(tokens, list)
            or not tokens
            or not all(isinstance(token, str) and token for token in tokens)
            for key, tokens in values.items()
        ):
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY",
                f"semantic_normalization.{field} 必须是非空词组映射",
            )
    equivalence_rules = semantic_normalization.get("equivalence_rules") or []
    if not isinstance(equivalence_rules, list) or any(
        not isinstance(item, dict)
        or not item.get("rule_id")
        or not item.get("comparison_type")
        or not item.get("measure_type")
        or set(item) - {
            "rule_id",
            "comparison_type",
            "measure_type",
            "required_metric_object",
            "required_units",
        }
        or (
            item.get("required_metric_object") is not None
            and item.get("required_metric_object") not in {"volume", "ratio"}
        )
        or (
            item.get("required_units") is not None
            and (
                not isinstance(item.get("required_units"), list)
                or not all(isinstance(unit, str) and unit for unit in item.get("required_units"))
            )
        )
        for item in equivalence_rules
    ):
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY",
            "semantic_normalization.equivalence_rules 配置非法",
        )
    candidate_evaluation = policy.get("candidate_evaluation") or {}
    if not isinstance(candidate_evaluation, dict):
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY", "candidate_evaluation 必须是对象"
        )
    unknown_evaluation = set(candidate_evaluation) - {
        "lexical_recall_floor",
        "core_semantic_floor",
        "minimum_candidate_margin",
        "allow_unique_capability_selection",
    }
    if unknown_evaluation:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD",
            "candidate_evaluation 包含未允许字段",
            {"fields": sorted(unknown_evaluation)},
        )
    for field in (
        "lexical_recall_floor",
        "core_semantic_floor",
        "minimum_candidate_margin",
    ):
        value = candidate_evaluation.get(
            field,
            0.78 if field in {"lexical_recall_floor", "core_semantic_floor"} else 0.1,
        )
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY",
                f"candidate_evaluation.{field} 必须是 0 到 1 的数值",
            )
    grain_rollup = policy.get("grain_rollup") or {}
    if not isinstance(grain_rollup, dict) or set(grain_rollup) - {
        "allowed_edges",
        "aggregation_prerequisites",
        "priority",
        "week_weight_rule",
    }:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD", "grain_rollup 包含未允许字段"
        )
    edges = grain_rollup.get("allowed_edges") or []
    allowed_grains = {"day", "week", "month", "quarter", "year"}
    if not isinstance(edges, list) or any(
        not isinstance(edge, list)
        or len(edge) != 2
        or any(not isinstance(grain, str) or grain not in allowed_grains for grain in edge)
        or edge[0] == edge[1]
        for edge in edges
    ):
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY", "grain_rollup.allowed_edges 必须是合法粒度边数组"
        )
    prerequisites = grain_rollup.get("aggregation_prerequisites")
    if prerequisites is not None and prerequisites != ["metric_additive"]:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY", "grain_rollup.aggregation_prerequisites 仅支持 metric_additive"
        )
    priority = grain_rollup.get("priority")
    if priority is not None:
        if not isinstance(priority, dict) or any(
            key not in {"month", "quarter", "year"}
            or not isinstance(value, list)
            or not value
            or any(item not in allowed_grains for item in value)
            for key, value in priority.items()
        ):
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY", "grain_rollup.priority 配置非法"
            )
    week_weight_rule = grain_rollup.get("week_weight_rule")
    if week_weight_rule is not None and week_weight_rule != {
        "calendar": "source.period_semantics.week",
        "formula": "overlap_days/7",
        "inclusive_bounds": True,
    }:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY", "grain_rollup.week_weight_rule 配置非法"
        )
    rules = policy.get("rules")
    if not isinstance(rules, dict):
        raise ResolutionPolicyError("INVALID_RESOLUTION_POLICY", "rules 必须是对象")
    required = {"fact_block_joint_resolution", "query_name_resolution"}
    if not required.issubset(rules):
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY",
            "缺少必需决策规则",
            {"missing": sorted(required - set(rules))},
        )
    for rule_id, rule in rules.items():
        if not isinstance(rule, dict):
            raise ResolutionPolicyError("INVALID_RESOLUTION_POLICY", f"规则 {rule_id} 必须是对象")
        unknown_fields = set(rule) - ALLOWED_RULE_KEYS
        if unknown_fields:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY_FIELD",
                f"规则 {rule_id} 包含未允许字段",
                {"fields": sorted(unknown_fields)},
            )
        gates = rule.get("hard_gates") or []
        evidence = rule.get("strong_evidence") or []
        auto = rule.get("auto") or {}
        confirm = rule.get("confirm") or {}
        if not isinstance(auto, dict) or set(auto) - ALLOWED_AUTO_KEYS:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY_FIELD", f"规则 {rule_id}.auto 包含未允许字段"
            )
        if not isinstance(confirm, dict) or set(confirm) - ALLOWED_CONFIRM_KEYS:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY_FIELD", f"规则 {rule_id}.confirm 包含未允许字段"
            )
        unknown = (set(gates) - ALLOWED_GATES) | (set(evidence) - ALLOWED_EVIDENCE)
        if unknown:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY_OPERATOR",
                f"规则 {rule_id} 使用了未允许的操作符",
                {"operators": sorted(unknown)},
            )


def _metric_object(metadata: dict[str, Any]) -> str | None:
    declared = str(metadata.get("metric_object") or "").strip().lower()
    if declared in {"volume", "ratio"}:
        return declared
    unit = str(metadata.get("unit") or "").strip().lower()
    return "ratio" if unit in {"%", "rate", "ratio", "share", "pp"} else "volume" if unit else None


def _unit_compatible(expected: Any, actual: Any) -> bool:
    expected_text = str(expected or "").strip().lower()
    actual_text = str(actual or "").strip().lower()
    return expected_text in UNRESOLVED_UNITS or not expected_text or expected_text == actual_text


def _containment_score(requested: Any, candidate: Any) -> float:
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


def _derived_tokens(policy: dict[str, Any]) -> list[str]:
    semantic = policy.get("semantic_normalization") or {}
    return sorted(
        {
            normalize_match_text(token)
            for group in ("comparison_terms", "measure_terms")
            for tokens in (semantic.get(group) or {}).values()
            for token in tokens
            if normalize_match_text(token)
        },
        key=len,
        reverse=True,
    )


def _core_strip_tokens(policy: dict[str, Any]) -> list[str]:
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


def _strip_derived_tokens(value: Any, policy: dict[str, Any]) -> str:
    text = normalize_match_text(value)
    for token in _core_strip_tokens(policy):
        text = text.replace(token, "")
    return text


def _grain_signature(value: Any, policy: dict[str, Any]) -> str | None:
    text = normalize_match_text(value)
    semantic = policy.get("semantic_normalization") or {}
    for grain, tokens in (semantic.get("grain_terms") or {}).items():
        if any(normalize_match_text(token) in text for token in tokens):
            return str(grain)
    return None


def _core_semantic_score(
    requested_core: Any, candidate_name: str, metadata: dict[str, Any], policy: dict[str, Any]
) -> float:
    requested = _strip_derived_tokens(requested_core, policy)
    candidate_texts = [candidate_name, *(metadata.get("aliases") or [])]
    return max(
        (_containment_score(requested, _strip_derived_tokens(value, policy)) for value in candidate_texts),
        default=0.0,
    )


def _derived_semantic_score(
    intent_id: Any, candidate_name: str, metadata: dict[str, Any], policy: dict[str, Any]
) -> float:
    if str(intent_id) == "declared_metric":
        return 0.0
    texts = [candidate_name, *(metadata.get("aliases") or [])]
    tokens = _derived_tokens(policy)
    return 1.0 if any(token in normalize_match_text(text) for text in texts for token in tokens) else 0.0


def _semantic_signature(
    value: Any,
    metadata: dict[str, Any],
    expected_object: str | None,
    policy: dict[str, Any],
) -> dict[str, str | None]:
    """Normalize comparison and measure terms without treating every token as distinct."""
    text = normalize_match_text(value)
    semantic = policy.get("semantic_normalization") or {}
    comparison_type: str | None = None
    for comparison, tokens in (semantic.get("comparison_terms") or {}).items():
        if any(normalize_match_text(token) in text for token in tokens):
            comparison_type = str(comparison)
            break
    measure_type: str | None = None
    for measure, tokens in (semantic.get("measure_terms") or {}).items():
        if any(normalize_match_text(token) in text for token in tokens):
            measure_type = str(measure)
            break
    actual_object = _metric_object(metadata)
    unit = str(metadata.get("unit") or "").strip().lower()
    # A comparison-only shorthand such as "同比" is expanded only by a
    # configured equivalence rule whose object and unit constraints pass.
    if comparison_type and measure_type is None:
        for rule in semantic.get("equivalence_rules") or []:
            required_object = str(rule.get("required_metric_object") or "")
            required_units = {
                str(item).strip().lower()
                for item in rule.get("required_units") or []
            }
            if (
                str(rule.get("comparison_type") or "") == comparison_type
                and (not required_object or expected_object == required_object or actual_object == required_object)
                and (not required_units or unit in required_units)
            ):
                measure_type = str(rule.get("measure_type") or "") or None
                break
    return {
        "comparison_type": comparison_type,
        "measure_type": measure_type,
        "grain_signature": _grain_signature(value, policy),
        "object": expected_object or actual_object,
        "unit": unit or None,
    }


def _semantic_compatibility(
    requested_text: str,
    candidate_text: str,
    candidate_name: str,
    metadata: dict[str, Any],
    expected_object: str | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Classify protected-token differences as equivalent, unknown, or conflict."""
    requested = _semantic_signature(requested_text, metadata, expected_object, policy)
    candidate = _semantic_signature(candidate_text, metadata, expected_object, policy)
    canonical = _semantic_signature(candidate_name, metadata, expected_object, policy)
    # Canonical metadata names are stronger evidence than a fragmented alias.
    for field in ("comparison_type", "measure_type"):
        if requested.get(field) is None:
            continue
        if candidate.get(field) is None:
            candidate[field] = canonical.get(field)
    requested_measure = requested.get("measure_type")
    candidate_measure = candidate.get("measure_type")
    requested_comparison = requested.get("comparison_type")
    candidate_comparison = candidate.get("comparison_type")
    if requested_measure and candidate_measure and requested_measure != candidate_measure:
        return {
            "status": "conflict",
            "requested": requested,
            "candidate": candidate,
            "reason": "measure_type_conflict",
        }
    if requested_comparison and candidate_comparison and requested_comparison != candidate_comparison:
        return {
            "status": "conflict",
            "requested": requested,
            "candidate": candidate,
            "reason": "comparison_type_conflict",
        }
    if (
        requested_measure
        and requested_measure == candidate_measure
        and (
            requested_comparison is None
            or candidate_comparison == requested_comparison
        )
    ) or (
        requested_comparison == candidate_comparison
        and requested_comparison is not None
        and requested_measure in {None, candidate_measure}
    ):
        status = "equivalent"
    elif requested_measure or requested_comparison or candidate_measure or candidate_comparison:
        status = "unknown"
    else:
        status = "equivalent"
    return {
        "status": status,
        "requested": requested,
        "candidate": candidate,
        "reason": "semantic_equivalence" if status == "equivalent" else "semantic_incomplete",
    }


def _context_tasks(contexts: list[dict[str, Any]], requested_name: str, candidate_name: str) -> list[str]:
    requested_token = normalize_match_text(requested_name)
    candidate_token = normalize_match_text(candidate_name)
    matched: list[str] = []
    for context in contexts:
        names = {
            normalize_match_text(item.get("name"))
            for item in context.get("metrics") or []
            if isinstance(item, dict)
        }
        names.update(
            normalize_match_text(item.get("metric"))
            for intent in context.get("composition_intents") or []
            if isinstance(intent, dict)
            for item in intent.get("inputs") or []
            if isinstance(item, dict) and item.get("metric")
        )
        if requested_token in names or candidate_token in names:
            matched.append(str(context.get("task_id") or "default"))
    return sorted(set(matched))


def _candidate_packet(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(candidate[key])
        for key in (
            "candidate_id",
            "metric",
            "dimension",
            "confidence",
            "evidence",
            "conflicts",
            "domain_summary",
        )
        if key in candidate
    }


def _patch_selection(
    case_id: str,
    patches: list[dict[str, Any]],
    source: dict[str, Any],
    policy_hash: str,
    expected_extra: dict[str, Any] | None = None,
    require_engine_version: bool = False,
) -> tuple[str | None, str | None]:
    for patch in patches:
        if not isinstance(patch, dict) or patch.get("case_id") != case_id:
            continue
        expected = {
            "source_revision": source.get("revision"),
            "schema_hash": source.get("schema_hash"),
            "resolution_policy_hash": policy_hash,
        }
        if require_engine_version:
            expected["resolution_engine_version"] = ENGINE_VERSION
        expected.update(expected_extra or {})
        mismatches = [key for key, value in expected.items() if patch.get(key) != value]
        if mismatches:
            return None, "stale_patch"
        return str(patch.get("candidate_id") or ""), None
    return None, None


def _query_metric_resolution(
    requested: dict[str, Any],
    catalogue: dict[str, dict[str, Any]],
    query: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    name = str(requested.get("name") or "")
    match = match_catalogue_name(name, catalogue, "metric")
    viable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    configured_gates = set(
        policy["rules"]["query_name_resolution"].get("hard_gates") or []
    )
    for raw in match.get("candidates") or []:
        candidate_name = str(raw.get("name") or "")
        metadata = catalogue.get(candidate_name) or {}
        raw_conflicts = list(raw.get("conflicts") or [])
        conflicts = [
            conflict
            for conflict in raw_conflicts
            if not str(conflict).startswith("protected_term_difference:")
        ]
        expected_object = requested.get("metric_object")
        actual_object = _metric_object(metadata)
        object_provenance = str(
            requested.get("metric_object_provenance")
            or requested.get("metric_object_source")
            or requested.get("provenance")
            or "model_inferred"
        )
        soft_conflicts: list[str] = []
        if (
            "metric_object_compatible" in configured_gates
            and expected_object
            and actual_object
            and expected_object != actual_object
        ):
            if object_provenance == "model_inferred":
                soft_conflicts.append("model_inferred_metric_object_mismatch")
            else:
                conflicts.append("metric_object_mismatch")
        unit_provenance = str(
            requested.get("unit_provenance")
            or requested.get("unit_source")
            or (
                "model_inferred"
                if str(requested.get("unit") or "").strip().lower() in UNRESOLVED_UNITS
                else "user_explicit"
            )
        )
        if (
            "unit_compatible" in configured_gates
            and not _unit_compatible(requested.get("unit"), metadata.get("unit"))
        ):
            if unit_provenance == "model_inferred":
                soft_conflicts.append("model_inferred_unit_mismatch")
            else:
                conflicts.append("unit_mismatch")
        semantic = _semantic_compatibility(
            name,
            str(raw.get("matched_text") or candidate_name),
            candidate_name,
            metadata,
            str(expected_object) if expected_object else None,
            policy,
        )
        containment_score = max(
            _containment_score(name, str(raw.get("matched_text") or candidate_name)),
            _containment_score(name, candidate_name),
        )
        lexical_score = float(raw.get("confidence") or 0.0)
        confidence = max(lexical_score, containment_score)
        protected_conflicts = [
            conflict
            for conflict in raw_conflicts
            if str(conflict).startswith("protected_term_difference:")
        ]
        if protected_conflicts and semantic["status"] == "conflict":
            conflicts.append(f"protected_semantics_conflict:{semantic['reason']}")
        elif protected_conflicts and semantic["status"] == "unknown":
            soft_conflicts.extend(protected_conflicts)
        candidate = {
            "name": candidate_name,
            "confidence": confidence,
            "lexical_score": lexical_score,
            "core_containment_score": containment_score,
            "evidence": [
                str(raw.get("match_method") or match.get("match_method") or "name_similarity")
            ],
            "conflicts": sorted(set(conflicts)),
            "soft_conflicts": sorted(set(soft_conflicts)),
            "semantic_status": semantic["status"],
            "semantic": semantic,
            "match_method": raw.get("match_method") or match.get("match_method"),
        }
        if conflicts:
            rejected.append(candidate)
            continue
        semantic_hint = (
            expected_object == "ratio"
            and any(token in query for token in ("同比", "增速", "增长率", "涨幅", "占比", "率"))
            and actual_object == "ratio"
        )
        provenance = str(requested.get("provenance") or "model_inferred")
        if containment_score >= 0.95:
            candidate["evidence"].append("core_term_containment")
        candidate["evidence"] += (["query_semantic_match"] if semantic_hint else [])
        candidate["strong"] = (
            str(candidate.get("match_method")) in {"standard_name", "metadata_alias", "builtin_alias"}
            or provenance in {"user_explicit", "user_formula", "registered_definition", "source_metadata"}
            or semantic_hint
            or containment_score >= 0.95
        )
        if semantic["status"] == "unknown" and not semantic_hint:
            candidate["strong"] = False
        if semantic["status"] == "equivalent":
            candidate["evidence"].append("semantic_equivalence")
        viable.append(candidate)
    viable.sort(key=lambda item: item["confidence"], reverse=True)
    rule = policy["rules"]["query_name_resolution"]["auto"]
    evaluation = policy.get("candidate_evaluation") or {}
    minimum_confidence = float(
        evaluation.get("lexical_recall_floor", rule.get("minimum_confidence", 1.0))
    )
    eligible = [
        item for item in viable if item["confidence"] >= minimum_confidence
    ]
    margin = (
        eligible[0]["confidence"] - (eligible[1]["confidence"] if len(eligible) > 1 else 0.0)
        if eligible
        else 0.0
    )
    if (
        len(eligible) == 1
        and margin >= float(
            evaluation.get("minimum_candidate_margin", rule.get("minimum_candidate_margin", 1.0))
        )
        and (eligible[0]["strong"] or not rule.get("require_strong_provenance_or_semantics"))
    ):
        return {
            "action": "auto",
            "status": "bound",
            "binding": eligible[0]["name"],
            "decision_basis": "unique_lexical_candidate_after_semantic_filter",
            "candidates": viable,
            "rejected_candidates": rejected,
        }
    if viable:
        status = "ambiguous"
    elif rejected and str(match.get("match_method") or "").startswith(
        ("standard_name", "metadata_alias", "builtin_alias")
    ):
        status = "semantic_conflict"
    else:
        status = "not_found"
    return {
        "action": "confirm" if viable else "block",
        "status": status,
        "binding": None,
        "candidates": viable or rejected,
        "rejected_candidates": rejected,
    }


def _metric_additive(metadata: dict[str, Any]) -> bool:
    if metadata.get("additive") is not None:
        return bool(metadata.get("additive"))
    return str(
        metadata.get("aggregation_mode") or metadata.get("aggregation") or ""
    ).strip().lower() in {"additive", "sum", "summable", "可加总"}


def _constraint_phrase(consumer: dict[str, Any]) -> str:
    return str(
        consumer.get("semantic_text") or consumer.get("query_fragment") or ""
    ).strip()


def _constraint_derived_terms(
    metric: dict[str, Any],
    consumer: dict[str, Any],
    intent_policy: dict[str, Any],
) -> tuple[list[str], list[str]]:
    derived_metric_id = str(consumer.get("derived_metric_id") or "")
    metric_name = str(metric.get("name") or "")
    terms: list[str] = []
    triggers: list[str] = []
    for rule in intent_policy.get("rules") or []:
        if derived_metric_id not in {
            str(value) for value in rule.get("derived_metric_ids") or []
        }:
            continue
        terms.extend(
            str(template).replace("{metric}", metric_name)
            for template in rule.get("metric_term_templates") or []
        )
        trigger = rule.get("triggers") or {}
        triggers.extend(str(value) for value in trigger.get("any") or [])
        triggers.extend(str(value) for value in trigger.get("all") or [])
    return list(dict.fromkeys(terms)), list(dict.fromkeys(triggers))


def _constraint_derived_evidence(
    consumer: dict[str, Any],
    candidate_name: str,
    metadata: dict[str, Any],
    recall: dict[str, Any],
    full_scope: bool,
    derived_triggers: list[str],
    core_score: float,
) -> dict[str, Any]:
    labels = [candidate_name, *(metadata.get("aliases") or [])]
    normalized_triggers = [
        normalize_match_text(value) for value in derived_triggers if normalize_match_text(value)
    ]
    source_derived = bool(normalized_triggers) and any(
        token in normalize_match_text(label)
        for label in labels
        for token in normalized_triggers
    )
    requirement_type = str(consumer.get("requirement_type") or "")
    if requirement_type != "derived_requirements":
        return {
            "status": "source_derived_not_allowed" if source_derived else "not_applicable",
            "tier": 3 if source_derived else 0,
            "score": 0.0,
            "conflict": "source_derived_cannot_replace_level" if source_derived else None,
        }
    derived_metric_id = str(consumer.get("derived_metric_id") or "")
    if not derived_metric_id:
        return {
            "status": "unresolved",
            "tier": 3,
            "score": 0.0,
            "conflict": "derived_metric_id_missing",
        }
    if source_derived:
        if full_scope and "derived_hypothesis" in set(recall.get("channels") or []):
            return {
                "status": "source_derived_exact",
                "tier": 0,
                "score": core_score,
                "derived_metric_id": derived_metric_id,
                "conflict": None,
            }
        return {
            "status": "source_derived_constraint_path_unsupported",
            "tier": 3,
            "score": 0.0,
            "derived_metric_id": derived_metric_id,
            "conflict": "source_derived_constraint_path_unsupported",
        }
    return {
        "status": "registered_local",
        "tier": 1,
        "score": 1.0,
        "derived_metric_id": derived_metric_id,
        "conflict": None,
    }


def _dimension_matches_constraint(
    constraint: dict[str, Any],
    index: dict[str, Any],
    supported_dimensions: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve a logical hint against physical dimensions using source metadata only."""
    requested_values = {
        normalize_match_text(value) for value in constraint.get("values") or []
    }
    hint = normalize_match_text(constraint.get("dimension_hint"))
    exact_matches: list[dict[str, Any]] = []
    domain_matches: list[dict[str, Any]] = []
    for name, metadata in (index.get("dimensions") or {}).items():
        if supported_dimensions is not None and str(name) not in supported_dimensions:
            continue
        names = {normalize_match_text(name)} | {
            normalize_match_text(alias) for alias in metadata.get("aliases") or []
        }
        domain = {
            normalize_match_text(value) for value in metadata.get("values") or []
        }
        if not requested_values.issubset(domain):
            continue
        name_exact = bool(hint and hint == normalize_match_text(name))
        alias_exact = bool(hint and hint in names and not name_exact)
        match = {
            "dimension": str(name),
            "dimension_hint_exact": name_exact,
            "dimension_alias_exact": alias_exact,
            "value_evidence": len(requested_values),
            "method": (
                "dimension_name_exact"
                if name_exact
                else "dimension_alias_exact"
                if alias_exact
                else "value_domain_after_metric_capability"
            ),
            "values": [str(value) for value in constraint.get("values") or []],
        }
        (exact_matches if name_exact or alias_exact else domain_matches).append(match)
    matches = exact_matches or domain_matches
    if len(matches) == 1 and matches[0]["method"] == "value_domain_after_metric_capability":
        matches[0]["method"] = "value_domain_unique_after_metric_capability"
    return sorted(matches, key=lambda item: item["dimension"])


def _full_scope_phrase_evidence(
    phrase: str,
    candidate_name: str,
    metadata: dict[str, Any],
    constraints: list[dict[str, Any]],
    core_evidence: dict[str, Any],
    core_floor: float,
    policy: dict[str, Any],
) -> dict[str, Any]:
    return full_scope_evidence(
        phrase,
        candidate_name,
        metadata,
        constraints,
        core_evidence,
        core_floor,
        policy,
    )


def _constraint_core_evidence(
    requested_core: Any,
    candidate_name: str,
    metadata: dict[str, Any],
    constraints: list[dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    return constrained_core_evidence(
        requested_core,
        candidate_name,
        metadata,
        constraints,
        policy,
    )


def _constraint_recall_names(
    index: dict[str, Any],
    metric: dict[str, Any],
    consumer: dict[str, Any],
    constraints: list[dict[str, Any]],
    policy: dict[str, Any],
    query: str,
    derived_terms: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Bounded union of full phrase, core and metadata-conditioned recall."""
    catalogue = index.get("metrics") or {}
    channels: dict[str, dict[str, Any]] = {}

    def collect(term: str, subset: dict[str, dict[str, Any]], channel: str) -> None:
        if not term or not subset:
            return
        requested = {**metric, "name": term}
        # The legacy matcher truncates to lexical Top3. Constrained recall scores
        # each canonical metric locally so structural evidence is evaluated first.
        for canonical, metadata in subset.items():
            resolution = _query_metric_resolution(
                requested, {canonical: metadata}, query, policy
            )
            for candidate in resolution.get("candidates") or []:
                name = str(candidate.get("name") or "")
                if not name:
                    continue
                entry = channels.setdefault(
                    name,
                    {"candidate": candidate, "channels": [], "recall_channels": {}},
                )
                if channel not in entry["channels"]:
                    entry["channels"].append(channel)
                channel_packet = {
                    "confidence": round(float(candidate.get("confidence") or 0.0), 6),
                    "lexical": round(float(candidate.get("lexical_score") or 0.0), 6),
                    "status": str(resolution.get("status") or "unknown"),
                }
                previous_channel = (entry.get("recall_channels") or {}).get(channel)
                if previous_channel is None or channel_packet["confidence"] > float(
                    previous_channel.get("confidence") or 0.0
                ):
                    entry["recall_channels"][channel] = channel_packet
                if float(candidate.get("confidence") or 0.0) > float(
                    (entry.get("candidate") or {}).get("confidence") or 0.0
                ):
                    entry["candidate"] = candidate

    phrase = _constraint_phrase(consumer)
    collect(phrase, catalogue, "full_phrase")
    collect(str(metric.get("name") or ""), catalogue, "core_metric")
    for term in derived_terms or []:
        collect(str(term), catalogue, "derived_hypothesis")
    possible_dimensions = {
        item["dimension"]
        for constraint in constraints
        for item in _dimension_matches_constraint(constraint, index)
    }
    conditioned = {
        name: metadata
        for name, metadata in catalogue.items()
        if possible_dimensions
        & {str(value) for value in metadata.get("dimensions") or []}
    }
    collect(str(metric.get("name") or ""), conditioned, "metadata_conditioned")
    return channels


def _resolve_constrained_requirement(
    index: dict[str, Any],
    context: dict[str, Any],
    metric: dict[str, Any],
    consumer: dict[str, Any],
    policy: dict[str, Any],
    intent_policy: dict[str, Any],
    core_override: str | None = None,
) -> dict[str, Any]:
    try:
        constraints = normalize_metric_constraints(consumer.get("metric_constraints"))
    except MetricConstraintError as exc:
        return {"binding": None, "candidates": [], "rejected_candidates": [], "error": str(exc)}
    catalogue = index.get("metrics") or {}
    derived_terms, derived_triggers = _constraint_derived_terms(
        metric, consumer, intent_policy
    )
    recalled = _constraint_recall_names(
        index,
        metric,
        consumer,
        constraints,
        policy,
        str(context.get("query") or ""),
        derived_terms,
    )
    evaluation = policy.get("candidate_evaluation") or {}
    core_floor = float(
        evaluation.get(
            "core_semantic_floor", evaluation.get("lexical_recall_floor", 0.78)
        )
    )
    rollup_edges = (policy.get("grain_rollup") or {}).get("allowed_edges") or []
    target_grains = {
        parsed[0]
        for period in consumer.get("periods") or metric.get("required_periods") or []
        if (parsed := _normalize_period(period)) is not None
    }
    phrase = _constraint_phrase(consumer)
    viable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    requested_core = core_override or metric.get("name")
    for name, recall in recalled.items():
        metadata = catalogue.get(name) or {}
        raw = recall.get("candidate") or {}
        core_evidence = _constraint_core_evidence(
            requested_core, name, metadata, constraints, policy
        )
        core_score = float(core_evidence.get("score") or 0.0)
        grain_checks = [
            evaluate_structural_grain_capability(metadata, grain, rollup_edges)
            for grain in sorted(target_grains)
        ]
        structural_available = bool(grain_checks) and all(
            item.get("status") == "available" for item in grain_checks
        )
        conflicts = list(raw.get("conflicts") or [])
        scope_evidence = _full_scope_phrase_evidence(
            phrase,
            name,
            metadata,
            constraints,
            core_evidence,
            core_floor,
            policy,
        )
        # Keep a private structural signal for the bounded fallback.  The
        # normal scope gate intentionally depends on the core floor, but a
        # candidate whose single alias covers the complete constraint may be
        # recoverable after replacing a drifted model core.
        context_scope_possible = bool(
            _full_scope_phrase_evidence(
                phrase,
                name,
                metadata,
                constraints,
                core_evidence,
                0.0,
                policy,
            ).get("full_scope")
        )
        full_scope = bool(scope_evidence.get("full_scope"))
        derived_evidence = _constraint_derived_evidence(
            consumer,
            name,
            metadata,
            recall,
            full_scope,
            (
                derived_triggers
                if consumer.get("requirement_type") == "derived_requirements"
                else _derived_tokens(policy)
            ),
            core_score,
        )
        if derived_evidence.get("conflict"):
            conflicts.append(str(derived_evidence["conflict"]))
        constraint_variants: list[tuple[list[dict[str, Any]], list[dict[str, Any]], int]] = [
            ([], [], 0)
        ]
        requires_confirmation = False
        path: str | None = (
            "source_derived_fact"
            if full_scope and derived_evidence.get("status") == "source_derived_exact"
            else "source_scoped_fact"
            if full_scope
            else None
        )
        constraint_tier = 0 if full_scope else 1
        fulfillment_tier = fulfillment_tier_for_path(path, 0) if full_scope else 1
        if not full_scope:
            supported = {str(value) for value in metadata.get("dimensions") or []}
            variant_limit = int(policy.get("limits", {}).get("max_candidates_per_case", 3))
            for constraint in constraints:
                matches = _dimension_matches_constraint(
                    constraint, index, supported_dimensions=supported
                )
                if not matches:
                    conflicts.append("constraint_dimension_unavailable")
                    continue
                if len(matches) > 1:
                    requires_confirmation = True
                expanded: list[
                    tuple[list[dict[str, Any]], list[dict[str, Any]], int]
                ] = []
                for selected in matches:
                    resolution = {
                        "status": "resolved",
                        "dimension": selected["dimension"],
                        "method": selected["method"],
                        "values": deepcopy(selected["values"]),
                    }
                    evidence = (
                        int(selected["dimension_hint_exact"])
                        + int(selected["dimension_alias_exact"])
                        + int(selected["value_evidence"])
                    )
                    for variant, resolutions, score in constraint_variants:
                        expanded.append((
                            [*variant, {
                                **deepcopy(constraint),
                                "source_dimension": selected["dimension"],
                            }],
                            [*resolutions, resolution],
                            score + evidence,
                        ))
                constraint_variants = sorted(
                    expanded,
                    key=lambda item: tuple(
                        resolution["dimension"] for resolution in item[1]
                    ),
                )[:variant_limit]
            operators = {
                item["operator"]
                for variant, _, _ in constraint_variants
                for item in variant
            }
            if "exclude" in operators:
                path = "same_metric_total_minus_members"
            elif any(
                item["operator"] == "in" and len(item["values"]) > 1
                for item in constraints
            ):
                path = "additive_member_sum"
            else:
                path = "member_selector"
            fulfillment_tier = fulfillment_tier_for_path(path)
            if path in {"same_metric_total_minus_members", "additive_member_sum"} and not _metric_additive(metadata):
                conflicts.append("metric_non_additive")
        if core_score < core_floor:
            conflicts.append("core_semantics_below_floor")
        if not structural_available:
            conflicts.extend(
                str(item.get("reason"))
                for item in grain_checks
                if item.get("status") != "available" and item.get("reason")
            )
        semantic_tier = 0 if core_score >= core_floor else 3
        match_confidence = (
            float(scope_evidence.get("score") or 0.0) if full_scope else core_score
        )
        lexical_confidence = round(
            float(raw.get("lexical_score") or raw.get("confidence") or 0.0), 6
        )
        for resolved_constraints, dimension_resolutions, variant_evidence in constraint_variants:
            candidate = {
                "metric": name,
                "status": "viable" if not conflicts else "infeasible",
                "path": path,
                "candidate_type": candidate_type_for_path(path),
                "semantic_tier": semantic_tier,
                "constraint_tier": constraint_tier,
                "derived_tier": int(derived_evidence.get("tier") or 0),
                "fulfillment_tier": fulfillment_tier,
                "requires_confirmation": requires_confirmation,
                "confidence": round(match_confidence, 6),
                "lexical_confidence": lexical_confidence,
                "confidence_detail": {
                    "core": round(core_score, 6),
                    "derived": round(float(derived_evidence.get("score") or 0.0), 6),
                    "grain_hint": _grain_signature(metric.get("name"), policy),
                    "dimension_evidence": variant_evidence,
                    "lexical": lexical_confidence,
                },
                "match_evidence": {
                    "recall_channels": deepcopy(recall.get("recall_channels") or {}),
                    "core": {
                        "score": round(core_score, 6),
                        "floor": core_floor,
                        "status": "pass" if core_score >= core_floor else "fail",
                        "matched_text": core_evidence.get("matched_text"),
                        "matched_core": core_evidence.get("matched_core"),
                    },
                    "constraint": deepcopy(scope_evidence),
                    "dimension_resolution": deepcopy(dimension_resolutions),
                    "derived": {
                        key: deepcopy(value)
                        for key, value in derived_evidence.items()
                        if key != "conflict"
                    },
                    "grain": {
                        "target": next(iter(target_grains)) if len(target_grains) == 1 else None,
                        "status": "available" if structural_available else "unavailable",
                    },
                },
                "dimension_resolution": deepcopy(dimension_resolutions),
                "constraints": resolved_constraints,
                "grain": next(iter(target_grains)) if len(target_grains) == 1 else None,
                "grain_checks": grain_checks,
                "evidence": [f"recall:{channel}" for channel in recall.get("channels") or []]
                + ["core_semantics_scored", "constraint_metadata_evaluated"],
                "soft_conflicts": list(raw.get("soft_conflicts") or []),
                "conflicts": sorted(set(conflicts)),
                "_context_scope_possible": context_scope_possible,
            }
            (viable if not conflicts else rejected).append(candidate)

    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        detail = item.get("confidence_detail") or {}
        return (
            int(item.get("semantic_tier") or 0),
            int(item.get("constraint_tier") or 0),
            int(item.get("derived_tier") or 0),
            int(item.get("fulfillment_tier") or 0),
            bool(item.get("requires_confirmation")),
            -float(detail.get("core") or 0.0),
            -float(detail.get("derived") or 0.0),
            -int(detail.get("dimension_evidence") or 0),
            -float(detail.get("lexical") or 0.0),
            str(item.get("metric") or ""),
        )

    identity = {
        "task_id": context.get("task_id"),
        "requirement_id": consumer.get("requirement_id"),
        "metric_ref": metric.get("metric_ref") or metric.get("name"),
        "constraints_fingerprint": metric_constraints_fingerprint(constraints),
    }
    for candidate in [*viable, *rejected]:
        candidate["candidate_id"] = stable_id("constraint_candidate", {
            **identity,
            "metric": candidate.get("metric"),
            "path": candidate.get("path"),
            "constraints": candidate.get("constraints"),
        })
    viable.sort(key=rank)
    rejected.sort(key=rank)
    best = viable[0] if viable else None
    best_prefix = rank(best)[:-1] if best else None
    tied = [item for item in viable if rank(item)[:-1] == best_prefix] if best else []
    binding = best if best and len(tied) == 1 and not best.get("requires_confirmation") else None
    limit = int(policy.get("limits", {}).get("max_candidates_per_case", 3))
    fallback_pool = [
        item
        for item in rejected
        if "core_semantics_below_floor" in set(item.get("conflicts") or [])
    ]
    fallback_pool.sort(
        key=lambda item: (
            not bool(item.get("_context_scope_possible")),
            rank(item),
        )
    )
    return {
        "identity": identity,
        "binding": deepcopy(binding) if binding else None,
        "candidates": deepcopy(viable[:limit]),
        "rejected_candidates": deepcopy(rejected[:limit]),
        # Keep the bounded public rejection list intact, while retaining only
        # core-gate failures for the one request-local fallback below.  This
        # prevents semantic-tier ordering from hiding a recoverable candidate.
        "_context_fallback_candidates": deepcopy(
            fallback_pool[:limit]
        ),
    }


_CONTEXT_FALLBACK_BLOCKERS = {
    "grain_not_supported",
    "grain_rollup_not_allowed",
    "metadata_grain_unsupported",
    "constraint_dimension_ambiguous",
    "metric_non_additive",
    "metric_object_mismatch",
    "unit_mismatch",
    "protected_semantics_conflict:comparison_semantics_conflict",
}


def _context_fallback_candidate_candidates(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    """Return rejected candidates eligible for one semantic-only retry."""
    eligible: list[dict[str, Any]] = []
    source = resolution.get("_context_fallback_candidates")
    for candidate in source if source is not None else (resolution.get("rejected_candidates") or []):
        conflicts = {str(item) for item in candidate.get("conflicts") or []}
        if "core_semantics_below_floor" not in conflicts:
            continue
        if conflicts & _CONTEXT_FALLBACK_BLOCKERS:
            continue
        if "constraint_dimension_unavailable" in conflicts and not candidate.get(
            "_context_scope_possible"
        ):
            continue
        if "source_derived_constraint_path_unsupported" in conflicts and not candidate.get(
            "_context_scope_possible"
        ):
            continue
        eligible.append(candidate)
    return eligible


def _try_context_fallback(
    index: dict[str, Any],
    context: dict[str, Any],
    metric: dict[str, Any],
    consumer: dict[str, Any],
    policy: dict[str, Any],
    intent_policy: dict[str, Any],
    legacy_resolution: dict[str, Any],
) -> dict[str, Any]:
    """Retry only a core-semantic failure, using current request text."""
    if legacy_resolution.get("binding") or legacy_resolution.get("candidates"):
        return legacy_resolution
    if not _context_fallback_candidate_candidates(legacy_resolution):
        return legacy_resolution
    hint = extract_current_core_hint(
        context.get("query"),
        consumer.get("semantic_text") or consumer.get("query_fragment"),
        policy,
        consumer.get("metric_constraints") or [],
    )
    core_hint = str(hint.get("hint") or "")
    if not core_hint:
        return legacy_resolution
    retry = _resolve_constrained_requirement(
        index,
        context,
        metric,
        consumer,
        policy,
        intent_policy,
        core_override=core_hint,
    )
    # A fallback is allowed to change the legacy result only when it produces
    # one unique binding.  Ambiguous or blocked retry results preserve legacy.
    if not retry.get("binding"):
        return legacy_resolution
    retry["context_guard"] = {
        "applied": True,
        "mode": "failure_fallback",
        "reason": "legacy_core_semantic_gate_failed",
        "core_hint": core_hint,
        "hint_source": hint.get("source"),
        "explicit_reference": bool(hint.get("explicit_reference")),
    }
    return retry


def _implicit_fallback_selects_composition_leaf(
    resolution: dict[str, Any],
    selected: dict[str, Any] | None,
    composition_intent: dict[str, Any] | None,
) -> bool:
    """Keep a query-level fallback leaf from masquerading as a composed output."""
    if selected is None or not isinstance(composition_intent, dict):
        return False
    guard = resolution.get("context_guard")
    if (
        not isinstance(guard, dict)
        or guard.get("mode") != "failure_fallback"
        or guard.get("reason") != "legacy_core_semantic_gate_failed"
        or guard.get("hint_source") != "query"
        or bool(guard.get("explicit_reference"))
    ):
        return False
    selected_metric = normalize_match_text(selected.get("metric"))
    if not selected_metric:
        return False
    input_metrics = {
        normalize_match_text(item.get("metric"))
        for item in composition_intent.get("inputs") or []
        if isinstance(item, dict) and item.get("metric")
    }
    return selected_metric in input_metrics


def _normalize_period(value: Any) -> tuple[str, str] | None:
    return _normalize_time_period(value)


def _candidate_dimension(
    context: dict[str, Any], metric: str, index: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    requested = [
        str(value)
        for value in (
            context.get("breakdown_dimensions")
            if "breakdown_dimensions" in context
            else context.get("dimensions") or []
        )
    ]
    metadata = (index.get("metrics") or {}).get(metric) or {}
    supported = {str(value) for value in metadata.get("dimensions") or []}
    if not requested:
        return None, None
    if not supported:
        return None, None
    resolved: set[str] = set()
    ambiguous: set[str] = set()
    unsupported: set[str] = set()
    for logical in requested:
        exact = resolve_catalogue_name(
            logical, index.get("dimensions") or {}, "dimension", strict=False
        )
        if exact in supported:
            resolved.add(str(exact))
            continue
        match = match_catalogue_name(logical, index.get("dimensions") or {}, "dimension")
        viable = {
            str(item.get("name"))
            for item in match.get("candidates") or []
            if not item.get("conflicts") and str(item.get("name")) in supported
        }
        if len(viable) == 1:
            resolved.update(viable)
        elif len(viable) > 1:
            ambiguous.add(logical)
        else:
            unsupported.add(logical)
    if unsupported:
        return None, {
            "status": "unavailable",
            "reason": "metadata_dimension_unsupported",
            "requested_dimensions": sorted(unsupported),
            "supported_dimensions": sorted(supported),
        }
    if ambiguous:
        return None, None
    if len(resolved) != 1:
        return None, {
            "status": "unavailable",
            "reason": "metadata_dimension_combination_unsupported",
            "requested_dimensions": requested,
            "resolved_dimensions": sorted(resolved),
        }
    return next(iter(resolved)), None


def _intent_candidate_packet(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(candidate[key])
        for key in (
            "candidate_id",
            "intent_id",
            "semantic_role",
            "status",
            "metric",
            "metric_object",
            "requested_terms",
            "confidence",
            "lexical_confidence",
            "semantic_tier",
            "constraint_tier",
            "derived_tier",
            "fulfillment_tier",
            "candidate_type",
            "requires_confirmation",
            "confidence_detail",
            "match_evidence",
            "semantic_status",
            "soft_conflicts",
            "path",
            "grain",
            "dimension",
            "periods",
            "capability",
            "evidence",
            "conflicts",
            "object_override_allowed",
            "constraints",
            "dimension_resolution",
        )
        if key in candidate
    }


def _resolve_business_intent_single(
    index: dict[str, Any],
    context: dict[str, Any],
    metric: dict[str, Any],
    resolution_policy: dict[str, Any],
    business_policy: dict[str, Any],
) -> dict[str, Any] | None:
    planning_context = deepcopy(context)
    planning_context["periods"] = list(
        metric.get("required_periods") or context.get("periods") or []
    )
    planning_context["breakdown_dimensions"] = list(
        metric.get("required_breakdown_dimensions")
        if "required_breakdown_dimensions" in metric
        else context.get("breakdown_dimensions")
        if "breakdown_dimensions" in context
        else context.get("dimensions") or []
    )
    if not planning_context.get("periods"):
        return None
    hypotheses = generate_metric_hypotheses(planning_context, metric, business_policy)
    if not hypotheses:
        return None
    candidates: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []
    evaluation = resolution_policy.get("candidate_evaluation") or {}
    core_floor = float(
        evaluation.get(
            "core_semantic_floor", evaluation.get("lexical_recall_floor", 0.78)
        )
    )
    rollup_edges = (resolution_policy.get("grain_rollup") or {}).get("allowed_edges") or []
    target_grains = {
        parsed[0]
        for period in planning_context.get("periods") or []
        if (parsed := _normalize_period(period)) is not None
    }
    consumers = [item for item in metric.get("consumers") or [] if isinstance(item, dict)]
    has_registered_derived = any(
        item.get("requirement_type") == "derived_requirements" for item in consumers
    )
    allowed_object_sets = [
        set(str(value) for value in consumer.get("allowed_metric_objects") or [])
        for consumer in consumers
        if consumer.get("allowed_metric_objects")
    ]
    candidate_margin = float(evaluation.get("minimum_candidate_margin", 0.1))

    contextual_terms = list(dict.fromkeys(
        str(value).strip()
        for consumer in consumers
        for value in (consumer.get("semantic_text"), consumer.get("query_fragment"))
        if str(value or "").strip()
    ))[:3]
    explicit_breakdown = bool(planning_context.get("breakdown_dimensions"))

    def competitive_band(evaluated: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in evaluated:
            key = (
                item.get("intent_id"),
                item.get("metric"),
                item.get("metric_object"),
                item.get("status"),
                item.get("path") or item.get("path_hint"),
            )
            previous = deduplicated.get(key)
            if previous is None or (
                -int(item.get("semantic_tier") or 0),
                float(item.get("confidence") or 0.0),
            ) > (
                -int(previous.get("semantic_tier") or 0),
                float(previous.get("confidence") or 0.0),
            ):
                deduplicated[key] = item
        if not deduplicated:
            return []
        best_tier = min(int(item.get("semantic_tier") or 0) for item in deduplicated.values())
        same_tier = [
            item
            for item in deduplicated.values()
            if int(item.get("semantic_tier") or 0) == best_tier
        ]
        top_confidence = max(float(item.get("confidence") or 0.0) for item in same_tier)
        return [
            item
            for item in same_tier
            if top_confidence - float(item.get("confidence") or 0.0) <= candidate_margin
        ]

    for hypothesis in hypotheses:
        evaluated_candidates: list[dict[str, Any]] = []
        for requested_term in hypothesis.get("requested_terms") or []:
            requested = {
                **metric,
                "name": requested_term,
                "metric_object": hypothesis.get("metric_object"),
                "unit": "待元信息解析" if hypothesis.get("object_override_allowed") else metric.get("unit"),
            }
            resolution = _query_metric_resolution(
                requested,
                index.get("metrics") or {},
                str(planning_context.get("query") or ""),
                resolution_policy,
            )
            recalled_by_name = {
                str(item.get("name")): deepcopy(item)
                for item in resolution.get("candidates") or []
                if item.get("name")
            }
            # One bounded contextual pass broadens lexical recall only. Every
            # result is still scored against metric.name and must pass the same
            # core/object/unit/grain/dimension gates below.
            if explicit_breakdown or not recalled_by_name:
                for contextual_term in contextual_terms:
                    contextual = _query_metric_resolution(
                        {**requested, "name": contextual_term},
                        index.get("metrics") or {},
                        str(planning_context.get("query") or ""),
                        resolution_policy,
                    )
                    for item in contextual.get("candidates") or []:
                        name = str(item.get("name") or "")
                        if not name:
                            continue
                        enriched = deepcopy(item)
                        enriched.setdefault("evidence", []).append("bounded_contextual_recall")
                        previous = recalled_by_name.get(name)
                        if previous is None or float(enriched.get("confidence") or 0) > float(
                            previous.get("confidence") or 0
                        ):
                            recalled_by_name[name] = enriched
            for recalled in recalled_by_name.values():
                binding = str(recalled.get("name") or "")
                if not binding:
                    continue
                metadata = (index.get("metrics") or {}).get(binding) or {}
                dimension, dimension_failure = _candidate_dimension(
                    planning_context, binding, index
                )
                requested_breakdowns = list(
                    planning_context.get("breakdown_dimensions") or []
                )
                grain_checks = [
                    evaluate_structural_grain_capability(metadata, grain, rollup_edges)
                    for grain in sorted(target_grains)
                ]
                structural_available = bool(grain_checks) and all(
                    item.get("status") == "available" for item in grain_checks
                )
                paths = {str(item.get("path")) for item in grain_checks if item.get("path")}
                path = (
                    "direct_fact"
                    if paths == {"direct_fact"}
                    else "aggregate_fact"
                    if paths and paths.issubset({"direct_fact", "aggregate_fact"})
                    else None
                )
                core_evidence = breakdown_core_evidence(
                    metric.get("name"),
                    binding,
                    metadata,
                    requested_breakdowns,
                    dimension,
                    index.get("dimensions") or {},
                    resolution_policy,
                )
                core_score = float(core_evidence.get("score") or 0.0)
                derived_score = _derived_semantic_score(
                    hypothesis.get("intent_id"), binding, metadata, resolution_policy
                )
                lexical_score = float(recalled.get("lexical_score") or 0.0)
                confidence = round(
                    0.70 * core_score + 0.20 * derived_score + 0.10 * lexical_score,
                    6,
                )
                intent_id = str(hypothesis.get("intent_id") or "")
                semantic_role = str(hypothesis.get("semantic_role") or "primary")
                object_mismatch = "model_inferred_metric_object_mismatch" in set(
                    recalled.get("soft_conflicts") or []
                )
                if core_score >= core_floor:
                    if semantic_role == "compatible_alternative":
                        semantic_tier = 1 if derived_score > 0 else 3
                    elif intent_id != "declared_metric":
                        semantic_tier = 0 if derived_score > 0 else 3
                    else:
                        semantic_tier = 0
                elif derived_score > 0:
                    semantic_tier = 2
                else:
                    semantic_tier = 3
                if object_mismatch and semantic_tier < 3:
                    semantic_tier = max(semantic_tier, 1)
                requires_confirmation = semantic_tier >= 2
                conflicts = {
                    str(item.get("reason"))
                    for item in ([dimension_failure] if dimension_failure else []) + grain_checks
                    if isinstance(item, dict)
                    and item.get("status") != "available"
                    and item.get("reason")
                }
                conflicts.update(str(value) for value in recalled.get("conflicts") or [])
                if (
                    object_mismatch
                    and recalled.get("semantic_status") != "equivalent"
                    and semantic_role != "compatible_alternative"
                ):
                    conflicts.add("model_inferred_object_semantics_unproven")
                # A source-side derived metric cannot replace a shared logical metric
                # until the downstream contract binds it to one requirement only.
                if intent_id != "declared_metric" and has_registered_derived:
                    conflicts.add("requirement_level_binding_required")
                if allowed_object_sets and intent_id == "declared_metric" and any(
                    hypothesis.get("metric_object") not in allowed
                    for allowed in allowed_object_sets
                ):
                    conflicts.add("operation_metric_object_unsupported")
                viable = (
                    dimension_failure is None
                    and structural_available
                    and semantic_tier <= 2
                    and not conflicts
                )
                candidate = {
                    "intent_id": intent_id,
                    "semantic_role": semantic_role,
                    "status": "viable" if viable else "infeasible",
                    "metric": binding,
                    "metric_object": hypothesis.get("metric_object"),
                    "requested_terms": list(hypothesis.get("requested_terms") or []),
                    "confidence": confidence,
                    "semantic_tier": semantic_tier,
                    "requires_confirmation": requires_confirmation,
                    "confidence_detail": {
                        "core": round(core_score, 6),
                        "core_mode": core_evidence.get("mode"),
                        "core_matched_text": core_evidence.get("matched_text"),
                        "derived": round(derived_score, 6),
                        "object_match": not object_mismatch,
                        "grain_hint": _grain_signature(
                            metric.get("name"), resolution_policy
                        ),
                        "lexical": lexical_score,
                        "semantic": recalled.get("semantic_status", "unknown"),
                        "metadata": "pass" if dimension_failure is None else "fail",
                        "structural_capability": "pass" if structural_available else "fail",
                    },
                    "semantic_status": recalled.get("semantic_status", "unknown"),
                    "soft_conflicts": list(recalled.get("soft_conflicts") or []),
                    "path": path if viable else None,
                    "path_hint": path,
                    "grain": next(iter(target_grains)) if len(target_grains) == 1 else None,
                    "dimension": dimension,
                    "periods": list(planning_context.get("periods") or []),
                    "capability": {
                        "dimension": dimension_failure
                        or (
                            {"status": "available", "dimension": dimension}
                            if dimension is not None
                            else {
                                "status": "deferred",
                                "reason": (
                                    "dimension_binding_ambiguous"
                                    if requested_breakdowns
                                    else "no_breakdown_requested"
                                ),
                                "requested_dimensions": requested_breakdowns,
                            }
                        ),
                        "grains": grain_checks,
                    },
                    "evidence": list(hypothesis.get("evidence") or [])
                    + list(recalled.get("evidence") or [])
                    + ["core_semantics_scored", "structural_grain_evaluated"]
                    + (["breakdown_scoped_core"] if core_evidence.get("mode") == "breakdown_scoped" else [])
                    + (["derived_semantics_match"] if derived_score > 0 else []),
                    "conflicts": sorted(conflicts),
                    "object_override_allowed": bool(hypothesis.get("object_override_allowed")),
                    "priority": int(hypothesis.get("priority") or 0),
                }
                evaluated_candidates.append(candidate)
        candidates.extend(competitive_band([
            item for item in evaluated_candidates if item.get("status") == "viable"
        ]))
        rejected_candidates.extend(competitive_band([
            item for item in evaluated_candidates if item.get("status") != "viable"
        ]))

    identity_base = {
        "task_id": context.get("task_id"),
        "metric_ref": metric.get("metric_ref") or metric.get("name"),
        "requested_name": metric.get("name"),
        "query": context.get("query"),
        "periods": planning_context.get("periods") or [],
        "dimensions": planning_context.get("dimensions") or [],
    }
    for candidate in [*candidates, *rejected_candidates]:
        candidate["candidate_id"] = stable_id("intent_candidate", {
            **identity_base,
            "intent_id": candidate.get("intent_id"),
            "metric": candidate.get("metric"),
            "metric_object": candidate.get("metric_object"),
            "path": candidate.get("path"),
        })
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate.get("metric"), candidate.get("path"), candidate.get("status"))
        previous = deduplicated.get(key)
        if previous is None or (
            -int(candidate.get("semantic_tier") or 0),
            bool((candidate.get("confidence_detail") or {}).get("object_match")),
            int(candidate.get("priority") or 0),
            float(candidate.get("confidence") or 0.0),
        ) > (
            -int(previous.get("semantic_tier") or 0),
            bool((previous.get("confidence_detail") or {}).get("object_match")),
            int(previous.get("priority") or 0),
            float(previous.get("confidence") or 0.0),
        ):
            deduplicated[key] = candidate
    bounded = sorted(
        deduplicated.values(),
        key=lambda item: (
            int(item.get("semantic_tier") or 0),
            -float(item.get("confidence") or 0.0),
            -int(item.get("priority") or 0),
            str(item.get("candidate_id")),
        ),
    )[: int(business_policy.get("limits", {}).get("max_candidates_per_case", 3))]
    return {
        "expanded": True,
        "identity": identity_base,
        "candidates": bounded,
        "viable_candidates": bounded,
        "rejected_candidates": rejected_candidates,
    }


def _resolve_business_intent(
    index: dict[str, Any],
    context: dict[str, Any],
    metric: dict[str, Any],
    resolution_policy: dict[str, Any],
    business_policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Evaluate shared logical metrics in Requirement-local contexts.

    Metric-level unions remain diagnostic compatibility fields.  A consumer's
    periods and breakdowns never become hard gates for another consumer.
    Constrained and path-neutral consumers are resolved by their dedicated paths.
    """
    consumers = [
        item for item in metric.get("consumers") or []
        if isinstance(item, dict)
        and not item.get("metric_constraints")
        and not item.get("resolution_intent")
    ]
    if not consumers:
        if metric.get("consumers"):
            return None
        return _resolve_business_intent_single(
            index, context, metric, resolution_policy, business_policy
        )
    resolutions: list[dict[str, Any]] = []
    for number, consumer in enumerate(consumers):
        local_metric = deepcopy(metric)
        local_metric["consumers"] = [deepcopy(consumer)]
        local_metric["required_periods"] = list(consumer.get("periods") or [])
        local_metric["required_dimensions"] = list(consumer.get("dimensions") or [])
        local_metric["required_breakdown_dimensions"] = list(
            consumer.get("breakdown_dimensions") or []
        )
        resolution = _resolve_business_intent_single(
            index, context, local_metric, resolution_policy, business_policy
        )
        if resolution is None:
            continue
        requirement_id = str(consumer.get("requirement_id") or f"consumer_{number + 1}")
        for candidate in [
            *(resolution.get("candidates") or []),
            *(resolution.get("rejected_candidates") or []),
        ]:
            candidate["requirement_id"] = requirement_id
        resolutions.append(resolution)
    if not resolutions:
        return None
    viable: dict[tuple[Any, ...], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    for resolution in resolutions:
        for candidate in resolution.get("viable_candidates") or []:
            key = (
                candidate.get("metric"), candidate.get("metric_object"),
                candidate.get("intent_id"),
            )
            previous = viable.get(key)
            if previous is None or float(candidate.get("confidence") or 0) > float(
                previous.get("confidence") or 0
            ):
                viable[key] = candidate
        rejected.extend(resolution.get("rejected_candidates") or [])
    identity = {
        "task_id": context.get("task_id"),
        "metric_ref": metric.get("metric_ref") or metric.get("name"),
        "requested_name": metric.get("name"),
        "query": context.get("query"),
        "requirement_ids": sorted(
            str(item.get("requirement_id"))
            for item in consumers if item.get("requirement_id")
        ),
    }
    bounded = sorted(
        viable.values(),
        key=lambda item: (
            int(item.get("semantic_tier") or 0),
            -float(item.get("confidence") or 0),
            str(item.get("candidate_id") or ""),
        ),
    )[: int(business_policy.get("limits", {}).get("max_candidates_per_case", 3))]
    return {
        "expanded": True,
        "identity": identity,
        "candidates": bounded,
        "viable_candidates": bounded,
        "rejected_candidates": rejected,
    }


def _source_derived_requirement_bindings(
    intent_resolution: dict[str, Any],
    metric: dict[str, Any],
    business_policy: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    intent_by_derived_id: dict[str, set[str]] = {}
    for rule in business_policy.get("rules") or []:
        intent_id = str(rule.get("intent_id") or "")
        for derived_id in rule.get("derived_metric_ids") or []:
            intent_by_derived_id.setdefault(str(derived_id), set()).add(intent_id)
    rejected = intent_resolution.get("rejected_candidates") or []
    bindings: dict[str, dict[str, Any]] = {}
    for consumer in metric.get("consumers") or []:
        if not isinstance(consumer, dict) or consumer.get("requirement_type") != "derived_requirements":
            continue
        requirement_id = str(consumer.get("requirement_id") or "")
        derived_metric_id = str(consumer.get("derived_metric_id") or "")
        if not requirement_id or not derived_metric_id:
            continue
        allowed_intents = intent_by_derived_id.get(derived_metric_id) or set()
        candidates = [
            item
            for item in rejected
            if str(item.get("requirement_id") or "") == requirement_id
            if str(item.get("intent_id") or "") in allowed_intents
            and int(item.get("semantic_tier", 99)) <= 2
            and set(item.get("conflicts") or []) == {"requirement_level_binding_required"}
            and item.get("path_hint") in {"direct_fact", "aggregate_fact"}
        ]
        if len(candidates) != 1:
            continue
        selected = candidates[0]
        source_series = derived_metric_id == "yoy_trend_change"
        bindings[requirement_id] = {
            "mode": (
                "source_derived_calculation" if source_series else "source_derived_fact"
            ),
            "derived_metric_id": derived_metric_id,
            "source_metric": selected.get("metric"),
            "source_metric_object": selected.get("metric_object"),
            "candidate_id": selected.get("candidate_id"),
            "intent_id": selected.get("intent_id"),
            "path_hint": selected.get("path_hint"),
            "source_period_role": next(
                iter(consumer.get("period_roles") or ["analysis"]), "analysis"
            ),
        }
        if source_series:
            bindings[requirement_id].update({
                "source_period_roles": ["analysis", "comparison"],
                "execution_derived_metric_id": "period_change",
            })
    return bindings


def _performance_requirement_bindings(
    metric: dict[str, Any],
    derived_bindings: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Let a proven precomputed performance fact fulfill a broad performance output.

    This is limited to sibling Requirements with the same breakdown. Explicit
    level requests (amount, scale, level, how much) are never rewritten.
    """
    consumers = [item for item in metric.get("consumers") or [] if isinstance(item, dict)]
    by_id = {
        str(item.get("requirement_id")): item
        for item in consumers if item.get("requirement_id")
    }
    additions: dict[str, dict[str, Any]] = {}
    for requirement_id, consumer in by_id.items():
        if consumer.get("requirement_type") != "fact_observations":
            continue
        semantic_text = str(consumer.get("semantic_text") or "")
        if "表现" not in semantic_text or any(
            token in semantic_text for token in ("金额", "规模", "水平", "多少")
        ):
            continue
        breakdown = set(str(value) for value in consumer.get("breakdown_dimensions") or [])
        periods = set(str(value) for value in consumer.get("periods") or [])
        compatible = []
        for sibling_id, binding in derived_bindings.items():
            sibling = by_id.get(sibling_id) or {}
            if set(str(value) for value in sibling.get("breakdown_dimensions") or []) != breakdown:
                continue
            if periods and not periods.intersection(
                str(value) for value in sibling.get("periods") or []
            ):
                continue
            compatible.append(binding)
        physical_metrics = {
            str(item.get("source_metric")) for item in compatible if item.get("source_metric")
        }
        if len(physical_metrics) != 1:
            continue
        selected = compatible[0]
        additions[requirement_id] = {
            "mode": "source_scoped_fact",
            "source_metric": next(iter(physical_metrics)),
            "candidate_id": selected.get("candidate_id"),
            "fulfillment_basis": "sibling_precomputed_performance_fact",
        }
    return additions


def _joint_block_candidates(
    unresolved: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    dimensions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = {normalize_match_text(value) for value in (unresolved.get("rows") or {}) if str(value)}
    candidates: list[dict[str, Any]] = []
    metric_candidates = (unresolved.get("metric_match") or {}).get("candidates") or []
    dimension_candidates = (unresolved.get("dimension_match") or {}).get("candidates") or []
    for metric_raw in metric_candidates:
        metric = str(metric_raw.get("name") or "")
        metric_metadata = metrics.get(metric) or {}
        supported = {str(value) for value in metric_metadata.get("dimensions") or []}
        metric_conflicts = list(metric_raw.get("conflicts") or [])
        for dimension_raw in dimension_candidates:
            dimension = str(dimension_raw.get("name") or "")
            dimension_values = {
                normalize_match_text(value)
                for value in (dimensions.get(dimension) or {}).get("values") or []
                if str(value)
            }
            conflicts = list(metric_conflicts) + list(dimension_raw.get("conflicts") or [])
            if supported and dimension not in supported:
                conflicts.append("metric_dimension_mismatch")
            if rows and not rows.issubset(dimension_values):
                conflicts.append("source_domain_not_subset")
            if conflicts:
                continue
            domain_exact = bool(rows) and rows == dimension_values
            dimension_exact = normalize_match_text(unresolved.get("raw_dimension")) == normalize_match_text(dimension)
            evidence = ["metric_dimension_compatible", "source_domain_subset"]
            if domain_exact:
                evidence.append("dimension_domain_exact")
            if dimension_exact:
                evidence.append("dimension_name_exact")
            identity = {
                "raw_metric": unresolved.get("raw_metric"),
                "raw_dimension": unresolved.get("raw_dimension"),
                "row_domain": sorted(rows),
                "metric": metric,
                "dimension": dimension,
            }
            candidates.append({
                "candidate_id": stable_id("resolution_candidate", identity),
                "metric": metric,
                "dimension": dimension,
                "confidence": round((float(metric_raw.get("confidence") or 0.0) + float(dimension_raw.get("confidence") or 0.0)) / 2, 6),
                "evidence": evidence,
                "conflicts": [],
                "strong_evidence_count": int(domain_exact) + int(dimension_exact),
                "domain_summary": {
                    "source_member_count": len(rows),
                    "candidate_member_count": len(dimension_values),
                    "exact": domain_exact,
                },
            })
    return sorted(candidates, key=lambda item: (-item["strong_evidence_count"], -item["confidence"], item["candidate_id"]))


def _fact_block_capability(
    index: dict[str, Any], metric: str, periods: list[str], dimension: str
) -> dict[str, Any]:
    """Prove one physical metric block covers all exact requested periods."""
    failures: list[dict[str, Any]] = []
    members: set[str] = set()
    for period in periods:
        parsed = _normalize_period(period)
        if parsed is None:
            failures.append({"period": period, "reason": "invalid_period"})
            continue
        grain, canonical = parsed
        sheet = (index.get("sheets") or {}).get(grain) or {}
        block = (sheet.get("blocks") or {}).get(metric) or {}
        if canonical not in (sheet.get("periods") or {}):
            failures.append({"period": canonical, "reason": "period_unavailable"})
            continue
        if not block:
            failures.append({"period": canonical, "reason": "metric_block_unavailable"})
            continue
        if str(block.get("dimension") or "无") != dimension:
            failures.append({
                "period": canonical,
                "reason": "metadata_fact_dimension_conflict",
                "fact_dimension": block.get("dimension"),
            })
            continue
        members.update(str(value) for value in (block.get("rows") or {}))
    return {
        "status": "available" if not failures else "unavailable",
        "dimension": dimension,
        "periods": list(periods),
        "members": sorted(members),
        "failures": failures,
    }


def _aggregate_scope_dimensions(
    intent: dict[str, Any], metadata: dict[str, Any], index: dict[str, Any]
) -> list[dict[str, Any]]:
    operand = intent.get("operand") or {}
    scope = operand.get("scope") or {}
    hint = str(scope.get("dimension_hint") or "")
    supported = {
        str(value) for value in metadata.get("dimensions") or []
        if str(value) != "无"
    }
    if not hint or not supported:
        return []
    normalized_hint = normalize_match_text(hint)
    exact: list[dict[str, Any]] = []
    contained: list[dict[str, Any]] = []
    for dimension in sorted(supported):
        dimension_metadata = (index.get("dimensions") or {}).get(dimension) or {}
        names = [dimension, *(dimension_metadata.get("aliases") or [])]
        normalized_names = {normalize_match_text(value) for value in names}
        if normalized_hint in normalized_names:
            exact.append({
                "dimension": dimension,
                "method": (
                    "dimension_name_exact"
                    if normalized_hint == normalize_match_text(dimension)
                    else "dimension_alias_exact"
                ),
            })
        elif any(
            normalized_hint and (
                normalized_hint in name or name in normalized_hint
            )
            for name in normalized_names if name
        ):
            contained.append({
                "dimension": dimension,
                "method": "dimension_name_containment",
            })
    return exact or contained


def _aggregate_level_resolution(
    index: dict[str, Any],
    context: dict[str, Any],
    metric: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Add direct and source-domain candidates without changing old paths."""
    intent = metric.get("resolution_intent") or {}
    periods = [str(value) for value in metric.get("required_periods") or []]
    logical_name = str(metric.get("logical_metric_name") or "")
    identity = {
        "task_id": context.get("task_id"),
        "requirement_id": metric.get("resolution_requirement_id"),
        "metric_ref": metric.get("metric_ref"),
        "logical_metric_ref": metric.get("logical_metric_ref"),
        "operation": "aggregate_level",
        "periods": periods,
        "intent": intent,
    }
    candidates: list[dict[str, Any]] = []

    direct_resolution = _query_metric_resolution(
        metric, index.get("metrics") or {}, str(context.get("query") or ""), policy
    )
    for recalled in direct_resolution.get("candidates") or []:
        source_metric = str(recalled.get("name") or "")
        capability = _fact_block_capability(index, source_metric, periods, "无")
        recall_conflicts = [str(value) for value in recalled.get("conflicts") or []]
        viable = capability["status"] == "available" and not recall_conflicts
        candidates.append({
            "candidate_id": stable_id("aggregate_fulfillment", {
                **identity, "type": "direct_fact", "metric": source_metric,
            }),
            "candidate_type": "direct_fact",
            "path": "direct_fact",
            "status": "viable" if viable else "infeasible",
            "metric": source_metric,
            "confidence": float(recalled.get("confidence") or 0.0),
            "semantic_status": recalled.get("semantic_status"),
            "evidence": list(recalled.get("evidence") or []) + ["scope_block_checked"],
            "capability": capability,
            "conflicts": [] if viable else sorted(set([
                *recall_conflicts,
                *(str(item.get("reason")) for item in capability.get("failures") or []),
            ])),
        })

    source_resolution = _query_metric_resolution(
        {
            **metric,
            "name": logical_name,
            "metric_object": intent.get("output_metric_object") or "volume",
        },
        index.get("metrics") or {},
        str(context.get("query") or ""),
        policy,
    )
    for recalled in source_resolution.get("candidates") or []:
        source_metric = str(recalled.get("name") or "")
        metadata = (index.get("metrics") or {}).get(source_metric) or {}
        dimension_matches = _aggregate_scope_dimensions(intent, metadata, index)
        conflicts: list[str] = [
            str(value) for value in recalled.get("conflicts") or []
        ]
        if not _metric_additive(metadata):
            conflicts.append("metric_non_additive")
        if not dimension_matches:
            conflicts.append("aggregate_dimension_unavailable")
        elif len(dimension_matches) > 1:
            conflicts.append("aggregate_dimension_ambiguous")
        source_dimension = (
            str(dimension_matches[0]["dimension"])
            if len(dimension_matches) == 1 else ""
        )
        capability = (
            _fact_block_capability(index, source_metric, periods, source_dimension)
            if source_dimension else {"status": "unavailable", "failures": []}
        )
        if source_dimension and capability["status"] != "available":
            conflicts.extend(
                str(item.get("reason"))
                for item in capability.get("failures") or []
            )
        domain = {
            str(value)
            for value in (
                ((index.get("dimensions") or {}).get(source_dimension) or {}).get("values")
                or []
            )
        }
        block_members = set(capability.get("members") or [])
        if source_dimension and (not domain or not domain.issubset(block_members)):
            conflicts.append("aggregate_domain_incomplete")
        candidates.append({
            "candidate_id": stable_id("aggregate_fulfillment", {
                **identity, "type": "set_aggregate", "metric": source_metric,
                "dimension": source_dimension,
            }),
            "candidate_type": "set_aggregate",
            "path": "source_dimension_all_sum",
            "status": "viable" if not conflicts else "infeasible",
            "metric": source_metric,
            "dimension": source_dimension or None,
            "dimension_resolution": deepcopy(dimension_matches),
            "confidence": float(recalled.get("confidence") or 0.0),
            "semantic_status": recalled.get("semantic_status"),
            "evidence": list(recalled.get("evidence") or []) + [
                "source_dimension_all_evaluated"
            ],
            "capability": capability,
            "conflicts": sorted(set(conflicts)),
        })

    viable_direct = [
        item for item in candidates
        if item["candidate_type"] == "direct_fact" and item["status"] == "viable"
    ]
    if len(viable_direct) > 1:
        margin = float(
            (policy.get("candidate_evaluation") or {}).get(
                "minimum_candidate_margin", 0.1
            )
        )
        ranked_direct = sorted(
            viable_direct, key=lambda item: -float(item.get("confidence") or 0.0)
        )
        if (
            float(ranked_direct[0].get("confidence") or 0.0)
            - float(ranked_direct[1].get("confidence") or 0.0)
            < margin
        ):
            for item in viable_direct:
                item["status"] = "infeasible"
                item["conflicts"] = ["direct_fact_ambiguous"]
            for item in candidates:
                if item["candidate_type"] == "set_aggregate" and item["status"] == "viable":
                    item["status"] = "infeasible"
                    item["conflicts"] = [
                        *item.get("conflicts", []), "higher_tier_direct_ambiguous",
                    ]

    viable_sets = [
        item for item in candidates
        if item["candidate_type"] == "set_aggregate" and item["status"] == "viable"
    ]
    if not any(item.get("status") == "viable" for item in viable_direct) and len(viable_sets) > 1:
        margin = float(
            (policy.get("candidate_evaluation") or {}).get(
                "minimum_candidate_margin", 0.1
            )
        )
        ranked_sets = sorted(
            viable_sets, key=lambda item: -float(item.get("confidence") or 0.0)
        )
        if (
            float(ranked_sets[0].get("confidence") or 0.0)
            - float(ranked_sets[1].get("confidence") or 0.0)
            < margin
        ):
            for item in viable_sets:
                item["status"] = "infeasible"
                item["conflicts"] = ["source_metric_ambiguous"]

    selected, ranked = select_fulfillment_candidate(candidates)
    return {
        "identity": identity,
        "selected": selected,
        "candidates": ranked,
        "viable_candidates": [item for item in ranked if item.get("status") == "viable"],
        "rejected_candidates": [item for item in ranked if item.get("status") != "viable"],
    }


def resolve_request_overlay(
    index: dict[str, Any],
    request: dict[str, Any],
    policy: dict[str, Any],
    business_intent_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve requested names and ambiguous blocks without mutating the shared raw index."""
    validate_resolution_policy(policy)
    intent_policy = business_intent_policy or load_business_intent_policy(
        DEFAULT_BUSINESS_INTENT_POLICY_PATH
    )
    intent_policy_hash = business_intent_policy_hash(intent_policy)
    overlay = deepcopy(index)
    contexts = [item for item in request.get("contexts") or [] if isinstance(item, dict)]
    if not contexts:
        # Keep the Gateway's pre-context contract working.  Rich callers supply
        # provenance and query semantics; legacy callers still get exact/alias
        # bindings without being promoted through fuzzy evidence.
        contexts = [{
            "task_id": "default",
            "query": "",
            "metrics": [
                {"name": str(name), "provenance": "model_inferred"}
                for name in request.get("metrics") or []
            ],
            "dimensions": [str(name) for name in request.get("dimensions") or []],
            "resolution_patches": [],
        }]
    patches = [
        patch
        for context in contexts
        for patch in context.get("resolution_patches") or []
        if isinstance(patch, dict)
    ]
    policy_hash = resolution_policy_hash(policy)
    source = index.get("source") or {}
    decisions: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    selected_blocks: list[dict[str, Any]] = []
    requested_names = {str(value) for value in request.get("metrics") or []}

    for grain, sheet in (overlay.get("sheets") or {}).items():
        if not isinstance(sheet, dict):
            continue
        for unresolved in sheet.get("unresolved_blocks") or []:
            if not isinstance(unresolved, dict):
                continue
            raw_metric = str(unresolved.get("raw_metric") or "")
            raw_candidates = {
                str(item.get("name") or "")
                for item in (unresolved.get("metric_match") or {}).get("candidates") or []
            }
            relevant = any(
                normalize_match_text(name) == normalize_match_text(raw_metric)
                or name in raw_candidates
                for name in requested_names
            )
            if not relevant:
                continue
            candidates = _joint_block_candidates(
                unresolved,
                overlay.get("metrics") or {},
                overlay.get("dimensions") or {},
            )
            identity = {
                "source_id": source.get("spreadsheet_token") or source.get("url"),
                "grain": grain,
                "raw_metric": raw_metric,
                "raw_dimension": unresolved.get("raw_dimension"),
                "row_domain_hash": stable_id("domain", sorted((unresolved.get("rows") or {}).keys())),
            }
            case_id = stable_id("resolution_case", identity)
            selected_id, patch_status = _patch_selection(case_id, patches, source, policy_hash)
            selected = next((item for item in candidates if item["candidate_id"] == selected_id), None)
            rule = policy["rules"]["fact_block_joint_resolution"]["auto"]
            if selected is None and len(candidates) == 1 and candidates[0]["strong_evidence_count"] >= int(rule.get("minimum_strong_evidence", 1)):
                selected = candidates[0]
            action = "auto" if selected is not None else "confirm" if candidates else "block"
            task_ids = sorted({
                task_id
                for name in requested_names
                for task_id in _context_tasks(contexts, name, raw_metric)
                if normalize_match_text(name) == normalize_match_text(raw_metric) or name in raw_candidates
            })
            if selected is not None:
                block = {
                    "header_row": unresolved.get("header_row"),
                    "raw_metric": raw_metric,
                    "dimension": selected["dimension"],
                    "raw_dimension": unresolved.get("raw_dimension"),
                    "rows": deepcopy(unresolved.get("rows") or {}),
                    "metric_match": deepcopy(unresolved.get("metric_match") or {}),
                    "dimension_match": deepcopy(unresolved.get("dimension_match") or {}),
                    "resolution_candidate_id": selected["candidate_id"],
                }
                existing = sheet.setdefault("blocks", {}).get(selected["metric"])
                if existing is None:
                    sheet["blocks"][selected["metric"]] = block
                    if sheet.get("periods"):
                        sheet["available"] = True
                        sheet.pop("reason", None)
                    selected_blocks.append({
                        "metric": selected["metric"],
                        "requested_dimension": str(unresolved.get("raw_dimension") or ""),
                        "source_dimension": selected["dimension"],
                    })
                elif existing.get("dimension") != selected["dimension"] or existing.get("rows") != block.get("rows"):
                    action = "confirm"
                    selected = None
            decision = {
                "case_id": case_id,
                "action": action,
                "kind": "fact_block",
                "requested_term": raw_metric,
                "task_ids": task_ids,
                "selected_candidate_id": selected.get("candidate_id") if selected else None,
                "policy_version": policy.get("policy_version"),
                "resolution_policy_hash": policy_hash,
                "source_revision": source.get("revision"),
                "schema_hash": source.get("schema_hash"),
                "resolution_engine_version": ENGINE_VERSION,
                "patch_status": patch_status,
                "candidates": [_candidate_packet(item) for item in candidates[: int(policy.get("limits", {}).get("max_candidates_per_case", 3))]],
            }
            decisions.append(decision)
            if action != "auto":
                cases.append(decision)

    metric_bindings: dict[str, str] = {}
    binding_votes: dict[str, set[str]] = {}
    binding_attempts: dict[str, int] = {}
    binding_successes: dict[str, int] = {}
    task_resolutions: dict[str, dict[str, Any]] = {}
    composition_registry_hash = str(request.get("composition_registry_hash") or "")
    for context in contexts:
        task_id = str(context.get("task_id") or "default")
        query = str(context.get("query") or "")
        context_patches = [
            item
            for item in context.get("resolution_patches") or []
            if isinstance(item, dict)
        ]
        intents = [
            item
            for item in context.get("composition_intents") or []
            if isinstance(item, dict)
        ]
        intent_by_metric_ref = {
            str(item.get("metric_ref")): item
            for item in intents
            if item.get("metric_ref")
        }
        task_resolution = {
            "metric_bindings": {},
            "requirement_bindings": {},
            "metric_statuses": {},
            "intent_resolutions": {},
            "composition_resolutions": [],
            "resolution_cases": [],
        }
        composition_deferred_requirements: dict[str, set[str]] = {}
        for metric in context.get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("name"):
                continue
            requested_name = str(metric["name"])
            binding_attempts[requested_name] = binding_attempts.get(requested_name, 0) + 1
            metric_ref = str(metric.get("metric_ref") or requested_name)
            metric_consumers = [
                item for item in metric.get("consumers") or [] if isinstance(item, dict)
            ]
            if metric.get("resolution_operation") == "aggregate_level":
                aggregate_resolution = _aggregate_level_resolution(
                    overlay, context, metric, policy
                )
                selected_aggregate = aggregate_resolution.get("selected")
                requirement_id = str(metric.get("resolution_requirement_id") or "")
                if selected_aggregate is not None and requirement_id:
                    binding_packet = {
                        "mode": (
                            "source_scoped_fact"
                            if selected_aggregate.get("candidate_type") == "direct_fact"
                            else selected_aggregate.get("path")
                        ),
                        "source_metric": selected_aggregate.get("metric"),
                        "candidate_id": selected_aggregate.get("candidate_id"),
                        "output_metric_ref": metric_ref,
                        "logical_metric_ref": metric.get("logical_metric_ref"),
                        "resolution_operation": "aggregate_level",
                    }
                    if selected_aggregate.get("candidate_type") == "set_aggregate":
                        binding_packet["source_dimension"] = selected_aggregate.get(
                            "dimension"
                        )
                    task_resolution["requirement_bindings"][requirement_id] = binding_packet
                rejected_aggregate = aggregate_resolution.get("rejected_candidates") or []
                ambiguous_aggregate = any(
                    conflict in {
                        "aggregate_dimension_ambiguous", "direct_fact_ambiguous",
                        "higher_tier_direct_ambiguous", "source_metric_ambiguous",
                    }
                    for candidate in rejected_aggregate
                    for conflict in candidate.get("conflicts") or []
                )
                action = (
                    "auto" if selected_aggregate is not None
                    else "confirm" if ambiguous_aggregate
                    else "block"
                )
                aggregate_case = {
                    "case_id": stable_id(
                        "resolution_case", aggregate_resolution["identity"]
                    ),
                    "action": action,
                    "kind": "aggregate_level",
                    "requested_term": requested_name,
                    "task_ids": [task_id],
                    "metric_ref": metric_ref,
                    "requirement_id": requirement_id,
                    "selected_candidate_id": (
                        selected_aggregate.get("candidate_id")
                        if selected_aggregate else None
                    ),
                    "policy_version": policy.get("policy_version"),
                    "resolution_policy_hash": policy_hash,
                    "source_revision": source.get("revision"),
                    "schema_hash": source.get("schema_hash"),
                    "resolution_engine_version": ENGINE_VERSION,
                    "candidates": [
                        _intent_candidate_packet(item)
                        for item in aggregate_resolution.get("viable_candidates") or []
                    ],
                    "rejected_candidates": [
                        _intent_candidate_packet(item)
                        for item in rejected_aggregate
                    ],
                }
                decisions.append(aggregate_case)
                task_resolution["metric_statuses"][metric_ref] = {
                    "requested_metric": requested_name,
                    "status": (
                        "requirement_bound" if selected_aggregate is not None
                        else "ambiguous" if action == "confirm"
                        else "not_executable"
                    ),
                    "binding": (
                        selected_aggregate.get("metric")
                        if selected_aggregate is not None else None
                    ),
                }
                if selected_aggregate is None:
                    cases.append(aggregate_case)
                    task_resolution["resolution_cases"].append(aggregate_case)
                continue
            constrained_consumers = [
                item for item in metric_consumers if item.get("metric_constraints")
            ]
            constrained_bound: set[str] = set()
            for consumer in constrained_consumers:
                requirement_id = str(consumer.get("requirement_id") or "")
                constraint_resolution = _resolve_constrained_requirement(
                    overlay, context, metric, consumer, policy, intent_policy
                )
                constraint_resolution = _try_context_fallback(
                    overlay,
                    context,
                    metric,
                    consumer,
                    policy,
                    intent_policy,
                    constraint_resolution,
                )
                constraint_identity = constraint_resolution.get("identity") or {
                    "task_id": task_id,
                    "requirement_id": requirement_id,
                    "metric_ref": metric_ref,
                    "constraints_fingerprint": stable_id(
                        "invalid_constraints", consumer.get("metric_constraints")
                    ),
                }
                case_identity = {
                    "source_id": source.get("spreadsheet_token") or source.get("url"),
                    "kind": "metric_constraint",
                    **constraint_identity,
                }
                case_id = stable_id("resolution_case", case_identity)
                selected_id, constraint_patch_status = _patch_selection(
                    case_id,
                    context_patches,
                    source,
                    policy_hash,
                    {
                        "metric_constraints_fingerprint": constraint_identity.get(
                            "constraints_fingerprint"
                        )
                    },
                    require_engine_version=True,
                )
                constraint_candidates = constraint_resolution.get("candidates") or []
                selected_constraint = next(
                    (
                        item
                        for item in constraint_candidates
                        if item.get("candidate_id") == selected_id
                    ),
                    None,
                ) or constraint_resolution.get("binding")
                composition_intent = intent_by_metric_ref.get(metric_ref)
                deferred_to_composition = _implicit_fallback_selects_composition_leaf(
                    constraint_resolution, selected_constraint, composition_intent
                )
                if deferred_to_composition:
                    selected_constraint = None
                    if requirement_id:
                        composition_deferred_requirements.setdefault(
                            metric_ref, set()
                        ).add(requirement_id)
                if selected_constraint is not None and requirement_id:
                    constrained_bound.add(requirement_id)
                    binding_packet = {
                        "mode": selected_constraint.get("path"),
                        "source_metric": selected_constraint.get("metric"),
                        "candidate_id": selected_constraint.get("candidate_id"),
                        "metric_constraints": deepcopy(
                            selected_constraint.get("constraints") or []
                        ),
                        "constraints_fingerprint": constraint_identity.get(
                            "constraints_fingerprint"
                        ),
                    }
                    if selected_constraint.get("path") == "source_derived_fact":
                        binding_packet["derived_metric_id"] = consumer.get(
                            "derived_metric_id"
                        )
                        binding_packet["source_period_role"] = next(
                            iter(consumer.get("period_roles") or ["analysis"]),
                            "analysis",
                        )
                    task_resolution["requirement_bindings"][requirement_id] = binding_packet
                constraint_action = (
                    "block"
                    if deferred_to_composition
                    else "auto"
                    if selected_constraint is not None
                    else "confirm"
                    if constraint_candidates
                    else "block"
                )
                constraint_case = {
                    "case_id": case_id,
                    "action": constraint_action,
                    "kind": "metric_constraint",
                    "requested_term": _constraint_phrase(consumer) or requested_name,
                    "task_ids": [task_id],
                    "metric_ref": metric_ref,
                    "requirement_id": requirement_id,
                    "selected_candidate_id": (
                        selected_constraint.get("candidate_id")
                        if selected_constraint
                        else None
                    ),
                    "policy_version": policy.get("policy_version"),
                    "resolution_policy_hash": policy_hash,
                    "source_revision": source.get("revision"),
                    "schema_hash": source.get("schema_hash"),
                    "resolution_engine_version": ENGINE_VERSION,
                    "metric_constraints_fingerprint": constraint_identity.get(
                        "constraints_fingerprint"
                    ),
                    "patch_status": constraint_patch_status,
                    "candidates": [
                        _intent_candidate_packet(item) for item in constraint_candidates
                    ],
                    "rejected_candidates": [
                        _intent_candidate_packet(item)
                        for item in (constraint_resolution.get("rejected_candidates") or [])
                    ],
                }
                if constraint_resolution.get("context_guard"):
                    constraint_case["context_guard"] = deepcopy(
                        constraint_resolution["context_guard"]
                    )
                if deferred_to_composition:
                    constraint_case["activation"] = "deferred_to_registered_composition"
                    constraint_case["deferred_reason"] = (
                        "implicit_query_fallback_selected_composition_input"
                    )
                decisions.append(constraint_case)
                if selected_constraint is None and not deferred_to_composition:
                    cases.append(constraint_case)
                    task_resolution["resolution_cases"].append(constraint_case)
            if constrained_consumers and len(constrained_consumers) == len(metric_consumers):
                all_requirement_ids = {
                    str(item.get("requirement_id") or "")
                    for item in constrained_consumers
                    if item.get("requirement_id")
                }
                task_resolution["metric_statuses"][metric_ref] = {
                    "requested_metric": requested_name,
                    "status": (
                        "composition_deferred"
                        if (
                            metric_ref in intent_by_metric_ref
                            and bool(composition_deferred_requirements.get(metric_ref))
                            and all_requirement_ids
                            == (
                                constrained_bound
                                | composition_deferred_requirements.get(metric_ref, set())
                            )
                        )
                        else "requirement_bound"
                        if all_requirement_ids == constrained_bound
                        else "ambiguous"
                        if any(
                            case.get("action") == "confirm"
                            and case.get("metric_ref") == metric_ref
                            for case in task_resolution["resolution_cases"]
                        )
                        else "not_executable"
                    ),
                    "binding": None,
                }
                continue
            intent_resolution = _resolve_business_intent(
                overlay, context, metric, policy, intent_policy
            )
            if intent_resolution is not None:
                intent_identity = intent_resolution["identity"]
                intent_case_id = stable_id(
                    "resolution_case", {**intent_identity, "kind": "interpretation"}
                )
                intent_fingerprint = stable_id("intent", intent_identity)
                selected_id, intent_patch_status = _patch_selection(
                    intent_case_id,
                    context_patches,
                    source,
                    policy_hash,
                    {
                        "business_intent_policy_hash": intent_policy_hash,
                        "intent_fingerprint": intent_fingerprint,
                    },
                    require_engine_version=True,
                )
                viable_candidates = intent_resolution["viable_candidates"]
                requirement_bindings = _source_derived_requirement_bindings(
                    intent_resolution, metric, intent_policy
                )
                requirement_bindings.update(
                    _performance_requirement_bindings(metric, requirement_bindings)
                )
                if requirement_bindings and not viable_candidates:
                    for consumer in metric.get("consumers") or []:
                        if not isinstance(consumer, dict):
                            continue
                        unresolved_id = str(consumer.get("requirement_id") or "")
                        if not unresolved_id or unresolved_id in requirement_bindings:
                            continue
                        requirement_bindings[unresolved_id] = {
                            "mode": "unavailable",
                            "reason": "no_complete_requirement_candidate",
                            "requested_periods": list(consumer.get("periods") or []),
                            "requested_breakdown_dimensions": list(
                                consumer.get("breakdown_dimensions") or []
                            ),
                        }
                task_resolution["requirement_bindings"].update(requirement_bindings)
                consumer_requirement_ids = {
                    str(item.get("requirement_id") or "")
                    for item in metric.get("consumers") or []
                    if isinstance(item, dict) and item.get("requirement_id")
                }
                requirement_only_resolution = (
                    bool(consumer_requirement_ids)
                    and consumer_requirement_ids == set(requirement_bindings)
                )
                best_tier = min(
                    (int(item.get("semantic_tier") or 0) for item in viable_candidates),
                    default=None,
                )
                auto_candidates = [
                    item
                    for item in viable_candidates
                    if int(item.get("semantic_tier") or 0) == best_tier
                    and not item.get("requires_confirmation")
                ]
                selected_intent = next(
                    (
                        item
                        for item in viable_candidates
                        if item.get("candidate_id") == selected_id
                    ),
                    None,
                )
                if selected_intent is None and len(auto_candidates) == 1:
                    selected_intent = auto_candidates[0]
                intent_action = (
                    "auto"
                    if selected_intent is not None or requirement_only_resolution
                    else "confirm"
                    if viable_candidates
                    else "block"
                )
                intent_case = {
                    "case_id": intent_case_id,
                    "action": intent_action,
                    "kind": "interpretation",
                    "requested_term": requested_name,
                    "task_ids": [task_id],
                    "metric_ref": metric_ref,
                    "selected_candidate_id": (
                        selected_intent.get("candidate_id") if selected_intent else None
                    ),
                    "policy_version": policy.get("policy_version"),
                    "resolution_policy_hash": policy_hash,
                    "business_intent_policy_version": intent_policy.get("policy_version"),
                    "business_intent_policy_hash": intent_policy_hash,
                    "source_revision": source.get("revision"),
                    "schema_hash": source.get("schema_hash"),
                    "resolution_engine_version": ENGINE_VERSION,
                    "intent_fingerprint": intent_fingerprint,
                    "patch_status": intent_patch_status,
                    "candidates": [
                        _intent_candidate_packet(item)
                        for item in intent_resolution["candidates"]
                    ],
                }
                decisions.append(intent_case)
                task_resolution["intent_resolutions"][metric_ref] = {
                    "status": (
                        "selected"
                        if selected_intent
                        else "requirement_selected"
                        if requirement_only_resolution
                        else "ambiguous"
                        if viable_candidates
                        else "infeasible"
                    ),
                    "selected_candidate": (
                        _intent_candidate_packet(selected_intent)
                        if selected_intent
                        else None
                    ),
                    "case_id": intent_case_id,
                    "business_intent_policy_hash": intent_policy_hash,
                    "requirement_bindings": deepcopy(requirement_bindings),
                }
                if selected_intent is None and not requirement_only_resolution:
                    composition_intent = intent_by_metric_ref.get(metric_ref)
                    if not viable_candidates and composition_intent is not None:
                        intent_case["activation"] = "deferred"
                        intent_case["deferred_to_composition_id"] = composition_intent.get(
                            "composition_id"
                        )
                        task_resolution["intent_resolutions"][metric_ref][
                            "status"
                        ] = "composition_deferred"
                        task_resolution["metric_statuses"][metric_ref] = {
                            "requested_metric": requested_name,
                            "status": "composition_deferred",
                            "binding": None,
                        }
                        continue
                    cases.append(intent_case)
                    task_resolution["resolution_cases"].append(intent_case)
                    task_resolution["metric_statuses"][metric_ref] = {
                        "requested_metric": requested_name,
                        "status": "ambiguous" if viable_candidates else "not_executable",
                        "binding": None,
                    }
                    continue
                if selected_intent is None:
                    task_resolution["metric_statuses"][metric_ref] = {
                        "requested_metric": requested_name,
                        "status": "requirement_bound",
                        "binding": None,
                    }
                    continue
                binding = str(selected_intent["metric"])
                resolution_requirement_id = str(
                    metric.get("resolution_requirement_id") or ""
                )
                if resolution_requirement_id:
                    task_resolution["requirement_bindings"][resolution_requirement_id] = {
                        "mode": "source_scoped_fact",
                        "source_metric": binding,
                        "candidate_id": selected_intent.get("candidate_id"),
                        "output_metric_ref": metric_ref,
                        "resolution_operation": "share_level",
                    }
                task_resolution["metric_bindings"][requested_name] = binding
                binding_votes.setdefault(requested_name, set()).add(binding)
                binding_successes[requested_name] = binding_successes.get(requested_name, 0) + 1
                task_resolution["metric_statuses"][metric_ref] = {
                    "requested_metric": requested_name,
                    "status": "bound",
                    "binding": binding,
                }
                continue
            resolution = _query_metric_resolution(metric, overlay.get("metrics") or {}, query, policy)
            semantics_fingerprint = stable_id("metric_semantics", {
                "metric_object": metric.get("metric_object"),
                "unit": str(metric.get("unit") or "").strip().lower(),
            })
            identity = {
                "source_id": source.get("spreadsheet_token") or source.get("url"),
                "kind": "query_metric",
                "task_id": task_id,
                "metric_ref": metric_ref,
                "requested_name": requested_name,
                "metric_semantics_fingerprint": semantics_fingerprint,
            }
            case_id = stable_id("resolution_case", identity)
            candidates = [
                {
                    "candidate_id": stable_id("resolution_candidate", {**identity, "name": item["name"]}),
                    "metric": item["name"],
                    "confidence": item["confidence"],
                    "evidence": item["evidence"],
                    "conflicts": list(item.get("conflicts") or []),
                }
                for item in resolution.get("candidates") or []
            ]
            selected_id, patch_status = _patch_selection(
                case_id,
                context_patches,
                source,
                policy_hash,
                {"metric_semantics_fingerprint": semantics_fingerprint},
                require_engine_version=True,
            )
            selected = next((item for item in candidates if item["candidate_id"] == selected_id), None)
            if selected is None and resolution.get("binding"):
                selected = next(
                    (
                        item for item in candidates
                        if item.get("metric") == resolution.get("binding")
                    ),
                    None,
                )
            action = "auto" if selected else str(resolution.get("action") or "block")
            status = "bound" if selected else str(resolution.get("status") or "not_found")
            if selected:
                binding = str(selected["metric"])
                resolution_requirement_id = str(
                    metric.get("resolution_requirement_id") or ""
                )
                if resolution_requirement_id:
                    task_resolution["requirement_bindings"][resolution_requirement_id] = {
                        "mode": "source_scoped_fact",
                        "source_metric": binding,
                        "candidate_id": selected.get("candidate_id"),
                        "output_metric_ref": metric_ref,
                        "resolution_operation": "share_level",
                    }
                task_resolution["metric_bindings"][requested_name] = binding
                binding_votes.setdefault(requested_name, set()).add(binding)
                binding_successes[requested_name] = binding_successes.get(requested_name, 0) + 1
            task_resolution["metric_statuses"][metric_ref] = {
                "requested_metric": requested_name,
                "status": status,
                "binding": str(selected["metric"]) if selected else None,
            }
            case = {
                "case_id": case_id,
                "action": action,
                "kind": "query_metric",
                "requested_term": requested_name,
                "task_ids": [task_id],
                "metric_ref": metric_ref,
                "selected_candidate_id": selected.get("candidate_id") if selected else None,
                "policy_version": policy.get("policy_version"),
                "resolution_policy_hash": policy_hash,
                "source_revision": source.get("revision"),
                "schema_hash": source.get("schema_hash"),
                "resolution_engine_version": ENGINE_VERSION,
                "metric_semantics_fingerprint": semantics_fingerprint,
                "patch_status": patch_status,
                "candidates": [_candidate_packet(item) for item in candidates[: int(policy.get("limits", {}).get("max_candidates_per_case", 3))]],
            }
            decisions.append(case)
            has_composition_fallback = metric_ref in intent_by_metric_ref
            if action != "auto" and not (
                status == "not_found" and has_composition_fallback
            ):
                cases.append(case)
                task_resolution["resolution_cases"].append(case)

        for intent in intents:
            metric_ref = str(intent.get("metric_ref") or "")
            composition_id = str(intent.get("composition_id") or "")
            composition_target_grains = {
                parsed[0]
                for consumer in intent.get("consumers") or []
                if isinstance(consumer, dict)
                for period in consumer.get("periods") or []
                if (parsed := _normalize_period(period)) is not None
            }
            composition_breakdown_dimensions = {
                str(dimension)
                for consumer in intent.get("consumers") or []
                if isinstance(consumer, dict)
                for dimension in consumer.get("breakdown_dimensions") or []
            }
            rollup_edges = (policy.get("grain_rollup") or {}).get("allowed_edges") or []
            input_bindings: dict[str, str] = {}
            input_statuses: dict[str, dict[str, Any]] = {}
            deferred_cases: list[dict[str, Any]] = []
            for number, item in enumerate(intent.get("inputs") or []):
                if not isinstance(item, dict) or not item.get("metric"):
                    continue
                role = str(item.get("role") or f"input_{number + 1}")
                requested_name = str(item["metric"])
                binding_attempts[requested_name] = binding_attempts.get(requested_name, 0) + 1
                requested_leaf = {
                    "name": requested_name,
                    "metric_object": item.get("metric_object"),
                    "unit": item.get("unit") or "待元信息解析",
                    "provenance": "registered_definition",
                }
                resolution = _query_metric_resolution(
                    requested_leaf, overlay.get("metrics") or {}, query, policy
                )
                semantics_fingerprint = stable_id("metric_semantics", {
                    "metric_object": requested_leaf.get("metric_object"),
                    "unit": str(requested_leaf.get("unit") or "").strip().lower(),
                })
                identity = {
                    "source_id": source.get("spreadsheet_token") or source.get("url"),
                    "kind": "composition_input",
                    "task_id": task_id,
                    "metric_ref": metric_ref,
                    "composition_id": composition_id,
                    "input_role": role,
                    "requested_name": requested_name,
                    "metric_semantics_fingerprint": semantics_fingerprint,
                    "composition_registry_hash": composition_registry_hash,
                }
                case_id = stable_id("resolution_case", identity)
                input_candidates = [
                    {
                        "candidate_id": stable_id(
                            "resolution_candidate", {**identity, "name": candidate["name"]}
                        ),
                        "metric": candidate["name"],
                        "confidence": candidate["confidence"],
                        "evidence": candidate["evidence"],
                        "conflicts": list(candidate.get("conflicts") or []),
                    }
                    for candidate in resolution.get("candidates") or []
                ]
                selected_id, patch_status = _patch_selection(
                    case_id,
                    context_patches,
                    source,
                    policy_hash,
                    {
                        "metric_semantics_fingerprint": semantics_fingerprint,
                        "composition_registry_hash": composition_registry_hash,
                    },
                    require_engine_version=True,
                )
                selected = next(
                    (candidate for candidate in input_candidates if candidate["candidate_id"] == selected_id),
                    None,
                )
                if selected is None and resolution.get("binding"):
                    selected = next(
                        (
                            candidate for candidate in input_candidates
                            if candidate.get("metric") == resolution.get("binding")
                        ),
                        None,
                    )
                structural_checks: list[dict[str, Any]] = []
                dimension_capability: dict[str, Any] = {
                    "status": "deferred",
                    "reason": "no_breakdown_requested",
                    "requested_dimensions": [],
                }
                if selected is not None and composition_target_grains:
                    metadata = (overlay.get("metrics") or {}).get(
                        str(selected.get("metric") or "")
                    ) or {}
                    structural_checks = [
                        evaluate_structural_grain_capability(
                            metadata, grain, rollup_edges
                        )
                        for grain in sorted(composition_target_grains)
                    ]
                    if any(item.get("status") == "unavailable" for item in structural_checks):
                        selected = None
                        input_candidates = []
                if selected is not None and composition_breakdown_dimensions:
                    dimension, dimension_failure = _candidate_dimension(
                        {"breakdown_dimensions": sorted(composition_breakdown_dimensions)},
                        str(selected.get("metric") or ""),
                        overlay,
                    )
                    dimension_capability = dimension_failure or (
                        {"status": "available", "dimension": dimension}
                        if dimension is not None
                        else {
                            "status": "deferred",
                            "reason": "dimension_binding_ambiguous",
                            "requested_dimensions": sorted(
                                composition_breakdown_dimensions
                            ),
                        }
                    )
                    if dimension_failure is not None:
                        selected = None
                        input_candidates = []
                input_status = "bound" if selected else str(resolution.get("status") or "not_found")
                if structural_checks and any(
                    item.get("status") == "unavailable" for item in structural_checks
                ):
                    input_status = "not_executable"
                if dimension_capability.get("status") == "unavailable":
                    input_status = "not_executable"
                if selected:
                    binding = str(selected["metric"])
                    input_bindings[role] = binding
                    task_resolution["metric_bindings"][requested_name] = binding
                    binding_votes.setdefault(requested_name, set()).add(binding)
                    binding_successes[requested_name] = binding_successes.get(requested_name, 0) + 1
                input_statuses[role] = {
                    "requested_metric": requested_name,
                    "status": input_status,
                    "binding": str(selected["metric"]) if selected else None,
                    "structural_capability": structural_checks,
                    "dimension_capability": dimension_capability,
                }
                if not selected:
                    deferred_cases.append({
                        "case_id": case_id,
                        "action": "confirm" if input_status == "ambiguous" else "block",
                        "activation": "deferred",
                        "kind": "composition_input",
                        "requested_term": requested_name,
                        "task_ids": [task_id],
                        "metric_ref": metric_ref,
                        "composition_id": composition_id,
                        "input_role": role,
                        "selected_candidate_id": None,
                        "policy_version": policy.get("policy_version"),
                        "resolution_policy_hash": policy_hash,
                        "source_revision": source.get("revision"),
                        "schema_hash": source.get("schema_hash"),
                        "resolution_engine_version": ENGINE_VERSION,
                        "metric_semantics_fingerprint": semantics_fingerprint,
                        "composition_registry_hash": composition_registry_hash,
                        "patch_status": patch_status,
                        "candidates": [
                            _candidate_packet(candidate)
                            for candidate in input_candidates[: int(
                                policy.get("limits", {}).get("max_candidates_per_case", 3)
                            )]
                        ],
                    })
            statuses = {item["status"] for item in input_statuses.values()}
            fallback_status = (
                "ready"
                if statuses == {"bound"}
                else "confirm"
                if "ambiguous" in statuses
                else "blocked"
            )
            direct_status = (
                task_resolution["metric_statuses"].get(metric_ref) or {}
            ).get("status", "not_found")
            direct_recall_floor = float(
                (policy.get("candidate_evaluation") or {}).get("lexical_recall_floor", 0.78)
            )
            direct_cases = [
                case for case in task_resolution.get("resolution_cases") or []
                if case.get("metric_ref") == metric_ref
                and case.get("kind") in {"query_metric", "interpretation"}
            ]
            has_viable_direct_ambiguity = direct_status == "ambiguous" and any(
                float(candidate.get("confidence") or 0.0) >= direct_recall_floor
                for case in direct_cases
                for candidate in case.get("candidates") or []
            )
            fulfillment_candidates: list[dict[str, Any]] = []
            direct_binding = (
                task_resolution["metric_statuses"].get(metric_ref) or {}
            ).get("binding")
            if direct_binding:
                fulfillment_candidates.append({
                    "candidate_id": f"direct:{direct_binding}",
                    "candidate_type": "direct_fact",
                    "status": "viable",
                    "metric": direct_binding,
                    "confidence": 1.0,
                })
            fulfillment_candidates.append({
                "candidate_id": f"composition:{composition_id}",
                "candidate_type": "registered_composition",
                "status": (
                    "viable"
                    if fallback_status == "ready"
                    and direct_status in {
                        "not_found", "not_executable", "composition_deferred"
                    }
                    or fallback_status == "ready"
                    and direct_status == "ambiguous"
                    and not has_viable_direct_ambiguity
                    else "infeasible"
                ),
                "composition_id": composition_id,
                "input_bindings": deepcopy(input_bindings),
                "confidence": 1.0 if fallback_status == "ready" else 0.0,
            })
            selected_fulfillment, ranked_fulfillments = select_fulfillment_candidate(
                fulfillment_candidates
            )
            composition_resolution = {
                "metric_ref": metric_ref,
                "requested_metric": intent.get("requested_metric"),
                "composition_id": composition_id,
                "direct_status": direct_status,
                "input_bindings": input_bindings,
                "input_statuses": input_statuses,
                "fallback_status": fallback_status,
                "deferred_cases": deferred_cases,
                "consumers": deepcopy(intent.get("consumers") or []),
                "fulfillment_candidates": ranked_fulfillments,
                "selected_fulfillment": deepcopy(selected_fulfillment),
            }
            task_resolution["composition_resolutions"].append(composition_resolution)
            resolution_requirement_id = str(
                intent.get("resolution_requirement_id") or ""
            )
            if (
                selected_fulfillment is not None
                and selected_fulfillment.get("candidate_type") == "registered_composition"
                and resolution_requirement_id
            ):
                task_resolution["requirement_bindings"][resolution_requirement_id] = {
                    "mode": "registered_composition",
                    "composition_id": composition_id,
                    "output_metric_ref": metric_ref,
                    "logical_metric_ref": intent.get("logical_metric_ref"),
                    "input_bindings": deepcopy(input_bindings),
                    "resolution_operation": "share_level",
                }
        task_resolutions[task_id] = task_resolution

    for requested_name, bindings in binding_votes.items():
        if (
            len(bindings) == 1
            and binding_successes.get(requested_name) == binding_attempts.get(requested_name)
        ):
            metric_bindings[requested_name] = next(iter(bindings))

    dimension_bindings: dict[str, str] = {}
    metric_dimension_bindings: dict[str, dict[str, str]] = {}
    for selected in selected_blocks:
        metric_dimension_bindings.setdefault(selected["metric"], {})[
            selected["requested_dimension"]
        ] = selected["source_dimension"]
    # A block may already be structurally valid while the Query uses a broader
    # logical dimension name (for example, 平台). Resolve that name jointly with
    # each metric's declared and observed physical dimensions. This remains
    # metric-scoped, so TOP4 and TOP6 can coexist in one request.
    task_metric_dimension_bindings: dict[str, dict[str, dict[str, str]]] = {
        task_id: {} for task_id in task_resolutions
    }
    for context in contexts:
        logical_dimensions = [str(value) for value in context.get("dimensions") or []]
        task_id = str(context.get("task_id") or "default")
        task_resolution = task_resolutions.get(task_id) or {}
        task_binding = task_resolution.get("metric_bindings") or {}
        requirement_source_metrics = {
            str(item.get("source_metric"))
            for item in (task_resolution.get("requirement_bindings") or {}).values()
            if isinstance(item, dict) and item.get("source_metric")
        }
        for source_metric in sorted(set(task_binding.values()) | requirement_source_metrics):
            declared = {
                str(value)
                for value in (overlay.get("metrics", {}).get(source_metric) or {}).get("dimensions") or []
            }
            observed = {
                str(block.get("dimension"))
                for sheet in (overlay.get("sheets") or {}).values()
                if isinstance(sheet, dict)
                for name, block in (sheet.get("blocks") or {}).items()
                if name == source_metric and isinstance(block, dict) and block.get("dimension")
            }
            physical_dimensions = (declared & observed) or declared or observed
            for logical_dimension in logical_dimensions:
                exact = resolve_catalogue_name(
                    logical_dimension, overlay.get("dimensions") or {}, "dimension", strict=False
                )
                if exact in physical_dimensions:
                    target = exact
                else:
                    match = match_catalogue_name(
                        logical_dimension, overlay.get("dimensions") or {}, "dimension"
                    )
                    viable = {
                        str(item.get("name"))
                        for item in match.get("candidates") or []
                        if not item.get("conflicts") and str(item.get("name")) in physical_dimensions
                    }
                    target = next(iter(viable)) if len(viable) == 1 else None
                if target:
                    metric_dimension_bindings.setdefault(source_metric, {})[
                        logical_dimension
                    ] = str(target)
                    task_metric_dimension_bindings.setdefault(task_id, {}).setdefault(
                        source_metric, {}
                    )[logical_dimension] = str(target)
    for requested in request.get("dimensions") or []:
        exact = resolve_catalogue_name(str(requested), overlay.get("dimensions") or {}, "dimension", strict=False)
        if exact:
            dimension_bindings[str(requested)] = exact
            continue
        targets = {
            bindings[str(requested)]
            for bindings in metric_dimension_bindings.values()
            if str(requested) in bindings
        }
        if len(targets) == 1:
            dimension_bindings[str(requested)] = next(iter(targets))

    context_by_task = {
        str(context.get("task_id") or "default"): context for context in contexts
    }
    final_cases: list[dict[str, Any]] = []
    for case in cases:
        if case.get("kind") != "fact_block":
            final_cases.append(case)
            continue
        direct_tasks: list[str] = []
        requested_token = normalize_match_text(case.get("requested_term"))
        for task_id in case.get("task_ids") or []:
            context = context_by_task.get(str(task_id)) or {}
            direct_names = {
                normalize_match_text(item.get("name"))
                for item in context.get("metrics") or []
                if isinstance(item, dict)
            }
            if requested_token in direct_names:
                direct_tasks.append(str(task_id))
                continue
            task_resolution = task_resolutions.get(str(task_id)) or {}
            attached = False
            for composition in task_resolution.get("composition_resolutions") or []:
                for role, status in (composition.get("input_statuses") or {}).items():
                    if normalize_match_text(status.get("requested_metric")) != requested_token:
                        continue
                    deferred = deepcopy(case)
                    deferred["activation"] = "deferred"
                    deferred["task_ids"] = [str(task_id)]
                    deferred["metric_ref"] = composition.get("metric_ref")
                    deferred["composition_id"] = composition.get("composition_id")
                    deferred["input_role"] = role
                    composition.setdefault("deferred_cases", []).append(deferred)
                    composition["fallback_status"] = (
                        "confirm" if case.get("action") == "confirm" else "blocked"
                    )
                    attached = True
            if not attached:
                direct_tasks.append(str(task_id))
        if direct_tasks:
            direct_case = deepcopy(case)
            direct_case["task_ids"] = direct_tasks
            final_cases.append(direct_case)
    cases = final_cases

    return {
        "index": overlay,
        "metric_bindings": metric_bindings,
        "task_resolutions": task_resolutions,
        "task_metric_dimension_bindings": task_metric_dimension_bindings,
        "dimension_bindings": dimension_bindings,
        "metric_dimension_bindings": metric_dimension_bindings,
        "resolution_cases": cases,
        "resolution_decisions": decisions,
        "resolution_policy": {
            "schema_version": POLICY_SCHEMA,
            "policy_version": policy.get("policy_version"),
            "engine_version": ENGINE_VERSION,
            "sha256": policy_hash,
        },
        "business_intent_policy": {
            "schema_version": intent_policy.get("schema_version"),
            "policy_version": intent_policy.get("policy_version"),
            "sha256": intent_policy_hash,
        },
    }
