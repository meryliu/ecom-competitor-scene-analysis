#!/usr/bin/env python3
"""Compile compact scene-analysis IR into the existing executable plan contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from dimension_domain_registry import (
    DEFAULT_REGISTRY_PATH as DEFAULT_DIMENSION_SET_REGISTRY,
    dimension_domain_ref,
    is_dimension_domain,
    load_dimension_set_registry,
    registry_hash,
    source_dimension_domain_ref,
)
from fact_contract import build_fact_demands
from fast_query_admission import assess_query
from prepare_analysis import normalize_analysis_ir
from selector_context import SelectorContextError, apply_task_selector_context
from validate_execution import Validator, reject_duplicate_keys


COMPILER_NAME = "scene-analysis-plan-compiler"
COMPILER_VERSION = "1.6.0"
IR_VERSION = "analysis_ir/1.0"
DEFAULT_RESIDUAL_TOLERANCE = 1e-8
ALLOWED_CRITICALITIES = {"core", "required", "optional"}
ALLOWED_EXPRESSION_OPS = {
    "add": (1, None),
    "subtract": (2, 2),
    "multiply": (1, None),
    "divide": (2, 2),
    "sum": (1, None),
    "negate": (1, 1),
}
SCENARIO_ROLES = {
    "metric_change": ["analysis", "comparison"],
    "yoy_trend_change": ["analysis", "analysis_last_year", "comparison", "comparison_last_year"],
}
SPARSE_STRATEGY = "merge_other_then_epsilon"
SPARSE_REFERENCE_RATE_POLICY = "paired_observed_self_rate"


class CompileError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]


def stable_node_id(prefix: str, requirement_id: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", requirement_id).strip("_") or "requirement"
    return f"{prefix}_{label}_{stable_hash(requirement_id, 6)}"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CompileError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompileError(f"JSON root must be an object: {path}")
    return value


def require_nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise CompileError(f"{path} must be a non-empty string")
    return value


def validate_sparse_policy(value: Any, path: str = "attribution sparse_policy") -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CompileError(f"{path} must be an object")
    strategy = value.get("strategy", SPARSE_STRATEGY)
    if strategy != SPARSE_STRATEGY:
        raise CompileError(f"{path}.strategy must be {SPARSE_STRATEGY!r}")
    reference_policy = value.get("reference_rate_policy", SPARSE_REFERENCE_RATE_POLICY)
    if reference_policy != SPARSE_REFERENCE_RATE_POLICY:
        raise CompileError(
            f"{path}.reference_rate_policy must be {SPARSE_REFERENCE_RATE_POLICY!r}"
        )
    epsilon = value.get("epsilon", 1e-9)
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)):
        raise CompileError(f"{path}.epsilon must be numeric")
    if not math.isfinite(float(epsilon)) or float(epsilon) <= 0:
        raise CompileError(f"{path}.epsilon must be finite and > 0")
    other_name = value.get("other_name", "其他/未覆盖")
    if not isinstance(other_name, str) or not other_name:
        raise CompileError(f"{path}.other_name must be a non-empty string")
    for flag in ("structural_absence_is_zero", "approximation_note_required"):
        if flag in value and not isinstance(value[flag], bool):
            raise CompileError(f"{path}.{flag} must be boolean")

    merge_rules = value.get("merge_rules", [])
    if not isinstance(merge_rules, list):
        raise CompileError(f"{path}.merge_rules must be an array")
    for rule_index, rule in enumerate(merge_rules):
        rule_path = f"{path}.merge_rules[{rule_index}]"
        if not isinstance(rule, dict):
            raise CompileError(f"{rule_path} must be an object")
        members = rule.get("members")
        if not isinstance(members, list) or not members:
            raise CompileError(f"{rule_path}.members must be a non-empty array")
        for member_index, member in enumerate(members):
            member_path = f"{rule_path}.members[{member_index}]"
            if isinstance(member, str):
                if not member:
                    raise CompileError(f"{member_path} must not be empty")
                continue
            if not isinstance(member, dict):
                raise CompileError(f"{member_path} must be a string or object")
            dimensions = member.get("dimensions", {})
            if not isinstance(dimensions, dict):
                raise CompileError(f"{member_path}.dimensions must be an object")
            if member.get("name") is None and not dimensions:
                raise CompileError(f"{member_path} requires name or dimensions")
        if "target_name" in rule:
            require_nonempty_string(rule["target_name"], f"{rule_path}.target_name")
        if "target_dimensions" in rule and not isinstance(rule["target_dimensions"], dict):
            raise CompileError(f"{rule_path}.target_dimensions must be an object")
        if "is_other" in rule and not isinstance(rule["is_other"], bool):
            raise CompileError(f"{rule_path}.is_other must be boolean")

    rollup_path = value.get("rollup_path", [])
    if not isinstance(rollup_path, list):
        raise CompileError(f"{path}.rollup_path must be an array")
    for level_index, level in enumerate(rollup_path):
        if (
            not isinstance(level, list)
            or not level
            or not all(isinstance(item, str) and item for item in level)
        ):
            raise CompileError(f"{path}.rollup_path[{level_index}] must be a non-empty string array")
    parent_dimensions = value.get("parent_dimensions", [])
    if (
        not isinstance(parent_dimensions, list)
        or not all(isinstance(item, str) and item for item in parent_dimensions)
    ):
        raise CompileError(f"{path}.parent_dimensions must be a string array")
    return deepcopy(value)


def criticality(requirement: dict[str, Any]) -> str:
    value = requirement.get("criticality", "required")
    if value not in ALLOWED_CRITICALITIES:
        raise CompileError(f"requirement criticality must be core, required, or optional: {value!r}")
    return str(value)


def make_node(
    node_id: str,
    node_type: str,
    requirement_refs: list[str],
    criticality_value: str,
    depends_on: list[str],
    execution: dict[str, Any],
    *,
    status: str = "planned",
    inputs: dict[str, Any] | None = None,
    outputs: list[str] | None = None,
    quality_gate: list[str] | None = None,
    failure_strategy: str = "isolate failure and preserve independent results",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    node = {
        "node_id": node_id,
        "type": node_type,
        "status": status,
        "criticality": criticality_value,
        "requirement_refs": sorted(set(requirement_refs)),
        "depends_on": sorted(set(depends_on)),
        "inputs": inputs or {},
        "outputs": outputs or [],
        "execution": execution,
        "quality_gate": quality_gate or [],
        "failure_strategy": failure_strategy,
    }
    if extra:
        node.update(extra)
    return node


class Compiler:
    def __init__(
        self,
        ir: dict[str, Any],
        registry: dict[str, Any],
        composition_registry: dict[str, Any] | None = None,
        dimension_set_registry: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.ir = apply_task_selector_context(normalize_analysis_ir(ir))
        except SelectorContextError as exc:
            raise CompileError(str(exc)) from exc
        self.registry = registry
        self.composition_registry = composition_registry or {"definitions": {}}
        self.dimension_set_registry = dimension_set_registry or {"schema_version": "dimension_set_registry/1.0", "sets": {}}
        self.task = self.ir.get("analysis_task")
        if not isinstance(self.task, dict):
            raise CompileError("$.analysis_task must be an object")
        self.periods = self.task.get("periods")
        if not isinstance(self.periods, dict):
            raise CompileError("$.analysis_task.periods must be an object")
        self.metrics: dict[str, dict[str, Any]] = {}
        self.views: set[str] = set()
        self.levels: set[str] = set()
        self.nodes: list[dict[str, Any]] = []
        self.fact_slots: dict[str, dict[str, Any]] = {}
        self.fact_slot_keys: dict[str, str] = {}
        self.requirement_compilation: list[dict[str, Any]] = []
        self.operator_contracts: list[dict[str, Any]] = []
        self.operator_queries: list[dict[str, Any]] = []
        self.operator_query_ids: set[str] = set()
        self.compiled_attribution_targets: list[dict[str, Any]] = []
        self.operator_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.operator_resolution_ms = 0.0
        self.attribution_engine_identity: dict[str, Any] | None = None
        self.requirement_node_ids: dict[str, str] = {}
        self.all_requirement_ids: set[str] = set()
        self.resolved_target_semantics: dict[str, dict[str, Any]] = {}
        self.target_semantic_warnings: dict[str, list[str]] = {}
        self.normalized_target_rankings: dict[str, dict[str, Any] | None] = {}
        self.input_adaptation_targets: dict[str, str] = {}
        self.requirement_adaptation_dependencies: dict[str, set[str]] = {}
        self.compiling_input_adaptation = False
        self.canonical_fact_selectors: dict[tuple[str, str, str], dict[str, Any]] = {}
        for selector in self.ir.get("canonical_fact_selectors") or []:
            if not isinstance(selector, dict):
                raise CompileError("$.canonical_fact_selectors must contain objects")
            key = (
                str(selector.get("metric_ref") or ""),
                str(selector.get("period_role") or ""),
                canonical_json(selector.get("selector_dimensions") or {}),
            )
            if not all(key[:2]):
                raise CompileError("canonical fact selector requires metric_ref and period_role")
            previous = self.canonical_fact_selectors.get(key)
            if previous is not None and previous != selector:
                raise CompileError("conflicting canonical fact selectors for one fact identity")
            self.canonical_fact_selectors[key] = deepcopy(selector)

    def validate_ir(self) -> None:
        if self.ir.get("ir_version") != IR_VERSION:
            raise CompileError(f"$.ir_version must be {IR_VERSION!r}")
        runtime = self.ir.get("runtime")
        if isinstance(runtime, dict) and "residual_tolerance" in runtime:
            raise CompileError(
                "$.runtime.residual_tolerance is runner-owned and cannot be overridden by analysis IR"
            )
        raw_metrics = self.task.get("metrics")
        if not isinstance(raw_metrics, list) or not raw_metrics:
            raise CompileError("$.analysis_task.metrics must be a non-empty array")
        for index, metric in enumerate(raw_metrics):
            if not isinstance(metric, dict):
                raise CompileError(f"$.analysis_task.metrics[{index}] must be an object")
            metric_id = require_nonempty_string(metric.get("metric_id"), f"$.analysis_task.metrics[{index}].metric_id")
            require_nonempty_string(metric.get("name"), f"$.analysis_task.metrics[{index}].name")
            if metric.get("metric_object") not in {"volume", "ratio"}:
                raise CompileError(f"$.analysis_task.metrics[{index}].metric_object must be volume or ratio")
            require_nonempty_string(metric.get("unit"), f"$.analysis_task.metrics[{index}].unit")
            if metric_id in self.metrics:
                raise CompileError(f"duplicate metric_id: {metric_id}")
            self.metrics[metric_id] = metric

        raw_views = self.ir.get("views", [])
        if not isinstance(raw_views, list):
            raise CompileError("$.views must be an array")
        for index, view in enumerate(raw_views):
            if not isinstance(view, dict):
                raise CompileError(f"$.views[{index}] must be an object")
            view_id = require_nonempty_string(view.get("view_id"), f"$.views[{index}].view_id")
            if view_id in self.views:
                raise CompileError(f"duplicate view_id: {view_id}")
            self.views.add(view_id)

        raw_trees = self.ir.get("dimension_trees", [])
        if not isinstance(raw_trees, list):
            raise CompileError("$.dimension_trees must be an array")
        tree_ids: set[str] = set()
        for tree_index, tree in enumerate(raw_trees):
            if not isinstance(tree, dict):
                raise CompileError(f"$.dimension_trees[{tree_index}] must be an object")
            tree_id = require_nonempty_string(tree.get("tree_id"), f"$.dimension_trees[{tree_index}].tree_id")
            if tree_id in tree_ids:
                raise CompileError(f"duplicate tree_id: {tree_id}")
            tree_ids.add(tree_id)
            levels = tree.get("levels")
            if not isinstance(levels, list) or not levels:
                raise CompileError(f"$.dimension_trees[{tree_index}].levels must be non-empty")
            for level_index, level in enumerate(levels):
                if not isinstance(level, dict):
                    raise CompileError(f"dimension tree level must be an object: {tree_id}[{level_index}]")
                level_id = require_nonempty_string(level.get("level_id"), f"{tree_id}.levels[{level_index}].level_id")
                require_nonempty_string(level.get("dimension_ref"), f"{tree_id}.levels[{level_index}].dimension_ref")
                if level_id in self.levels:
                    raise CompileError(f"duplicate level_id: {level_id}")
                self.levels.add(level_id)
        for view in raw_views:
            tree_ref = view.get("dimension_tree_ref")
            if tree_ref is not None and tree_ref not in tree_ids:
                raise CompileError(f"view references unknown dimension tree: {tree_ref}")

        collections = (
            ("input_adaptations", "requirement_id", "adaptation"),
            ("fact_observations", "requirement_id", "fact"),
            ("metric_compositions", "requirement_id", "composition"),
            ("derived_requirements", "requirement_id", "derived"),
            ("custom_calculations", "requirement_id", "custom"),
            ("attribution_targets", "target_id", "attribution"),
            ("output_requirements", "requirement_id", "output"),
        )
        for collection_name, id_field, node_prefix in collections:
            values = self.ir.get(collection_name, [])
            if not isinstance(values, list):
                raise CompileError(f"$.{collection_name} must be an array")
            for index, requirement in enumerate(values):
                if not isinstance(requirement, dict):
                    raise CompileError(f"$.{collection_name}[{index}] must be an object")
                requirement_id = require_nonempty_string(
                    requirement.get(id_field), f"$.{collection_name}[{index}].{id_field}"
                )
                if requirement_id in self.all_requirement_ids:
                    raise CompileError(f"duplicate requirement ID: {requirement_id}")
                self.all_requirement_ids.add(requirement_id)
                self.requirement_node_ids[requirement_id] = (
                    "conclusion_organization"
                    if collection_name == "output_requirements"
                    else stable_node_id(node_prefix, requirement_id)
                )
                self._validate_common_requirement(requirement)
                if collection_name == "attribution_targets":
                    policy = validate_sparse_policy(
                        requirement.get("sparse_policy"),
                        f"$.attribution_targets[{index}].sparse_policy",
                    )
                    parent_dimensions = self._attribution_parent_dimensions(requirement)
                    policy_parent_dimensions = policy.get("parent_dimensions")
                    if (
                        policy_parent_dimensions is not None
                        and policy_parent_dimensions != parent_dimensions
                    ):
                        raise CompileError(
                            f"$.attribution_targets[{index}].sparse_policy.parent_dimensions "
                            "must match attribution parent_dimensions"
                        )
                    self._attribution_expansion(requirement)

    def _validate_common_requirement(self, requirement: dict[str, Any]) -> None:
        criticality(requirement)
        view_id = requirement.get("view_id")
        if view_id is not None and view_id not in self.views:
            raise CompileError(f"requirement references unknown view_id: {view_id}")
        apply_to = requirement.get("apply_to", [])
        if not isinstance(apply_to, list):
            raise CompileError("requirement apply_to must be an array")
        unknown_levels = sorted(set(apply_to) - self.levels)
        if unknown_levels:
            raise CompileError(f"requirement apply_to references unknown levels: {unknown_levels}")

    @staticmethod
    def _attribution_parent_dimensions(target: dict[str, Any]) -> list[str]:
        expansion = target.get("expansion")
        if expansion is not None and not isinstance(expansion, dict):
            raise CompileError("attribution expansion must be an object")
        target_dimensions = target.get("parent_dimensions")
        expansion_dimensions = expansion.get("parent_dimensions") if isinstance(expansion, dict) else None
        selected = target_dimensions if target_dimensions is not None else expansion_dimensions
        if selected is None:
            return []
        if (
            not isinstance(selected, list)
            or not all(isinstance(item, str) and item for item in selected)
        ):
            raise CompileError("attribution parent_dimensions must be a string array")
        if target_dimensions is not None and expansion_dimensions is not None and target_dimensions != expansion_dimensions:
            raise CompileError("attribution parent_dimensions must match expansion.parent_dimensions")
        return list(selected)

    @classmethod
    def _attribution_expansion(cls, target: dict[str, Any]) -> dict[str, Any]:
        parent_dimensions = cls._attribution_parent_dimensions(target)
        raw = target.get("expansion")
        expansion = deepcopy(raw) if isinstance(raw, dict) else {}
        mode = expansion.get("mode")
        if parent_dimensions:
            if mode not in (None, "for_each_parent_group"):
                raise CompileError(
                    "attribution with parent_dimensions must use expansion.mode=for_each_parent_group"
                )
            expansion["mode"] = "for_each_parent_group"
            expansion["parent_dimensions"] = parent_dimensions
        else:
            if mode not in (None, "none"):
                raise CompileError("for_each_parent_group requires non-empty parent_dimensions")
            expansion = {"mode": "none"}
        return expansion

    def metric(self, metric_ref: Any) -> dict[str, Any]:
        metric_id = require_nonempty_string(metric_ref, "metric_ref")
        if metric_id not in self.metrics:
            raise CompileError(f"unknown metric_ref: {metric_id}")
        return self.metrics[metric_id]

    def period(self, role: str) -> str:
        value = self.periods.get(role)
        if not isinstance(value, str) or not value:
            raise CompileError(f"missing analysis_task.periods.{role}")
        return value

    def add_fact_slot(
        self,
        requirement_id: str,
        metric_ref: str,
        period_role: str,
        *,
        view_id: str | None = None,
        dimension_refs: list[str] | None = None,
        component: str | None = None,
        selector_dimensions: dict[str, Any] | None = None,
        full_dimension_domains: list[str] | None = None,
    ) -> str:
        metric = self.metric(metric_ref)
        logical_dimension_refs = sorted(set(dimension_refs or []))
        logical_selectors = deepcopy(selector_dimensions or {})
        canonical_selector = self.canonical_fact_selectors.get((
            metric_ref,
            period_role,
            canonical_json(logical_selectors),
        ))
        if canonical_selector is not None:
            canonical_period = canonical_selector.get("period")
            if canonical_period != self.period(period_role):
                raise CompileError(
                    f"canonical selector period conflicts with analysis_task.periods.{period_role}"
                )
        source_dimension_bindings = metric.get("source_dimension_bindings") or {}
        source_dimension_refs = sorted({
            str(source_dimension_bindings.get(name) or name)
            for name in logical_dimension_refs
        })
        source_selectors = {
            str(source_dimension_bindings.get(name) or name): deepcopy(value)
            for name, value in logical_selectors.items()
        }
        dimension_projection = {
            str(source_dimension_bindings.get(name) or name): str(name)
            for name in logical_dimension_refs
        }
        logical_full_domains = sorted(set(full_dimension_domains or []))
        unknown_full_domains = sorted(set(logical_full_domains) - set(logical_dimension_refs))
        if unknown_full_domains:
            raise CompileError(
                "full_dimension_domains must be included in dimension_refs: "
                + ", ".join(unknown_full_domains)
            )
        source_dimension_domains = {
            str(source_dimension_bindings.get(name) or name): source_dimension_domain_ref(
                str(source_dimension_bindings.get(name) or name)
            )
            for name in logical_full_domains
        }
        slot_identity = {
            "metric_ref": metric_ref,
            "period_role": period_role,
            "period": self.period(period_role),
            "view_id": view_id,
            "dimension_refs": logical_dimension_refs,
            "selector_dimensions": logical_selectors,
            "source_dimension_refs": source_dimension_refs,
            "source_selector_dimensions": source_selectors,
            "dimension_projection": dimension_projection,
            "source_dimension_domains": source_dimension_domains,
            "component": component,
            "scope": self.task.get("scope"),
            "filters": self.task.get("filters", []),
        }
        adaptation_identity = self._adaptation_identity(slot_identity)
        materialized_by = (
            None
            if self.compiling_input_adaptation
            else self.input_adaptation_targets.get(adaptation_identity)
        )
        key = canonical_json(slot_identity)
        slot_id = self.fact_slot_keys.get(key)
        if slot_id is None:
            slot_id = f"fact_{stable_hash(slot_identity)}"
            self.fact_slot_keys[key] = slot_id
            self.fact_slots[slot_id] = {
                "fact_slot_id": slot_id,
                **slot_identity,
                "metric": metric["name"],
                "source_metric_name": (
                    canonical_selector.get("source_metric_name")
                    if canonical_selector is not None
                    else metric.get("source_metric_name") or metric["name"]
                ),
                "metric_object": metric["metric_object"],
                "unit": metric["unit"],
                "requirement_refs": [],
            }
            if canonical_selector is not None:
                self.fact_slots[slot_id].update({
                    "grain": canonical_selector.get("grain"),
                    "capability_path": canonical_selector.get("capability_path"),
                    "source_binding": deepcopy(canonical_selector.get("source_binding") or {}),
                })
            if materialized_by is not None:
                self.fact_slots[slot_id]["materialized_by"] = materialized_by
        elif materialized_by is not None:
            self.fact_slots[slot_id]["materialized_by"] = materialized_by
        refs = self.fact_slots[slot_id]["requirement_refs"]
        if requirement_id not in refs:
            refs.append(requirement_id)
            refs.sort()
        if materialized_by is not None:
            self.requirement_adaptation_dependencies.setdefault(requirement_id, set()).add(
                materialized_by
            )
            for node in self.nodes:
                if node.get("node_id") == materialized_by:
                    node_refs = node.setdefault("requirement_refs", [])
                    if requirement_id not in node_refs:
                        node_refs.append(requirement_id)
                        node_refs.sort()
                    break
        return slot_id

    def _adaptation_identity(self, slot_identity: dict[str, Any]) -> str:
        scalar_dimensions = self._expression_dimensions(
            slot_identity.get("selector_dimensions") or {}
        )
        return canonical_json({
            "metric_ref": slot_identity.get("metric_ref"),
            "period_role": slot_identity.get("period_role"),
            "period": slot_identity.get("period"),
            "view_id": slot_identity.get("view_id"),
            "dimension_refs": sorted(set(slot_identity.get("dimension_refs") or [])),
            "dimensions": scalar_dimensions,
            "component": slot_identity.get("component"),
            "scope": slot_identity.get("scope"),
            "filters": slot_identity.get("filters") or [],
        })

    def _node_dependencies(self, requirement_id: str, fact_slot_ids: list[str]) -> list[str]:
        dependencies = set(self.requirement_adaptation_dependencies.get(requirement_id, set()))
        if any(
            not self.fact_slots[slot_id].get("materialized_by")
            for slot_id in fact_slot_ids
        ):
            dependencies.add("fact_artifact")
        return sorted(dependencies)

    @staticmethod
    def _fact_selector(
        metric: str,
        *,
        view_id: str | None,
        dimensions: dict[str, Any] | None = None,
        exact: bool = True,
    ) -> dict[str, Any]:
        selector: dict[str, Any] = {
            "metric": metric,
            "dimensions": deepcopy(dimensions or {}),
        }
        if view_id is not None:
            selector["view_id"] = view_id
        if exact:
            selector["dimensions_exact"] = True
        return selector

    def _dimension_domains(self, dimensions: dict[str, Any]) -> dict[str, dict[str, Any]]:
        domains: dict[str, dict[str, Any]] = {}
        for dimension, value in dimensions.items():
            if is_dimension_domain(str(dimension), value, self.dimension_set_registry):
                domains[str(dimension)] = {
                    "domain_ref": dimension_domain_ref(
                        str(dimension), value, self.dimension_set_registry
                    ),
                    "requested_values": deepcopy(value if isinstance(value, list) else [value]),
                }
        return domains

    def _expression_dimensions(self, dimensions: dict[str, Any]) -> dict[str, Any]:
        """Return only scalar fact identity constraints, excluding selection domains."""
        domains = self._dimension_domains(dimensions)
        return {
            str(dimension): deepcopy(value)
            for dimension, value in dimensions.items()
            if str(dimension) not in domains
        }

    def _fact_dimension_refs(
        self, dimensions: dict[str, Any], dimension_refs: list[str] | None
    ) -> list[str]:
        return sorted({
            *(dimension_refs or []),
            *(str(dimension) for dimension in dimensions),
        })

    def _bind_fact_domain(
        self,
        fact_selector: dict[str, Any],
        dimensions: dict[str, Any],
        group_dimensions: list[str] | None,
    ) -> dict[str, Any]:
        ungrouped = {
            dimension: domain
            for dimension, domain in self._dimension_domains(dimensions).items()
            if dimension not in (group_dimensions or [])
        }
        if not ungrouped:
            return {"fact": fact_selector}
        if len(ungrouped) != 1:
            raise CompileError("collection aggregation currently supports exactly one dimension")
        dimension, domain = next(iter(ungrouped.items()))
        return {
            "aggregate": {
                "selector": fact_selector,
                "dimension": dimension,
                "domain_ref": domain["domain_ref"],
            }
        }

    def _block_apply_to(self, requirement: dict[str, Any], node_type: str) -> bool:
        apply_to = requirement.get("apply_to", [])
        if not apply_to:
            return False
        if node_type in {"derived_metric", "metric_composition"}:
            return False
        requirement_id = str(requirement["requirement_id"])
        node_id = self.requirement_node_ids[requirement_id]
        self.nodes.append(make_node(
            node_id,
            node_type,
            [requirement_id],
            criticality(requirement),
            [],
            {"mode": "blocked", "handler": "derived"},
            status="blocked",
            inputs={
                "view_id": requirement.get("view_id"),
                "apply_to": apply_to,
            },
            extra={"reason_code": "CALCULATION_SCOPE_UNSUPPORTED"},
        ))
        self.requirement_compilation.append({
            "requirement_id": requirement_id,
            "kind": (
                "derived"
                if node_type == "derived_metric"
                else "metric_composition"
                if node_type == "metric_composition"
                else "custom_calculation"
            ),
            "status": "blocked",
            "node_ids": [node_id],
            "fact_slot_ids": [],
        })
        return True

    def _resolution_block(self, requirement: dict[str, Any], node_type: str) -> bool:
        requirement_id = str(
            requirement.get("requirement_id") or requirement.get("target_id") or ""
        )
        block = next(
            (
                item for item in self.ir.get("resolution_blocks") or []
                if isinstance(item, dict)
                and str(item.get("requirement_id")) == requirement_id
            ),
            None,
        )
        if block is None:
            return False
        node_id = self.requirement_node_ids[requirement_id]
        self.nodes.append(make_node(
            node_id,
            node_type,
            [requirement_id],
            criticality(requirement),
            [],
            {"mode": "blocked", "handler": "derived"},
            status="blocked",
            inputs={"resolution_cases": deepcopy(block.get("resolution_cases") or [])},
            outputs=list(requirement.get("required_outputs", [requirement_id])),
            extra={"reason_code": block.get("reason_code", "SOURCE_RESOLUTION_REQUIRED")},
        ))
        self.requirement_compilation.append({
            "requirement_id": requirement_id,
            "kind": node_type,
            "status": "blocked",
            "node_ids": [node_id],
            "fact_slot_ids": [],
        })
        return True

    def _transform_expression(
        self,
        expression: Any,
        requirement: dict[str, Any],
        fact_slot_ids: list[str],
        result_dependencies: set[str],
    ) -> dict[str, Any]:
        if not isinstance(expression, dict):
            raise CompileError("calculation expression must be an object")
        if "literal" in expression:
            value = expression["literal"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CompileError("expression literal must be numeric")
            return {"literal": value}
        if "fact" in expression:
            fact = expression["fact"]
            if not isinstance(fact, dict):
                raise CompileError("expression fact must be an object")
            transformed = deepcopy(fact)
            metric_ref = transformed.pop("metric_ref", None)
            if metric_ref is None:
                raise CompileError("IR expression fact requires metric_ref")
            metric = self.metric(metric_ref)
            transformed["metric"] = metric["name"]
            period_role = require_nonempty_string(transformed.get("period_role"), "expression.fact.period_role")
            self.period(period_role)
            inherited_dimension_refs = requirement.get("dimension_refs", [])
            declared_dimension_refs = transformed.pop("dimension_refs", [])
            if not isinstance(inherited_dimension_refs, list) or not isinstance(declared_dimension_refs, list):
                raise CompileError("expression.fact.dimension_refs must be an array")
            source_dimension_refs = sorted({
                *(str(item) for item in inherited_dimension_refs),
                *(str(item) for item in declared_dimension_refs),
            })
            dimensions = self._merge_factor_dimensions(
                requirement.get("dimensions") or {},
                transformed.get("dimensions"),
                "expression.fact",
            )
            transformed["dimensions"] = self._expression_dimensions(dimensions)
            transformed["dimensions_exact"] = True
            if requirement.get("view_id") is not None:
                transformed["view_id"] = requirement["view_id"]
            slot_id = self.add_fact_slot(
                str(requirement.get("requirement_id")),
                str(metric_ref),
                period_role,
                view_id=requirement.get("view_id"),
                dimension_refs=self._fact_dimension_refs(
                    dimensions, source_dimension_refs
                ),
                component=transformed.get("field") if transformed.get("field") in {"numerator", "denominator"} else None,
                selector_dimensions=dimensions,
            )
            fact_slot_ids.append(slot_id)
            return self._bind_fact_domain(
                transformed, dimensions, source_dimension_refs
            )
        if "result" in expression:
            result = expression["result"]
            if not isinstance(result, dict):
                raise CompileError("expression result must be an object")
            requirement_ref = result.get("requirement_ref")
            if not isinstance(requirement_ref, str) or requirement_ref not in self.requirement_node_ids:
                raise CompileError(f"expression result references unknown requirement: {requirement_ref!r}")
            node_id = self.requirement_node_ids[requirement_ref]
            result_dependencies.add(node_id)
            return {"result": {"node_id": node_id, "path": result.get("path", "result")}}
        op = expression.get("op")
        args = expression.get("args")
        if op not in ALLOWED_EXPRESSION_OPS or not isinstance(args, list):
            raise CompileError(f"unsupported calculation expression operation: {op!r}")
        minimum, maximum = ALLOWED_EXPRESSION_OPS[str(op)]
        if len(args) < minimum or (maximum is not None and len(args) > maximum):
            raise CompileError(f"invalid arity for calculation expression operation: {op!r}")
        return {
            "op": op,
            "args": [
                self._transform_expression(arg, requirement, fact_slot_ids, result_dependencies)
                for arg in args
            ],
        }

    def _instantiate_registered_expression(
        self,
        template: Any,
        requirement: dict[str, Any],
        metric_ref: str,
        fact_slot_ids: list[str],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise CompileError("registered expression template must be an object")
        if "fact_role" in template:
            role = require_nonempty_string(template.get("fact_role"), "registered expression fact_role")
            metric = self.metric(metric_ref)
            composition_id = metric.get("composition_id")
            if composition_id:
                return self._instantiate_composed_metric(
                    metric_ref, composition_id, role, requirement, fact_slot_ids
                )
            slot_id = self.add_fact_slot(
                str(requirement["requirement_id"]),
                metric_ref,
                role,
                view_id=requirement.get("view_id"),
                dimension_refs=self._fact_dimension_refs(
                    requirement.get("dimensions") or {},
                    requirement.get("dimension_refs", []),
                ),
                selector_dimensions=requirement.get("dimensions") or {},
            )
            fact_slot_ids.append(slot_id)
            selector: dict[str, Any] = {"metric": metric["name"], "period_role": role}
            dimensions = requirement.get("dimensions")
            if dimensions is not None:
                if not isinstance(dimensions, dict):
                    raise CompileError("derived requirement dimensions must be an object")
            selector.update(self._fact_selector(
                metric["name"],
                view_id=requirement.get("view_id"),
                dimensions=self._expression_dimensions(dimensions or {}),
            ))
            return self._bind_fact_domain(
                selector,
                dimensions or {},
                requirement.get("dimension_refs", []),
            )
        if "literal" in template:
            return {"literal": template["literal"]}
        return {
            "op": template.get("op"),
            "args": [
                self._instantiate_registered_expression(arg, requirement, metric_ref, fact_slot_ids)
                for arg in template.get("args", [])
            ],
        }

    def _metric_ref_by_name(self, name: str) -> str:
        for metric_ref, metric in self.metrics.items():
            if metric.get("name") == name:
                return metric_ref
        metric_ref = f"auto_metric_{stable_hash(name, 10)}"
        generated = {
            "metric_id": metric_ref,
            "name": name,
            "metric_object": "volume",
            "unit": "待元信息解析",
            "definition": "待元信息解析",
            "generated_from": "metric_composition_registry",
        }
        self.metrics[metric_ref] = generated
        self.task.setdefault("metrics", []).append(generated)
        return metric_ref

    def _instantiate_composed_metric(
        self,
        metric_ref: str,
        composition_id: str,
        role: str,
        requirement: dict[str, Any],
        fact_slot_ids: list[str],
    ) -> dict[str, Any]:
        definition = (self.composition_registry.get("definitions") or {}).get(composition_id)
        if not isinstance(definition, dict):
            raise CompileError(f"unknown metric composition: {composition_id}")
        if definition.get("operator") != "divide":
            raise CompileError(f"{composition_id}.operator must be divide")
        inputs = definition.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != 2:
            raise CompileError(f"{composition_id} currently requires exactly two inputs")
        input_refs: list[str] = []
        for item in inputs:
            if not isinstance(item, dict):
                raise CompileError(f"{composition_id}.inputs must contain objects")
            input_ref = item.get("metric_ref")
            if input_ref is None:
                input_ref = self._metric_ref_by_name(require_nonempty_string(item.get("metric"), "composition input metric"))
            self.metric(input_ref)
            input_refs.append(str(input_ref))
        dimensions = requirement.get("dimensions") or {}
        if not isinstance(dimensions, dict):
            raise CompileError("derived requirement dimensions must be an object")
        expressions: list[dict[str, Any]] = []
        for input_ref in input_refs:
            base_metric = self.metric(input_ref)
            slot_id = self.add_fact_slot(
                str(requirement["requirement_id"]),
                input_ref,
                role,
                view_id=requirement.get("view_id"),
                dimension_refs=self._fact_dimension_refs(
                    dimensions, requirement.get("dimension_refs", [])
                ),
                selector_dimensions=dimensions,
            )
            fact_slot_ids.append(slot_id)
            expressions.append(self._bind_fact_domain(
                self._fact_selector(
                    base_metric["name"],
                    view_id=requirement.get("view_id"),
                    dimensions=self._expression_dimensions(dimensions),
                ) | {"period_role": role},
                dimensions,
                requirement.get("dimension_refs", []),
            ))
        return {
            "op": "divide",
            "args": expressions,
            "composition_id": composition_id,
            "composition_metric": self.metric(metric_ref)["name"],
        }

    def _instantiate_selected_set_share(
        self,
        requirement: dict[str, Any],
        metric_ref: str,
        fact_slot_ids: list[str],
    ) -> dict[str, Any]:
        dimensions = requirement.get("dimensions") or {}
        dimension_refs = requirement.get("dimension_refs") or []
        if not isinstance(dimensions, dict):
            raise CompileError("selected_set_share dimensions must be an object")
        if not isinstance(dimension_refs, list):
            raise CompileError("selected_set_share dimension_refs must be an array")
        full_source_domain = not dimensions and len(dimension_refs) == 1
        if full_source_domain:
            dimension = require_nonempty_string(
                dimension_refs[0], "selected_set_share dimension_ref"
            )
            metric = self.metric(metric_ref)
            source_dimension = str(
                (metric.get("source_dimension_bindings") or {}).get(dimension)
                or dimension
            )
            domain = None
            domain_ref = source_dimension_domain_ref(source_dimension)
        else:
            if len(dimensions) != 1:
                raise CompileError(
                    "selected_set_share requires one selected domain or one physical dimension_ref"
                )
            dimension, raw_domain = next(iter(dimensions.items()))
            if not isinstance(raw_domain, (str, list)):
                raise CompileError("selected_set_share dimension domain must be a string or array")
            domain = raw_domain if isinstance(raw_domain, list) else [raw_domain]
            if not domain:
                raise CompileError("selected_set_share dimension domain must not be empty")
            domain_ref = dimension_domain_ref(
                str(dimension), domain, self.dimension_set_registry
            )
        role = require_nonempty_string(
            (self.registry.get("definitions", {}).get("selected_set_share") or {}).get(
                "required_period_roles", ["analysis"]
            )[0],
            "selected_set_share period role",
        )
        metric = self.metric(metric_ref)
        slot_id = self.add_fact_slot(
            str(requirement["requirement_id"]),
            metric_ref,
            role,
            view_id=requirement.get("view_id"),
            dimension_refs=[str(dimension)],
            selector_dimensions={} if full_source_domain else {str(dimension): domain},
            full_dimension_domains=[str(dimension)] if full_source_domain else [],
        )
        fact_slot_ids.append(slot_id)
        selector = self._fact_selector(
            metric["name"],
            view_id=requirement.get("view_id"),
            dimensions={},
        ) | {"period_role": role}
        return {
            "op": "divide",
            "args": [
                {"fact": selector},
                {
                    "aggregate": {
                        "selector": {
                            "metric": metric["name"],
                            "view_id": requirement.get("view_id"),
                            "period_role": role,
                        },
                        "dimension": str(dimension),
                        "domain_ref": domain_ref,
                    }
                },
            ],
            "share_type": "selected_set_share",
            "denominator_domain": (
                {"kind": "source_dimension_all", "dimension": str(dimension)}
                if full_source_domain
                else domain
            ),
            "denominator_domain_ref": domain_ref,
        }

    def compile_input_adaptations(self) -> None:
        for requirement in self.ir.get("input_adaptations", []):
            requirement_id = str(requirement["requirement_id"])
            metric_ref = require_nonempty_string(
                requirement.get("metric_ref"), f"{requirement_id}.metric_ref"
            )
            metric = self.metric(metric_ref)
            target_role = require_nonempty_string(
                requirement.get("target_period_role"),
                f"{requirement_id}.target_period_role",
            )
            target_period = self.period(target_role)
            dimensions = requirement.get("dimensions") or {}
            if not isinstance(dimensions, dict):
                raise CompileError(f"{requirement_id}.dimensions must be an object")
            dimension_refs = self._fact_dimension_refs(
                dimensions, requirement.get("dimension_refs", [])
            )
            rule_source = require_nonempty_string(
                requirement.get("rule_source"), f"{requirement_id}.rule_source"
            )
            validation = requirement.get(
                "validation", ["facts_present", "unit_consistent"]
            )
            if (
                not isinstance(validation, list)
                or not validation
                or not all(isinstance(item, str) and item for item in validation)
            ):
                raise CompileError(f"{requirement_id}.validation must be a non-empty string array")
            unsupported_validation = sorted(
                set(validation) - {
                    "facts_present", "unit_consistent", "metric_additive", "unit_scale_verified"
                }
            )
            if unsupported_validation:
                raise CompileError(
                    f"{requirement_id}.validation contains unsupported checks: {unsupported_validation}"
                )
            unit_conversion = None
            if "unit_scale_verified" in validation:
                raw_conversion = requirement.get("unit_conversion")
                if not isinstance(raw_conversion, dict):
                    raise CompileError(
                        f"{requirement_id}.unit_conversion is required for unit_scale_verified"
                    )
                if raw_conversion.get("target_unit") != metric["unit"]:
                    raise CompileError(
                        f"{requirement_id}.unit_conversion.target_unit must match target metric unit"
                    )
                scale_factor = raw_conversion.get("scale_factor")
                if (
                    isinstance(scale_factor, bool)
                    or not isinstance(scale_factor, (int, float))
                    or not math.isfinite(float(scale_factor))
                    or float(scale_factor) == 0
                ):
                    raise CompileError(
                        f"{requirement_id}.unit_conversion.scale_factor must be finite and non-zero"
                    )
                expected_by_metric: dict[str, str] = {}
                expected = raw_conversion.get("expected_input_units")
                if not isinstance(expected, list) or not expected:
                    raise CompileError(
                        f"{requirement_id}.unit_conversion.expected_input_units must be non-empty"
                    )
                for index, item in enumerate(expected):
                    if not isinstance(item, dict):
                        raise CompileError(
                            f"{requirement_id}.unit_conversion.expected_input_units[{index}] must be an object"
                        )
                    input_metric = self.metric(item.get("metric_ref"))
                    unit = require_nonempty_string(
                        item.get("unit"),
                        f"{requirement_id}.unit_conversion.expected_input_units[{index}].unit",
                    )
                    metric_name = str(input_metric["name"])
                    if metric_name in expected_by_metric and expected_by_metric[metric_name] != unit:
                        raise CompileError(
                            f"{requirement_id}.unit_conversion has conflicting units for {metric_name!r}"
                        )
                    expected_by_metric[metric_name] = unit
                unit_conversion = {
                    "expected_input_units": expected_by_metric,
                    "target_unit": metric["unit"],
                    "scale_factor": float(scale_factor),
                }

            fact_slot_ids: list[str] = []
            result_dependencies: set[str] = set()
            self.compiling_input_adaptation = True
            try:
                expression = self._transform_expression(
                    requirement.get("expression"),
                    requirement,
                    fact_slot_ids,
                    result_dependencies,
                )
            finally:
                self.compiling_input_adaptation = False
            if not fact_slot_ids and not result_dependencies:
                raise CompileError(f"{requirement_id} adaptation requires facts or prior results")

            target_identity = self._adaptation_identity({
                "metric_ref": metric_ref,
                "period_role": target_role,
                "period": target_period,
                "view_id": requirement.get("view_id"),
                "dimension_refs": dimension_refs,
                "selector_dimensions": dimensions,
                "component": None,
                "scope": self.task.get("scope"),
                "filters": self.task.get("filters", []),
            })
            node_id = self.requirement_node_ids[requirement_id]
            if target_identity in self.input_adaptation_targets:
                raise CompileError(
                    f"multiple input adaptations materialize the same target: {requirement_id}"
                )
            self.input_adaptation_targets[target_identity] = node_id
            dependencies = set(result_dependencies)
            dependencies.update(self._node_dependencies(requirement_id, fact_slot_ids))
            self.nodes.append(make_node(
                node_id,
                "input_adaptation",
                [requirement_id],
                criticality(requirement),
                sorted(dependencies),
                {
                    "mode": "lightweight_executor",
                    "handler": "derived",
                    "derived_metric_id": requirement_id,
                    "definition_source": rule_source,
                    "definition_version": "input_adaptation/1.0",
                    "definition_status": "adaptation",
                    "expression": expression,
                    "formula": expression,
                    "unit": metric["unit"],
                    "metric": metric["name"],
                    "metric_object": metric["metric_object"],
                    "view_id": requirement.get("view_id"),
                    "period_roles": [target_role],
                    "group_dimensions": dimension_refs,
                    "materialize_as": {
                        "metric_ref": metric_ref,
                        "metric": metric["name"],
                        "metric_object": metric["metric_object"],
                        "period_role": target_role,
                        "period": target_period,
                        "view_id": requirement.get("view_id"),
                        "dimensions": self._expression_dimensions(dimensions),
                        "unit": metric["unit"],
                        "validation": list(validation),
                        "rule_source": rule_source,
                        **(
                            {"unit_conversion": deepcopy(unit_conversion)}
                            if unit_conversion is not None else {}
                        ),
                    },
                },
                inputs={
                    "metric_ref": metric_ref,
                    "view_id": requirement.get("view_id"),
                    "target_period_role": target_role,
                },
                outputs=["materialized_intermediate_facts"],
                quality_gate=list(validation),
            ))
            self.requirement_compilation.append({
                "requirement_id": requirement_id,
                "kind": "input_adaptation",
                "status": "compiled",
                "node_ids": [node_id],
                "fact_slot_ids": sorted(set(fact_slot_ids)),
            })

    def compile_fact_observations(self) -> None:
        for requirement in self.ir.get("fact_observations", []):
            requirement_id = str(requirement["requirement_id"])
            if self._resolution_block(requirement, "fact_observation"):
                continue
            metric_ref = require_nonempty_string(requirement.get("metric_ref"), f"{requirement_id}.metric_ref")
            roles = requirement.get("period_roles")
            if not isinstance(roles, list) or not roles:
                raise CompileError(f"{requirement_id}.period_roles must be non-empty")
            selector_dimensions = (
                requirement.get("selector_dimensions")
                or requirement.get("dimensions")
                or {}
            )
            if not isinstance(selector_dimensions, dict):
                raise CompileError(f"{requirement_id}.dimensions must be an object")
            slots = [
                self.add_fact_slot(
                    requirement_id,
                    metric_ref,
                    require_nonempty_string(role, f"{requirement_id}.period_roles"),
                    view_id=requirement.get("view_id"),
                    dimension_refs=self._fact_dimension_refs(
                        selector_dimensions, requirement.get("dimension_refs", [])
                    ),
                    selector_dimensions=selector_dimensions,
                )
                for role in roles
            ]
            dependencies = self._node_dependencies(requirement_id, slots)
            self.requirement_compilation.append({
                "requirement_id": requirement_id,
                "kind": "fact_observation",
                "status": "compiled",
                "node_ids": dependencies or ["fact_artifact"],
                "fact_slot_ids": sorted(set(slots)),
            })

    def compile_metric_compositions(self) -> None:
        for requirement in self.ir.get("metric_compositions", []):
            requirement_id = str(requirement["requirement_id"])
            if self._resolution_block(requirement, "metric_composition"):
                continue
            if self._block_apply_to(requirement, "metric_composition"):
                continue
            metric_ref = require_nonempty_string(
                requirement.get("metric_ref"), f"{requirement_id}.metric_ref"
            )
            metric = self.metric(metric_ref)
            composition_id = require_nonempty_string(
                requirement.get("composition_id") or metric.get("composition_id"),
                f"{requirement_id}.composition_id",
            )
            composition_definition = (
                self.composition_registry.get("definitions", {}).get(composition_id) or {}
            )
            if composition_definition.get("metric_object") != metric.get("metric_object"):
                raise CompileError(
                    f"{requirement_id} metric_object conflicts with {composition_id} definition"
                )
            roles = requirement.get("period_roles")
            if not isinstance(roles, list) or len(roles) != 1:
                raise CompileError(
                    f"{requirement_id}.period_roles must contain exactly one role; "
                    "create one composition requirement per period"
                )
            role = require_nonempty_string(roles[0], f"{requirement_id}.period_roles")
            self.period(role)
            fact_slot_ids: list[str] = []
            expression = self._instantiate_composed_metric(
                metric_ref, composition_id, role, requirement, fact_slot_ids
            )
            node_id = self.requirement_node_ids[requirement_id]
            self.nodes.append(make_node(
                node_id,
                "metric_composition",
                [requirement_id],
                criticality(requirement),
                self._node_dependencies(requirement_id, fact_slot_ids),
                {
                    "mode": "lightweight_executor",
                    "handler": "derived",
                    "derived_metric_id": composition_id,
                    "definition_source": "references/metric-composition-registry.json",
                    "definition_version": composition_definition.get("definition_version"),
                    "definition_status": "registered",
                    "expression": expression,
                    "formula": expression,
                    "unit": composition_definition.get("unit") or metric.get("unit", "rate"),
                    "metric": metric.get("name"),
                    "metric_object": metric.get("metric_object", "ratio"),
                    "view_id": requirement.get("view_id"),
                    "period_roles": roles,
                    "composition_id": composition_id,
                    "apply_to": requirement.get("apply_to", []),
                    "group_dimensions": requirement.get("dimension_refs", []),
                },
                inputs={"metric_ref": metric_ref, "view_id": requirement.get("view_id")},
                outputs=list(requirement.get("required_outputs", [composition_id])),
                quality_gate=list(
                    composition_definition.get(
                        "minimal_validation", ["facts_present", "denominator_nonzero"]
                    )
                ),
            ))
            self.requirement_compilation.append({
                "requirement_id": requirement_id,
                "kind": "metric_composition",
                "status": "compiled",
                "node_ids": [node_id],
                "fact_slot_ids": sorted(set(fact_slot_ids)),
            })

    def compile_derived(self) -> None:
        definitions = self.registry.get("definitions")
        if not isinstance(definitions, dict):
            raise CompileError("derived registry definitions must be an object")
        for requirement in self.ir.get("derived_requirements", []):
            requirement_id = str(requirement["requirement_id"])
            if self._resolution_block(requirement, "derived_metric"):
                continue
            if self._block_apply_to(requirement, "derived_metric"):
                continue
            definition_status = requirement.get("definition_status", "registered")
            metric_ref = require_nonempty_string(requirement.get("metric_ref"), f"{requirement_id}.metric_ref")
            metric = self.metric(metric_ref)
            metric_object = requirement.get("metric_object", metric["metric_object"])
            if metric_object != metric.get("metric_object"):
                raise CompileError(
                    f"{requirement_id}.metric_object conflicts with metric declaration"
                )
            fact_slot_ids: list[str] = []
            result_dependencies: set[str] = set()
            if requirement.get("fulfillment_mode") == "source_derived_fact":
                derived_metric_id = require_nonempty_string(
                    requirement.get("derived_metric_id"), f"{requirement_id}.derived_metric_id"
                )
                if not isinstance(definitions.get(derived_metric_id), dict):
                    raise CompileError(f"unregistered derived_metric_id: {derived_metric_id}")
                source_metric_ref = require_nonempty_string(
                    requirement.get("source_metric_ref"), f"{requirement_id}.source_metric_ref"
                )
                source_metric = self.metric(source_metric_ref)
                source_role = require_nonempty_string(
                    requirement.get("source_period_role"), f"{requirement_id}.source_period_role"
                )
                dimensions = requirement.get("dimensions") or {}
                slot_id = self.add_fact_slot(
                    requirement_id,
                    source_metric_ref,
                    source_role,
                    view_id=requirement.get("view_id"),
                    dimension_refs=self._fact_dimension_refs(
                        dimensions, requirement.get("dimension_refs", [])
                    ),
                    selector_dimensions=dimensions,
                )
                fact_slot_ids.append(slot_id)
                expression = self._bind_fact_domain(
                    self._fact_selector(
                        source_metric["name"],
                        view_id=requirement.get("view_id"),
                        dimensions=self._expression_dimensions(dimensions),
                    ) | {"period_role": source_role},
                    dimensions,
                    requirement.get("dimension_refs", []),
                )
                intermediate_expressions = {}
                unit = source_metric["unit"]
                definition_source = "source_metric_metadata"
                definition_version = "source_precomputed/1.0"
                definition_status = "source_precomputed"
                inference_basis = None
                kind = "source_precomputed_derived"
                quality_gate = ["facts_present", "unit_consistent"]
                period_roles = [source_role]
            elif definition_status == "registered":
                derived_metric_id = require_nonempty_string(
                    requirement.get("derived_metric_id"), f"{requirement_id}.derived_metric_id"
                )
                definition = definitions.get(derived_metric_id)
                if not isinstance(definition, dict):
                    raise CompileError(f"unregistered derived_metric_id: {derived_metric_id}")
                if metric_object not in definition.get("metric_objects", []):
                    raise CompileError(f"{derived_metric_id} does not support metric_object={metric_object}")
                for role in definition.get("required_period_roles", []):
                    self.period(str(role))
                if definition.get("operator") == "selected_set_share":
                    expression = self._instantiate_selected_set_share(
                        requirement, metric_ref, fact_slot_ids
                    )
                else:
                    expression_template = (definition.get("expressions") or {}).get(metric_object)
                    expression = self._instantiate_registered_expression(
                        expression_template, requirement, metric_ref, fact_slot_ids
                    )
                intermediate_expressions: dict[str, Any] = {}
                intermediate_templates = (definition.get("intermediate_expressions") or {}).get(metric_object, {})
                if not isinstance(intermediate_templates, dict):
                    raise CompileError(f"{derived_metric_id}.intermediate_expressions must be an object")
                for name, template in intermediate_templates.items():
                    intermediate_expressions[str(name)] = self._instantiate_registered_expression(
                        template, requirement, metric_ref, fact_slot_ids
                    )
                unit = (definition.get("output_units") or {}).get(metric_object)
                if unit == "metric_unit":
                    unit = metric["unit"]
                definition_source = "references/derived-metric-registry.json"
                definition_version = definition.get("definition_version")
                inference_basis = None
                kind = "registered_derived"
                quality_gate = list(definition.get("minimal_validation", []))
                period_roles = list(definition.get("required_period_roles", []))
            elif definition_status == "inferred":
                derived_metric_id = require_nonempty_string(
                    requirement.get("derived_metric_id"), f"{requirement_id}.derived_metric_id"
                )
                definition = requirement.get("definition")
                if not isinstance(definition, dict):
                    raise CompileError(f"{requirement_id}.definition must be an object for inferred derived")
                inference_basis = require_nonempty_string(
                    requirement.get("inference_basis"), f"{requirement_id}.inference_basis"
                )
                expression = self._transform_expression(
                    definition.get("expression"), requirement, fact_slot_ids, result_dependencies
                )
                unit = require_nonempty_string(definition.get("unit"), f"{requirement_id}.definition.unit")
                definition_source = "query_inference"
                definition_version = "inferred/1.0"
                kind = "inferred_derived"
                quality_gate = list(definition.get("minimal_validation", ["facts_present", "unit_consistent"]))
                intermediate_expressions = {}
                period_roles = list(requirement.get("required_period_roles", []))
            else:
                raise CompileError(f"{requirement_id}.definition_status must be registered or inferred")

            node_id = self.requirement_node_ids[requirement_id]
            depends_on = sorted(
                set(result_dependencies)
                | set(self._node_dependencies(requirement_id, fact_slot_ids))
            )
            execution = {
                "mode": "lightweight_executor",
                "handler": "derived",
                "derived_metric_id": derived_metric_id,
                "definition_source": definition_source,
                "definition_version": definition_version,
                "definition_status": definition_status,
                "expression": expression,
                "formula": expression,
                "unit": unit,
                "metric": metric["name"],
                "metric_object": metric_object,
                "view_id": requirement.get("view_id"),
                "period_roles": period_roles,
                "apply_to": requirement.get("apply_to", []),
                "group_dimensions": requirement.get("dimension_refs", []),
            }
            if intermediate_expressions:
                execution["intermediate_expressions"] = intermediate_expressions
            if inference_basis is not None:
                execution["inference_basis"] = inference_basis
            self.nodes.append(make_node(
                node_id,
                "derived_metric",
                [requirement_id],
                criticality(requirement),
                depends_on,
                execution,
                inputs={
                    "metric_ref": metric_ref,
                    "view_id": requirement.get("view_id"),
                    "apply_to": requirement.get("apply_to", []),
                },
                outputs=list(requirement.get("required_outputs", [derived_metric_id])),
                quality_gate=quality_gate,
            ))
            self.requirement_compilation.append({
                "requirement_id": requirement_id,
                "kind": kind,
                "status": "compiled",
                "node_ids": [node_id],
                "fact_slot_ids": sorted(set(fact_slot_ids)),
            })

    def compile_custom_calculations(self) -> None:
        for requirement in self.ir.get("custom_calculations", []):
            requirement_id = str(requirement["requirement_id"])
            if self._resolution_block(requirement, "custom_calculation"):
                continue
            if self._block_apply_to(requirement, "custom_calculation"):
                continue
            if requirement.get("definition_source") != "user_query":
                raise CompileError(f"{requirement_id}.definition_source must be user_query")
            fact_slot_ids: list[str] = []
            result_dependencies: set[str] = set()
            expression = self._transform_expression(
                requirement.get("expression"), requirement, fact_slot_ids, result_dependencies
            )
            node_id = self.requirement_node_ids[requirement_id]
            depends_on = sorted(
                set(result_dependencies)
                | set(self._node_dependencies(requirement_id, fact_slot_ids))
            )
            unit = require_nonempty_string(requirement.get("unit"), f"{requirement_id}.unit")
            self.nodes.append(make_node(
                node_id,
                "custom_calculation",
                [requirement_id],
                criticality(requirement),
                depends_on,
                {
                    "mode": "lightweight_executor",
                    "handler": "derived",
                    "derived_metric_id": requirement_id,
                    "definition_source": "user_query",
                    "definition_version": "user_query/1.0",
                    "definition_status": "custom",
                    "expression": expression,
                    "formula": expression,
                    "unit": unit,
                    "view_id": requirement.get("view_id"),
                    "period_roles": list(requirement.get("period_roles", [])),
                },
                inputs={
                    "view_id": requirement.get("view_id"),
                    "apply_to": requirement.get("apply_to", []),
                },
                outputs=list(requirement.get("required_outputs", [requirement_id])),
                quality_gate=list(requirement.get("quality_gate", ["facts_present", "unit_consistent"])),
            ))
            self.requirement_compilation.append({
                "requirement_id": requirement_id,
                "kind": "custom_calculation",
                "status": "compiled",
                "node_ids": [node_id],
                "fact_slot_ids": sorted(set(fact_slot_ids)),
            })

    def resolve_operator(self, target: dict[str, Any]) -> dict[str, Any]:
        supplied = target.get("operator_contract")
        if supplied is not None:
            if not isinstance(supplied, dict):
                raise CompileError("operator_contract must be an object")
            supplied_identity = supplied.get("engine_identity")
            if not isinstance(supplied_identity, dict):
                raise CompileError("supplied operator_contract requires embedded engine_identity")
            if self.attribution_engine_identity is not None and supplied_identity != self.attribution_engine_identity:
                raise CompileError("supplied operator_contract engine_identity conflicts with another target")
            self.attribution_engine_identity = deepcopy(supplied_identity)
            return deepcopy(supplied)
        scenario = require_nonempty_string(target.get("scenario"), "attribution target scenario")
        metric_object = require_nonempty_string(target.get("metric_object"), "attribution target metric_object")
        decomposition = require_nonempty_string(target.get("decomposition"), "attribution target decomposition")
        cache_key = (scenario, metric_object, decomposition)
        if cache_key in self.operator_cache:
            return deepcopy(self.operator_cache[cache_key])
        started = time.perf_counter()
        try:
            from _vendor.attribution_core import query_operator

            contract = query_operator({
                "scenario": scenario,
                "metric_object": metric_object,
                "decomposition": decomposition,
            })
        except Exception as exc:  # noqa: BLE001 - unresolved capability becomes a blocked node.
            contract = {"supported": False, "error": str(exc)}
        if not isinstance(contract, dict):
            contract = {"supported": False, "error": "embedded query_operator response is not an object"}
        engine_identity = contract.get("engine_identity")
        if isinstance(engine_identity, dict):
            self.attribution_engine_identity = deepcopy(engine_identity)
        self.operator_resolution_ms += (time.perf_counter() - started) * 1000
        self.operator_cache[cache_key] = deepcopy(contract)
        return contract

    @staticmethod
    def _merge_factor_dimensions(
        target_dimensions: Any,
        factor_dimensions: Any,
        path: str,
    ) -> dict[str, Any]:
        if target_dimensions is None:
            target_dimensions = {}
        if factor_dimensions is None:
            factor_dimensions = {}
        if not isinstance(target_dimensions, dict) or not isinstance(factor_dimensions, dict):
            raise CompileError(f"{path}.dimensions and target dimensions must be objects")
        merged = deepcopy(target_dimensions)
        for dimension, value in factor_dimensions.items():
            if dimension in merged and canonical_json(merged[dimension]) != canonical_json(value):
                raise CompileError(
                    f"{path}.dimensions conflicts with target dimensions for {dimension!r}"
                )
            merged[str(dimension)] = deepcopy(value)
        return merged

    @staticmethod
    def _formula_factor_refs(expression: Any, inverted: bool = False) -> list[tuple[str, str]]:
        if not isinstance(expression, dict):
            raise CompileError("attribution formula must be an object")
        if "factor_ref" in expression:
            factor_ref = require_nonempty_string(
                expression.get("factor_ref"), "attribution formula.factor_ref"
            )
            return [(factor_ref, "denominator" if inverted else "multiplier")]
        if "literal" in expression:
            value = expression["literal"]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise CompileError("attribution formula literal must be numeric")
            return []
        op = expression.get("op")
        args = expression.get("args")
        if op not in {"multiply", "divide"} or not isinstance(args, list):
            raise CompileError(
                "FORMULA_SHAPE_UNSUPPORTED: formula attribution supports multiply/divide only"
            )
        if (op == "multiply" and not args) or (op == "divide" and len(args) != 2):
            raise CompileError(f"invalid attribution formula arity for {op!r}")
        refs: list[tuple[str, str]] = []
        for index, arg in enumerate(args):
            refs.extend(
                Compiler._formula_factor_refs(
                    arg,
                    inverted=(not inverted if op == "divide" and index == 1 else inverted),
                )
            )
        return refs

    @staticmethod
    def _formula_from_factor_roles(factors: list[dict[str, Any]]) -> dict[str, Any]:
        numerator = [
            {"factor_ref": factor["factor_id"]}
            for factor in factors
            if factor["role"] in {"multiplier", "numerator"}
        ]
        denominator = [
            {"factor_ref": factor["factor_id"]}
            for factor in factors
            if factor["role"] in {"denominator", "divisor"}
        ]
        if not numerator:
            raise CompileError("attribution formula requires at least one numerator/multiplier factor")

        def product(items: list[dict[str, Any]]) -> dict[str, Any]:
            return items[0] if len(items) == 1 else {"op": "multiply", "args": items}

        expression = product(numerator)
        if denominator:
            expression = {"op": "divide", "args": [expression, product(denominator)]}
        return expression

    def _normalize_formula_target(self, target: dict[str, Any]) -> dict[str, Any]:
        raw_factors = target.get("factors")
        if not isinstance(raw_factors, list) or not raw_factors:
            return deepcopy(target)
        scenario = str(target.get("scenario"))
        roles = SCENARIO_ROLES.get(scenario)
        if roles is None:
            raise CompileError(f"unsupported attribution scenario: {scenario}")
        normalized = deepcopy(target)
        target_dimensions = target.get("dimensions") or {}
        if not isinstance(target_dimensions, dict):
            raise CompileError("attribution target dimensions must be an object")
        factors: list[dict[str, Any]] = []
        factor_ids: set[str] = set()
        for index, raw in enumerate(raw_factors):
            path = f"{target.get('target_id')}.factors[{index}]"
            if not isinstance(raw, dict):
                raise CompileError(f"{path} must be an object")
            kind = raw.get("kind")
            if kind is None:
                if raw.get("metric_ref") is not None:
                    kind = "metric"
                elif "values_by_period_role" in raw or "literal" in raw:
                    kind = "literal"
                elif "expressions_by_period_role" in raw:
                    kind = "derived"
            if kind not in {"metric", "literal", "derived"}:
                raise CompileError(f"{path}.kind must be metric, literal, or derived")
            factor_id = raw.get("factor_id")
            if factor_id is None:
                factor_id = f"factor_{index + 1}_{stable_hash(raw, 8)}"
            factor_id = require_nonempty_string(factor_id, f"{path}.factor_id")
            if factor_id in factor_ids:
                raise CompileError(f"duplicate attribution factor_id: {factor_id}")
            factor_ids.add(factor_id)
            factor = {
                "factor_id": factor_id,
                "kind": kind,
                "name": str(raw.get("name") or factor_id),
                "role": str(raw.get("role") or "multiplier"),
            }
            if factor["role"] not in {"multiplier", "numerator", "denominator", "divisor"}:
                raise CompileError(f"{path}.role is invalid")
            if "sign" in raw:
                factor["sign"] = raw["sign"]
            if kind == "metric":
                metric_ref = require_nonempty_string(raw.get("metric_ref"), f"{path}.metric_ref")
                metric = self.metric(metric_ref)
                factor.update({
                    "metric_ref": metric_ref,
                    "name": str(raw.get("name") or metric["name"]),
                    "dimensions": self._merge_factor_dimensions(
                        target_dimensions, raw.get("dimensions"), path
                    ),
                })
            elif kind == "literal":
                values = raw.get("values_by_period_role")
                if values is None and "literal" in raw:
                    values = {role: raw["literal"] for role in roles}
                if not isinstance(values, dict):
                    raise CompileError(f"{path}.values_by_period_role must be an object")
                parsed: dict[str, float] = {}
                for role in roles:
                    value = values.get(role)
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        raise CompileError(f"{path}.values_by_period_role.{role} must be numeric")
                    parsed[role] = float(value)
                factor["values_by_period_role"] = parsed
            else:
                expressions = raw.get("expressions_by_period_role")
                if not isinstance(expressions, dict):
                    raise CompileError(f"{path}.expressions_by_period_role must be an object")
                missing = [role for role in roles if not isinstance(expressions.get(role), dict)]
                if missing:
                    raise CompileError(f"{path}.expressions_by_period_role is missing roles: {missing}")
                factor.update({
                    "expressions_by_period_role": deepcopy(expressions),
                    "dimensions": self._merge_factor_dimensions(
                        target_dimensions, raw.get("dimensions"), path
                    ),
                })
            factors.append(factor)

        formula = deepcopy(target.get("formula"))
        if formula is None:
            formula = self._formula_from_factor_roles(factors)
        refs = self._formula_factor_refs(formula)
        ref_ids = [factor_ref for factor_ref, _ in refs]
        if len(ref_ids) != len(set(ref_ids)):
            raise CompileError("attribution formula must reference every factor exactly once")
        if set(ref_ids) != factor_ids:
            raise CompileError(
                "attribution formula factor set must exactly match attribution factors"
            )
        role_by_id = dict(refs)
        for index, factor in enumerate(factors):
            formula_role = role_by_id[factor["factor_id"]]
            declared_role = factor["role"]
            if declared_role in {"denominator", "divisor"} and formula_role != "denominator":
                raise CompileError(f"factor {factor['factor_id']} role conflicts with formula position")
            if declared_role in {"multiplier", "numerator"} and formula_role == "denominator" and "role" in raw_factors[index]:
                raise CompileError(f"factor {factor['factor_id']} role conflicts with formula position")
            factor["role"] = formula_role
        formula_shape = "division" if any(role == "denominator" for _, role in refs) else "multiplication"
        declared_decomposition = target.get("decomposition")
        if declared_decomposition not in (None, "formula", formula_shape):
            raise CompileError(
                f"attribution decomposition {declared_decomposition!r} conflicts with formula shape {formula_shape!r}"
            )
        normalized.update({
            "decomposition": formula_shape,
            "factors": factors,
            "formula": formula,
            "formula_shape": formula_shape,
            "factor_order": [factor["factor_id"] for factor in factors],
            "formula_fingerprint": stable_hash(formula, 32),
        })
        return normalized

    def _resolve_target_semantics(
        self,
        target_id: str,
        stack: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if target_id in self.resolved_target_semantics:
            return deepcopy(self.resolved_target_semantics[target_id])
        target = self.ir_target_by_id(target_id)
        metric_ref = target.get("metric_ref", target.get("metric"))
        metric = self.metric(metric_ref)
        warnings: list[str] = []
        supplied = target.get("metric_semantics")
        if supplied is not None and not isinstance(supplied, dict):
            warnings.append("metric_semantics must be an object; ignored")
            supplied = {}
        supplied = deepcopy(supplied or {})
        supplied.setdefault("metric_id", metric.get("metric_id", str(metric_ref)))
        for key in ("direction", "direction_source", "direction_confidence"):
            if key in target:
                supplied[key] = target[key]
            elif key not in supplied and key in metric:
                supplied[key] = metric[key]

        parent_ref = target.get("parent_target_ref")
        parent_semantics: dict[str, Any] = {}
        if isinstance(parent_ref, str) and parent_ref:
            if parent_ref in stack or parent_ref == target_id:
                warnings.append("parent_target_ref cycle detected; direction left unknown")
            elif any(item.get("target_id") == parent_ref for item in self.original_attribution_targets):
                parent_semantics = self._resolve_target_semantics(parent_ref, stack + (target_id,))
            else:
                warnings.append(f"unknown parent_target_ref {parent_ref!r}; direction left unknown")

        relation = target.get("relation_to_parent")
        if relation is not None and not isinstance(relation, dict):
            warnings.append("relation_to_parent must be an object; ignored")
            relation = {}
        try:
            from _vendor.attribution_core.semantics import resolve_metric_semantics

            semantics, core_warnings = resolve_metric_semantics({
                "metric_semantics": supplied,
                "parent_metric_semantics": parent_semantics,
                "relation_to_parent": relation or {},
            })
            warnings.extend(core_warnings)
        except Exception as exc:  # noqa: BLE001 - semantic enhancement must not block attribution.
            semantics = {
                "metric_id": str(supplied.get("metric_id", "")),
                "direction": "unknown",
                "direction_source": "unknown",
                "direction_confidence": "unknown",
            }
            warnings.append(f"metric semantics could not be resolved: {exc}")
        self.resolved_target_semantics[target_id] = semantics
        self.target_semantic_warnings[target_id] = warnings
        return deepcopy(semantics)

    def _normalize_target_ranking(self, target: dict[str, Any]) -> dict[str, Any] | None:
        target_id = str(target["target_id"])
        if target_id in self.normalized_target_rankings:
            return deepcopy(self.normalized_target_rankings[target_id])
        try:
            from _vendor.attribution_core.semantics import normalize_ranking

            ranking, warnings = normalize_ranking(target.get("ranking"))
        except Exception as exc:  # noqa: BLE001 - ranking is a non-blocking presentation view.
            ranking, warnings = None, [f"ranking could not be normalized: {exc}"]
        self.target_semantic_warnings.setdefault(target_id, []).extend(warnings)
        self.normalized_target_rankings[target_id] = ranking
        return deepcopy(ranking)

    def _decorate_attribution_binding(
        self,
        binding: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, Any]:
        target_id = str(target["target_id"])
        binding["metric_semantics"] = self._resolve_target_semantics(target_id)
        parent_ref = target.get("parent_target_ref")
        if isinstance(parent_ref, str) and parent_ref in self.resolved_target_semantics:
            binding["parent_metric_semantics"] = deepcopy(self.resolved_target_semantics[parent_ref])
        relation = target.get("relation_to_parent")
        if isinstance(relation, dict):
            binding["relation_to_parent"] = deepcopy(relation)
        ranking = self._normalize_target_ranking(target)
        if ranking is not None:
            binding["ranking"] = ranking
        warnings = self.target_semantic_warnings.get(target_id, [])
        if warnings:
            binding["semantic_warnings"] = list(warnings)
        return binding

    @staticmethod
    def _validate_supplied_formula_binding(
        supplied: dict[str, Any],
        canonical: dict[str, Any],
    ) -> None:
        for key in ("scenario", "metric_object", "decomposition", "periods", "metric"):
            if key in supplied and canonical_json(supplied[key]) != canonical_json(canonical.get(key)):
                raise CompileError(
                    f"supplied formula binding.{key} must match compiler-generated binding"
                )

        supplied_factors = supplied.get("factors")
        if supplied_factors is None:
            return
        canonical_factors = canonical.get("factors")
        if not isinstance(supplied_factors, list) or not isinstance(canonical_factors, list):
            raise CompileError("supplied formula binding factors must be an array")
        if len(supplied_factors) != len(canonical_factors):
            raise CompileError("supplied binding factors must exactly match target factors")

        source_keys = {"selector", "values_by_period_role", "expressions_by_period_role", "literal"}
        for index, (bound, expected) in enumerate(zip(supplied_factors, canonical_factors)):
            if not isinstance(bound, dict):
                raise CompileError(f"supplied binding factors[{index}] must be an object")
            if bound.get("factor_id") != expected["factor_id"]:
                raise CompileError(
                    f"supplied binding factors[{index}].factor_id must match target"
                )
            for key in ("kind", "name", "role"):
                if key in bound and canonical_json(bound[key]) != canonical_json(expected.get(key)):
                    raise CompileError(
                        f"supplied binding factors[{index}].{key} must match target factor"
                    )

            expected_source = {
                key: expected[key]
                for key in source_keys
                if key in expected
            }
            supplied_source = {
                key: bound[key]
                for key in source_keys
                if key in bound
            }
            if canonical_json(supplied_source) != canonical_json(expected_source):
                raise CompileError(
                    f"supplied binding factors[{index}] source must match target factor"
                )

    def _attribution_binding(
        self, target: dict[str, Any], metric: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str], set[str]]:
        supplied = target.get("binding")
        supplied_formula_binding: dict[str, Any] | None = None
        if supplied is not None:
            if not isinstance(supplied, dict):
                raise CompileError("attribution binding must be an object")
            if isinstance(target.get("factors"), list) and target["factors"]:
                supplied_formula_binding = deepcopy(supplied)
            else:
                binding = deepcopy(supplied)
                if binding.get("metric_object") == "ratio" and isinstance(binding.get("groups"), dict):
                    sparse_policy = validate_sparse_policy(binding.get("sparse_policy"))
                    binding["sparse_policy"] = {
                        "strategy": SPARSE_STRATEGY,
                        "other_name": "其他/未覆盖",
                        "epsilon": 1e-9,
                        "reference_rate_policy": SPARSE_REFERENCE_RATE_POLICY,
                        "structural_absence_is_zero": True,
                        "approximation_note_required": True,
                        **deepcopy(sparse_policy),
                    }
                    parent_dimensions = self._attribution_parent_dimensions(target)
                    if parent_dimensions and "parent_dimensions" not in binding["sparse_policy"]:
                        binding["sparse_policy"]["parent_dimensions"] = parent_dimensions
                return self._decorate_attribution_binding(binding, target), [], set()
        scenario = str(target["scenario"])
        roles = SCENARIO_ROLES.get(scenario)
        if roles is None:
            raise CompileError(f"unsupported attribution scenario: {scenario}")
        periods = target.get("periods", self.periods)
        if not isinstance(periods, dict):
            raise CompileError("attribution target periods must be an object")
        view_id = target.get("view_id")
        group_dimensions = target.get("group_dimensions")
        has_groups = isinstance(group_dimensions, list) and bool(group_dimensions)
        coverage = target.get("coverage")
        if coverage is not None and not isinstance(coverage, dict):
            raise CompileError("attribution coverage must be an object")
        auto_residual = isinstance(coverage, dict) and coverage.get("mode") == "auto_residual"
        include_overall = (
            not has_groups
            or target.get("partial_coverage") is True
            or target.get("include_overall_metric") is True
            or auto_residual
            or (isinstance(coverage, dict) and isinstance(coverage.get("parent_selector"), dict))
        )
        binding: dict[str, Any] = {
            "scenario": scenario,
            "metric_object": target["metric_object"],
            "decomposition": target["decomposition"],
            "periods": {role: require_nonempty_string(periods.get(role), f"target.periods.{role}") for role in roles},
        }
        dimensions = target.get("dimensions") or {}
        if not isinstance(dimensions, dict):
            raise CompileError("attribution target dimensions must be an object")
        selector_dimensions = self._expression_dimensions(dimensions)
        if include_overall:
            parent_selector = coverage.get("parent_selector") if isinstance(coverage, dict) else None
            binding["metric"] = {
                "name": metric["name"],
                "selector": deepcopy(parent_selector) if isinstance(parent_selector, dict) else self._fact_selector(
                    metric["name"], view_id=view_id, dimensions=selector_dimensions
                ),
            }
        factors = target.get("factors")
        factor_slots: list[str] = []
        result_dependencies: set[str] = set()
        if isinstance(factors, list) and factors:
            binding["factors"] = []
            for factor in factors:
                if not isinstance(factor, dict):
                    raise CompileError("attribution factors must contain objects")
                item = {
                    "factor_id": factor["factor_id"],
                    "kind": factor["kind"],
                    "name": factor["name"],
                    "role": factor["role"],
                }
                if "sign" in factor:
                    item["sign"] = factor["sign"]
                if factor["kind"] == "metric":
                    factor_metric = self.metric(factor["metric_ref"])
                    factor_dimensions = factor.get("dimensions") or {}
                    item["selector"] = self._fact_selector(
                        factor_metric["name"],
                        view_id=view_id,
                        dimensions=self._expression_dimensions(factor_dimensions),
                    )
                elif factor["kind"] == "literal":
                    item["values_by_period_role"] = deepcopy(
                        factor["values_by_period_role"]
                    )
                else:
                    compiled_expressions: dict[str, dict[str, Any]] = {}
                    factor_requirement = {
                        "requirement_id": target["target_id"],
                        "view_id": view_id,
                        "dimensions": deepcopy(factor.get("dimensions") or {}),
                        "dimension_refs": self._fact_dimension_refs(
                            factor.get("dimensions") or {},
                            target.get("dimension_refs", []),
                        ),
                    }
                    for role in roles:
                        compiled_expressions[role] = self._transform_expression(
                            factor["expressions_by_period_role"][role],
                            factor_requirement,
                            factor_slots,
                            result_dependencies,
                        )
                    item["expressions_by_period_role"] = compiled_expressions
                binding["factors"].append(item)
            binding.update({
                "formula": deepcopy(target["formula"]),
                "formula_shape": target["formula_shape"],
                "factor_order": list(target["factor_order"]),
                "formula_fingerprint": target["formula_fingerprint"],
            })
        if has_groups:
            binding["groups"] = {
                "selector": self._fact_selector(
                    metric["name"], view_id=view_id, dimensions=selector_dimensions
                ),
                "group_dimensions": group_dimensions,
            }
            if target.get("metric_object") == "ratio":
                sparse_policy = validate_sparse_policy(target.get("sparse_policy"))
                binding["sparse_policy"] = {
                    "strategy": SPARSE_STRATEGY,
                    "other_name": (
                        coverage.get("residual_name", "其他/未覆盖")
                        if isinstance(coverage, dict)
                        else "其他/未覆盖"
                    ),
                    "epsilon": 1e-9,
                    "reference_rate_policy": SPARSE_REFERENCE_RATE_POLICY,
                    "structural_absence_is_zero": True,
                    "approximation_note_required": True,
                    **deepcopy(sparse_policy),
                }
                parent_dimensions = self._attribution_parent_dimensions(target)
                if parent_dimensions and "parent_dimensions" not in binding["sparse_policy"]:
                    binding["sparse_policy"]["parent_dimensions"] = parent_dimensions
        for key in ("partial_coverage", "sparse_strategy", "epsilon", "reference_rate_policy", "coverage"):
            if key in target:
                binding[key] = deepcopy(target[key])
        if "factors" not in binding and "groups" not in binding:
            raise CompileError(f"attribution target {target['target_id']} requires factors, group_dimensions, or binding")
        if supplied_formula_binding is not None:
            self._validate_supplied_formula_binding(supplied_formula_binding, binding)
        return (
            self._decorate_attribution_binding(binding, target),
            sorted(set(factor_slots)),
            result_dependencies,
        )

    def _attribution_fact_slots(self, target: dict[str, Any], metric_ref: str) -> list[str]:
        target_id = str(target["target_id"])
        roles = SCENARIO_ROLES.get(str(target.get("scenario")))
        if roles is None:
            raise CompileError(f"unsupported attribution scenario: {target.get('scenario')}")
        slots: list[str] = []
        group_dimensions = target.get("group_dimensions", [])
        has_groups = isinstance(group_dimensions, list) and bool(group_dimensions)
        parent_dimensions = self._attribution_parent_dimensions(target)
        dimensions = target.get("dimensions") or {}
        if not isinstance(dimensions, dict):
            raise CompileError("attribution target dimensions must be an object")
        selector_dimension_refs = self._fact_dimension_refs(dimensions, parent_dimensions)
        include_overall = (
            not has_groups
            or target.get("partial_coverage") is True
            or target.get("include_overall_metric") is True
            or (isinstance(target.get("coverage"), dict) and (
                target["coverage"].get("mode") == "auto_residual"
                or isinstance(target["coverage"].get("parent_selector"), dict)
            ))
        )
        if has_groups:
            components = ("numerator", "denominator") if target.get("metric_object") == "ratio" else (None,)
            for role in roles:
                for component in components:
                    slots.append(self.add_fact_slot(
                        target_id,
                        metric_ref,
                        role,
                        view_id=target.get("view_id"),
                        dimension_refs=selector_dimension_refs + group_dimensions,
                        component=component,
                        selector_dimensions=dimensions,
                    ))
            if include_overall:
                for role in roles:
                    for component in components:
                        slots.append(self.add_fact_slot(
                            target_id,
                            metric_ref,
                            role,
                            view_id=target.get("view_id"),
                            dimension_refs=selector_dimension_refs,
                            component=component,
                            selector_dimensions=dimensions,
                        ))
        else:
            for role in roles:
                slots.append(self.add_fact_slot(
                    target_id,
                    metric_ref,
                    role,
                    view_id=target.get("view_id"),
                    dimension_refs=selector_dimension_refs,
                    selector_dimensions=dimensions,
                ))
        for factor in target.get("factors", []) if isinstance(target.get("factors"), list) else []:
            if not isinstance(factor, dict) or factor.get("kind") != "metric":
                continue
            factor_ref = require_nonempty_string(factor.get("metric_ref"), f"{target_id}.factors.metric_ref")
            factor_dimensions = factor.get("dimensions") or {}
            for role in roles:
                slots.append(self.add_fact_slot(
                    target_id,
                    factor_ref,
                    role,
                    view_id=target.get("view_id"),
                    dimension_refs=self._fact_dimension_refs(
                        factor_dimensions, parent_dimensions
                    ),
                    selector_dimensions=factor_dimensions,
                ))
        return sorted(set(slots))

    def compile_attribution(self) -> None:
        for raw_target in self.ir.get("attribution_targets", []):
            target = self._normalize_formula_target(raw_target)
            target_id = str(target["target_id"])
            original_target = self.ir_target_by_id(target_id)
            if self._resolution_block(original_target, "attribution"):
                continue
            metric_ref = target.get("metric_ref", target.get("metric"))
            metric = self.metric(metric_ref)
            if target.get("metric_object") != metric.get("metric_object"):
                raise CompileError(
                    f"{target_id}.metric_object conflicts with metric declaration"
                )
            contract = self.resolve_operator(target)
            query_id = f"operator_query_{stable_hash([target.get('scenario'), target.get('metric_object'), target.get('decomposition')], 12)}"
            contract["query_id"] = query_id
            contract_identity = contract.get("engine_identity")
            identity_name = contract_identity.get("name") if isinstance(contract_identity, dict) else None
            if identity_name:
                contract.setdefault("contract_source", identity_name)
            if query_id not in self.operator_query_ids:
                self.operator_query_ids.add(query_id)
                self.operator_contracts.append(contract)
                self.operator_queries.append({
                    "query_id": query_id,
                    "scenario": target.get("scenario"),
                    "metric_object": target.get("metric_object"),
                    "decomposition": target.get("decomposition"),
                })
            normalized_target = {
                "target_id": target_id,
                "metric": metric["name"],
                "metric_object": target.get("metric_object"),
                "unit": metric["unit"] if target.get("metric_object") == "volume" else "pp",
                "scenario": target.get("scenario"),
                "target_semantics": target.get("target_semantics"),
                "periods": target.get("periods", self.periods),
                "view_id": target.get("view_id"),
                "metric_semantics": self._resolve_target_semantics(target_id),
            }
            for key in (
                "dimensions",
                "formula",
                "formula_shape",
                "factor_order",
                "formula_fingerprint",
                "factors",
            ):
                if key in target:
                    normalized_target[key] = deepcopy(target[key])
            parent_ref = target.get("parent_target_ref")
            if isinstance(parent_ref, str) and parent_ref:
                normalized_target["parent_target_ref"] = parent_ref
            relation = target.get("relation_to_parent")
            if isinstance(relation, dict):
                normalized_target["relation_to_parent"] = deepcopy(relation)
            ranking = self._normalize_target_ranking(target)
            if ranking is not None:
                normalized_target["ranking"] = ranking
            semantic_warnings = self.target_semantic_warnings.get(target_id, [])
            if semantic_warnings:
                normalized_target["semantic_warnings"] = list(semantic_warnings)
            self.compiled_attribution_targets.append(normalized_target)
            supported_semantics = contract.get("supported_target_semantics")
            capability_resolved = (
                contract.get("supported") is True
                and contract.get("contract_source") == identity_name
                and isinstance(supported_semantics, list)
                and bool(supported_semantics)
            )
            node_id = self.requirement_node_ids[target_id]
            if not capability_resolved:
                reason = "ATTRIBUTION_CAPABILITY_UNRESOLVED"
                self.nodes.append(make_node(
                    node_id,
                    "attribution",
                    [target_id],
                    criticality(original_target),
                    [],
                    {"mode": "blocked", "handler": "attribution"},
                    status="blocked",
                    extra={
                        "target_ref": target_id,
                        "operator_contract_ref": query_id,
                        "reason_code": reason,
                    },
                ))
                compilation_status = "blocked"
                slots: list[str] = []
            elif target.get("target_semantics") not in supported_semantics:
                reason = "ATTRIBUTION_TARGET_UNSUPPORTED"
                self.nodes.append(make_node(
                    node_id,
                    "attribution",
                    [target_id],
                    criticality(original_target),
                    [],
                    {"mode": "blocked", "handler": "attribution"},
                    status="blocked",
                    extra={
                        "target_ref": target_id,
                        "operator_contract_ref": query_id,
                        "reason_code": reason,
                        "required_target_semantics": target.get("target_semantics"),
                        "supported_target_semantics": supported_semantics,
                    },
                ))
                compilation_status = "blocked"
                slots = []
            else:
                slots = self._attribution_fact_slots(normalized_target | {
                    "target_id": target_id,
                    "metric_ref": metric_ref,
                    "decomposition": target.get("decomposition"),
                    "factors": target.get("factors", []),
                    "group_dimensions": self.ir_target_by_id(target_id).get("group_dimensions", []),
                    "parent_dimensions": self._attribution_parent_dimensions(self.ir_target_by_id(target_id)),
                    "view_id": normalized_target["view_id"],
                    "partial_coverage": self.ir_target_by_id(target_id).get("partial_coverage"),
                    "include_overall_metric": self.ir_target_by_id(target_id).get("include_overall_metric"),
                    "coverage": self.ir_target_by_id(target_id).get("coverage"),
                }, str(metric_ref))
                binding, binding_slots, result_dependencies = self._attribution_binding(
                    target, metric
                )
                slots = sorted(set(slots + binding_slots))
                expansion = self._attribution_expansion(original_target)
                self.nodes.append(make_node(
                    node_id,
                    "dimension_attribution" if original_target.get("group_dimensions") else "formula_attribution",
                    [target_id],
                    criticality(original_target),
                    sorted(
                        set(self._node_dependencies(target_id, slots))
                        | result_dependencies
                    ),
                    {
                        "mode": "lightweight_executor",
                        "handler": "attribution",
                        "operator": contract.get("operator"),
                        "metric": metric["name"],
                        "metric_object": target.get("metric_object"),
                        "unit": normalized_target["unit"],
                        "binding": binding,
                        "expansion": expansion,
                    },
                    inputs={"view_id": normalized_target["view_id"]},
                    outputs=list(contract.get("outputs", [])),
                    quality_gate=list(contract.get("constraints", [])),
                    extra={"target_ref": target_id, "operator_contract_ref": query_id},
                ))
                compilation_status = "compiled"
            self.requirement_compilation.append({
                "requirement_id": target_id,
                "kind": "attribution",
                "status": compilation_status,
                "node_ids": [node_id],
                "fact_slot_ids": slots,
            })

    def ir_target_by_id(self, target_id: str) -> dict[str, Any]:
        for target in self.original_attribution_targets:
            if target.get("target_id") == target_id:
                return target
        raise CompileError(f"unknown attribution target: {target_id}")

    def build_fact_layout(self, slots: list[dict[str, Any]]) -> dict[str, Any]:
        grains = {
            canonical_json({
                "view_id": slot.get("view_id"),
                "dimension_refs": slot.get("dimension_refs", []),
                "selector_dimensions": slot.get("selector_dimensions", {}),
                "scope": slot.get("scope"),
                "filters": slot.get("filters", []),
            })
            for slot in slots
        }
        if len(grains) != 1:
            return {"type": "long", "reason": "heterogeneous_grains"}

        by_metric: dict[str, list[dict[str, Any]]] = {}
        for slot in slots:
            by_metric.setdefault(str(slot["metric_ref"]), []).append(slot)
        mappings: dict[str, dict[str, Any]] = {}
        for metric_ref, metric_slots in sorted(by_metric.items()):
            components = {slot.get("component") for slot in metric_slots}
            if any(component is not None for component in components) and not {"numerator", "denominator"}.issubset(components):
                return {"type": "long", "reason": "incomplete_ratio_components"}
            sample = metric_slots[0]
            mapping_id = f"mapping_{stable_hash([metric_ref, sample.get('view_id'), sample.get('dimension_refs'), sample.get('selector_dimensions')], 12)}"
            mapping: dict[str, Any] = {
                "metric": sample["metric"],
                "view_id": sample.get("view_id"),
                "unit": sample["unit"],
            }
            if None in components:
                mapping["value_column"] = f"{mapping_id}__value"
            if "numerator" in components:
                mapping["numerator_column"] = f"{mapping_id}__numerator"
                mapping["denominator_column"] = f"{mapping_id}__denominator"
            mappings[mapping_id] = mapping
        return {
            "type": "wide_by_grain",
            "format": "wide_facts/1.0",
            "row_keys": ["view_id", "period", "dimensions"],
            "fact_mappings": mappings,
        }

    def build_fetch_request(self) -> list[dict[str, Any]]:
        slots = [
            deepcopy(self.fact_slots[key])
            for key in sorted(self.fact_slots)
            if not self.fact_slots[key].get("materialized_by")
        ]
        if not slots:
            return []
        declared_domains: dict[str, str] = {}
        for slot in self.fact_slots.values():
            for dimension, domain_ref in (slot.get("source_dimension_domains") or {}).items():
                previous = declared_domains.get(str(dimension))
                if previous is not None and previous != domain_ref:
                    raise CompileError(
                        f"physical dimension {dimension} has conflicting full-domain refs"
                    )
                declared_domains[str(dimension)] = str(domain_ref)
        for slot in slots:
            selectors = slot.get("source_selector_dimensions") or {}
            inherited = {
                dimension: domain_ref
                for dimension, domain_ref in declared_domains.items()
                if dimension in (slot.get("source_dimension_refs") or [])
                and dimension not in selectors
            }
            if inherited:
                slot["source_dimension_domains"] = inherited
        fact_layout = self.build_fact_layout(slots)
        return [{
            "request_id": "fetch_unified_1",
            "dimension_set_registry_hash": registry_hash(self.dimension_set_registry),
            "purpose": "initial_unified_fetch",
            "scope": self.task.get("scope"),
            "filters": self.task.get("filters", []),
            "metrics": sorted({slot["metric"] for slot in slots}),
            "periods": sorted({slot["period"] for slot in slots}),
            "fact_layout": fact_layout,
            "fact_slots": slots,
            "fact_demands": build_fact_demands(slots),
            "covered_requirement_refs": sorted({ref for slot in slots for ref in slot["requirement_refs"]}),
        }]

    def compile(self) -> dict[str, Any]:
        self.validate_ir()
        self.original_attribution_targets = deepcopy(self.ir.get("attribution_targets", []))
        self.compile_input_adaptations()
        self.compile_fact_observations()
        self.compile_metric_compositions()
        self.compile_derived()
        self.compile_custom_calculations()
        self.compile_attribution()

        physical_slots = {
            slot_id: slot
            for slot_id, slot in self.fact_slots.items()
            if not slot.get("materialized_by")
        }
        fact_requirement_refs = sorted({
            ref for slot in physical_slots.values() for ref in slot.get("requirement_refs", [])
        })
        if physical_slots:
            self.nodes.insert(0, make_node(
                "fact_artifact",
                "fact_query",
                fact_requirement_refs,
                "core",
                [],
                {"mode": "lightweight_executor", "handler": "fact_artifact"},
                inputs={"fact_slot_ids": sorted(physical_slots)},
                outputs=["normalized_facts"],
                quality_gate=["all required fact slots resolved or explicitly missing"],
                failure_strategy="block calculations that depend on unavailable facts",
            ))

        output_requirements = self.ir.get("output_requirements", [])
        if not isinstance(output_requirements, list):
            raise CompileError("$.output_requirements must be an array")
        conclusion_dependencies = [node["node_id"] for node in self.nodes if node.get("status") != "blocked"]
        if output_requirements:
            producer_nodes_by_requirement = {
                item["requirement_id"]: item.get("node_ids", [])
                for item in self.requirement_compilation
            }
            output_refs = []
            output_criticalities = []
            output_source_refs: set[str] = set()
            output_requirement_ids = {
                str(item.get("requirement_id"))
                for item in output_requirements
                if isinstance(item, dict)
            }
            for item in output_requirements:
                if not isinstance(item, dict):
                    raise CompileError("output_requirements must contain objects")
                output_refs.append(require_nonempty_string(item.get("requirement_id"), "output requirement_id"))
                output_criticalities.append(criticality(item))
                source_refs = item.get("source_requirement_refs")
                if not isinstance(source_refs, list) or not source_refs:
                    raise CompileError(
                        "output requirement source_requirement_refs must be a non-empty array"
                    )
                for source_ref in source_refs:
                    if (
                        not isinstance(source_ref, str)
                        or source_ref not in self.requirement_node_ids
                        or source_ref in output_requirement_ids
                    ):
                        raise CompileError(
                            f"output requirement references unknown calculation requirement: {source_ref!r}"
                        )
                    producer_node_ids = producer_nodes_by_requirement.get(source_ref)
                    if not producer_node_ids:
                        raise CompileError(
                            f"output requirement source has no compiled producer nodes: {source_ref!r}"
                        )
                    output_source_refs.add(source_ref)
            conclusion_dependencies = sorted({
                node_id
                for source_ref in output_source_refs
                for node_id in producer_nodes_by_requirement[source_ref]
            })
            criticality_rank = {"optional": 0, "required": 1, "core": 2}
            output_criticality = max(output_criticalities, key=lambda value: criticality_rank[value])
            self.nodes.append(make_node(
                "conclusion_organization",
                "conclusion_organization",
                output_refs,
                output_criticality,
                conclusion_dependencies,
                {"mode": "model_owned", "handler": "model_owned"},
                inputs={"source_requirement_refs": sorted(output_source_refs)},
                outputs=["conclusions", "scope_and_quality_notes"],
                quality_gate=["only use successful result references", "disclose blocked and failed requirements"],
            ))
            for requirement_id in output_refs:
                self.requirement_compilation.append({
                    "requirement_id": requirement_id,
                    "kind": "output_requirement",
                    "status": "compiled",
                    "node_ids": ["conclusion_organization"],
                    "fact_slot_ids": [],
                })

        dimensions = []
        for tree in self.ir.get("dimension_trees", []):
            for level in tree.get("levels", []):
                dimension = level.get("dimension_ref")
                if dimension not in dimensions:
                    dimensions.append(dimension)
        compiled_targets = deepcopy(self.compiled_attribution_targets)
        plan = {
            "execution_mode": self.task.get("execution_mode", "analysis_orchestration"),
            "analysis_ir": self.ir,
            "compiler": {
                "name": COMPILER_NAME,
                "version": COMPILER_VERSION,
                "source_ir_version": IR_VERSION,
                "source_ir_sha256": stable_hash(self.ir, 64),
                "timings": {
                    "operator_resolution_ms": round(self.operator_resolution_ms, 3),
                    "compile_ms": 0.0,
                    "plan_validation_ms": 0.0,
                    "recompile_ms": None,
                },
            },
            "analysis_task": {
                "analysis_goal": self.task.get("analysis_goal"),
                "query": self.task.get("query"),
                "metrics": deepcopy(self.task.get("metrics", [])),
                "periods": deepcopy(self.periods),
                "dimensions": dimensions,
                "filters": deepcopy(self.task.get("filters", [])),
                "selector_dimensions": deepcopy(self.task.get("selector_dimensions", {})),
                "scope": self.task.get("scope"),
                "fact_requirements": [self.fact_slots[key] for key in sorted(self.fact_slots)],
                "input_adaptations": deepcopy(self.ir.get("input_adaptations", [])),
                "metric_compositions": deepcopy(self.ir.get("metric_compositions", [])),
                "derived_requirements": deepcopy(self.ir.get("derived_requirements", [])),
                "custom_calculations": deepcopy(self.ir.get("custom_calculations", [])),
                "attribution_requirements": [target.get("target_id") for target in compiled_targets],
                "operator_queries": self.operator_queries,
                "operator_contracts": self.operator_contracts,
                "assumptions": deepcopy(self.task.get("assumptions", [])),
                "degradation_scope": [],
            },
            "attribution_targets": compiled_targets,
            "derived_metric_registry": {
                "source": "references/derived-metric-registry.json",
                "version": self.registry.get("registry_version"),
                "sha256": stable_hash(self.registry, 64),
            },
            "metric_composition_registry": {
                "source": "references/metric-composition-registry.json",
                "version": self.composition_registry.get("registry_version"),
                "sha256": stable_hash(self.composition_registry, 64),
            },
            "dimension_set_registry": {
                "source": "references/dimension-set-registry.json",
                "schema_version": self.dimension_set_registry.get("schema_version"),
                "sha256": registry_hash(self.dimension_set_registry),
            },
            "execution_runtime": {
                "version": "1.0",
                "periods": deepcopy(self.periods),
                "dimension_fields": dimensions,
                "max_workers": int(self.ir.get("runtime", {}).get("max_workers", 4)) if isinstance(self.ir.get("runtime", {}), dict) else 4,
                "residual_tolerance": DEFAULT_RESIDUAL_TOLERANCE,
            },
            "nodes": self.nodes,
            "resolution_requests": [],
            "fetch_requests": self.build_fetch_request(),
            "clarifications": deepcopy(self.ir.get("clarifications", [])),
            "requirement_compilation": sorted(self.requirement_compilation, key=lambda item: item["requirement_id"]),
            "validation_reports": {},
            "status": "ready_for_fetch",
        }
        if self.attribution_engine_identity is not None:
            plan["attribution_engine"] = deepcopy(self.attribution_engine_identity)
        admission = assess_query(self.ir, plan)
        plan["execution_profile"] = admission["execution_profile"]
        plan["fast_query_admission"] = admission
        return plan


def compile_and_validate(
    ir: dict[str, Any],
    registry_path: Path,
    composition_registry_path: Path | None = None,
    dimension_set_registry_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    if composition_registry_path is None:
        composition_registry_path = registry_path.parent / "metric-composition-registry.json"
    if dimension_set_registry_path is None:
        dimension_set_registry_path = DEFAULT_DIMENSION_SET_REGISTRY
    compiler = Compiler(
        ir,
        load_json(registry_path),
        load_json(composition_registry_path),
        load_dimension_set_registry(dimension_set_registry_path),
    )
    plan = compiler.compile()
    compile_ms = (time.perf_counter() - started) * 1000
    plan["compiler"]["timings"]["compile_ms"] = round(compile_ms, 3)

    validation_started = time.perf_counter()
    initial_report = Validator(plan, "plan").validate()
    if initial_report.get("computed_status"):
        plan["status"] = initial_report["computed_status"]
    report = Validator(plan, "plan").validate()
    validation_ms = (time.perf_counter() - validation_started) * 1000
    plan["compiler"]["timings"]["plan_validation_ms"] = round(validation_ms, 3)
    return plan, report


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="analysis_ir/1.0 JSON")
    parser.add_argument("--output", required=True, type=Path, help="compiled plan JSON")
    parser.add_argument("--validation-report", type=Path, help="optional plan validation report")
    parser.add_argument(
        "--derived-registry",
        type=Path,
        default=root / "references" / "derived-metric-registry.json",
    )
    parser.add_argument(
        "--composition-registry",
        type=Path,
        default=root / "references" / "metric-composition-registry.json",
    )
    parser.add_argument(
        "--dimension-set-registry",
        type=Path,
        default=DEFAULT_DIMENSION_SET_REGISTRY,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        ir = load_json(args.input)
        plan, report = compile_and_validate(
            ir,
            args.derived_registry,
            args.composition_registry,
            args.dimension_set_registry,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.validation_report:
            args.validation_report.parent.mkdir(parents=True, exist_ok=True)
            args.validation_report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except CompileError as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 3
    errors = int(report.get("summary", {}).get("errors", 0))
    warnings = int(report.get("summary", {}).get("warnings", 0))
    return 2 if errors else 1 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
