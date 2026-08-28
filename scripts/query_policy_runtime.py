#!/usr/bin/env python3
"""Load, validate, and select bounded Query Policy rule packets."""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from ir_contract_guard import IR_DECOMPOSITIONS, SCENARIO_TARGET_SEMANTICS


POLICY_ROOT = Path(__file__).resolve().parents[1] / "references" / "query-understanding"
POLICY_INDEX_SCHEMA = "query_policy_index/1.0"
POLICY_MANIFEST_SCHEMA = "query_policy_manifest/1.0"
POLICY_RULE_SCHEMA = "query_policy_rule/1.0"
POLICY_PACKET_SCHEMA = "query_policy_packet/1.0"
ALLOWED_ACTIONS = {"preserve", "set_default", "rewrite", "expand_sub_queries", "clarify"}
DEFAULT_LIMITS = {
    "max_selected_rules": 8,
    "max_dependency_depth": 4,
    "max_application_rounds": 8,
    "max_expanded_requirements": 20,
    "max_packet_bytes": 8192,
}


class QueryPolicyError(ValueError):
    def __init__(self, code: str, message: str, *, stage: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details or {}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def value_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise QueryPolicyError(
            "QP_RESOURCE_INVALID", f"cannot read Query Policy resource: {path.name}",
            stage="load", details={"resource": path.name},
        ) from exc
    if not isinstance(value, dict):
        raise QueryPolicyError(
            "QP_RESOURCE_INVALID", f"Query Policy resource must be an object: {path.name}",
            stage="load", details={"resource": path.name},
        )
    return value


def _require_strings(values: Any, path: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(values, list) or (not allow_empty and not values) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise QueryPolicyError("QP_SCHEMA_INVALID", f"{path} must be a string array", stage="validate")
    return list(values)


def _rule_dependencies(rule: dict[str, Any]) -> list[str]:
    relations = rule.get("relations") or {}
    if not isinstance(relations, dict):
        raise QueryPolicyError("QP_SCHEMA_INVALID", "rule relations must be an object", stage="validate")
    return _require_strings(relations.get("depends_on") or [], "relations.depends_on", allow_empty=True)


def validate_rule(rule: dict[str, Any]) -> None:
    if rule.get("schema_version") != POLICY_RULE_SCHEMA:
        raise QueryPolicyError("QP_SCHEMA_INVALID", "rule schema_version is invalid", stage="validate")
    rule_id = rule.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id:
        raise QueryPolicyError("QP_SCHEMA_INVALID", "rule_id is required", stage="validate")
    if not isinstance(rule.get("priority"), int):
        raise QueryPolicyError("QP_SCHEMA_INVALID", f"{rule_id}.priority must be an integer", stage="validate")
    if not isinstance(rule.get("routing"), dict) or not isinstance(rule.get("applicability"), dict):
        raise QueryPolicyError("QP_SCHEMA_INVALID", f"{rule_id} routing/applicability must be objects", stage="validate")
    actions = rule.get("actions")
    if not isinstance(actions, list) or not actions:
        raise QueryPolicyError("QP_SCHEMA_INVALID", f"{rule_id}.actions must be non-empty", stage="validate")
    seen_actions: set[str] = set()
    for action in actions:
        if not isinstance(action, dict):
            raise QueryPolicyError("QP_SCHEMA_INVALID", f"{rule_id} action must be an object", stage="validate")
        action_id = action.get("action_id")
        if not isinstance(action_id, str) or not action_id or action_id in seen_actions:
            raise QueryPolicyError(
                "QP_ACTION_ID_INVALID", f"{rule_id} action_id must be unique and non-empty", stage="validate"
            )
        seen_actions.add(action_id)
        if action.get("op") not in ALLOWED_ACTIONS:
            raise QueryPolicyError(
                "QP_ACTION_UNSUPPORTED", f"{rule_id}.{action_id} uses an unsupported action", stage="validate"
            )
        if action.get("idempotency") != "application_key":
            raise QueryPolicyError(
                "QP_ACTION_NOT_IDEMPOTENT",
                f"{rule_id}.{action_id} must declare idempotency=application_key",
                stage="validate",
            )
        effect = action.get("ir_effect_contract")
        if effect is None:
            continue
        if not isinstance(effect, dict) or set(effect) - {
            "collection", "scenario", "target_semantics", "required_shape",
        }:
            raise QueryPolicyError(
                "QP_EFFECT_CONTRACT_INVALID",
                f"{rule_id}.{action_id} ir_effect_contract is invalid",
                stage="validate",
            )
        scenario = effect.get("scenario")
        expected_semantics = SCENARIO_TARGET_SEMANTICS.get(str(scenario or ""))
        if (
            effect.get("collection") != "attribution_targets"
            or expected_semantics is None
            or effect.get("target_semantics") != expected_semantics
        ):
            raise QueryPolicyError(
                "QP_EFFECT_CONTRACT_INVALID",
                f"{rule_id}.{action_id} attribution protocol fields are invalid",
                stage="validate",
            )
        required_shape = effect.get("required_shape")
        if not isinstance(required_shape, dict) or set(required_shape) - {
            "decomposition", "factors", "formula",
        }:
            raise QueryPolicyError(
                "QP_EFFECT_CONTRACT_INVALID",
                f"{rule_id}.{action_id} required_shape is invalid",
                stage="validate",
            )
        if required_shape.get("decomposition") not in IR_DECOMPOSITIONS:
            raise QueryPolicyError(
                "QP_EFFECT_CONTRACT_INVALID",
                f"{rule_id}.{action_id} decomposition is invalid",
                stage="validate",
            )
        if (
            required_shape.get("factors") not in (None, "non_empty")
            or required_shape.get("formula") not in (None, "object")
        ):
            raise QueryPolicyError(
                "QP_EFFECT_CONTRACT_INVALID",
                f"{rule_id}.{action_id} formula shape is invalid",
                stage="validate",
            )
    _rule_dependencies(rule)


def _validate_dependency_graph(rules: dict[str, dict[str, Any]], max_depth: int) -> None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(rule_id: str, depth: int) -> None:
        if depth > max_depth:
            raise QueryPolicyError(
                "QP_DEPENDENCY_DEPTH_EXCEEDED", "Query Policy dependency depth exceeds the limit",
                stage="dependency", details={"rule_id": rule_id, "max_depth": max_depth},
            )
        if rule_id in visiting:
            cycle = visiting[visiting.index(rule_id):] + [rule_id]
            raise QueryPolicyError(
                "QP_DEPENDENCY_CYCLE", "Query Policy dependency cycle detected",
                stage="dependency", details={"cycle": cycle},
            )
        if rule_id in visited:
            return
        visiting.append(rule_id)
        for dependency in _rule_dependencies(rules[rule_id]):
            if dependency not in rules:
                raise QueryPolicyError(
                    "QP_DEPENDENCY_NOT_FOUND", "Query Policy dependency does not exist",
                    stage="dependency", details={"rule_id": rule_id, "dependency": dependency},
                )
            visit(dependency, depth + 1)
        visiting.pop()
        visited.add(rule_id)

    for current in rules:
        visit(current, 0)


def validate_policy(
    index: dict[str, Any], manifest: dict[str, Any], rules: dict[str, dict[str, Any]],
) -> dict[str, int]:
    if index.get("schema_version") != POLICY_INDEX_SCHEMA:
        raise QueryPolicyError("QP_SCHEMA_INVALID", "policy index schema is invalid", stage="validate")
    if manifest.get("schema_version") != POLICY_MANIFEST_SCHEMA:
        raise QueryPolicyError("QP_SCHEMA_INVALID", "policy manifest schema is invalid", stage="validate")
    policy_version = index.get("policy_version")
    if not isinstance(policy_version, str) or policy_version != manifest.get("policy_version"):
        raise QueryPolicyError("QP_VERSION_MISMATCH", "policy index and manifest versions differ", stage="validate")
    active_ids = _require_strings(index.get("active_rule_ids"), "active_rule_ids")
    if len(active_ids) != len(set(active_ids)) or set(active_ids) != set(rules):
        raise QueryPolicyError("QP_INDEX_INCONSISTENT", "active rule IDs do not match rule files", stage="validate")
    for rule_id, rule in rules.items():
        validate_rule(rule)
        if rule.get("rule_id") != rule_id or rule.get("policy_version") != policy_version:
            raise QueryPolicyError("QP_VERSION_MISMATCH", f"rule identity mismatch: {rule_id}", stage="validate")
    rule_hashes = manifest.get("rule_hashes")
    if not isinstance(rule_hashes, dict) or set(rule_hashes) != set(rules):
        raise QueryPolicyError("QP_MANIFEST_INCONSISTENT", "manifest rule hashes are incomplete", stage="validate")
    for rule_id, rule in rules.items():
        if rule_hashes.get(rule_id) != value_hash(rule):
            raise QueryPolicyError(
                "QP_RESOURCE_HASH_MISMATCH", f"rule hash mismatch: {rule_id}", stage="validate"
            )
    if manifest.get("index_sha256") != value_hash(index):
        raise QueryPolicyError("QP_RESOURCE_HASH_MISMATCH", "policy index hash mismatch", stage="validate")
    if manifest.get("policy_sha256") != value_hash({"index": index, "rules": rules}):
        raise QueryPolicyError("QP_RESOURCE_HASH_MISMATCH", "complete policy hash mismatch", stage="validate")
    limits = deepcopy(DEFAULT_LIMITS)
    declared_limits = index.get("limits") or {}
    if not isinstance(declared_limits, dict) or set(declared_limits) - set(DEFAULT_LIMITS):
        raise QueryPolicyError("QP_SCHEMA_INVALID", "policy limits are invalid", stage="validate")
    for key, default in DEFAULT_LIMITS.items():
        value = declared_limits.get(key, default)
        if not isinstance(value, int) or value <= 0:
            raise QueryPolicyError("QP_SCHEMA_INVALID", f"policy limit {key} is invalid", stage="validate")
        limits[key] = value
    _validate_dependency_graph(rules, limits["max_dependency_depth"])
    routing = index.get("routing")
    if not isinstance(routing, dict):
        raise QueryPolicyError("QP_SCHEMA_INVALID", "policy routing must be an object", stage="validate")
    routed_ids = {
        rule_id
        for values in routing.values()
        for rule_id in _require_strings(values, "routing rule IDs")
    }
    always_ids = set(_require_strings(index.get("always_rule_ids") or [], "always_rule_ids", allow_empty=True))
    if not (routed_ids | always_ids) <= set(rules):
        raise QueryPolicyError("QP_INDEX_INCONSISTENT", "routing references an unknown rule", stage="validate")
    return limits


def load_policy(root: Path = POLICY_ROOT) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, int]]:
    index = load_json(root / "policy-index.json")
    manifest = load_json(root / "policy-manifest.json")
    active_ids = index.get("active_rule_ids") if isinstance(index.get("active_rule_ids"), list) else []
    rules = {str(rule_id): load_json(root / "rules" / f"{rule_id}.json") for rule_id in active_ids}
    limits = validate_policy(index, manifest, rules)
    return index, manifest, rules, limits


def _selected_ids(query: str, index: dict[str, Any], rules: dict[str, dict[str, Any]]) -> list[str]:
    matched: set[str] = set()
    for route, rule_ids in (index.get("routing") or {}).items():
        terms = [term for term in str(route).split("|") if term]
        if any(term in query for term in terms):
            matched.update(str(rule_id) for rule_id in rule_ids)
    if not matched:
        return []
    matched.update(str(rule_id) for rule_id in index.get("always_rule_ids") or [])
    roots = sorted(matched, key=lambda rule_id: (-int(rules[rule_id]["priority"]), rule_id))
    ordered: list[str] = []
    added: set[str] = set()

    def add(rule_id: str) -> None:
        for dependency in _rule_dependencies(rules[rule_id]):
            add(dependency)
        if rule_id not in added:
            added.add(rule_id)
            ordered.append(rule_id)

    for rule_id in roots:
        add(rule_id)
    return ordered


def _runtime_rule(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(rule[key])
        for key in (
            "rule_id", "version", "priority", "routing", "applicability", "actions",
            "relations", "user_explicit_protection", "boundaries",
        )
        if key in rule
    }


def select_query_policy(
    query: str,
    *,
    root: Path = POLICY_ROOT,
    policy_data: tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    if not isinstance(query, str) or not query.strip():
        raise QueryPolicyError("QP_QUERY_INVALID", "raw Query must be non-empty", stage="selection")
    if policy_data is None:
        index, manifest, rules, limits = load_policy(root)
    else:
        index, manifest, rules = policy_data
        limits = validate_policy(index, manifest, rules)
    selected_ids = _selected_ids(query, index, rules)
    if len(selected_ids) > limits["max_selected_rules"]:
        raise QueryPolicyError(
            "QP_RULE_BUDGET_EXCEEDED", "selected rule count exceeds the runtime limit",
            stage="selection", details={"selected": len(selected_ids)},
        )
    packet = {
        "schema_version": POLICY_PACKET_SCHEMA,
        "status": "selected" if selected_ids else "no_match",
        "raw_query": query,
        "policy_version": index["policy_version"],
        "policy_hash": manifest["policy_sha256"],
        "limits": limits,
        "selected_rule_ids": selected_ids,
        "rules": [_runtime_rule(rules[rule_id]) for rule_id in selected_ids],
        "timing_ms": round((time.perf_counter() - started) * 1000, 3),
    }
    if len(canonical_json(packet).encode("utf-8")) > limits["max_packet_bytes"]:
        raise QueryPolicyError(
            "QP_PACKET_BUDGET_EXCEEDED", "Query Policy packet exceeds the context budget",
            stage="selection", details={"limit": limits["max_packet_bytes"]},
        )
    return packet


def select_query_policy_fail_open(
    query: str,
    *,
    root: Path = POLICY_ROOT,
    policy_data: tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        return select_query_policy(query, root=root, policy_data=policy_data)
    except Exception as exc:  # Query Policy is an optional enhancement.
        return {
            "schema_version": POLICY_PACKET_SCHEMA,
            "status": "fallback_raw",
            "raw_query": query if isinstance(query, str) else "",
            "selected_rule_ids": [],
            "rules": [],
            "failure": {
                "stage": str(getattr(exc, "stage", "selection")),
                "code": str(getattr(exc, "code", exc.__class__.__name__)),
            },
            "fallback": "raw_query",
            "timing_ms": round((time.perf_counter() - started) * 1000, 3),
        }
