#!/usr/bin/env python3
"""Provider-neutral helpers for resolved source capabilities."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from data_gateway import RESOLVED_CAPABILITIES_V1


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
    parsed = normalize_period(period)
    if parsed is None:
        return False
    grain, canonical = parsed
    available = (capabilities.get("availability") or {}).get(grain) or {}
    metric = (available.get("metrics") or {}).get(source_metric) or {}
    return canonical in (available.get("periods") or []) and metric.get("dimension") == source_dimension
