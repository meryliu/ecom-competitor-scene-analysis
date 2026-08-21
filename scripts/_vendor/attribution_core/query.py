#!/usr/bin/env python3
"""Query attribution operator capability without running a calculation."""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .identity import CONTRACT_SCHEMA_VERSION, identity
from .registry import (
    REGISTRY_NAME,
    REGISTRY_VERSION,
    get_operator,
    operator_matches,
    route_operator,
)


def _period_role(path: str) -> Optional[str]:
    for suffix, role in (
        ("analysis_last_year_value", "analysis_last_year"),
        ("comparison_last_year_value", "comparison_last_year"),
        ("analysis_last_year_numerator", "analysis_last_year"),
        ("analysis_last_year_denominator", "analysis_last_year"),
        ("comparison_last_year_numerator", "comparison_last_year"),
        ("comparison_last_year_denominator", "comparison_last_year"),
        ("analysis_value", "analysis"),
        ("comparison_value", "comparison"),
        ("analysis_numerator", "analysis"),
        ("analysis_denominator", "analysis"),
        ("comparison_numerator", "comparison"),
        ("comparison_denominator", "comparison"),
    ):
        if path.endswith(suffix):
            return role
    return None


def _input_kind(path: str) -> str:
    if path.endswith(".name"):
        return "entity_label"
    if path.endswith(".sign") or path.endswith(".role"):
        return "operator_configuration"
    if path in {"metric_semantics", "parent_metric_semantics", "relation_to_parent", "ranking"}:
        return "semantic_configuration"
    return "fact_value"


def _subject(path: str) -> str:
    if path.startswith("metric."):
        return "overall_metric"
    if path.startswith("factors[]."):
        return "formula_factor"
    if path.startswith("groups[]."):
        return "dimension_group"
    return "operator_input"


def _component(path: str) -> Optional[str]:
    for component in ("numerator", "denominator"):
        if path.endswith(component):
            return component
    return None


def _fact_requirements(fields: Iterable[Dict[str, Any]], required: bool) -> list[Dict[str, Any]]:
    requirements = []
    for field in fields:
        path = str(field["path"])
        kind = _input_kind(path)
        if kind in {"operator_configuration", "semantic_configuration"}:
            continue
        requirement = {
            "path": path,
            "required": required,
            "fetch_policy": "required" if required else "recommended",
            "kind": kind,
            "subject": _subject(path),
            "period_role": _period_role(path),
            "component": _component(path),
            "description": field.get("description", ""),
            "dimension": "由用户 Query 或事实节点确定" if path.startswith(("groups[]", "factors[]")) else None,
            "value_type": "label" if kind == "entity_label" else "absolute_value",
        }
        requirements.append(requirement)
    return requirements


def _configuration_requirements(fields: Iterable[Dict[str, Any]], required: bool) -> list[Dict[str, Any]]:
    requirements = []
    for field in fields:
        path = str(field["path"])
        kind = _input_kind(path)
        if kind not in {"operator_configuration", "semantic_configuration"}:
            continue
        requirements.append({
            "path": path,
            "required": required,
            "fetch_policy": "required" if required else "optional",
            "kind": kind,
            "description": field.get("description", ""),
        })
    return requirements


def load_request(args: argparse.Namespace) -> Dict[str, Any]:
    request: Dict[str, Any] = {}
    if args.input:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("input JSON must be an object")
    for key in ("operator", "scenario", "metric_object", "decomposition", "formula", "dimension"):
        value = getattr(args, key)
        if value is not None:
            request[key] = value
    return request


def query_operator(request: Dict[str, Any]) -> Dict[str, Any]:
    operator = request.get("operator")
    scenario = request.get("scenario")
    metric_object = request.get("metric_object")
    decomposition = request.get("decomposition")
    if operator:
        definition = get_operator(str(operator))
        if not definition:
            return unsupported_response(request, f"unknown operator: {operator}")
        if not operator_matches(str(operator), scenario, metric_object, decomposition):
            return unsupported_response(request, f"operator {operator} does not match the supplied route fields")
    else:
        missing = [name for name, value in (("scenario", scenario), ("metric_object", metric_object), ("decomposition", decomposition)) if not value]
        if missing:
            raise ValueError(f"missing required query fields: {', '.join(missing)}")
        try:
            operator = route_operator(str(scenario), str(metric_object), str(decomposition))
        except ValueError as exc:
            return unsupported_response(request, str(exc))
        definition = get_operator(operator)

    response = {
        "ok": True,
        "operation": "query_operator",
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "engine_identity": identity(),
        "contract_version_status": "reported_by_query_operator",
        "registry": {"name": REGISTRY_NAME, "version": REGISTRY_VERSION},
        "contract_source": REGISTRY_NAME,
        "request": deepcopy(request),
        "supported": True,
        "operator": operator,
        "scenario": definition["scenario"],
        "metric_objects": definition["metric_objects"],
        "decompositions": definition["decompositions"],
        "supported_target_semantics": definition["supported_target_semantics"],
        "description": definition["description"],
        "required_inputs": definition["required_inputs"],
        "optional_inputs": definition["optional_inputs"],
        "constraints": definition["constraints"],
        "outputs": definition["outputs"],
        "execution_protocol": definition["execution_protocol"],
    }
    response["fact_requirements"] = _fact_requirements(definition["required_inputs"], required=True)
    response["recommended_fact_requirements"] = _fact_requirements(
        definition["optional_inputs"], required=False
    )
    response["configuration_requirements"] = _configuration_requirements(
        definition["required_inputs"], required=True
    ) + _configuration_requirements(definition["optional_inputs"], required=False)
    if request.get("formula"):
        response["formula"] = request["formula"]
    if request.get("dimension"):
        response["dimension"] = request["dimension"]
    response["match_reason"] = definition["description"]
    return response


def unsupported_response(request: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "ok": True,
        "operation": "query_operator",
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "engine_identity": identity(),
        "contract_version_status": "reported_by_query_operator",
        "registry": {"name": REGISTRY_NAME, "version": REGISTRY_VERSION},
        "contract_source": REGISTRY_NAME,
        "request": deepcopy(request),
        "supported": False,
        "reason": reason,
        "required_inputs": [],
        "constraints": [],
        "outputs": [],
        "fact_requirements": [],
        "recommended_fact_requirements": [],
        "configuration_requirements": [],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Query attribution operator capability without executing it")
    parser.add_argument("--input", help="JSON query file; use '-' to read JSON from stdin")
    parser.add_argument("--operator", help="explicit operator id")
    parser.add_argument("--scenario", choices=["metric_change", "yoy_trend_change"])
    parser.add_argument("--metric-object", dest="metric_object", choices=["volume", "ratio"])
    parser.add_argument("--decomposition", choices=["addition", "subtraction", "multiplication", "division", "dimension", "structure"])
    parser.add_argument("--formula", help="user-provided formula, retained as query context")
    parser.add_argument("--dimension", help="user-requested dimension, retained as query context")
    parser.add_argument("--output", help="output JSON file; stdout if omitted")
    args = parser.parse_args(argv)
    try:
        result = query_operator(load_request(args))
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI returns a stable JSON error.
        text = json.dumps({"ok": False, "operation": "query_operator", "error": str(exc), "error_type": exc.__class__.__name__}, ensure_ascii=False, indent=2)
        print(text, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
