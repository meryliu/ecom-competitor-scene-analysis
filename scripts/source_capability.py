#!/usr/bin/env python3
"""Provider-neutral helpers for resolved source capabilities."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from data_gateway import RESOLVED_CAPABILITIES_V1


DEFAULT_GRAIN_ROLLUP_EDGES = (
    ("day", "week"),
    ("week", "month"),
    ("week", "quarter"),
    ("month", "quarter"),
    ("week", "year"),
    ("month", "year"),
    ("quarter", "year"),
)


def normalize_match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = re.sub(r"\s+", "", text).strip()
    return re.sub(r"[:：,，;；/\\_\-—()（）\[\]【】]+", "", text)


def normalize_period(value: Any) -> tuple[str, str] | None:
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()
    patterns = [
        ("month", r"(20\d{2})(?:年|[-/.])?(1[0-2]|0?[1-9])月?"),
        ("week", r"(20\d{2})(?:年)?(?:第|[-/]?w)([0-5]?\d)周?"),
        ("quarter", r"(20\d{2})(?:年)?(?:第?([1-4])季度|[-/]?q([1-4]))"),
        ("year", r"(20\d{2})年?"),
    ]
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


def can_rollup_grain(
    source_grain: str,
    target_grain: str,
    edges: list[list[str]] | tuple[tuple[str, str], ...] | None = None,
) -> bool:
    """Return whether a registered directed grain path reaches the target."""
    if source_grain == target_grain:
        return True
    adjacency: dict[str, set[str]] = {}
    for edge in edges or DEFAULT_GRAIN_ROLLUP_EDGES:
        if len(edge) != 2:
            continue
        adjacency.setdefault(str(edge[0]), set()).add(str(edge[1]))
    pending = list(adjacency.get(source_grain, set()))
    visited = {source_grain}
    while pending:
        grain = pending.pop()
        if grain == target_grain:
            return True
        if grain in visited:
            continue
        visited.add(grain)
        pending.extend(adjacency.get(grain, set()) - visited)
    return False


def evaluate_structural_grain_capability(
    metadata: dict[str, Any],
    target_grain: str,
    edges: list[list[str]] | tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    """Evaluate grain reachability without inspecting periods, blocks, or values."""
    supported = [str(value) for value in metadata.get("supported_grains") or []]
    if not supported:
        return {
            "status": "unknown",
            "reason": "metadata_grain_unknown",
            "target_grain": target_grain,
        }
    if target_grain in supported:
        return {
            "status": "available",
            "path": "direct_fact",
            "source_grain": target_grain,
            "target_grain": target_grain,
        }
    additive = (
        metadata.get("aggregation_mode") == "additive"
        or metadata.get("additive") is True
    )
    if additive:
        source_grain = next(
            (
                grain
                for grain in supported
                if can_rollup_grain(grain, target_grain, edges)
            ),
            None,
        )
        if source_grain is not None:
            return {
                "status": "available",
                "path": "aggregate_fact",
                "source_grain": source_grain,
                "target_grain": target_grain,
            }
    return {
        "status": "unavailable",
        "reason": "metadata_grain_unsupported",
        "target_grain": target_grain,
        "supported_grains": supported,
        "aggregation_mode": metadata.get("aggregation_mode") or "unknown",
    }


def validate_capabilities(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("schema_version") != RESOLVED_CAPABILITIES_V1:
        raise ValueError("unsupported resolved capabilities")
    for field in ("source", "metric_bindings", "dimension_bindings", "metrics", "dimensions", "availability"):
        if not isinstance(value.get(field), dict):
            raise ValueError(f"resolved capabilities missing {field}")
    return value


def project_task_capabilities(
    capabilities: dict[str, Any], task_id: str
) -> dict[str, Any]:
    """Return a compact task-scoped view without changing the pinned snapshot."""
    validate_capabilities(capabilities)
    task = (capabilities.get("task_resolutions") or {}).get(str(task_id)) or {}
    projected = deepcopy(capabilities)
    metric_bindings = dict(task.get("metric_bindings") or {})
    projected["metric_bindings"] = metric_bindings
    projected["metric_statuses"] = deepcopy(task.get("metric_statuses") or {})
    projected["requirement_bindings"] = deepcopy(task.get("requirement_bindings") or {})
    projected["intent_resolutions"] = deepcopy(task.get("intent_resolutions") or {})
    projected["composition_resolutions"] = deepcopy(
        task.get("composition_resolutions") or []
    )
    projected["resolution_cases"] = deepcopy(task.get("resolution_cases") or [])
    projected["metric_dimension_bindings"] = deepcopy(
        (capabilities.get("task_metric_dimension_bindings") or {}).get(str(task_id), {})
    )
    resolved_metrics = set(metric_bindings.values())
    for composition in projected["composition_resolutions"]:
        if not isinstance(composition, dict):
            continue
        resolved_metrics.update(
            str(value)
            for value in (composition.get("input_bindings") or {}).values()
            if value
        )
    resolved_metrics.update(
        str(item.get("source_metric"))
        for item in projected["requirement_bindings"].values()
        if isinstance(item, dict) and item.get("source_metric")
    )
    projected["metrics"] = {
        name: deepcopy(metadata)
        for name, metadata in (capabilities.get("metrics") or {}).items()
        if name in resolved_metrics
    }
    resolved_dimensions = {
        str(value)
        for value in (capabilities.get("dimension_bindings") or {}).values()
        if value
    }
    for bindings in projected["metric_dimension_bindings"].values():
        if isinstance(bindings, dict):
            resolved_dimensions.update(str(value) for value in bindings.values())
    projected["dimensions"] = {
        name: deepcopy(metadata)
        for name, metadata in (capabilities.get("dimensions") or {}).items()
        if name in resolved_dimensions
    }
    # Keep availability unchanged: it is a source snapshot, while the binding
    # maps above define which rows this task may consume.
    return projected


def resolve_metric(name: str, capabilities: dict[str, Any]) -> str | None:
    return (capabilities.get("metric_bindings") or {}).get(name)


def resolve_dimension(name: str, capabilities: dict[str, Any]) -> str | None:
    if not name:
        return None
    return (capabilities.get("dimension_bindings") or {}).get(name)


def direct_available(
    capabilities: dict[str, Any],
    source_metric: str,
    period: str,
    source_dimension: str,
) -> bool:
    return evaluate_direct_capability(
        capabilities, source_metric, period, source_dimension
    )["status"] == "available"


def evaluate_direct_capability(
    capabilities: dict[str, Any],
    source_metric: str,
    period: str,
    source_dimension: str,
) -> dict[str, Any]:
    """Evaluate a direct fact using source metadata and the live fact index.

    Metadata is authoritative for declared grain/dimension support; the fact
    sheet is still required to prove that the requested period and block exist.
    """
    parsed = normalize_period(period)
    if parsed is None:
        return {"status": "blocked", "reason": "invalid_period"}
    grain, canonical = parsed
    metadata = (capabilities.get("metrics") or {}).get(source_metric) or {}
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
    supported_dimensions = metadata.get("dimensions")
    if "dimensions" in metadata and not supported_dimensions:
        return {
            "status": "unavailable",
            "reason": "metadata_dimension_unknown",
            "dimension": source_dimension,
        }
    if source_dimension == "无":
        dimension_supported = not supported_dimensions or "无" in supported_dimensions
    else:
        dimension_supported = not supported_dimensions or source_dimension in supported_dimensions
    if not dimension_supported:
        return {
            "status": "unavailable",
            "reason": "metadata_dimension_unsupported",
            "dimension": source_dimension,
            "supported_dimensions": list(supported_dimensions or []),
        }
    available = (capabilities.get("availability") or {}).get(grain) or {}
    metric = (available.get("metrics") or {}).get(source_metric) or {}
    if canonical not in (available.get("periods") or []):
        return {"status": "unavailable", "reason": "period_unavailable", "grain": grain, "period": canonical}
    if not metric:
        return {"status": "unavailable", "reason": "metric_block_unavailable", "grain": grain}
    if metric.get("dimension") != source_dimension:
        return {
            "status": "blocked",
            "reason": "metadata_fact_dimension_conflict",
            "metadata_dimension": source_dimension,
            "fact_dimension": metric.get("dimension"),
        }
    return {"status": "available", "grain": grain, "period": canonical, "dimension": source_dimension}
