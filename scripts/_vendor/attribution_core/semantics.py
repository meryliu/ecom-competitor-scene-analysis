"""Optional semantic interpretation and deterministic contribution ranking."""
from __future__ import annotations

import heapq
import math
from copy import deepcopy
from typing import Any, Dict, List, Tuple

DIRECTIONS = {"higher_is_better", "lower_is_better", "unknown"}
DIRECTION_ALIASES = {
    "positive": "higher_is_better",
    "good": "higher_is_better",
    "higher_is_better": "higher_is_better",
    "negative": "lower_is_better",
    "bad": "lower_is_better",
    "lower_is_better": "lower_is_better",
    "unknown": "unknown",
    "": "unknown",
}
DIRECTION_SOURCES = {
    "user",
    "official_metadata",
    "registry",
    "parent_derivation",
    "inference",
    "unknown",
}
CONFIDENCES = {"high", "medium", "low", "unknown"}
MONOTONICITIES = {"positive", "negative", "conditional", "unknown"}
RANKING_FILTERS = {"positive", "negative", "all"}
RANKING_ORDERS = {"asc", "desc", "abs_asc", "abs_desc"}


def normalize_direction(value: Any) -> str:
    return DIRECTION_ALIASES.get(str(value or "").strip().lower(), "unknown")


def derive_direction(parent_direction: Any, monotonicity: Any) -> str:
    parent = normalize_direction(parent_direction)
    relation = str(monotonicity or "unknown").strip().lower()
    if parent == "unknown" or relation not in {"positive", "negative"}:
        return "unknown"
    if relation == "positive":
        return parent
    return "lower_is_better" if parent == "higher_is_better" else "higher_is_better"


def resolve_metric_semantics(payload: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    metric = payload.get("metric") if isinstance(payload.get("metric"), dict) else {}
    supplied = payload.get("metric_semantics") if isinstance(payload.get("metric_semantics"), dict) else {}
    parent = payload.get("parent_metric_semantics") if isinstance(payload.get("parent_metric_semantics"), dict) else {}
    relation = payload.get("relation_to_parent") if isinstance(payload.get("relation_to_parent"), dict) else {}

    raw_direction = supplied.get("direction", metric.get("direction", metric.get("polarity")))
    direction = normalize_direction(raw_direction)
    source = str(supplied.get("direction_source", metric.get("direction_source", "unknown"))).strip().lower()
    confidence = str(supplied.get("direction_confidence", metric.get("direction_confidence", "unknown"))).strip().lower()
    warnings: List[str] = []

    if raw_direction not in (None, "") and direction == "unknown" and str(raw_direction).lower() != "unknown":
        warnings.append(f"unrecognized metric direction {raw_direction!r}; business-effect labels omitted")
    if source not in DIRECTION_SOURCES:
        warnings.append(f"unrecognized direction_source {source!r}; normalized to unknown")
        source = "unknown"
    if confidence not in CONFIDENCES:
        warnings.append(f"unrecognized direction_confidence {confidence!r}; normalized to unknown")
        confidence = "unknown"

    derived = derive_direction(parent.get("direction"), relation.get("monotonicity"))
    should_use_parent = direction == "unknown" or (
        source == "inference" and confidence in {"low", "unknown"}
    )
    if derived != "unknown" and should_use_parent:
        direction = derived
        source = "parent_derivation"
        parent_confidence = str(parent.get("direction_confidence", "unknown")).lower()
        confidence = parent_confidence if parent_confidence in CONFIDENCES else "unknown"

    return {
        "metric_id": str(supplied.get("metric_id", metric.get("metric_id", ""))),
        "direction": direction,
        "direction_source": source,
        "direction_confidence": confidence,
    }, warnings


def add_semantic_labels(payload: Dict[str, Any], output: Dict[str, Any]) -> List[str]:
    semantics, warnings = resolve_metric_semantics(payload)
    summary = output.setdefault("summary", {})
    summary["metric_semantics"] = semantics
    delta = summary.get("yoy_trend_delta", summary.get("change_value"))
    direction = semantics["direction"]

    performance_good = None
    if isinstance(delta, (int, float)) and not isinstance(delta, bool) and delta != 0 and direction != "unknown":
        performance_good = (
            (direction == "higher_is_better" and delta > 0)
            or (direction == "lower_is_better" and delta < 0)
        )
        summary["metric_direction"] = "positive" if direction == "higher_is_better" else "negative"
        summary["performance_label"] = "表现好" if performance_good else "表现不好"
        summary["business_performance"] = "improved" if performance_good else "deteriorated"

    for row in output.get("rows", []):
        rate = row.get("contribution_rate")
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(float(rate)) or rate == 0:
            row["contribution_direction"] = "neutral"
            if performance_good is not None:
                row["business_effect"] = "neutral"
                row["interpretation"] = "中性"
            continue
        same_direction = rate > 0
        row["contribution_direction"] = "same_direction" if same_direction else "offset"
        if performance_good is None:
            continue
        favorable = same_direction == performance_good
        if performance_good:
            row["business_effect"] = "improvement_driver" if same_direction else "negative_offset"
        else:
            row["business_effect"] = "deterioration_driver" if same_direction else "positive_offset"
        row["interpretation"] = (
            "好，且绝对贡献率越大越好" if favorable else "不好，且绝对贡献率越大越不好"
        )
    return warnings


def normalize_ranking(value: Any) -> Tuple[Dict[str, Any] | None, List[str]]:
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, ["ranking must be an object; ranking view omitted"]

    warnings: List[str] = []
    metric = value.get("metric", "contribution_rate")
    if metric != "contribution_rate":
        warnings.append(f"unsupported ranking metric {metric!r}; contribution_rate used")
        metric = "contribution_rate"

    filter_value = str(value.get("filter", "all")).lower()
    if filter_value not in RANKING_FILTERS:
        warnings.append(f"unsupported ranking filter {filter_value!r}; all used")
        filter_value = "all"

    default_order = "desc" if filter_value != "negative" else "asc"
    order = str(value.get("order", default_order)).lower()
    if order not in RANKING_ORDERS:
        warnings.append(f"unsupported ranking order {order!r}; {default_order} used")
        order = default_order

    top_k = value.get("top_k", 3)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        warnings.append(f"invalid ranking top_k {top_k!r}; 3 used")
        top_k = 3
    return {"metric": metric, "filter": filter_value, "order": order, "top_k": top_k}, warnings


def rank_rows(rows: List[Dict[str, Any]], ranking: Dict[str, Any]) -> Dict[str, Any]:
    candidates: List[Tuple[int, Dict[str, Any], float]] = []
    for index, row in enumerate(rows):
        raw = row.get("contribution_rate")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool) or not math.isfinite(float(raw)):
            continue
        rate = float(raw)
        if ranking["filter"] == "positive" and rate <= 0:
            continue
        if ranking["filter"] == "negative" and rate >= 0:
            continue
        candidates.append((index, row, rate))

    order = ranking["order"]
    top_k = min(ranking["top_k"], len(candidates))
    if order == "desc":
        selected = heapq.nlargest(top_k, candidates, key=lambda item: (item[2], -item[0]))
    elif order == "asc":
        selected = heapq.nsmallest(top_k, candidates, key=lambda item: (item[2], item[0]))
    elif order == "abs_desc":
        selected = heapq.nlargest(top_k, candidates, key=lambda item: (abs(item[2]), -item[0]))
    else:
        selected = heapq.nsmallest(top_k, candidates, key=lambda item: (abs(item[2]), item[0]))

    ranked_rows = []
    for rank, (source_index, row, _) in enumerate(selected, start=1):
        item = deepcopy(row)
        item["rank"] = rank
        item["source_row_index"] = source_index
        ranked_rows.append(item)
    return {
        **ranking,
        "available_count": len(candidates),
        "selected_count": len(ranked_rows),
        "rows": ranked_rows,
    }


def add_ranking(payload: Dict[str, Any], output: Dict[str, Any]) -> List[str]:
    ranking, warnings = normalize_ranking(payload.get("ranking"))
    if ranking is not None:
        output["ranking"] = rank_rows(output.get("rows", []), ranking)
    return warnings
