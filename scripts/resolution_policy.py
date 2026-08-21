#!/usr/bin/env python3
"""Deterministic business resolution policy and query-scoped source overlay."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from _vendor.ecom_competitor_source import (
    match_catalogue_name,
    normalize_match_text,
    resolve_catalogue_name,
)


POLICY_SCHEMA = "resolution_policy/1.0"
ENGINE_VERSION = "2.0.0"
DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "resolution-policy-registry.json"
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
ALLOWED_POLICY_KEYS = {"schema_version", "policy_version", "limits", "rules"}
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
        conflicts = list(raw.get("conflicts") or [])
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
        candidate = {
            "name": candidate_name,
            "confidence": float(raw.get("confidence") or 0.0),
            "evidence": [
                str(raw.get("match_method") or match.get("match_method") or "name_similarity")
            ],
            "conflicts": sorted(set(conflicts)),
            "match_method": raw.get("match_method") or match.get("match_method"),
        }
        if conflicts:
            rejected.append(candidate)
            continue
        semantic_hint = (
            expected_object == "ratio"
            and any(token in query for token in ("同比", "增速", "占比", "率"))
            and actual_object == "ratio"
        )
        provenance = str(requested.get("provenance") or "model_inferred")
        candidate["evidence"] += (["query_semantic_match"] if semantic_hint else [])
        candidate["strong"] = (
            str(candidate.get("match_method")) in {"standard_name", "metadata_alias", "builtin_alias"}
            or provenance in {"user_explicit", "user_formula", "registered_definition", "source_metadata"}
            or semantic_hint
        )
        viable.append(candidate)
    viable.sort(key=lambda item: item["confidence"], reverse=True)
    rule = policy["rules"]["query_name_resolution"]["auto"]
    minimum_confidence = float(rule.get("minimum_confidence", 1.0))
    eligible = [
        item for item in viable if item["confidence"] >= minimum_confidence
    ]
    margin = viable[0]["confidence"] - (viable[1]["confidence"] if len(viable) > 1 else 0.0) if viable else 0.0
    if (
        len(eligible) == 1
        and margin >= float(rule.get("minimum_candidate_margin", 1.0))
        and (eligible[0]["strong"] or not rule.get("require_strong_provenance_or_semantics"))
    ):
        return {
            "action": "auto",
            "status": "bound",
            "binding": eligible[0]["name"],
            "decision_basis": "policy",
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
) -> dict[str, Any]:
    """Resolve requested names and ambiguous blocks without mutating the shared raw index."""
    validate_resolution_policy(policy)
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
            "composition_resolutions": [],
            "resolution_cases": [],
        }
        for metric in context.get("metrics") or []:
            if not isinstance(metric, dict) or not metric.get("name"):
                continue
            requested_name = str(metric["name"])
            binding_attempts[requested_name] = binding_attempts.get(requested_name, 0) + 1
            metric_ref = str(metric.get("metric_ref") or requested_name)
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
    }
