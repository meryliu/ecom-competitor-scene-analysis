#!/usr/bin/env python3
"""Feishu implementation of the competitor data gateway."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from competitor_fact_provider import FACT_PROVIDER_VERSION, fetch_facts_from_index
from business_intent_policy import (
    DEFAULT_POLICY_PATH as DEFAULT_BUSINESS_INTENT_POLICY,
    business_intent_policy_hash,
    load_business_intent_policy,
)
from data_gateway import DataGateway, RESOLVED_CAPABILITIES_V1, SOURCE_BINDING_V1
from resolution_policy import (
    DEFAULT_POLICY_PATH,
    ENGINE_VERSION,
    load_resolution_policy,
    resolve_request_overlay,
    resolution_policy_hash,
)
from source_runtime import ManagedLarkClient, ensure_shared_index


class FeishuCompetitorGateway(DataGateway):
    def __init__(
        self,
        config: dict[str, Any],
        *,
        identity: str = "user",
        index_path: Path | None = None,
        allow_stale: bool | None = None,
        dimension_set_registry_path: Path | None = None,
        resolution_policy_path: Path | None = None,
        business_intent_policy_path: Path | None = None,
    ) -> None:
        self.config = deepcopy(config)
        self.identity = identity
        self.index_path = index_path
        self.allow_stale = (
            bool(config.get("allow_stale_by_default")) if allow_stale is None else allow_stale
        )
        self.dimension_set_registry_path = dimension_set_registry_path
        self.resolution_policy_path = resolution_policy_path or DEFAULT_POLICY_PATH
        self.resolution_policy = load_resolution_policy(self.resolution_policy_path)
        self.business_intent_policy_path = (
            business_intent_policy_path or DEFAULT_BUSINESS_INTENT_POLICY
        )
        self.business_intent_policy = load_business_intent_policy(
            self.business_intent_policy_path
        )
        self.client = ManagedLarkClient(identity=identity)
        self._index: dict[str, Any] | None = None
        self._cache_status: str | None = None
        self._resolved_index_path: Path | None = None
        self._binding: dict[str, Any] | None = None

    @property
    def source_binding(self) -> dict[str, Any]:
        if self._binding is None:
            raise RuntimeError("gateway.resolve() must run before source_binding is used")
        return deepcopy(self._binding)

    def resolve(self, request: dict[str, Any]) -> dict[str, Any]:
        sheet_roles = {
            role: list(value.get("allowed_names") or [])
            for role, value in (self.config.get("sheet_roles") or {}).items()
            if isinstance(value, dict)
        }
        index, status, path = ensure_shared_index(
            self.client,
            str(self.config["source_url"]),
            identity=self.identity,
            index_path=self.index_path,
            allow_stale=self.allow_stale,
            sheet_roles=sheet_roles,
            config_hash=str(self.config["config_hash"]),
        )
        resolution = resolve_request_overlay(
            index, request, self.resolution_policy, self.business_intent_policy
        )
        candidate_index = resolution["index"]
        # Preserve the pinned raw-index object on the exact path.  A request-scoped
        # overlay is retained only when resolution actually changed source blocks.
        resolved_index = index if candidate_index == index else candidate_index
        self._index, self._cache_status, self._resolved_index_path = resolved_index, status, path
        source = index.get("source") or {}
        self._binding = {
            "schema_version": SOURCE_BINDING_V1,
            "provider_id": self.config["provider_id"],
            "source_id": self.config["source_id"],
            "config_hash": self.config["config_hash"],
            "revision": source.get("revision"),
            "schema_hash": source.get("schema_hash"),
            "freshness": "stale" if status == "stale" else "live",
            "resolution_policy_hash": resolution_policy_hash(self.resolution_policy),
            "business_intent_policy_hash": business_intent_policy_hash(
                self.business_intent_policy
            ),
            "resolution_engine_version": ENGINE_VERSION,
            "fact_provider_version": FACT_PROVIDER_VERSION,
        }
        metric_catalogue = resolved_index.get("metrics") or {}
        dimension_catalogue = resolved_index.get("dimensions") or {}
        metric_bindings = resolution["metric_bindings"]
        dimension_bindings = resolution["dimension_bindings"]
        task_resolutions = deepcopy(resolution.get("task_resolutions") or {})
        for case in resolution.get("resolution_cases") or []:
            if not isinstance(case, dict) or case.get("kind") != "fact_block":
                continue
            for task_id in case.get("task_ids") or []:
                task_resolutions.setdefault(str(task_id), {}).setdefault(
                    "resolution_cases", []
                ).append(deepcopy(case))
        for task_id, task_resolution in task_resolutions.items():
            task_resolution["metric_dimension_bindings"] = deepcopy(
                (resolution.get("task_metric_dimension_bindings") or {}).get(task_id, {})
            )
        resolved_metrics = set(metric_bindings.values())
        for task_resolution in task_resolutions.values():
            resolved_metrics.update(
                str(value)
                for value in (task_resolution.get("metric_bindings") or {}).values()
                if value
            )
            resolved_metrics.update(
                str(item.get("source_metric"))
                for item in (task_resolution.get("requirement_bindings") or {}).values()
                if isinstance(item, dict) and item.get("source_metric")
            )
        resolved_dimensions = set(dimension_bindings.values())
        for bindings in (resolution.get("metric_dimension_bindings") or {}).values():
            if isinstance(bindings, dict):
                resolved_dimensions.update(str(value) for value in bindings.values())
        for task_resolution in task_resolutions.values():
            for binding in (task_resolution.get("requirement_bindings") or {}).values():
                if not isinstance(binding, dict):
                    continue
                resolved_dimensions.update(
                    str(item.get("source_dimension"))
                    for item in binding.get("metric_constraints") or []
                    if isinstance(item, dict) and item.get("source_dimension")
                )
        availability: dict[str, Any] = {}
        for grain, sheet in (resolved_index.get("sheets") or {}).items():
            if not isinstance(sheet, dict) or not sheet.get("available"):
                continue
            availability[str(grain)] = {
                "periods": sorted((sheet.get("periods") or {}).keys()),
                "metrics": {
                    str(metric): {"dimension": block.get("dimension")}
                    for metric, block in (sheet.get("blocks") or {}).items()
                    if isinstance(block, dict) and str(metric) in resolved_metrics
                },
            }
        return {
            "schema_version": RESOLVED_CAPABILITIES_V1,
            "provider": {
                "provider_id": self.config["provider_id"],
                "contract_version": "1.0",
            },
            "source": deepcopy(self._binding),
            "metric_bindings": metric_bindings,
            "task_resolutions": task_resolutions,
            "dimension_bindings": dimension_bindings,
            "metrics": {
                key: deepcopy(metric_catalogue[key]) for key in sorted(resolved_metrics)
            },
            "dimensions": {
                key: deepcopy(dimension_catalogue[key]) for key in sorted(resolved_dimensions)
            },
            "availability": availability,
            "metric_dimension_bindings": deepcopy(resolution["metric_dimension_bindings"]),
            "task_metric_dimension_bindings": deepcopy(
                resolution.get("task_metric_dimension_bindings") or {}
            ),
            "resolution_cases": deepcopy(resolution["resolution_cases"]),
            "resolution_decisions": deepcopy(resolution["resolution_decisions"]),
            "resolution_policy": deepcopy(resolution["resolution_policy"]),
            "business_intent_policy": deepcopy(resolution["business_intent_policy"]),
        }

    def fetch(self, request: dict[str, Any]) -> dict[str, Any]:
        if self._index is None or self._cache_status is None or self._resolved_index_path is None:
            raise RuntimeError("gateway.resolve() must run before gateway.fetch()")
        if request.get("source_binding") != self.source_binding:
            raise ValueError("fetch request source_binding differs from the resolved source")
        return fetch_facts_from_index(
            request,
            self._index,
            self.client,
            self._cache_status,
            self._resolved_index_path,
            dimension_set_registry_path=self.dimension_set_registry_path,
        )
