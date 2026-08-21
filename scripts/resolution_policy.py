#!/usr/bin/env python3
"""Deterministic business resolution policy and query-scoped source overlay."""
from __future__ import annotations

import hashlib
import json
import re
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


POLICY_SCHEMA = "resolution_policy/2.0"
ENGINE_VERSION = "2.3.0"
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
    allowed_semantic_keys = {"comparison_terms", "measure_terms", "equivalence_rules"}
    unknown_semantic = set(semantic_normalization) - allowed_semantic_keys
    if unknown_semantic:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD",
            "semantic_normalization 包含未允许字段",
            {"fields": sorted(unknown_semantic)},
        )
    for field in ("comparison_terms", "measure_terms"):
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
        "minimum_candidate_margin",
        "allow_unique_capability_selection",
    }
    if unknown_evaluation:
        raise ResolutionPolicyError(
            "INVALID_RESOLUTION_POLICY_FIELD",
            "candidate_evaluation 包含未允许字段",
            {"fields": sorted(unknown_evaluation)},
        )
    for field in ("lexical_recall_floor", "minimum_candidate_margin"):
        value = candidate_evaluation.get(field, 0.78 if field == "lexical_recall_floor" else 0.1)
        if not isinstance(value, (int, float)) or not 0 <= float(value) <= 1:
            raise ResolutionPolicyError(
                "INVALID_RESOLUTION_POLICY",
                f"candidate_evaluation.{field} 必须是 0 到 1 的数值",
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
        if (
            "metric_object_compatible" in configured_gates
            and expected_object
            and actual_object
            and expected_object != actual_object
        ):
            conflicts.append("metric_object_mismatch")
        if (
            "unit_compatible" in configured_gates
            and not _unit_compatible(requested.get("unit"), metadata.get("unit"))
        ):
            conflicts.append("unit_mismatch")
        semantic = _semantic_compatibility(
            name,
            str(raw.get("matched_text") or candidate_name),
            candidate_name,
            metadata,
            str(expected_object) if expected_object else None,
            policy,
        )
        protected_conflicts = [
            conflict
            for conflict in raw_conflicts
            if str(conflict).startswith("protected_term_difference:")
        ]
        soft_conflicts: list[str] = []
        if protected_conflicts and semantic["status"] == "conflict":
            conflicts.append(f"protected_semantics_conflict:{semantic['reason']}")
        elif protected_conflicts and semantic["status"] == "unknown":
            soft_conflicts.extend(protected_conflicts)
        candidate = {
            "name": candidate_name,
            "confidence": float(raw.get("confidence") or 0.0),
            "lexical_score": float(raw.get("confidence") or 0.0),
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
        candidate["evidence"] += (["query_semantic_match"] if semantic_hint else [])
        candidate["strong"] = (
            str(candidate.get("match_method")) in {"standard_name", "metadata_alias", "builtin_alias"}
            or provenance in {"user_explicit", "user_formula", "registered_definition", "source_metadata"}
            or semantic_hint
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


def _normalize_period(value: Any) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", str(value or "")).lower()
    patterns = (
        ("month", r"(20\d{2})(?:年|[-/.])?(1[0-2]|0?[1-9])月?"),
        ("week", r"(20\d{2})(?:年)?(?:第|[-/]?w)([0-5]?\d)周?"),
        ("quarter", r"(20\d{2})(?:年)?(?:第?([1-4])季度|[-/]?q([1-4]))"),
        ("year", r"(20\d{2})年?"),
    )
    for grain, pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        year = int(match.group(1))
        if grain == "month":
            return grain, f"{year:04d}-{int(match.group(2)):02d}"
        if grain == "week":
            week = int(match.group(2))
            return (grain, f"{year:04d}-W{week:02d}") if 1 <= week <= 53 else None
        if grain == "quarter":
            return grain, f"{year:04d}-Q{int(match.group(2) or match.group(3))}"
        return grain, f"{year:04d}"
    return None


def _candidate_dimension(
    context: dict[str, Any], metric: str, index: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    requested = [str(value) for value in context.get("dimensions") or []]
    metadata = (index.get("metrics") or {}).get(metric) or {}
    supported = {str(value) for value in metadata.get("dimensions") or []}
    if not requested:
        if supported and "无" not in supported:
            return None, {
                "status": "unavailable",
                "reason": "metadata_dimension_unsupported",
                "requested_dimension": "无",
                "supported_dimensions": sorted(supported),
            }
        return "无", None
    resolved: set[str] = set()
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
    if len(resolved) != 1:
        return None, {
            "status": "blocked",
            "reason": "dimension_ambiguous",
            "requested_dimensions": requested,
            "supported_dimensions": sorted(supported),
        }
    return next(iter(resolved)), None


def _child_period_candidates(period: str) -> list[list[str]]:
    parsed = _normalize_period(period)
    if parsed is None:
        return []
    grain, canonical = parsed
    year = int(canonical[:4])
    if grain == "quarter":
        quarter = int(canonical[-1])
        start = (quarter - 1) * 3 + 1
        return [[f"{year:04d}-{month:02d}" for month in range(start, start + 3)]]
    if grain == "year":
        return [
            [f"{year:04d}-Q{quarter}" for quarter in range(1, 5)],
            [f"{year:04d}-{month:02d}" for month in range(1, 13)],
        ]
    return []


def _direct_intent_capability(
    index: dict[str, Any], metric: str, period: str, dimension: str
) -> dict[str, Any]:
    parsed = _normalize_period(period)
    if parsed is None:
        return {"status": "blocked", "reason": "invalid_period", "period": period}
    grain, canonical = parsed
    metadata = (index.get("metrics") or {}).get(metric) or {}
    supported_grains = metadata.get("supported_grains")
    if "supported_grains" in metadata and not supported_grains:
        return {"status": "unavailable", "reason": "metadata_grain_unknown", "grain": grain}
    if supported_grains is not None and grain not in supported_grains:
        return {
            "status": "unavailable",
            "reason": "metadata_grain_unsupported",
            "grain": grain,
            "supported_grains": list(supported_grains),
        }
    sheet = (index.get("sheets") or {}).get(grain) or {}
    if canonical not in (sheet.get("periods") or {}):
        return {
            "status": "unavailable",
            "reason": "period_unavailable",
            "grain": grain,
            "period": canonical,
        }
    block = (sheet.get("blocks") or {}).get(metric) or {}
    if not block:
        return {"status": "unavailable", "reason": "metric_block_unavailable", "grain": grain}
    if block.get("dimension") != dimension:
        return {
            "status": "blocked",
            "reason": "metadata_fact_dimension_conflict",
            "expected_dimension": dimension,
            "fact_dimension": block.get("dimension"),
        }
    return {
        "status": "available",
        "path": "direct_fact",
        "grain": grain,
        "period": canonical,
        "dimension": dimension,
    }


def _intent_period_capability(
    index: dict[str, Any], metric: str, period: str, dimension: str
) -> dict[str, Any]:
    direct = _direct_intent_capability(index, metric, period, dimension)
    if direct.get("status") == "available":
        return direct
    metadata = (index.get("metrics") or {}).get(metric) or {}
    if (
        metadata.get("aggregation_mode") not in (None, "additive")
        and metadata.get("additive") is not True
    ) or (
        metadata.get("aggregation_mode") is None and metadata.get("additive") is not True
    ):
        return direct
    for children in _child_period_candidates(period):
        checks = [
            _direct_intent_capability(index, metric, child, dimension) for child in children
        ]
        if all(item.get("status") == "available" for item in checks):
            parsed = _normalize_period(period)
            return {
                "status": "available",
                "path": "aggregate_fact",
                "grain": parsed[0] if parsed else None,
                "period": parsed[1] if parsed else period,
                "dimension": dimension,
                "source_periods": children,
            }
    return direct


def _intent_candidate_packet(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(candidate[key])
        for key in (
            "candidate_id",
            "intent_id",
            "status",
            "metric",
            "metric_object",
            "requested_terms",
            "confidence",
            "confidence_detail",
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
        )
        if key in candidate
    }


def _resolve_business_intent(
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
    planning_context["dimensions"] = list(
        metric.get("required_dimensions")
        if "required_dimensions" in metric
        else context.get("dimensions") or []
    )
    if not planning_context.get("periods"):
        return None
    hypotheses = generate_metric_hypotheses(planning_context, metric, business_policy)
    if len(hypotheses) <= 1:
        return None
    candidates: list[dict[str, Any]] = []
    for hypothesis in hypotheses:
        best: dict[str, Any] | None = None
        mapping_failures: list[dict[str, Any]] = []
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
            binding = resolution.get("binding")
            if not binding:
                mapping_failures.append({
                    "requested_term": requested_term,
                    "status": resolution.get("status"),
                    "candidates": [
                        {
                            "metric": item.get("name"),
                            "confidence": item.get("confidence"),
                            "semantic_status": item.get("semantic_status"),
                            "conflicts": list(item.get("conflicts") or []),
                            "soft_conflicts": list(item.get("soft_conflicts") or []),
                        }
                        for item in (resolution.get("candidates") or [])[:3]
                    ],
                })
                continue
            dimension, dimension_failure = _candidate_dimension(
                planning_context, str(binding), index
            )
            period_checks: list[dict[str, Any]] = []
            if dimension_failure is None and dimension is not None:
                period_checks = [
                    _intent_period_capability(index, str(binding), str(period), dimension)
                    for period in planning_context.get("periods") or []
                ]
            viable = bool(period_checks) and all(
                item.get("status") == "available" for item in period_checks
            )
            paths = {str(item.get("path")) for item in period_checks if item.get("path")}
            path = (
                "direct_fact"
                if paths == {"direct_fact"}
                else "aggregate_fact"
                if paths and paths.issubset({"direct_fact", "aggregate_fact"})
                else None
            )
            parsed_grains = {
                parsed[0]
                for period in planning_context.get("periods") or []
                if (parsed := _normalize_period(period)) is not None
            }
            selected_resolution_candidate = next(
                (
                    item
                    for item in resolution.get("candidates") or []
                    if str(item.get("name") or "") == str(binding)
                ),
                {},
            )
            candidate = {
                "intent_id": hypothesis.get("intent_id"),
                "status": "viable" if viable else "infeasible",
                "metric": str(binding),
                "metric_object": hypothesis.get("metric_object"),
                "requested_terms": list(hypothesis.get("requested_terms") or []),
                "confidence": max(
                    [float(item.get("confidence") or 0.0) for item in resolution.get("candidates") or []]
                    or [0.0]
                ),
                "confidence_detail": {
                    "lexical": float(selected_resolution_candidate.get("lexical_score") or 0.0),
                    "semantic": selected_resolution_candidate.get("semantic_status", "unknown"),
                    "metadata": "pass" if dimension_failure is None else "fail",
                    "fact_capability": "pass" if viable else "fail",
                },
                "semantic_status": selected_resolution_candidate.get("semantic_status", "unknown"),
                "soft_conflicts": list(selected_resolution_candidate.get("soft_conflicts") or []),
                "path": path,
                "grain": next(iter(parsed_grains)) if len(parsed_grains) == 1 else None,
                "dimension": dimension,
                "periods": list(planning_context.get("periods") or []),
                "capability": {
                    "dimension": dimension_failure or {"status": "available", "dimension": dimension},
                    "periods": period_checks,
                },
                "evidence": list(hypothesis.get("evidence") or [])
                + ["source_metadata_evaluated"]
                + (["semantic_equivalence"] if selected_resolution_candidate.get("semantic_status") == "equivalent" else []),
                "conflicts": [] if viable else sorted({
                    str(item.get("reason"))
                    for item in ([dimension_failure] if dimension_failure else []) + period_checks
                    if isinstance(item, dict) and item.get("status") != "available" and item.get("reason")
                }),
                "object_override_allowed": bool(hypothesis.get("object_override_allowed")),
                "priority": int(hypothesis.get("priority") or 0),
            }
            allowed_object_sets = [
                set(str(value) for value in consumer.get("allowed_metric_objects") or [])
                for consumer in metric.get("consumers") or []
                if isinstance(consumer, dict) and consumer.get("allowed_metric_objects")
            ]
            if allowed_object_sets and any(
                candidate.get("metric_object") not in allowed
                for allowed in allowed_object_sets
            ):
                candidate["status"] = "infeasible"
                candidate["path"] = None
                candidate["conflicts"] = sorted(
                    set(candidate.get("conflicts") or []) | {"operation_metric_object_unsupported"}
                )
                candidate["capability"]["operation"] = {
                    "status": "unavailable",
                    "reason": "operation_metric_object_unsupported",
                    "allowed_metric_objects": sorted(set.intersection(*allowed_object_sets)),
                }
            if best is None or (
                candidate["status"] == "viable",
                candidate["confidence"],
            ) > (
                best["status"] == "viable",
                best["confidence"],
            ):
                best = candidate
        if best is None:
            best = {
                "intent_id": hypothesis.get("intent_id"),
                "status": "infeasible",
                "metric_object": hypothesis.get("metric_object"),
                "requested_terms": list(hypothesis.get("requested_terms") or []),
                "periods": list(planning_context.get("periods") or []),
                "capability": {"metric": {"status": "unavailable", "attempts": mapping_failures}},
                "evidence": list(hypothesis.get("evidence") or []),
                "conflicts": ["metric_unresolved"],
                "object_override_allowed": bool(hypothesis.get("object_override_allowed")),
                "priority": int(hypothesis.get("priority") or 0),
                "confidence": 0.0,
            }
        candidates.append(best)

    identity_base = {
        "task_id": context.get("task_id"),
        "metric_ref": metric.get("metric_ref") or metric.get("name"),
        "requested_name": metric.get("name"),
        "query": context.get("query"),
        "periods": planning_context.get("periods") or [],
        "dimensions": planning_context.get("dimensions") or [],
    }
    for candidate in candidates:
        candidate["candidate_id"] = stable_id("intent_candidate", {
            **identity_base,
            "intent_id": candidate.get("intent_id"),
            "metric": candidate.get("metric"),
            "metric_object": candidate.get("metric_object"),
            "path": candidate.get("path"),
        })
    deduplicated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            candidate.get("metric"),
            candidate.get("metric_object"),
            candidate.get("path"),
            candidate.get("status"),
        )
        previous = deduplicated.get(key)
        if previous is None or (
            int(candidate.get("priority") or 0), float(candidate.get("confidence") or 0.0)
        ) > (
            int(previous.get("priority") or 0), float(previous.get("confidence") or 0.0)
        ):
            deduplicated[key] = candidate
    bounded = sorted(
        deduplicated.values(),
        key=lambda item: (
            item.get("status") != "viable",
            -int(item.get("priority") or 0),
            -float(item.get("confidence") or 0.0),
            str(item.get("candidate_id")),
        ),
    )[: int(business_policy.get("limits", {}).get("max_candidates_per_case", 3))]
    return {
        "expanded": True,
        "identity": identity_base,
        "candidates": bounded,
        "viable_candidates": [item for item in bounded if item.get("status") == "viable"],
    }


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
            "metric_statuses": {},
            "intent_resolutions": {},
            "composition_resolutions": [],
            "resolution_cases": [],
        }
        for metric in context.get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("name"):
                continue
            requested_name = str(metric["name"])
            binding_attempts[requested_name] = binding_attempts.get(requested_name, 0) + 1
            metric_ref = str(metric.get("metric_ref") or requested_name)
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
                selected_intent = next(
                    (
                        item
                        for item in viable_candidates
                        if item.get("candidate_id") == selected_id
                    ),
                    None,
                )
                if selected_intent is None and len(viable_candidates) == 1:
                    selected_intent = viable_candidates[0]
                intent_action = (
                    "auto"
                    if selected_intent is not None
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
                }
                if selected_intent is None:
                    cases.append(intent_case)
                    task_resolution["resolution_cases"].append(intent_case)
                    task_resolution["metric_statuses"][metric_ref] = {
                        "requested_metric": requested_name,
                        "status": "ambiguous" if viable_candidates else "not_executable",
                        "binding": None,
                    }
                    continue
                binding = str(selected_intent["metric"])
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
                input_status = "bound" if selected else str(resolution.get("status") or "not_found")
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
            task_resolution["composition_resolutions"].append({
                "metric_ref": metric_ref,
                "requested_metric": intent.get("requested_metric"),
                "composition_id": composition_id,
                "direct_status": (
                    task_resolution["metric_statuses"].get(metric_ref) or {}
                ).get("status", "not_found"),
                "input_bindings": input_bindings,
                "input_statuses": input_statuses,
                "fallback_status": fallback_status,
                "deferred_cases": deferred_cases,
                "consumers": deepcopy(intent.get("consumers") or []),
            })
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
        task_binding = (task_resolutions.get(task_id) or {}).get("metric_bindings") or {}
        for source_metric in sorted(set(task_binding.values())):
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
