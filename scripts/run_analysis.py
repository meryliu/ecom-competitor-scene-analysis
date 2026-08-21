#!/usr/bin/env python3
"""Compile, fetch once, resume safely, and execute one or more competitor analyses."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _vendor.ecom_competitor_source import error_json
from compile_plan import compile_and_validate, load_json
from data_gateway import DataGateway, SOURCE_BINDING_V1, build_resolve_request, load_source_config
from dimension_domain_registry import (
    DEFAULT_REGISTRY_PATH as DEFAULT_DIMENSION_SET_REGISTRY,
)
from execution_runner import execute_plan
from fact_contract import SCENE_FACTS_V2, merge_fetch_requests, project_scene_facts
from prepare_analysis import (
    PreparationError,
    normalize_analysis_input,
    prepare_analysis_ir,
)
from providers.feishu_competitor import FeishuCompetitorGateway
from resolution_policy import DEFAULT_POLICY_PATH as DEFAULT_RESOLUTION_POLICY
from source_capability import project_task_capabilities
from run_fast_query import answer_payload, finalize_model_nodes
from run_state import (
    artifact_record,
    atomic_write_json,
    load_state,
    new_state,
    reusable_fetch,
    value_hash,
)
from validate_execution import Validator


class InputProtocolError(ValueError):
    code = "INPUT_PROTOCOL_INVALID"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not safe:
        raise ValueError("task_id must contain a usable character")
    return safe


def normalize_input(value: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    if "ir_version" in value and "schema_version" in value:
        raise InputProtocolError(
            "input cannot declare both ir_version and schema_version",
            {"received_fields": ["ir_version", "schema_version"]},
        )
    if value.get("ir_version") == "analysis_ir/1.0":
        return [("default", value)]
    if "analysis_task" in value and value.get("schema_version") is not None:
        raise InputProtocolError(
            "single analysis input must use ir_version='analysis_ir/1.0', not schema_version",
            {
                "received_schema_version": value.get("schema_version"),
                "expected_field": "ir_version",
                "expected_value": "analysis_ir/1.0",
            },
        )
    if "tasks" in value and value.get("ir_version") is not None:
        raise InputProtocolError(
            "analysis bundle must use schema_version='analysis_bundle/1.0', not ir_version",
            {
                "received_ir_version": value.get("ir_version"),
                "expected_field": "schema_version",
                "expected_value": "analysis_bundle/1.0",
            },
        )
    if value.get("schema_version") != "analysis_bundle/1.0" or not isinstance(value.get("tasks"), list):
        raise InputProtocolError(
            "input must be analysis_ir/1.0 or analysis_bundle/1.0",
            {
                "received_ir_version": value.get("ir_version"),
                "received_schema_version": value.get("schema_version"),
            },
        )
    tasks: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    seen_paths: set[str] = set()
    for index, item in enumerate(value["tasks"]):
        if not isinstance(item, dict) or not isinstance(item.get("analysis_ir"), dict):
            raise InputProtocolError(f"tasks[{index}] must contain analysis_ir")
        if (
            item["analysis_ir"].get("ir_version") != "analysis_ir/1.0"
            or "schema_version" in item["analysis_ir"]
        ):
            raise InputProtocolError(
                f"tasks[{index}].analysis_ir must declare ir_version='analysis_ir/1.0'",
                {"task_index": index, "received": item["analysis_ir"].get("ir_version")},
            )
        task_id = str(item.get("task_id") or f"task_{index + 1}")
        if task_id in seen:
            raise InputProtocolError(f"duplicate task_id: {task_id}")
        task_path = _task_name(task_id)
        if task_path in seen_paths:
            raise InputProtocolError(f"task_id path collision after normalization: {task_id}")
        seen.add(task_id)
        seen_paths.add(task_path)
        tasks.append((task_id, item["analysis_ir"]))
    if not tasks:
        raise InputProtocolError("analysis bundle must contain at least one task")
    return tasks


def task_error_answer(task_id: str, stage: str, exc: Exception) -> dict[str, Any]:
    code = getattr(exc, "code", exc.__class__.__name__)
    error: dict[str, Any] = {
        "stage": stage,
        "code": str(code),
        "message": str(exc),
    }
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details:
        error["details"] = deepcopy(details)
    return {
        "schema_version": "analysis_task_error/1.0",
        "status": "blocked",
        "task_id": task_id,
        "error": error,
        "views": [],
        "derived_results": [],
        "attribution_results": [],
    }


def task_resolution_answer(
    task_id: str,
    cases: list[dict[str, Any]],
    *,
    status: str,
) -> dict[str, Any]:
    """Return only the bounded logical packet needed to resume a task."""
    return {
        "schema_version": "analysis_task_resolution/1.0",
        "status": status,
        "task_id": task_id,
        "resolution_cases": deepcopy(cases),
        "views": [],
        "derived_results": [],
        "attribution_results": [],
    }


def compact_task_answer(answer: dict[str, Any]) -> dict[str, Any]:
    """Keep the bundle answer business-facing; task artifacts retain diagnostics."""
    if answer.get("schema_version") in {
        "analysis_task_error/1.0",
        "analysis_task_resolution/1.0",
    }:
        return deepcopy(answer)
    compact = {
        key: deepcopy(answer[key])
        for key in (
            "schema_version",
            "status",
            "execution_profile",
            "task_id",
            "source_revision",
            "quality",
            "scope_and_assumptions",
        )
        if key in answer
    }
    compact["views"] = []
    for view in answer.get("views") or []:
        rows = [
            deepcopy(row) for row in view.get("rows") or []
            if not str(row.get("period_role") or "").startswith("__auto_")
        ]
        if rows:
            compact["views"].append({"view_id": view.get("view_id"), "rows": rows})
    compact["derived_results"] = []
    for result in answer.get("derived_results") or []:
        compact["derived_results"].append({
            key: deepcopy(result[key])
            for key in (
                "derived_metric_id",
                "node_id",
                "status",
                "unit",
                "value",
                "definition_source",
                "definition_version",
                "definition_status",
                "metric",
                "metric_object",
                "view_id",
                "period_roles",
            )
            if key in result
        })
    compact["attribution_results"] = []
    for item in answer.get("attribution_results") or []:
        copied = {
            key: deepcopy(item[key])
            for key in ("result_id", "node_id", "status", "warnings")
            if key in item
        }
        result = deepcopy(item.get("result") or {})
        for internal in ("engine_identity", "registry", "routing"):
            result.pop(internal, None)
        copied["result"] = result
        compact["attribution_results"].append(copied)
    completion = answer.get("model_completion") or {}
    compact["model_completion"] = {
        key: deepcopy(completion[key])
        for key in ("required", "incomplete_node_ids")
        if key in completion
    }
    return compact


def _append_attempt(state: dict[str, Any], record: dict[str, Any]) -> None:
    attempts = state.setdefault("fetch_attempts", [])
    if any(item.get("attempt_id") == record.get("attempt_id") for item in attempts):
        raise ValueError(f"duplicate fetch attempt: {record.get('attempt_id')}")
    previous = next(
        (
            item for item in reversed(attempts)
            if item.get("request_id") == record.get("request_id")
        ),
        None,
    )
    if previous is not None and not record.get("retry_of") and not record.get("refresh_of"):
        lineage = "refresh_of" if previous.get("status") == "success" else "retry_of"
        record[lineage] = previous.get("attempt_id")
    attempts.append(record)


def _fetch(
    request: dict[str, Any],
    *,
    response_file: Path | None,
    gateway: DataGateway | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = utc_now()
    started = time.perf_counter()
    attempt_id = f"attempt_{time.time_ns()}"
    try:
        payload = load_json(response_file) if response_file else gateway.fetch(request)  # type: ignore[union-attr]
        if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
            raise ValueError("Provider response has no facts array")
        record = {
            "attempt_id": attempt_id,
            "request_id": request.get("request_id"),
            "status": "success",
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "response_format": payload.get("schema_version"),
            "source": deepcopy(payload.get("source") or {}),
            "replayed": response_file is not None,
        }
        return payload, record
    except Exception as exc:
        record = {
            "attempt_id": attempt_id,
            "request_id": request.get("request_id"),
            "status": "failed",
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": json.loads(error_json(exc)),
        }
        raise RuntimeError(json.dumps(record, ensure_ascii=False)) from exc


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--resume", choices=("auto", "never"), default="auto")
    parser.add_argument("--fresh", action="store_true", help="bypass a successful facts checkpoint")
    parser.add_argument("--index", type=Path, help="optional shared-index override")
    parser.add_argument("--response-file", type=Path, help="offline Provider payload for tests/replay")
    parser.add_argument("--source-url", help="override source-config URL for this run")
    parser.add_argument("--identity", default="user", choices=("user", "bot"))
    parser.add_argument("--allow-stale", action="store_true", default=None)
    parser.add_argument(
        "--source-config",
        type=Path,
        default=root / "references" / "data-sources" / "competitor-macro" / "source-config.json",
    )
    parser.add_argument("--derived-registry", type=Path, default=root / "references" / "derived-metric-registry.json")
    parser.add_argument("--composition-registry", type=Path, default=root / "references" / "metric-composition-registry.json")
    parser.add_argument("--dimension-set-registry", type=Path, default=DEFAULT_DIMENSION_SET_REGISTRY)
    parser.add_argument("--resolution-policy", type=Path, default=DEFAULT_RESOLUTION_POLICY)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workflow_started = time.perf_counter()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.work_dir / "run-state.json"
    state: dict[str, Any] | None = None
    try:
        raw_input = load_json(args.input)
        input_digest = value_hash(raw_input)
        existing = load_state(state_path)
        checkpoint_state = existing
        if existing and existing.get("input_hash") != input_digest:
            archive = args.work_dir / f"run-state.{existing.get('input_hash', 'unknown')[:12]}.json"
            if not archive.exists():
                atomic_write_json(archive, existing)
            existing = None
        state = existing or new_state(input_digest)
        normalize_input(raw_input)
        input_value = normalize_analysis_input(raw_input)
        tasks = normalize_input(input_value)
        task_order = [task_id for task_id, _ in tasks]
        state["stages"]["input"] = {"status": "success", "task_count": len(tasks)}

        derived_registry = load_json(args.derived_registry)
        composition_registry = load_json(args.composition_registry)
        capabilities: dict[str, Any] | None = None
        gateway: DataGateway | None = None
        source_binding: dict[str, Any]
        if args.response_file is None:
            prepare_started = time.perf_counter()
            source_config = load_source_config(args.source_config, source_url=args.source_url)
            gateway = FeishuCompetitorGateway(
                source_config,
                identity=args.identity,
                index_path=args.index,
                allow_stale=args.allow_stale,
                dimension_set_registry_path=args.dimension_set_registry,
                resolution_policy_path=args.resolution_policy,
            )
            capabilities = gateway.resolve(build_resolve_request(tasks, composition_registry))
            source_binding = deepcopy(capabilities["source"])
            capabilities_path = args.work_dir / "resolved-capabilities.json"
            atomic_write_json(capabilities_path, capabilities)
            state["artifacts"]["resolved_capabilities"] = artifact_record(capabilities_path)
            state["stages"]["source_prepare"] = {
                "status": "success",
                "source_revision": source_binding.get("revision"),
                "schema_hash": source_binding.get("schema_hash"),
                "config_hash": source_binding.get("config_hash"),
                "freshness": source_binding.get("freshness"),
            }
            state["timings_ms"]["source_prepare"] = round(
                (time.perf_counter() - prepare_started) * 1000, 3
            )

        compile_started = time.perf_counter()
        compiled: list[tuple[str, dict[str, Any], dict[str, Any], Path]] = []
        requests: list[tuple[str, dict[str, Any]]] = []
        task_answers_by_id: dict[str, dict[str, Any]] = {}
        prepared_bundle: list[dict[str, Any]] = []
        resolution_cases_by_task: dict[str, list[dict[str, Any]]] = {}
        if capabilities is not None:
            for case in capabilities.get("resolution_cases") or []:
                if not isinstance(case, dict):
                    continue
                case_tasks = [str(value) for value in case.get("task_ids") or []]
                for affected_task in case_tasks:
                    resolution_cases_by_task.setdefault(affected_task, []).append(
                        deepcopy(case)
                    )
        for task_id, ir in tasks:
            task_dir = args.work_dir / "tasks" / _task_name(task_id)
            task_dir.mkdir(parents=True, exist_ok=True)
            task_capabilities = capabilities
            if capabilities is not None and capabilities.get("task_resolutions") is not None:
                task_capabilities = project_task_capabilities(capabilities, task_id)
            task_cases = (
                list((task_capabilities or {}).get("resolution_cases") or [])
                if task_capabilities is not capabilities
                else resolution_cases_by_task.get(task_id) or []
            )
            if task_cases:
                waiting = any(case.get("action") == "confirm" for case in task_cases)
                status = "waiting_confirmation" if waiting else "blocked"
                answer = task_resolution_answer(task_id, task_cases, status=status)
                atomic_write_json(task_dir / "answer-payload.json", answer)
                task_answers_by_id[task_id] = answer
                prepared_bundle.append({
                    "task_id": task_id,
                    "analysis_ir": ir,
                    "source_resolution": task_cases,
                })
                continue
            try:
                prepared_ir = ir
                decisions: list[dict[str, Any]] = []
                if task_capabilities is not None:
                    prepared_ir, decisions = prepare_analysis_ir(
                        ir,
                        task_capabilities,
                        composition_registry,
                        derived_registry,
                    )
                active_cases = [
                    deepcopy(item.get("case") or {})
                    for item in decisions
                    if isinstance(item, dict)
                    and item.get("mode") == "resolution_case"
                    and isinstance(item.get("case"), dict)
                ]
                if active_cases:
                    waiting = any(case.get("action") == "confirm" for case in active_cases)
                    status = "waiting_confirmation" if waiting else "blocked"
                    answer = task_resolution_answer(task_id, active_cases, status=status)
                    atomic_write_json(task_dir / "answer-payload.json", answer)
                    task_answers_by_id[task_id] = answer
                    prepared_bundle.append({
                        "task_id": task_id,
                        "analysis_ir": prepared_ir,
                        "source_resolution": decisions,
                    })
                    continue
                atomic_write_json(task_dir / "prepared-ir.json", prepared_ir)
                prepared_bundle.append({
                    "task_id": task_id,
                    "analysis_ir": prepared_ir,
                    "source_resolution": decisions,
                })
                plan, report = compile_and_validate(
                    prepared_ir,
                    args.derived_registry,
                    args.composition_registry,
                    args.dimension_set_registry,
                )
                plan["source_resolution"] = decisions
                atomic_write_json(task_dir / "compiled-plan.json", plan)
                atomic_write_json(task_dir / "plan-validation.json", report)
                if not report.get("valid"):
                    errors = [
                        item for item in report.get("issues", [])
                        if isinstance(item, dict) and item.get("severity") == "ERROR"
                    ]
                    raise ValueError(f"plan validation failed: {errors[:3]}")
                task_requests = plan.get("fetch_requests") or []
                if len(task_requests) != 1:
                    raise ValueError("task must compile to one unified fetch request")
                compiled.append((task_id, plan, report, task_dir))
                requests.append((task_id, task_requests[0]))
            except Exception as exc:  # Task-local preparation/compile isolation.
                answer = task_error_answer(task_id, "compile", exc)
                atomic_write_json(task_dir / "answer-payload.json", answer)
                task_answers_by_id[task_id] = answer
        prepared_path = args.work_dir / "prepared-input.json"
        atomic_write_json(prepared_path, {
            "schema_version": "prepared_analysis_bundle/1.0",
            "tasks": prepared_bundle,
        })
        state["artifacts"]["prepared_input"] = artifact_record(prepared_path)
        waiting_count = sum(
            answer.get("status") == "waiting_confirmation"
            for answer in task_answers_by_id.values()
        )
        blocked_count = len(tasks) - len(compiled) - waiting_count
        state["stages"]["compile"] = {
            "status": "success" if len(compiled) == len(tasks) else (
                "waiting_confirmation" if waiting_count == len(tasks) else "partial_success"
            ),
            "task_count": len(tasks),
            "compiled_task_count": len(compiled),
            "waiting_confirmation_task_count": waiting_count,
            "blocked_task_count": blocked_count,
        }
        state["timings_ms"]["compile"] = round((time.perf_counter() - compile_started) * 1000, 3)
        if not compiled:
            terminal_status = (
                "waiting_confirmation"
                if waiting_count and blocked_count == 0
                else "blocked"
            )
            answer_document = {
                "schema_version": "analysis_bundle_answer/1.0" if len(tasks) > 1 else "analysis_answer/1.0",
                "status": terminal_status,
                "source_revision": source_binding.get("revision") if args.response_file is None else None,
                "fetch_reused": False,
                "tasks": [task_answers_by_id[task_id] for task_id in task_order],
                "workflow_duration_ms": round((time.perf_counter() - workflow_started) * 1000, 3),
            }
            answer_path = args.work_dir / "answer-payload.json"
            atomic_write_json(answer_path, answer_document)
            state["artifacts"]["answer"] = artifact_record(answer_path)
            state["status"] = terminal_status
            state["timings_ms"]["workflow"] = answer_document["workflow_duration_ms"]
            atomic_write_json(state_path, state)
            print(json.dumps({"status": terminal_status, "answer": str(answer_path), "fetch_reused": False}, ensure_ascii=False))
            return 0
        if args.response_file is not None:
            replay_source = (load_json(args.response_file).get("source") or {})
            source_binding = {
                "schema_version": SOURCE_BINDING_V1,
                "provider_id": "response_file",
                "source_id": replay_source.get("source_id") or "competitor_macro_sheet",
                "config_hash": value_hash({
                    "mode": "response_file",
                    "revision": replay_source.get("revision"),
                    "schema_hash": replay_source.get("schema_hash"),
                }),
                "revision": replay_source.get("revision", 0),
                "schema_hash": replay_source.get("schema_hash") or "response_file",
                "freshness": replay_source.get("freshness") or "replay",
            }
        merged_request = merge_fetch_requests(requests, source_binding=source_binding)
        dimension_registry_digest = str(
            merged_request.get("dimension_set_registry_hash") or ""
        )
        if not dimension_registry_digest:
            raise ValueError("compiled fetch request is missing dimension set registry hash")
        request_digest = value_hash(merged_request)
        request_path = args.work_dir / "fetch-request.json"
        atomic_write_json(request_path, merged_request)
        state["artifacts"]["fetch_request"] = artifact_record(request_path)
        atomic_write_json(state_path, state)

        reuse = False
        reuse_reason = "disabled"
        reusable_path: Path | None = None
        if args.resume == "auto" and not args.fresh:
            reuse_state = state if existing is not None else checkpoint_state
            reuse, reuse_reason, reusable_path = reusable_fetch(
                reuse_state,
                request_hash=request_digest,
                dimension_set_registry_hash=dimension_registry_digest,
            )
        state["resume_decisions"].append({"at": utc_now(), "stage": "fetch", "reused": reuse, "reason": reuse_reason})
        facts_path = args.work_dir / "facts.json"
        if reuse and reusable_path is not None:
            payload = load_json(reusable_path)
            fetch_record = next(
                item for item in reversed((reuse_state or {}).get("fetch_attempts", [])) if item.get("status") == "success"
            )
            state["fetch_attempts"] = deepcopy((reuse_state or {}).get("fetch_attempts", []))
            state["artifacts"]["facts"] = deepcopy((reuse_state or {}).get("artifacts", {}).get("facts"))
            state["stages"]["fetch"] = deepcopy((reuse_state or {}).get("stages", {}).get("fetch"))
        else:
            try:
                payload, fetch_record = _fetch(
                    merged_request,
                    response_file=args.response_file,
                    gateway=gateway,
                )
            except RuntimeError as exc:
                failed_record = json.loads(str(exc))
                _append_attempt(state, failed_record)
                state["stages"]["fetch"] = {
                    "status": "failed",
                    "request_hash": request_digest,
                }
                state["status"] = "failed"
                atomic_write_json(state_path, state)
                raise
            atomic_write_json(facts_path, payload)
            fetch_record["raw_bytes"] = facts_path.stat().st_size
            fetch_record["facts_artifact"] = str(facts_path)
            _append_attempt(state, fetch_record)
            state["artifacts"]["facts"] = artifact_record(facts_path)
            state["stages"]["fetch"] = {
                "status": "success",
                "request_hash": request_digest,
                "source_revision": (payload.get("source") or {}).get("revision"),
                "schema_hash": (payload.get("source") or {}).get("schema_hash"),
                "source_binding": deepcopy(source_binding),
                "dimension_set_registry_hash": dimension_registry_digest,
            }
            atomic_write_json(state_path, state)
            if payload.get("schema_version") != SCENE_FACTS_V2 and len(tasks) > 1:
                raise ValueError("bundle execution requires scene_facts/2.0 bindings")

        execute_started = time.perf_counter()
        revisions: set[Any] = set()
        revisions.add((payload.get("source") or {}).get("revision"))
        for task_id, plan, _, task_dir in compiled:
            try:
                logical_facts = project_scene_facts(payload, task_id=task_id) if payload.get("schema_version") == SCENE_FACTS_V2 else payload["facts"]
                task_request_id = str(plan["fetch_requests"][0]["request_id"])
                plan["resolved_dimension_domains"] = deepcopy(
                    payload.get("resolved_dimension_domains") or {}
                )
                for row in logical_facts:
                    row["source_request_id"] = task_request_id
                logical_path = task_dir / "logical-facts.json"
                atomic_write_json(logical_path, {"schema_version": "scene_facts/1.0", "facts": logical_facts, "source": payload.get("source", {})})
                task_fetch_record = deepcopy(fetch_record)
                task_fetch_record["bundle_request_id"] = task_fetch_record.get("request_id")
                task_fetch_record["request_id"] = task_request_id
                task_fetch_record["facts_artifact"] = str(logical_path)
                plan["fetch_results"] = [task_fetch_record]
                atomic_write_json(task_dir / "compiled-plan.json", plan)
                manifest_path = task_dir / "execution-manifest.json"
                final_report_path = task_dir / "final-validation.json"
                manifest = execute_plan(plan, logical_facts, manifest_path, task_dir / "execution-events.jsonl", storage_mode="inline")
                finalize_model_nodes(manifest)
                manifest.setdefault("validation_reports", {})["final"] = {"artifact": str(final_report_path), "exit_code": 0}
                atomic_write_json(manifest_path, manifest)
                final_report = Validator(manifest, "final", task_dir).validate()
                atomic_write_json(final_report_path, final_report)
                if not final_report.get("valid"):
                    errors = [
                        item for item in final_report.get("issues", [])
                        if isinstance(item, dict) and item.get("severity") == "ERROR"
                    ]
                    raise ValueError(f"final validation failed: {errors[:3]}")
                answer = answer_payload(manifest, str(plan.get("execution_profile")))
                answer["task_id"] = task_id
                answer["source_revision"] = (payload.get("source") or {}).get("revision")
            except Exception as exc:  # Preserve independent task results.
                answer = task_error_answer(task_id, "execute", exc)
                answer["source_revision"] = (payload.get("source") or {}).get("revision")
            atomic_write_json(task_dir / "answer-payload.json", answer)
            task_answers_by_id[task_id] = answer
        if len(revisions) != 1:
            raise ValueError("bundle tasks did not execute from one source revision")
        task_answers = [
            compact_task_answer(task_answers_by_id[task_id])
            for task_id in task_order
        ]
        task_statuses = [str(answer.get("status")) for answer in task_answers]
        if all(status == "success" for status in task_statuses):
            workflow_status = "success"
        elif any(status in {"success", "partial_success"} for status in task_statuses):
            workflow_status = "partial_success"
        elif all(status == "waiting_confirmation" for status in task_statuses):
            workflow_status = "waiting_confirmation"
        else:
            workflow_status = "blocked"
        answer_document = {
            "schema_version": "analysis_bundle_answer/1.0" if len(tasks) > 1 else "analysis_answer/1.0",
            "status": workflow_status,
            "source_revision": next(iter(revisions)),
            "fetch_reused": reuse,
            "tasks": task_answers,
            "workflow_duration_ms": round((time.perf_counter() - workflow_started) * 1000, 3),
        }
        answer_path = args.work_dir / "answer-payload.json"
        atomic_write_json(answer_path, answer_document)
        state["stages"]["execute"] = {"status": workflow_status, "task_count": len(task_answers)}
        state["timings_ms"]["execute"] = round((time.perf_counter() - execute_started) * 1000, 3)
        state["timings_ms"]["workflow"] = answer_document["workflow_duration_ms"]
        state["artifacts"]["answer"] = artifact_record(answer_path)
        state["status"] = workflow_status
        state.pop("error", None)
        atomic_write_json(state_path, state)
        print(json.dumps({"status": workflow_status, "answer": str(answer_path), "fetch_reused": reuse}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI emits a concise machine-readable failure.
        if state is not None:
            state["status"] = "failed"
            state["error"] = {
                "type": exc.__class__.__name__,
                "code": str(getattr(exc, "code", exc.__class__.__name__)),
                "message": str(exc),
            }
            details = getattr(exc, "details", None)
            if isinstance(details, dict) and details:
                state["error"]["details"] = deepcopy(details)
            if isinstance(exc, InputProtocolError):
                state.setdefault("stages", {})["input"] = {
                    "status": "failed",
                    "code": state["error"]["code"],
                }
            state["timings_ms"]["workflow"] = round((time.perf_counter() - workflow_started) * 1000, 3)
            atomic_write_json(state_path, state)
        print(json.dumps({
            "status": "failed",
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "error_code": str(getattr(exc, "code", exc.__class__.__name__)),
            "details": getattr(exc, "details", None),
        }, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
