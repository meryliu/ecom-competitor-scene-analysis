#!/usr/bin/env python3
"""Validate a model-applied Query Policy transaction before committing its IR."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

from analysis_ir_normalizer import (
    AnalysisIRNormalizationError,
    normalize_analysis_input,
    validate_period_values,
)
from business_parameter_preflight import preflight_business_parameters
from ir_contract_guard import SCENARIO_TARGET_SEMANTICS, validate_analysis_input_contract
from query_policy_runtime import POLICY_PACKET_SCHEMA, load_json


DECISION_SCHEMA = "query_policy_decision/1.0"
VALIDATION_SCHEMA = "query_policy_application_validation/1.0"
REQUIREMENT_ARRAYS = (
    "fact_observations", "metric_compositions", "derived_requirements",
    "custom_calculations", "attribution_targets", "output_requirements",
)


class QueryPolicyApplicationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "application",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details or {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _action_catalog(packet: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (str(rule.get("rule_id")), str(action.get("action_id"))): action
        for rule in packet.get("rules") or []
        if isinstance(rule, dict)
        for action in rule.get("actions") or []
        if isinstance(action, dict)
    }


def _validate_applied_actions(
    packet: dict[str, Any], decision: dict[str, Any]
) -> list[dict[str, Any]]:
    catalog = _action_catalog(packet)
    seen: set[tuple[str, str, str, str]] = set()
    effect_bindings: list[dict[str, Any]] = []
    actions = decision.get("applied_actions")
    if not isinstance(actions, list):
        raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", "applied_actions must be an array")
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", f"applied_actions[{index}] must be an object")
        rule_id = str(action.get("rule_id") or "")
        action_id = str(action.get("action_id") or "")
        scope = str(action.get("target_scope_fingerprint") or "")
        task_id = str(action.get("task_id") or "default")
        if (rule_id, action_id) not in catalog or not scope:
            raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", "applied action is not in the selected packet")
        key = (task_id, rule_id, action_id, scope)
        if key in seen:
            raise QueryPolicyApplicationError("QP_ACTION_DUPLICATED", "an action was committed twice for one semantic scope")
        seen.add(key)
        effect = catalog[(rule_id, action_id)].get("ir_effect_contract")
        if not isinstance(effect, dict):
            continue
        produced_refs = action.get("produced_refs")
        if not isinstance(produced_refs, list) or not produced_refs:
            raise QueryPolicyApplicationError(
                "QP_EFFECT_BINDING_MISSING",
                "an applied action with an IR effect contract requires produced_refs",
                details={"rule_id": rule_id, "action_id": action_id},
            )
        normalized_refs: list[dict[str, str]] = []
        seen_refs: set[tuple[str, str]] = set()
        scoped_roles = {
            str(item.get("role")): item
            for item in effect.get("roles") or []
            if isinstance(item, dict) and item.get("role")
        } if effect.get("contract_type") == "scoped_expansion" else {}
        seen_roles: set[str] = set()
        for ref_index, ref in enumerate(produced_refs):
            expected_keys = (
                {"role", "collection", "id"}
                if scoped_roles else {"collection", "id"}
            )
            role = str(ref.get("role") or "") if isinstance(ref, dict) else ""
            expected_collection = (
                (scoped_roles.get(role) or {}).get("collection")
                if scoped_roles else effect.get("collection")
            )
            if (
                not isinstance(ref, dict)
                or set(ref) != expected_keys
                or (scoped_roles and role not in scoped_roles)
                or ref.get("collection") != expected_collection
                or not isinstance(ref.get("id"), str)
                or not ref.get("id")
            ):
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_BINDING_INVALID",
                    "produced_refs must bind the contracted collection to a non-empty ID",
                    details={
                        "rule_id": rule_id,
                        "action_id": action_id,
                        "ref_index": ref_index,
                    },
                )
            identity = (str(ref["collection"]), str(ref["id"]))
            if identity in seen_refs:
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_BINDING_INVALID",
                    "produced_refs contains a duplicate binding",
                    details={"collection": identity[0], "id": identity[1]},
                )
            seen_refs.add(identity)
            if scoped_roles:
                if role in seen_roles:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_BINDING_INVALID",
                        "produced_refs contains a duplicate scoped expansion role",
                        details={"role": role},
                    )
                seen_roles.add(role)
                normalized_refs.append({
                    "role": role, "collection": identity[0], "id": identity[1],
                })
            else:
                normalized_refs.append({"collection": identity[0], "id": identity[1]})
        if scoped_roles and seen_roles != set(scoped_roles):
            raise QueryPolicyApplicationError(
                "QP_EFFECT_BINDING_MISSING",
                "produced_refs must bind every scoped expansion role exactly once",
                details={
                    "expected_roles": sorted(scoped_roles),
                    "actual_roles": sorted(seen_roles),
                },
            )
        effect_bindings.append({
            "rule_id": rule_id,
            "action_id": action_id,
            "contract": effect,
            "produced_refs": normalized_refs,
        })
    return effect_bindings


def _canonicalize_policy_effects(
    candidate_ir: dict[str, Any], effect_bindings: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    canonical = deepcopy(candidate_ir)
    changes: list[dict[str, Any]] = []
    owned_refs: set[tuple[str, str]] = set()
    valid_semantics = set(SCENARIO_TARGET_SEMANTICS.values())
    for binding in effect_bindings:
        contract = binding["contract"]
        if contract.get("contract_type") == "scoped_expansion":
            roles = {
                str(item["role"]): item
                for item in contract.get("roles") or []
                if isinstance(item, dict) and item.get("role")
            }
            metric_names = {
                str(item.get("metric_id")): str(item.get("name") or "")
                for item in ((canonical.get("analysis_task") or {}).get("metrics") or [])
                if isinstance(item, dict) and item.get("metric_id")
            }
            for ref in binding["produced_refs"]:
                role_contract = roles[str(ref["role"])]
                collection = str(ref["collection"])
                identity = (collection, str(ref["id"]))
                if identity in owned_refs:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_BINDING_INVALID",
                        "an IR target is owned by more than one applied action",
                        details={"collection": collection, "id": identity[1]},
                    )
                owned_refs.add(identity)
                items = canonical.get(collection)
                if not isinstance(items, list):
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_TARGET_MISSING",
                        "the contracted IR collection is missing",
                        details={"collection": collection},
                    )
                matches = [
                    (index, item)
                    for index, item in enumerate(items)
                    if isinstance(item, dict)
                    and item.get("requirement_id") == identity[1]
                ]
                if len(matches) != 1:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_TARGET_MISSING",
                        "the applied action does not bind exactly one IR target",
                        details={
                            "collection": collection, "id": identity[1],
                            "matches": len(matches),
                        },
                    )
                index, target = matches[0]
                path = f"{collection}[{index}]"
                actual_metric_name = metric_names.get(str(target.get("metric_ref") or ""))
                expected_metric_name = str(role_contract["metric_name"])
                if actual_metric_name != expected_metric_name:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_CONTRACT_CONFLICT",
                        "the scoped expansion role is bound to the wrong metric",
                        details={
                            "path": f"{path}.metric_ref", "role": ref["role"],
                            "actual_metric_name": actual_metric_name,
                            "expected_metric_name": expected_metric_name,
                        },
                    )
                constraints = target.get("metric_constraints") or []
                if not isinstance(constraints, list) or not all(
                    isinstance(item, dict) for item in constraints
                ):
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_CONTRACT_CONFLICT",
                        "metric_constraints must be an array of objects",
                        details={"path": f"{path}.metric_constraints"},
                    )
                for scope_effect in role_contract.get("metric_constraint_effects") or []:
                    dimension_hint = str(scope_effect["dimension_hint"])
                    before = deepcopy(constraints)
                    constraints = [
                        item for item in constraints
                        if str(item.get("dimension_hint") or "") != dimension_hint
                    ]
                    if scope_effect["state"] == "required":
                        canonical_constraint = {
                            "kind": "dimension_filter",
                            "operator": scope_effect["operator"],
                            "values": deepcopy(scope_effect["values"]),
                            "dimension_hint": dimension_hint,
                            "provenance": scope_effect.get("provenance") or "registered_definition",
                        }
                        constraints.append(canonical_constraint)
                    if constraints != before:
                        changes.append({
                            "path": f"{path}.metric_constraints",
                            "dimension_hint": dimension_hint,
                            "from": before,
                            "to": deepcopy(constraints),
                            "rule_id": binding["rule_id"],
                            "action_id": binding["action_id"],
                            "role": ref["role"],
                        })
                if constraints:
                    target["metric_constraints"] = constraints
                else:
                    target.pop("metric_constraints", None)
            continue
        collection = str(contract["collection"])
        items = canonical.get(collection)
        if not isinstance(items, list):
            raise QueryPolicyApplicationError(
                "QP_EFFECT_TARGET_MISSING",
                "the contracted IR collection is missing",
                details={"collection": collection},
            )
        id_field = "target_id" if collection == "attribution_targets" else "requirement_id"
        for ref in binding["produced_refs"]:
            identity = (collection, str(ref["id"]))
            if identity in owned_refs:
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_BINDING_INVALID",
                    "an IR target is owned by more than one applied action",
                    details={"collection": collection, "id": identity[1]},
                )
            owned_refs.add(identity)
            matches = [
                (index, item)
                for index, item in enumerate(items)
                if isinstance(item, dict) and item.get(id_field) == identity[1]
            ]
            if len(matches) != 1:
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_TARGET_MISSING",
                    "the applied action does not bind exactly one IR target",
                    details={"collection": collection, "id": identity[1], "matches": len(matches)},
                )
            index, target = matches[0]
            path = f"{collection}[{index}]"
            expected_scenario = contract["scenario"]
            actual_scenario = target.get("scenario")
            if actual_scenario != expected_scenario:
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_CONTRACT_CONFLICT",
                    "the policy-owned target scenario conflicts with its action contract",
                    details={
                        "path": f"{path}.scenario",
                        "actual": actual_scenario,
                        "expected": expected_scenario,
                    },
                )
            expected_semantics = contract["target_semantics"]
            actual_semantics = target.get("target_semantics")
            if actual_semantics != expected_semantics:
                if not isinstance(actual_semantics, str) and actual_semantics is not None:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_CONTRACT_CONFLICT",
                        "the policy-owned target has a non-string target_semantics",
                        details={
                            "path": f"{path}.target_semantics",
                            "actual_type": type(actual_semantics).__name__,
                            "expected": expected_semantics,
                        },
                    )
                if actual_semantics in valid_semantics:
                    raise QueryPolicyApplicationError(
                        "QP_EFFECT_CONTRACT_CONFLICT",
                        "the policy-owned target uses a conflicting canonical target_semantics",
                        details={
                            "path": f"{path}.target_semantics",
                            "actual": actual_semantics,
                            "expected": expected_semantics,
                        },
                    )
                target["target_semantics"] = expected_semantics
                changes.append({
                    "path": f"{path}.target_semantics",
                    "from": actual_semantics,
                    "to": expected_semantics,
                    "rule_id": binding["rule_id"],
                    "action_id": binding["action_id"],
                })
            required_shape = contract.get("required_shape") or {}
            expected_decomposition = required_shape.get("decomposition")
            if target.get("decomposition") != expected_decomposition:
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_CONTRACT_CONFLICT",
                    "the policy-owned target decomposition conflicts with its action contract",
                    details={
                        "path": f"{path}.decomposition",
                        "actual": target.get("decomposition"),
                        "expected": expected_decomposition,
                    },
                )
            if required_shape.get("factors") == "non_empty" and not (
                isinstance(target.get("factors"), list) and target["factors"]
            ):
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_CONTRACT_CONFLICT",
                    "the policy-owned target requires non-empty factors",
                    details={"path": f"{path}.factors", "expected": "non_empty"},
                )
            if required_shape.get("formula") == "object" and not isinstance(target.get("formula"), dict):
                raise QueryPolicyApplicationError(
                    "QP_EFFECT_CONTRACT_CONFLICT",
                    "the policy-owned target requires a formula object",
                    details={"path": f"{path}.formula", "expected": "object"},
                )
    return canonical, changes


def _commit_ir(
    path: Path | None, ir: dict[str, Any], result: dict[str, Any]
) -> None:
    if path is None:
        return
    _write_json(path, ir)
    encoded = json.dumps(ir, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    result["committed_ir_path"] = str(path)
    result["committed_ir_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_application(
    packet: dict[str, Any], decision: dict[str, Any], candidate_ir: dict[str, Any] | None,
    *, committed_ir_output: Path | None = None,
) -> dict[str, Any]:
    if packet.get("schema_version") != POLICY_PACKET_SCHEMA or packet.get("status") != "selected":
        raise QueryPolicyApplicationError("QP_PACKET_INVALID", "application requires a selected policy packet", stage="selection")
    if decision.get("schema_version") != DECISION_SCHEMA:
        raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", "decision schema is invalid")
    raw_query = packet.get("raw_query")
    if decision.get("raw_query") != raw_query or decision.get("policy_version") != packet.get("policy_version"):
        raise QueryPolicyApplicationError("QP_APPLICATION_CONTEXT_MISMATCH", "decision does not match the selected Query context")
    rounds = decision.get("application_rounds", 1)
    limit = int((packet.get("limits") or {}).get("max_application_rounds", 8))
    if not isinstance(rounds, int) or rounds < 1 or rounds > limit:
        raise QueryPolicyApplicationError("QP_APPLICATION_BUDGET_EXCEEDED", "application rounds exceed the policy limit")
    effect_bindings = _validate_applied_actions(packet, decision)
    status = decision.get("status")
    if status == "needs_clarification":
        clarifications = decision.get("clarifications")
        if not isinstance(clarifications, list) or not clarifications:
            raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", "business clarification requires questions")
        return {
            "schema_version": VALIDATION_SCHEMA,
            "status": "commit_clarification",
            "policy_version": packet["policy_version"],
            "fallback": None,
        }
    if status != "applied" or not isinstance(candidate_ir, dict):
        raise QueryPolicyApplicationError("QP_APPLICATION_INVALID", "applied decision requires a candidate IR")
    task = candidate_ir.get("analysis_task")
    if not isinstance(task, dict) or task.get("query") != raw_query:
        raise QueryPolicyApplicationError("QP_RAW_QUERY_MUTATED", "analysis_task.query must preserve the raw Query")
    requirement_count = sum(
        len(candidate_ir.get(key) or [])
        for key in REQUIREMENT_ARRAYS
        if isinstance(candidate_ir.get(key) or [], list)
    )
    requirement_limit = int((packet.get("limits") or {}).get("max_expanded_requirements", 20))
    if requirement_count > requirement_limit:
        raise QueryPolicyApplicationError("QP_REQUIREMENT_BUDGET_EXCEEDED", "enhanced requirements exceed the policy limit")
    canonical_ir, changes = _canonicalize_policy_effects(candidate_ir, effect_bindings)
    try:
        validate_period_values(canonical_ir)
    except AnalysisIRNormalizationError as exc:
        raise QueryPolicyApplicationError(
            exc.code, str(exc), details=exc.details
        ) from exc
    preflight = preflight_business_parameters(canonical_ir)
    if preflight.get("status") == "waiting_confirmation":
        result = {
            "schema_version": VALIDATION_SCHEMA,
            "status": "commit_pending_confirmation",
            "policy_version": packet["policy_version"],
            "business_parameter_cases": len(preflight.get("resolution_cases") or []),
            "canonicalization": {"change_count": len(changes), "changes": changes},
            "fallback": None,
        }
        _commit_ir(committed_ir_output, preflight["analysis_ir"], result)
        return result
    normalized_ir = normalize_analysis_input(preflight["analysis_ir"])
    validate_analysis_input_contract(normalized_ir)
    result = {
        "schema_version": VALIDATION_SCHEMA,
        "status": "commit",
        "policy_version": packet["policy_version"],
        "canonicalization": {"change_count": len(changes), "changes": changes},
        "fallback": None,
    }
    _commit_ir(committed_ir_output, normalized_ir, result)
    return result


def validate_application_fail_open(
    packet: dict[str, Any], decision: dict[str, Any], candidate_ir: dict[str, Any] | None,
    *, committed_ir_output: Path | None = None,
) -> dict[str, Any]:
    try:
        return validate_application(
            packet, decision, candidate_ir, committed_ir_output=committed_ir_output
        )
    except Exception as exc:
        failure = {
            "stage": str(getattr(exc, "stage", "application")),
            "code": str(getattr(exc, "code", exc.__class__.__name__)),
        }
        details = getattr(exc, "details", None)
        if isinstance(details, dict) and details:
            failure["details"] = deepcopy(details)
        return {
            "schema_version": VALIDATION_SCHEMA,
            "status": "fallback_raw",
            "failure": failure,
            "fallback": "raw_query",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--ir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--committed-ir-output", type=Path)
    args = parser.parse_args()
    try:
        packet = load_json(args.packet)
        decision = load_json(args.decision)
        candidate_ir = load_json(args.ir) if args.ir else None
        committed_ir_output = args.committed_ir_output or args.output.with_name("committed-ir.json")
        result = validate_application_fail_open(
            packet,
            decision,
            candidate_ir,
            committed_ir_output=committed_ir_output,
        )
    except Exception as exc:
        result = {
            "schema_version": VALIDATION_SCHEMA,
            "status": "fallback_raw",
            "failure": {"stage": "load", "code": str(getattr(exc, "code", exc.__class__.__name__))},
            "fallback": "raw_query",
        }
    _write_json(args.output, result)
    print(json.dumps({"status": result["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
