#!/usr/bin/env python3
"""Live source discovery and schema cache for ecom competitor macro data."""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_SOURCE_URL = (
    "https://bytedance.larkoffice.com/wiki/"
    "TrBAw0rDXiBrcUkJlbgcjsyYnkg?sheet=ESXBdZ&table=tbl5Ny6EgnsBwEBK&view=vew6r3PPJm"
)
INDEX_SCHEMA_VERSION = "competitor_source_index/2.2"
AUTO_MATCH_THRESHOLD = 0.90
CLARIFY_MATCH_THRESHOLD = 0.70
MIN_CANDIDATE_MARGIN = 0.12
PROTECTED_METRIC_TERMS = (
    "支付",
    "结算",
    "同比",
    "环比",
    "累计",
    "当期",
    "增量",
    "增速",
    "订单量",
    "订单价",
    "gmv",
)

BUILTIN_ALIASES = {
    "支付成交gmv": "支付GMV",
    "支付gmv": "支付GMV",
    "结算gmv": "结算GMV",
    "淘宝": "淘系",
    "天猫": "淘系",
    "pdd": "拼多多",
    "jd": "京东",
}

STANDARD_SHEETS = {
    "metric_metadata": "指标元信息",
    "dimension_metadata": "维度元信息",
    "week": "周度表",
    "month": "月度表",
    "quarter": "季度表",
    "year": "年度表",
}


class SkillError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "error", "code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", "", text).strip()
    return text.lower()


def display_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def split_terms(value: Any) -> list[str]:
    text = display_text(value)
    if not text:
        return []
    return [item.strip() for item in re.split(r"[、,，;；/\n]+", text) if item.strip()]


def normalize_grain(value: Any) -> str | None:
    """Map source metadata grain labels to the canonical sheet grain names."""
    token = normalize_text(value)
    aliases = {
        "月": "month", "月度": "month", "month": "month",
        "周": "week", "周度": "week", "week": "week",
        "季": "quarter", "季度": "quarter", "quarter": "quarter",
        "年": "year", "年度": "year", "year": "year",
    }
    return aliases.get(token)


def split_grains(value: Any) -> list[str]:
    grains: list[str] = []
    for term in split_terms(value):
        grain = normalize_grain(term)
        if grain and grain not in grains:
            grains.append(grain)
    return grains


def split_enum_values(value: Any) -> list[str]:
    """Split enum cells without breaking values that contain Chinese enumeration commas."""
    text = display_text(value)
    if not text:
        return []
    if "\n" in text or "\r" in text:
        return [item.strip() for item in re.split(r"[\r\n]+", text) if item.strip()]
    if ";" in text or "；" in text:
        return [item.strip() for item in re.split(r"[;；]+", text) if item.strip()]
    return [item.strip() for item in re.split(r"[、,，]+", text) if item.strip()]


def normalize_match_text(value: Any) -> str:
    text = normalize_text(value)
    return re.sub(r"[:：,，;；/\\_\-—()（）\[\]【】]+", "", text)


def _protected_term_conflicts(source: str, candidate: str) -> list[str]:
    source_normalized = normalize_match_text(source)
    candidate_normalized = normalize_match_text(candidate)
    conflicts: list[str] = []
    for term in PROTECTED_METRIC_TERMS:
        if (term in source_normalized) != (term in candidate_normalized):
            conflicts.append(f"protected_term_difference:{term}")
    return conflicts


def _candidate_names(
    catalogue: dict[str, dict[str, Any]],
) -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for name, metadata in catalogue.items():
        candidates.append((name, name, "standard_name"))
        for alias in metadata.get("aliases", []):
            candidates.append((alias, name, "metadata_alias"))
    for alias, name in BUILTIN_ALIASES.items():
        if name in catalogue:
            candidates.append((alias, name, "builtin_alias"))
    return candidates


def match_catalogue_name(
    value: str,
    catalogue: dict[str, dict[str, Any]],
    kind: str,
    context_dimension: str | None = None,
    dimension_values: list[str] | None = None,
    fact_values: list[str] | None = None,
) -> dict[str, Any]:
    """Return an auditable deterministic match decision for a fact label."""
    normalized = normalize_match_text(value)
    exact_matches: dict[str, tuple[str, float, str]] = {}
    for candidate_text, canonical, method in _candidate_names(catalogue):
        if normalize_match_text(candidate_text) == normalized:
            score = {"standard_name": 1.0, "metadata_alias": 0.98, "builtin_alias": 0.97}[method]
            previous = exact_matches.get(canonical)
            if previous is None or score > previous[1]:
                exact_matches[canonical] = (method, score, candidate_text)
    if len(exact_matches) == 1:
        canonical, (method, score, matched_text) = next(iter(exact_matches.items()))
        metadata = catalogue[canonical]
        conflicts = _protected_term_conflicts(value, matched_text) if kind == "metric" else []
        if context_dimension and metadata.get("dimensions"):
            if context_dimension not in metadata["dimensions"]:
                conflicts.append(f"dimension_mismatch:{context_dimension}")
        return {
            "decision": "reject" if conflicts else "auto",
            "confidence": score,
            "source_value": value,
            "canonical_name": canonical,
            "match_method": method,
            "evidence": [method],
            "conflicts": conflicts,
            "candidate_margin": 1.0,
            "candidates": [
                {
                    "name": canonical,
                    "confidence": score,
                    "matched_text": matched_text,
                    "match_method": method,
                    "conflicts": conflicts,
                }
            ],
        }
    if len(exact_matches) > 1:
        candidates = [
            {
                "name": name,
                "confidence": score,
                "matched_text": matched_text,
                "match_method": method,
                "conflicts": (
                    _protected_term_conflicts(value, matched_text)
                    if kind == "metric"
                    else []
                ),
            }
            for name, (method, score, matched_text) in sorted(
                exact_matches.items(), key=lambda item: item[1][1], reverse=True
            )
        ]
        return {
            "decision": "clarify",
            "confidence": candidates[0]["confidence"],
            "source_value": value,
            "canonical_name": None,
            "match_method": "ambiguous_exact",
            "evidence": ["multiple_exact_candidates"],
            "conflicts": ["candidate_not_unique"],
            "candidate_margin": 0.0,
            "candidates": candidates[:3],
        }

    fact_set = {normalize_match_text(item) for item in (fact_values or []) if display_text(item)}
    dimension_set = {
        normalize_match_text(item) for item in (dimension_values or []) if display_text(item)
    }
    overlap = (
        len(fact_set & dimension_set) / max(1, len(fact_set))
        if fact_set and dimension_set
        else None
    )
    scored_by_canonical: dict[str, dict[str, Any]] = {}
    for candidate_text, canonical, method in _candidate_names(catalogue):
        candidate_normalized = normalize_match_text(candidate_text)
        name_similarity = difflib.SequenceMatcher(None, normalized, candidate_normalized).ratio()
        metadata = catalogue[canonical]
        supported_dimensions = metadata.get("dimensions", [])
        dimension_match = (
            1.0
            if context_dimension and context_dimension in supported_dimensions
            else 0.0
        )
        if context_dimension:
            if overlap is None:
                score = 0.78 * name_similarity + 0.22 * dimension_match
            else:
                score = 0.68 * name_similarity + 0.22 * dimension_match + 0.10 * overlap
        else:
            score = name_similarity
        conflicts = _protected_term_conflicts(value, candidate_text) if kind == "metric" else []
        if context_dimension and supported_dimensions and not dimension_match:
            conflicts.append(f"dimension_mismatch:{context_dimension}")
        candidate = {
            "name": canonical,
            "confidence": round(score, 6),
            "matched_text": candidate_text,
            "name_similarity": round(name_similarity, 6),
            "dimension_match": bool(dimension_match) if context_dimension else None,
            "row_overlap": round(overlap, 6) if overlap is not None else None,
            "conflicts": conflicts,
            "match_method": f"contextual:{method}",
        }
        previous = scored_by_canonical.get(canonical)
        if previous is None or candidate["confidence"] > previous["confidence"]:
            scored_by_canonical[canonical] = candidate

    candidates = sorted(
        scored_by_canonical.values(), key=lambda item: item["confidence"], reverse=True
    )[:3]
    if not candidates:
        return {
            "decision": "reject",
            "confidence": 0.0,
            "source_value": value,
            "canonical_name": None,
            "match_method": "no_candidate",
            "evidence": [],
            "conflicts": ["no_candidate"],
            "candidate_margin": 0.0,
            "candidates": [],
        }
    best = candidates[0]
    margin = best["confidence"] - (candidates[1]["confidence"] if len(candidates) > 1 else 0.0)
    hard_conflicts = [item for item in best["conflicts"] if item.startswith("dimension_mismatch")]
    protected_conflicts = [
        item for item in best["conflicts"] if item.startswith("protected_term_difference")
    ]
    if best["confidence"] < CLARIFY_MATCH_THRESHOLD or hard_conflicts:
        decision = "reject"
    elif protected_conflicts or margin < MIN_CANDIDATE_MARGIN:
        decision = "clarify"
    elif best["confidence"] >= AUTO_MATCH_THRESHOLD:
        decision = "auto"
    else:
        decision = "clarify"
    evidence = ["name_similarity"]
    if context_dimension and best["dimension_match"]:
        evidence.append("dimension_exact")
    if best["row_overlap"] is not None:
        evidence.append("dimension_value_overlap")
    return {
        "decision": decision,
        "confidence": best["confidence"],
        "source_value": value,
        "canonical_name": best["name"] if decision == "auto" else None,
        "suggested_name": best["name"],
        "match_method": best["match_method"],
        "evidence": evidence,
        "conflicts": best["conflicts"],
        "candidate_margin": round(margin, 6),
        "candidates": candidates,
    }


def column_letter(number: int) -> str:
    if number < 1:
        raise ValueError("column number must be positive")
    chars: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        chars.append(chr(65 + remainder))
    return "".join(reversed(chars))


def column_number(letter: str) -> int:
    number = 0
    for char in letter.upper():
        if not "A" <= char <= "Z":
            raise ValueError(f"invalid column: {letter}")
        number = number * 26 + ord(char) - 64
    return number


def normalize_period(value: Any) -> tuple[str, str] | None:
    text = normalize_text(value)
    patterns = [
        ("month", r"(20\d{2})(?:年|[-/.])?(1[0-2]|0?[1-9])月?"),
        ("week", r"(20\d{2})(?:年)?(?:第|[-/]?w)([0-5]?\d)周?"),
        ("quarter", r"(20\d{2})(?:年)?(?:第?([1-4])季度|[-/]?q([1-4]))"),
        ("year", r"(20\d{2})年?"),
    ]
    for granularity, pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        year = int(match.group(1))
        if granularity == "month":
            return granularity, f"{year:04d}-{int(match.group(2)):02d}"
        if granularity == "week":
            week = int(match.group(2))
            if 1 <= week <= 53:
                return granularity, f"{year:04d}-W{week:02d}"
            return None
        if granularity == "quarter":
            quarter = int(match.group(2) or match.group(3))
            return granularity, f"{year:04d}-Q{quarter}"
        return granularity, f"{year:04d}"
    return None


def previous_year_period(period: str) -> str:
    match = re.match(r"^(20\d{2})(.*)$", period)
    if not match:
        raise SkillError("invalid_period", f"无法生成去年同期：{period}")
    return f"{int(match.group(1)) - 1:04d}{match.group(2)}"


def parse_csv_payload(payload: dict[str, Any]) -> list[list[str]]:
    text = payload.get("annotated_csv", "")
    text = re.sub(r"(?m)^\[row=\d+\]\s*", "", text)
    # lark-cli leaves one leading space when row prefixes are disabled.
    return list(csv.reader(io.StringIO(text), skipinitialspace=True))


class LarkClient:
    def __init__(self, identity: str = "user", timeout: int = 40):
        self.identity = identity
        self.timeout = timeout

    def _run(self, args: list[str]) -> dict[str, Any]:
        env = os.environ.copy()
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        try:
            proc = subprocess.run(
                ["lark-cli", *args, "--as", self.identity, "--format", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SkillError("missing_dependency", "当前环境未安装 lark-cli") from exc
        except subprocess.TimeoutExpired as exc:
            raise SkillError("source_timeout", "读取飞书表格超时") from exc

        raw = proc.stdout.strip() or proc.stderr.strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SkillError(
                "invalid_lark_response",
                "lark-cli 未返回可解析的 JSON",
                {"returncode": proc.returncode, "stderr": proc.stderr[-1000:]},
            ) from exc
        if proc.returncode != 0 or not result.get("ok"):
            error = result.get("error", result)
            raise SkillError(
                "source_read_failed",
                error.get("message", "读取飞书表格失败"),
                error,
            )
        return result["data"]

    def resolve_source(self, source_url: str) -> dict[str, Any]:
        data = self._run(["wiki", "+node-get", "--node-token", source_url])
        if data.get("obj_type") != "sheet":
            raise SkillError(
                "unsupported_source",
                f"目标 Wiki 节点不是电子表格：{data.get('obj_type')}",
            )
        return data

    def workbook_info(self, spreadsheet_token: str) -> dict[str, Any]:
        return self._run(
            ["sheets", "+workbook-info", "--spreadsheet-token", spreadsheet_token]
        )

    def revision(self, spreadsheet_token: str) -> int:
        return int(
            self._run(
                ["sheets", "+revision-get", "--spreadsheet-token", spreadsheet_token]
            )["revision"]
        )

    def read_csv(
        self,
        spreadsheet_token: str,
        sheet_id: str,
        cell_range: str,
        max_chars: int = 500000,
    ) -> dict[str, Any]:
        return self._run(
            [
                "sheets",
                "+csv-get",
                "--spreadsheet-token",
                spreadsheet_token,
                "--sheet-id",
                sheet_id,
                "--range",
                cell_range,
                "--include-row-prefix=false",
                "--max-chars",
                str(max_chars),
            ]
        )


def _header_index(row: list[str], candidates: Iterable[str]) -> int | None:
    wanted = {normalize_text(item) for item in candidates}
    for index, value in enumerate(row):
        if normalize_text(value) in wanted:
            return index
    return None


def classify_preview(rows: list[list[str]], sheet_name: str) -> dict[str, Any]:
    metric_header = False
    dimension_header = False
    metric_blocks = 0
    period_counts = {key: 0 for key in ("month", "week", "quarter", "year")}
    for row in rows:
        normalized = {normalize_text(cell) for cell in row if display_text(cell)}
        if normalized.intersection({"指标名称", "指标名"}) and normalized.intersection(
            {"聚合方式", "聚合规则", "可聚合性"}
        ):
            metric_header = True
        if normalized.intersection({"维度名称", "维度名"}) and normalized.intersection(
            {"枚举值", "维度值", "可选值"}
        ):
            dimension_header = True
        for cell in row:
            if re.search(r"指标\s*[:：]", display_text(cell)):
                metric_blocks += 1
            parsed = normalize_period(cell)
            if parsed:
                period_counts[parsed[0]] += 1
    best_granularity = max(period_counts, key=period_counts.get)
    if period_counts[best_granularity] == 0:
        best_granularity = ""
    name = normalize_text(sheet_name)
    name_hint = next(
        (
            granularity
            for granularity, hints in {
                "month": ("月", "month"),
                "week": ("周", "week"),
                "quarter": ("季", "quarter"),
                "year": ("年", "year"),
            }.items()
            if any(hint in name for hint in hints)
        ),
        "",
    )
    return {
        "metric_metadata": metric_header,
        "dimension_metadata": dimension_header,
        "metric_blocks": metric_blocks,
        "period_counts": period_counts,
        "granularity": best_granularity,
        "name_hint": name_hint,
    }


def parse_metric_metadata(rows: list[list[str]]) -> dict[str, dict[str, Any]]:
    header_row = None
    positions: dict[str, int] = {}
    for row_number, row in enumerate(rows):
        metric_pos = _header_index(row, ["指标名称", "指标名"])
        aggregation_pos = _header_index(row, ["聚合方式", "聚合规则", "可聚合性"])
        if metric_pos is not None and aggregation_pos is not None:
            header_row = row_number
            positions = {
                "name": metric_pos,
                "aliases": _header_index(row, ["指标别名", "别名"]),
                "unit": _header_index(row, ["数值单位", "单位"]),
                "supported_grains": _header_index(row, ["可支持时间粒度", "支持时间粒度", "时间粒度"]),
                "dimensions": _header_index(row, ["可支持拆解维度", "支持维度", "拆解维度"]),
                "aggregation": aggregation_pos,
                "notes": _header_index(row, ["口径备注", "备注", "口径说明"]),
            }
            break
    if header_row is None:
        raise SkillError("invalid_metric_metadata", "未找到完整的指标元信息表头")

    metrics: dict[str, dict[str, Any]] = {}
    for row in rows[header_row + 1 :]:
        if positions["name"] >= len(row):
            continue
        name = display_text(row[positions["name"]])
        if not name:
            continue

        def cell(key: str) -> str:
            pos = positions[key]
            return display_text(row[pos]) if pos is not None and pos < len(row) else ""

        aggregation = cell("aggregation")
        additive = "不可聚合" not in aggregation and "可聚合" in aggregation
        supported_grains = split_grains(cell("supported_grains"))
        metrics[name] = {
            "aliases": split_terms(cell("aliases")),
            "unit": cell("unit"),
            "supported_grains": supported_grains,
            "dimensions": split_terms(cell("dimensions")),
            "aggregation": aggregation,
            "aggregation_mode": "additive" if additive else "non_additive" if aggregation else "unknown",
            "additive": additive,
            "arithmetic": "加减乘除" in aggregation,
            "notes": cell("notes"),
        }
    if not metrics:
        raise SkillError("invalid_metric_metadata", "指标元信息中没有可用指标")
    return metrics


def parse_dimension_metadata(rows: list[list[str]]) -> dict[str, dict[str, Any]]:
    header_row = None
    positions: dict[str, int | None] = {}
    for row_number, row in enumerate(rows):
        name_pos = _header_index(row, ["维度名称", "维度名"])
        values_pos = _header_index(row, ["枚举值", "维度值", "可选值"])
        if name_pos is not None and values_pos is not None:
            header_row = row_number
            positions = {
                "name": name_pos,
                "aliases": _header_index(row, ["维度别名", "别名"]),
                "values": values_pos,
            }
            break
    if header_row is None:
        raise SkillError("invalid_dimension_metadata", "未找到完整的维度元信息表头")

    dimensions: dict[str, dict[str, Any]] = {}
    for row in rows[header_row + 1 :]:
        name_pos = int(positions["name"])
        if name_pos >= len(row):
            continue
        name = display_text(row[name_pos])
        if not name:
            continue
        aliases_pos = positions["aliases"]
        values_pos = int(positions["values"])
        dimensions[name] = {
            "aliases": split_terms(row[aliases_pos])
            if aliases_pos is not None and aliases_pos < len(row)
            else [],
            "values": split_enum_values(row[values_pos]) if values_pos < len(row) else [],
        }
    if not dimensions:
        raise SkillError("invalid_dimension_metadata", "维度元信息中没有可用维度")
    return dimensions


def _alias_map(catalogue: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}

    def add(alias: Any, canonical: str) -> None:
        token = normalize_match_text(alias)
        values = aliases.setdefault(token, [])
        if canonical not in values:
            values.append(canonical)

    for name, metadata in catalogue.items():
        add(name, name)
        for alias in metadata.get("aliases", []):
            add(alias, name)
    for alias, name in BUILTIN_ALIASES.items():
        if name in catalogue:
            add(alias, name)
    return aliases


def resolve_catalogue_name(
    value: str,
    catalogue: dict[str, dict[str, Any]],
    kind: str,
    strict: bool = True,
) -> str | None:
    aliases = _alias_map(catalogue)
    normalized = normalize_match_text(value)
    exact = aliases.get(normalized) or []
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        if not strict:
            return None
        raise SkillError(
            f"ambiguous_{kind}",
            f"{kind}名称或别名对应多个候选：{value}",
            {"candidates": exact[:3]},
        )
    if not strict:
        return None
    candidates = difflib.get_close_matches(normalized, aliases.keys(), n=3, cutoff=0.55)
    raise SkillError(
        f"unknown_{kind}",
        f"无法通过元信息标准化{kind}：{value}",
        {
            "candidates": list(dict.fromkeys(
                canonical
                for item in candidates
                for canonical in aliases[item]
            ))[:3]
        },
    )


def _extract_marker(cells: Iterable[str], marker: str) -> str:
    pattern = re.compile(rf"{marker}\s*[:：]\s*(.+)", re.IGNORECASE)
    for cell in cells:
        match = pattern.search(display_text(cell))
        if match:
            return match.group(1).strip()
    return ""


def parse_data_sheet(
    header_rows: list[list[str]],
    label_rows: list[list[str]],
    sheet: dict[str, Any],
    metrics: dict[str, dict[str, Any]],
    dimensions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    period_choice: tuple[int, str, dict[str, str]] | None = None
    for row_number, row in enumerate(header_rows, start=1):
        by_granularity: dict[str, dict[str, str]] = {
            key: {} for key in ("month", "week", "quarter", "year")
        }
        for column_index, cell in enumerate(row, start=1):
            parsed = normalize_period(cell)
            if parsed:
                by_granularity[parsed[0]][parsed[1]] = column_letter(column_index)
        granularity = max(by_granularity, key=lambda key: len(by_granularity[key]))
        periods = by_granularity[granularity]
        if periods and (period_choice is None or len(periods) > len(period_choice[2])):
            period_choice = (row_number, granularity, periods)

    blocks: list[dict[str, Any]] = []
    for row_number, row in enumerate(label_rows, start=1):
        metric_raw = _extract_marker(row[:2], "指标")
        if not metric_raw:
            continue
        dimension_raw = _extract_marker(row[:2], "维度") or "无"
        blocks.append(
            {
                "header_row": row_number,
                "raw_metric": metric_raw,
                "raw_dimension": dimension_raw,
            }
        )

    block_map: dict[str, dict[str, Any]] = {}
    unresolved_blocks: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, block in enumerate(blocks):
        end_row = blocks[index + 1]["header_row"] - 1 if index + 1 < len(blocks) else len(label_rows)
        rows: dict[str, int] = {}
        for row_number in range(block["header_row"] + 1, end_row + 1):
            row = label_rows[row_number - 1]
            value = display_text(row[0]) if row else ""
            if not value:
                continue
            rows[value] = row_number
        if normalize_text(block["raw_dimension"]) in {"无", "none"}:
            dimension_match = {
                "decision": "auto",
                "confidence": 1.0,
                "source_value": block["raw_dimension"],
                "canonical_name": "无",
                "match_method": "no_dimension",
                "evidence": ["explicit_no_dimension"],
                "conflicts": [],
                "candidate_margin": 1.0,
                "candidates": [{"name": "无", "confidence": 1.0}],
            }
        else:
            dimension_match = match_catalogue_name(
                block["raw_dimension"], dimensions, "dimension"
            )
        dimension = dimension_match.get("canonical_name")
        dimension_enum = dimensions.get(dimension, {}).get("values", []) if dimension else []
        metric_match = match_catalogue_name(
            block["raw_metric"],
            metrics,
            "metric",
            context_dimension=dimension,
            dimension_values=dimension_enum,
            fact_values=list(rows),
        )
        metric = metric_match.get("canonical_name")
        if metric and dimension:
            key = metric
            if key in block_map:
                raise SkillError(
                    "duplicate_metric_block",
                    f"同一数据表中存在重复指标块：{key}",
                    {
                        "first_row": block_map[key]["header_row"],
                        "duplicate_row": block["header_row"],
                    },
                )
            block_map[key] = {
                "header_row": block["header_row"],
                "raw_metric": block["raw_metric"],
                "dimension": dimension,
                "raw_dimension": block["raw_dimension"],
                "rows": rows,
                "metric_match": metric_match,
                "dimension_match": dimension_match,
            }
        else:
            decision = (
                "clarify"
                if "clarify" in {metric_match["decision"], dimension_match["decision"]}
                else "reject"
            )
            unresolved = {
                "decision": decision,
                "header_row": block["header_row"],
                "raw_metric": block["raw_metric"],
                "raw_dimension": block["raw_dimension"],
                "rows": rows,
                "metric_match": metric_match,
                "dimension_match": dimension_match,
            }
            unresolved_blocks.append(unresolved)
            suggested = metric_match.get("suggested_name")
            if decision == "clarify":
                warnings.append(
                    f"数据块需澄清：{block['raw_metric']} -> {suggested or '无候选'}"
                )
            else:
                warnings.append(
                    f"数据块拒绝匹配：{block['raw_metric']} / {block['raw_dimension']}"
                )

    name_hint = classify_preview(header_rows, sheet["sheet_name"])["name_hint"]
    if not period_choice:
        return {
            "sheet_id": sheet["sheet_id"],
            "sheet_name": sheet["sheet_name"],
            "available": False,
            "reason": "未同时发现标准周期表头和可识别指标块",
            "name_hint": name_hint,
            "warnings": warnings,
            "unresolved_blocks": unresolved_blocks,
        }
    period_row, granularity, periods = period_choice
    return {
        "sheet_id": sheet["sheet_id"],
        "sheet_name": sheet["sheet_name"],
        "available": bool(block_map),
        "reason": None if block_map else "已发现标准周期表头，但指标块尚待解析",
        "granularity": granularity,
        "period_row": period_row,
        "periods": periods,
        "blocks": block_map,
        "unresolved_blocks": unresolved_blocks,
        "name_hint": name_hint,
        "warnings": warnings,
    }


def _read_preview(client: LarkClient, token: str, sheet: dict[str, Any]) -> dict[str, Any]:
    last_col = column_letter(int(sheet["column_count"]))
    last_row = min(int(sheet["row_count"]), 25)
    payload = client.read_csv(token, sheet["sheet_id"], f"A1:{last_col}{last_row}", 120000)
    return {"sheet": sheet, "payload": payload, "rows": parse_csv_payload(payload)}


def select_standard_sheets(
    sheets: list[dict[str, Any]],
    role_names: dict[str, list[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve only the six supported sheets; unrelated workbook tabs are invisible."""
    selected: dict[str, dict[str, Any]] = {}
    configured = role_names or {role: [name] for role, name in STANDARD_SHEETS.items()}
    for role, default_name in STANDARD_SHEETS.items():
        expected_names = configured.get(role) or [default_name]
        matches = [
            sheet for sheet in sheets
            if any(
                normalize_text(sheet.get("sheet_name")) == normalize_text(expected_name)
                for expected_name in expected_names
            )
        ]
        if len(matches) > 1:
            raise SkillError(
                "duplicate_standard_sheet",
                f"标准工作表角色 {role} 只能匹配一张",
                {"role": role, "candidates": [item.get("sheet_name") for item in matches]},
            )
        if matches:
            selected[role] = matches[0]
    missing_metadata = [
        "/".join(configured.get(role) or [STANDARD_SHEETS[role]])
        for role in ("metric_metadata", "dimension_metadata")
        if role not in selected
    ]
    if missing_metadata:
        raise SkillError(
            "missing_metadata_sheet",
            "缺少必需的元信息工作表",
            {"missing": missing_metadata},
        )
    return selected


def _assert_revision(payload: dict[str, Any], expected: int) -> None:
    observed = payload.get("revision")
    if observed is not None and int(observed) != expected:
        raise SkillError(
            "concurrent_modification",
            "解析结构期间表格 revision 发生变化",
            {"expected": expected, "observed": observed},
        )


def _build_once(
    client: LarkClient,
    source_url: str,
    role_names: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    node = client.resolve_source(source_url)
    token = node["obj_token"]
    start_revision = client.revision(token)
    workbook = client.workbook_info(token)
    if int(workbook.get("revision", start_revision)) != start_revision:
        raise SkillError("concurrent_modification", "读取工作表清单时 revision 已变化")
    sheets = workbook.get("sheets", [])
    if not sheets:
        raise SkillError("empty_workbook", "目标工作簿没有工作表")
    standard = select_standard_sheets(sheets, role_names)
    metric_sheet = standard["metric_metadata"]
    dimension_sheet = standard["dimension_metadata"]
    metric_last_col = column_letter(min(int(metric_sheet["column_count"]), 20))
    dimension_last_col = column_letter(min(int(dimension_sheet["column_count"]), 20))
    with ThreadPoolExecutor(max_workers=2) as pool:
        metric_future = pool.submit(
            client.read_csv,
            token,
            metric_sheet["sheet_id"],
            f"A1:{metric_last_col}{metric_sheet['row_count']}",
        )
        dimension_future = pool.submit(
            client.read_csv,
            token,
            dimension_sheet["sheet_id"],
            f"A1:{dimension_last_col}{dimension_sheet['row_count']}",
        )
        metric_payload = metric_future.result()
        dimension_payload = dimension_future.result()
    _assert_revision(metric_payload, start_revision)
    _assert_revision(dimension_payload, start_revision)
    metrics = parse_metric_metadata(parse_csv_payload(metric_payload))
    dimensions = parse_dimension_metadata(parse_csv_payload(dimension_payload))

    grain_sheets: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    fact_roles = ("week", "month", "quarter", "year")
    previews: dict[str, dict[str, Any]] = {}
    present = {role: standard[role] for role in fact_roles if role in standard}
    if present:
        with ThreadPoolExecutor(max_workers=len(present)) as pool:
            futures = {
                pool.submit(_read_preview, client, token, sheet): role
                for role, sheet in present.items()
            }
            for future in as_completed(futures):
                role = futures[future]
                preview = future.result()
                _assert_revision(preview["payload"], start_revision)
                previews[role] = preview

    label_payloads: dict[str, dict[str, Any]] = {}
    if previews:
        with ThreadPoolExecutor(max_workers=len(previews)) as pool:
            futures = {
                pool.submit(
                    client.read_csv,
                    token,
                    preview["sheet"]["sheet_id"],
                    f"A1:B{preview['sheet']['row_count']}",
                ): role
                for role, preview in previews.items()
            }
            for future in as_completed(futures):
                role = futures[future]
                payload = future.result()
                _assert_revision(payload, start_revision)
                label_payloads[role] = payload

    for granularity in fact_roles:
        sheet = standard.get(granularity)
        if sheet is None:
            grain_sheets[granularity] = {
                "available": False,
                "sheet_name": STANDARD_SHEETS[granularity],
                "reason": "标准粒度工作表不存在",
            }
            continue
        preview = previews[granularity]
        classification = classify_preview(preview["rows"], sheet["sheet_name"])
        if not classification["metric_blocks"] or not classification["granularity"]:
            grain_sheets[granularity] = {
                "sheet_id": sheet["sheet_id"],
                "sheet_name": sheet["sheet_name"],
                "available": False,
                "reason": "工作表存在但未发现数据结构",
            }
            continue
        parsed = parse_data_sheet(
            preview["rows"],
            parse_csv_payload(label_payloads[granularity]),
            sheet,
            metrics,
            dimensions,
        )
        if parsed.get("available") and parsed.get("granularity") != granularity:
            raise SkillError(
                "fact_sheet_granularity_mismatch",
                f"{sheet['sheet_name']} 的内容粒度与标准名称不一致",
                {"expected": granularity, "observed": parsed.get("granularity")},
            )
        grain_sheets[granularity] = parsed
        warnings.extend(parsed.get("warnings", []))

    end_revision = client.revision(token)
    if end_revision != start_revision:
        raise SkillError(
            "concurrent_modification",
            "解析结构期间表格发生变化",
            {"before": start_revision, "after": end_revision},
        )

    index: dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "status": "ok",
        "source": {
            "url": source_url,
            "wiki_node_token": node.get("node_token"),
            "spreadsheet_token": token,
            "title": node.get("title"),
            "revision": start_revision,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        "metadata_sheets": {
            "metric": {
                "sheet_id": metric_sheet["sheet_id"],
                "sheet_name": metric_sheet["sheet_name"],
            },
            "dimension": {
                "sheet_id": dimension_sheet["sheet_id"],
                "sheet_name": dimension_sheet["sheet_name"],
            },
        },
        "metrics": metrics,
        "dimensions": dimensions,
        "sheets": grain_sheets,
        "values": {},
        "warnings": sorted(set(warnings)),
    }
    schema_payload = {
        key: index[key] for key in ("metadata_sheets", "metrics", "dimensions", "sheets")
    }
    index["source"]["schema_hash"] = hashlib.sha256(
        json.dumps(schema_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return index


def build_live_index(
    client: LarkClient,
    source_url: str = DEFAULT_SOURCE_URL,
    attempts: int = 2,
    role_names: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    last_error: SkillError | None = None
    for _ in range(attempts):
        try:
            return _build_once(client, source_url, role_names)
        except SkillError as exc:
            last_error = exc
            if exc.code != "concurrent_modification":
                raise
    assert last_error is not None
    raise last_error


def load_index(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SkillError("missing_index", f"索引文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SkillError("invalid_index", f"索引文件不是有效 JSON：{path}") from exc


def save_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, prefix=f".{path.name}."
    ) as handle:
        json.dump(index, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temp_name = handle.name
    Path(temp_name).replace(path)


def ensure_fresh_index(
    path: Path,
    client: LarkClient,
    source_url: str = DEFAULT_SOURCE_URL,
    allow_stale: bool = False,
    role_names: dict[str, list[str]] | None = None,
) -> tuple[dict[str, Any], str]:
    try:
        index = load_index(path)
    except SkillError as exc:
        if exc.code != "missing_index":
            raise
        index = build_live_index(client, source_url, role_names=role_names)
        save_index(path, index)
        return index, "rebuilt"

    if index.get("schema_version") != INDEX_SCHEMA_VERSION:
        index = build_live_index(client, source_url, role_names=role_names)
        save_index(path, index)
        return index, "rebuilt"

    token = index.get("source", {}).get("spreadsheet_token")
    if not token:
        index = build_live_index(client, source_url, role_names=role_names)
        save_index(path, index)
        return index, "rebuilt"
    try:
        remote_revision = client.revision(token)
    except SkillError:
        if allow_stale:
            return index, "stale"
        raise
    if remote_revision == int(index.get("source", {}).get("revision", -1)):
        return index, "hit"
    index = build_live_index(client, source_url, role_names=role_names)
    save_index(path, index)
    return index, "rebuilt"


def error_json(exc: Exception) -> str:
    if isinstance(exc, SkillError):
        payload = exc.as_dict()
    else:
        payload = {"status": "error", "code": "internal_error", "message": str(exc)}
    return json.dumps(payload, ensure_ascii=False)
