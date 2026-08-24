#!/usr/bin/env python3
"""Deterministic period parsing and ISO-week rollup helpers."""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, timedelta
from typing import Any


ISO_WEEK_CALENDAR = "iso8601"


def _normalized(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def normalize_period(value: Any) -> tuple[str, str] | None:
    """Return a canonical period and reject invalid ISO weeks for their year."""
    text = _normalized(value)
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
            try:
                date.fromisocalendar(year, week, 1)
            except ValueError:
                return None
            return grain, f"{year:04d}-W{week:02d}"
        if grain == "quarter":
            return grain, f"{year:04d}-Q{int(match.group(2) or match.group(3))}"
        return grain, f"{year:04d}"
    return None


def period_bounds(period: str) -> tuple[date, date]:
    parsed = normalize_period(period)
    if parsed is None:
        raise ValueError(f"invalid period: {period!r}")
    grain, canonical = parsed
    year = int(canonical[:4])
    if grain == "week":
        week = int(canonical[-2:])
        start = date.fromisocalendar(year, week, 1)
        return start, start + timedelta(days=6)
    if grain == "month":
        month = int(canonical[-2:])
        start = date(year, month, 1)
        return start, date(year, month, calendar.monthrange(year, month)[1])
    if grain == "quarter":
        quarter = int(canonical[-1])
        month = (quarter - 1) * 3 + 1
        start = date(year, month, 1)
        end_month = month + 2
        return start, date(year, end_month, calendar.monthrange(year, end_month)[1])
    return date(year, 1, 1), date(year, 12, 31)


def overlap_days(child: str, target: str) -> int:
    child_start, child_end = period_bounds(child)
    target_start, target_end = period_bounds(target)
    start = max(child_start, target_start)
    end = min(child_end, target_end)
    return max(0, (end - start).days + 1)


def iso_weeks_covering(target: str) -> list[dict[str, Any]]:
    """Return every ISO week intersecting a month, quarter, or year."""
    target_start, target_end = period_bounds(target)
    cursor = target_start - timedelta(days=target_start.weekday())
    result: list[dict[str, Any]] = []
    while cursor <= target_end:
        iso = cursor.isocalendar()
        label = f"{iso.year:04d}-W{iso.week:02d}"
        days = overlap_days(label, target)
        if days:
            result.append({
                "period": label,
                "overlap_days": days,
                "weight": days / 7.0,
                "calendar": ISO_WEEK_CALENDAR,
            })
        cursor += timedelta(days=7)
    return result


