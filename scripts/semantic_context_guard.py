"""Small, request-local guard for semantic context drift.

The guard deliberately extracts only a conservative core hint from the current
query.  It is used as a failure fallback; it never inherits a metric from a
previous turn or changes a successful legacy binding.
"""
from __future__ import annotations

import re
from typing import Any

from _vendor.ecom_competitor_source import normalize_match_text


_QUESTION_TOKENS = (
    "多少",
    "是多少",
    "如何",
    "怎么样",
    "表现",
    "请问",
    "呢",
    "吗",
)
_TEMPORAL_TOKENS = (
    "最新",
    "自然周",
    "一周",
    "上一周",
    "本周",
    "上周",
    "本月",
    "上月",
    "本季度",
    "上季度",
    "本年度",
    "去年",
    "今年",
)
_REFERENCE_MARKERS = (
    "这个指标",
    "该指标",
    "上述指标",
    "前述指标",
    "上文指标",
    "刚才指标",
    "同一指标",
)


def _registered_tokens(policy: dict[str, Any], field_groups: tuple[str, ...]) -> list[str]:
    semantic = policy.get("semantic_normalization") or {}
    return sorted(
        {
            normalize_match_text(token)
            for group in field_groups
            for tokens in (semantic.get(group) or {}).values()
            for token in tokens
            if normalize_match_text(token)
        },
        key=len,
        reverse=True,
    )


def has_explicit_reference(text: Any) -> bool:
    normalized = normalize_match_text(text)
    return any(marker in normalized for marker in _REFERENCE_MARKERS)


def extract_current_core_hint(
    query: Any,
    semantic_text: Any,
    policy: dict[str, Any],
    constraints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a conservative core hint from the current request only.

    Query text is preferred over model-produced semantic text.  Dates, period
    words, comparison/measure operators, constraint values and question words
    are removed.  An explicit historical-reference phrase without a concrete
    core returns no hint, so prior context cannot silently leak into resolve.
    """
    raw_query = str(query or "").strip()
    raw_semantic = str(semantic_text or "").strip()
    source = raw_query or raw_semantic
    if not source:
        return {"hint": "", "source": None, "explicit_reference": False}
    normalized = normalize_match_text(source)
    explicit_reference = has_explicit_reference(normalized)

    # Remove calendar/date notation without maintaining a list of years or
    # months.  This also handles "第33周" and "2026年7月" forms.
    text = re.sub(r"\d{4}\s*年", "", normalized)
    text = re.sub(r"第\s*\d{1,2}\s*(?:周|季度|月)", "", text)
    text = re.sub(r"\d{1,2}\s*(?:年|月|周|季度)", "", text)
    text = re.sub(r"\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?", "", text)

    tokens = _registered_tokens(
        policy,
        ("comparison_terms", "measure_terms", "grain_terms", "constraint_operator_terms"),
    )
    tokens.extend(
        normalize_match_text(value)
        for constraint in constraints or []
        for value in constraint.get("values") or []
        if normalize_match_text(value)
    )
    for token in sorted(set(tokens), key=len, reverse=True):
        text = text.replace(token, "")
    for token in _QUESTION_TOKENS:
        text = text.replace(normalize_match_text(token), "")
    for token in _TEMPORAL_TOKENS:
        text = text.replace(normalize_match_text(token), "")
    for token in _REFERENCE_MARKERS:
        text = text.replace(normalize_match_text(token), "")
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text, flags=re.UNICODE)
    text = text.strip("_ ")

    # Pure reference requests ("这个指标同比呢") do not identify a new
    # metric and must not borrow a prior metric implicitly.
    if explicit_reference and len(text) < 2:
        text = ""
    if len(text) < 2:
        text = ""
    return {
        "hint": text,
        "source": "query" if raw_query else "semantic_text",
        "explicit_reference": explicit_reference,
    }
