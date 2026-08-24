#!/usr/bin/env python3
"""Declarative, bounded business-intent hypothesis generation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


POLICY_SCHEMA = "business_intent_policy/1.0"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "business-intent-policy-registry.json"
)
ALLOWED_TOP_LEVEL = {"schema_version", "policy_version", "limits", "rules"}
ALLOWED_LIMITS = {"max_hypotheses_per_metric", "max_candidates_per_case"}
ALLOWED_RULE_FIELDS = {
    "intent_id",
    "priority",
    "triggers",
    "metric_term_templates",
    "metric_object",
    "skip_if_metric_contains_any",
    "allowed_object_provenance",
    "derived_metric_ids",
}
ALLOWED_TRIGGER_FIELDS = {"mode", "any", "all"}
ALLOWED_PROVENANCE = {
    "model_inferred",
    "user_explicit",
    "user_formula",
    "registered_definition",
    "source_metadata",
}


class BusinessIntentPolicyError(ValueError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def business_intent_policy_hash(policy: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(policy).encode("utf-8")).hexdigest()


def validate_business_intent_policy(policy: dict[str, Any]) -> None:
    if not isinstance(policy, dict) or policy.get("schema_version") != POLICY_SCHEMA:
        raise BusinessIntentPolicyError(
            "INVALID_BUSINESS_INTENT_POLICY", f"策略必须使用 {POLICY_SCHEMA}"
        )
    unknown = set(policy) - ALLOWED_TOP_LEVEL
    if unknown:
        raise BusinessIntentPolicyError(
            "INVALID_BUSINESS_INTENT_POLICY_FIELD",
            "策略包含未允许的顶层字段",
            {"fields": sorted(unknown)},
        )
    limits = policy.get("limits") or {}
    if not isinstance(limits, dict) or set(limits) - ALLOWED_LIMITS:
        raise BusinessIntentPolicyError(
            "INVALID_BUSINESS_INTENT_POLICY_FIELD", "limits 包含未允许字段"
        )
    for field in ALLOWED_LIMITS:
        value = limits.get(field, 3)
        if not isinstance(value, int) or not 1 <= value <= 10:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY_LIMIT", f"{field} 必须是 1 到 10 的整数"
            )
    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules:
        raise BusinessIntentPolicyError(
            "INVALID_BUSINESS_INTENT_POLICY", "rules 必须是非空数组"
        )
    seen: set[str] = set()
    for number, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY", f"rules[{number}] 必须是对象"
            )
        unknown_rule = set(rule) - ALLOWED_RULE_FIELDS
        if unknown_rule:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY_FIELD",
                f"规则 {number} 包含未允许字段",
                {"fields": sorted(unknown_rule)},
            )
        intent_id = str(rule.get("intent_id") or "")
        if not intent_id or intent_id in seen:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY", "intent_id 必须非空且唯一"
            )
        seen.add(intent_id)
        priority = rule.get("priority")
        if not isinstance(priority, int):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY", f"{intent_id}.priority 必须是整数"
            )
        triggers = rule.get("triggers") or {}
        if not isinstance(triggers, dict) or set(triggers) - ALLOWED_TRIGGER_FIELDS:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY_FIELD", f"{intent_id}.triggers 非法"
            )
        if triggers.get("mode") not in (None, "always"):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY", f"{intent_id}.triggers.mode 非法"
            )
        for field in ("any", "all"):
            values = triggers.get(field) or []
            if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                raise BusinessIntentPolicyError(
                    "INVALID_BUSINESS_INTENT_POLICY", f"{intent_id}.triggers.{field} 非法"
                )
        templates = rule.get("metric_term_templates") or []
        if (
            not isinstance(templates, list)
            or not templates
            or not all(isinstance(value, str) and value.count("{metric}") == 1 for value in templates)
        ):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY",
                f"{intent_id}.metric_term_templates 必须是包含一次 {{metric}} 的非空数组",
            )
        metric_object = rule.get("metric_object")
        if metric_object not in {"inherit", "volume", "ratio"}:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY", f"{intent_id}.metric_object 非法"
            )
        provenance = rule.get("allowed_object_provenance") or []
        if not isinstance(provenance, list) or not provenance or set(provenance) - ALLOWED_PROVENANCE:
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY",
                f"{intent_id}.allowed_object_provenance 非法",
            )
        skip_tokens = rule.get("skip_if_metric_contains_any") or []
        if not isinstance(skip_tokens, list) or not all(
            isinstance(value, str) and value for value in skip_tokens
        ):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY",
                f"{intent_id}.skip_if_metric_contains_any 非法",
            )
        derived_metric_ids = rule.get("derived_metric_ids") or []
        if not isinstance(derived_metric_ids, list) or not all(
            isinstance(value, str) and value for value in derived_metric_ids
        ):
            raise BusinessIntentPolicyError(
                "INVALID_BUSINESS_INTENT_POLICY",
                f"{intent_id}.derived_metric_ids 非法",
            )


def load_business_intent_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = path or DEFAULT_POLICY_PATH
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    validate_business_intent_policy(policy)
    return policy


def _triggered(
    rule: dict[str, Any], query: str, consumers: list[dict[str, Any]]
) -> bool:
    triggers = rule.get("triggers") or {}
    if triggers.get("mode") == "always":
        return True
    derived_metric_ids = set(str(value) for value in rule.get("derived_metric_ids") or [])
    consumer_derived_ids = {
        str(item.get("derived_metric_id"))
        for item in consumers
        if isinstance(item, dict) and item.get("derived_metric_id")
    }
    if derived_metric_ids and consumer_derived_ids & derived_metric_ids:
        return True
    consumer_texts = [
        str(item.get("semantic_text") or item.get("query_fragment") or "")
        for item in consumers
        if isinstance(item, dict)
        and (item.get("semantic_text") or item.get("query_fragment"))
    ]
    # Structured consumers are requirement-scoped. Falling back to the full Query
    # here would leak a modifier from one clause or metric into every other metric.
    trigger_text = " ".join(consumer_texts) if consumers else query
    if not trigger_text:
        return False
    any_tokens = triggers.get("any") or []
    all_tokens = triggers.get("all") or []
    return (not any_tokens or any(token in trigger_text for token in any_tokens)) and all(
        token in trigger_text for token in all_tokens
    )


def generate_metric_hypotheses(
    context: dict[str, Any], metric: dict[str, Any], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """Generate only bounded semantic hypotheses; source binding happens elsewhere."""
    validate_business_intent_policy(policy)
    query = str(context.get("query") or "")
    metric_name = str(metric.get("name") or "")
    consumers = [
        item for item in metric.get("consumers") or [] if isinstance(item, dict)
    ]
    object_provenance = str(metric.get("metric_object_provenance") or "model_inferred")
    consumer_types = {
        str(item.get("requirement_type"))
        for item in metric.get("consumers") or []
        if isinstance(item, dict) and item.get("requirement_type")
    }
    alternatives_allowed = bool(consumer_types) and consumer_types.issubset({
        "fact_observations", "derived_requirements"
    })
    hypotheses: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for rule in sorted(policy["rules"], key=lambda item: -int(item["priority"])):
        if rule.get("intent_id") != "declared_metric" and not alternatives_allowed:
            continue
        if object_provenance not in set(rule.get("allowed_object_provenance") or []):
            continue
        if not _triggered(rule, query, consumers):
            continue
        if any(token in metric_name for token in rule.get("skip_if_metric_contains_any") or []):
            continue
        terms = tuple(dict.fromkeys(
            template.replace("{metric}", metric_name)
            for template in rule.get("metric_term_templates") or []
        ))
        metric_object = (
            metric.get("metric_object")
            if rule.get("metric_object") == "inherit"
            else rule.get("metric_object")
        )
        identity = (terms, str(metric_object or ""))
        if identity in seen:
            continue
        seen.add(identity)
        hypotheses.append({
            "intent_id": str(rule["intent_id"]),
            "priority": int(rule["priority"]),
            "requested_terms": list(terms),
            "metric_object": metric_object,
            "object_override_allowed": (
                object_provenance == "model_inferred"
                and metric_object in {"volume", "ratio"}
                and metric_object != metric.get("metric_object")
            ),
            "evidence": [f"business_intent_rule:{rule['intent_id']}"],
        })
        if len(hypotheses) >= int(policy.get("limits", {}).get("max_hypotheses_per_metric", 3)):
            break
    return hypotheses
