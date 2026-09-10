#!/usr/bin/env python3
"""Deterministic helpers for bounded period requests."""
from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Any


ALLOWED_GRAINS = {"year", "quarter", "month", "week"}
SPAN_PATTERN = re.compile(r"^span:(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")


def _iso_date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{path} must be an ISO date") from exc


def normalize_period_request(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    if value.get("type", "bounded_span") != "bounded_span":
        raise ValueError(f"{path}.type must be bounded_span")
    start = _iso_date(value.get("start"), f"{path}.start")
    end = _iso_date(value.get("end"), f"{path}.end")
    if start > end:
        raise ValueError(f"{path}.start must not be after end")
    grain = value.get("requested_grain")
    if grain is not None and grain not in ALLOWED_GRAINS:
        raise ValueError(
            f"{path}.requested_grain must be year, quarter, month, week, or null"
        )
    grain_source = value.get("grain_source") or (
        "user_explicit" if grain is not None else "not_specified"
    )
    if grain_source not in {"user_explicit", "not_specified"}:
        raise ValueError(f"{path}.grain_source is invalid")
    if grain is None and grain_source != "not_specified":
        raise ValueError(f"{path}.grain_source must be not_specified without a grain")
    return {
        "type": "bounded_span",
        "label": str(value.get("label") or f"{start.isoformat()}至{end.isoformat()}"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "requested_grain": grain,
        "grain_source": grain_source,
    }


def span_token(start: date, end: date) -> str:
    return f"span:{start.isoformat()}/{end.isoformat()}"


def parse_span_token(value: Any) -> tuple[date, date] | None:
    match = SPAN_PATTERN.fullmatch(str(value or ""))
    if not match:
        return None
    start = date.fromisoformat(match.group(1))
    end = date.fromisoformat(match.group(2))
    return (start, end) if start <= end else None


def native_period(start: date, end: date) -> str | None:
    if start.year == end.year and start == date(start.year, 1, 1) and end == date(start.year, 12, 31):
        return f"{start.year:04d}"
    quarter = (start.month - 1) // 3 + 1
    quarter_start = date(start.year, (quarter - 1) * 3 + 1, 1)
    quarter_end_month = quarter * 3
    quarter_end = date(start.year, quarter_end_month, monthrange(start.year, quarter_end_month)[1])
    if start == quarter_start and end == quarter_end:
        return f"{start.year:04d}-Q{quarter}"
    month_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
    if start.day == 1 and end == month_end:
        return f"{start.year:04d}-{start.month:02d}"
    if start.weekday() == 0 and end == start + timedelta(days=6):
        iso_year, iso_week, _ = start.isocalendar()
        return f"{iso_year:04d}-W{iso_week:02d}"
    return None


def _quarter_periods(start: date, end: date) -> list[str]:
    if start.day != 1 or start.month not in {1, 4, 7, 10}:
        return []
    if end.month not in {3, 6, 9, 12} or end.day != monthrange(end.year, end.month)[1]:
        return []
    periods: list[str] = []
    cursor = start
    while cursor <= end:
        quarter = (cursor.month - 1) // 3 + 1
        periods.append(f"{cursor.year:04d}-Q{quarter}")
        next_month = cursor.month + 3
        cursor = date(cursor.year + (next_month - 1) // 12, (next_month - 1) % 12 + 1, 1)
    return periods if cursor - timedelta(days=1) == end else []


def _month_periods(start: date, end: date) -> list[str]:
    if start.day != 1 or end.day != monthrange(end.year, end.month)[1]:
        return []
    periods: list[str] = []
    cursor = start
    while cursor <= end:
        periods.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    return periods


def _week_periods(start: date, end: date, *, intersecting: bool) -> list[str]:
    if not intersecting and (start.weekday() != 0 or end.weekday() != 6):
        return []
    cursor = start - timedelta(days=start.weekday()) if intersecting else start
    periods: list[str] = []
    while cursor <= end:
        iso_year, iso_week, _ = cursor.isocalendar()
        periods.append(f"{iso_year:04d}-W{iso_week:02d}")
        cursor += timedelta(days=7)
    return periods


def periods_for_grain(
    start: date,
    end: date,
    grain: str,
    *,
    allow_intersecting_weeks: bool = False,
) -> list[str]:
    if grain == "year":
        if start == date(start.year, 1, 1) and end == date(end.year, 12, 31):
            return [f"{year:04d}" for year in range(start.year, end.year + 1)]
        return []
    if grain == "quarter":
        return _quarter_periods(start, end)
    if grain == "month":
        return _month_periods(start, end)
    if grain == "week":
        return _week_periods(start, end, intersecting=allow_intersecting_weeks)
    return []


def default_grains(start: date, end: date) -> list[str]:
    quarters = _quarter_periods(start, end)
    months = _month_periods(start, end)
    if quarters and len(months) >= 6:
        return ["quarter", "month", "week"]
    if months:
        return ["month", "week"]
    return ["week"]
