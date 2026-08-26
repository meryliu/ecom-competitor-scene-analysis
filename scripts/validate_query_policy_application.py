#!/usr/bin/env python3
"""Validate a model-applied Query Policy transaction before committing its IR."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from business_parameter_preflight import preflight_business_parameters
from ir_contract_guard import validate_analysis_input_contract
from query_policy_runtime import POLICY_PACKET_SCHEMA, load_json


DECISION_SCHEMA = "query_policy_decision/1.0"
VALIDATION_SCHEMA = "query_policy_application_validation/1.0"
REQUIREMENT_ARRAYS = (
    "fact_observations", "metric_compositions", "derived_requirements",
    "custom_calculations", "attribution_targets", "output_requirements",
)


class QueryPolicyApplicationError(ValueError):
    def __init__(self, code: str, message: str, *, stage: str = "application"):
        super().__init__(message)
        self.code = code
        self.stage = stage


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


def _action_catalog(packet: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(rule.get("rule_id")), str(action.get("action_id")))
        for rule in packet.get("rules") or []
        if isinstance(rule, dict)
        for action in rule.get("actions") or []
        if isinstance(action, dict)
    }


def _validate_applied_actions(packet: dict[str, Any], decision: dict[str, Any]) -> None:
    catalog = _action_catalog(packet)
    seen: set[tuple[str, str, str, str]] = set()
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


def validate_application(
    packet: dict[str, Any], decision: dict[str, Any], candidate_ir: dict[str, Any] | None,
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
    _validate_applied_actions(packet, decision)
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
    preflight = preflight_business_parameters(candidate_ir)
    if preflight.get("status") == "waiting_confirmation":
        return {
            "schema_version": VALIDATION_SCHEMA,
            "status": "commit_pending_confirmation",
            "policy_version": packet["policy_version"],
            "business_parameter_cases": len(preflight.get("resolution_cases") or []),
            "fallback": None,
        }
    validate_analysis_input_contract(preflight["analysis_ir"])
    return {
        "schema_version": VALIDATION_SCHEMA,
        "status": "commit",
        "policy_version": packet["policy_version"],
        "fallback": None,
    }


def validate_application_fail_open(
    packet: dict[str, Any], decision: dict[str, Any], candidate_ir: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        return validate_application(packet, decision, candidate_ir)
    except Exception as exc:
        return {
            "schema_version": VALIDATION_SCHEMA,
            "status": "fallback_raw",
            "failure": {
                "stage": str(getattr(exc, "stage", "application")),
                "code": str(getattr(exc, "code", exc.__class__.__name__)),
            },
            "fallback": "raw_query",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--ir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        packet = load_json(args.packet)
        decision = load_json(args.decision)
        candidate_ir = load_json(args.ir) if args.ir else None
        result = validate_application_fail_open(packet, decision, candidate_ir)
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
