#!/usr/bin/env python3
"""Read competitor Feishu cells and emit scene-analysis facts directly."""
from __future__ import annotations

import math
import re
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

VENDOR = Path(__file__).resolve().parent / "_vendor"
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from ecom_competitor_source import (  # noqa: E402
    BUILTIN_ALIASES,
    DEFAULT_SOURCE_URL,
    SkillError,
    column_letter,
    column_number,
    display_text,
    normalize_period,
    parse_csv_payload,
    resolve_catalogue_name,
)
from dimension_domain_registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH as DEFAULT_DIMENSION_SET_REGISTRY,
    DimensionDomainError,
    load_dimension_set_registry,
    normalize_token,
    registry_hash,
    resolve_dimension_domain,
    source_dimension_domain_ref,
)
from fact_contract import SCENE_FACTS_V2, build_fact_demands, stable_id  # noqa: E402
from source_runtime import ManagedLarkClient as LarkClient, ensure_shared_index  # noqa: E402


MISSING_VALUES = {"", "-", "--", "/", "N/A", "n/a", "null", "None"}
FACT_PROVIDER_VERSION = "1.1.0"
_UNIT_SUFFIX = re.compile(r"^(.*?)(%|pp)\s*$", re.IGNORECASE)


def parse_source_number(value: Any, expected_unit: Any) -> float | None:
    """Parse a source cell according to its declared metric unit."""
    text = display_text(value).replace(",", "")
    if text in MISSING_VALUES:
        return None
    declared_unit = str(expected_unit or "").strip().lower()
    suffix_match = _UNIT_SUFFIX.fullmatch(text.strip())
    if suffix_match is not None:
        text, suffix = suffix_match.groups()
        suffix = suffix.lower()
        if suffix != declared_unit:
            raise SkillError(
                "numeric_unit_mismatch",
                f"源单元格数值后缀 {suffix!r} 与指标声明单位 {expected_unit!r} 不一致：{value}",
            )
        text = text.strip()
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise SkillError("invalid_numeric_value", f"源单元格不是数值：{value}") from exc
    if not math.isfinite(number):
        raise SkillError("invalid_numeric_value", f"源单元格不是有限数值：{value}")
    return number


def _source_metric(slot: dict[str, Any], index: dict[str, Any]) -> str:
    requested = str(slot.get("source_metric_name") or slot.get("metric") or slot.get("metric_ref") or "")
    try:
        resolved = resolve_catalogue_name(requested, index["metrics"], "metric")
    except SkillError:
        resolved = None
    if resolved:
        return resolved
    raise SkillError(
        "unknown_metric",
        f"竞品元信息中不存在指标：{requested}",
        {"candidates": sorted(index.get("metrics", {}))},
    )


def _source_dimension(value: str, index: dict[str, Any]) -> str:
    resolved = resolve_catalogue_name(value, index["dimensions"], "dimension")
    if resolved is None:
        raise SkillError("unknown_dimension", f"竞品元信息中不存在维度：{value}")
    return resolved


def _selector_raw(
    slot: dict[str, Any], dimension: str, index: dict[str, Any]
) -> Any:
    selectors = slot.get("selector_dimensions") or slot.get("dimensions") or {}
    if not isinstance(selectors, dict):
        raise SkillError("invalid_fact_slot", "selector_dimensions 必须是对象")
    if dimension in selectors:
        return selectors[dimension]
    for requested_dimension, raw in selectors.items():
        try:
            if _source_dimension(str(requested_dimension), index) == dimension:
                return raw
        except SkillError:
            continue
    return None


def _uses_named_dimension_set(
    dimension: str,
    requested: Any,
    registry: dict[str, Any],
) -> bool:
    requested_values = requested if isinstance(requested, list) else [requested]
    requested_tokens = {normalize_token(value) for value in requested_values}
    for set_id, definition in (registry.get("sets") or {}).items():
        if normalize_token(definition.get("dimension")) != normalize_token(dimension):
            continue
        aliases = {normalize_token(set_id)} | {
            normalize_token(alias) for alias in definition.get("aliases") or []
        }
        if requested_tokens & aliases:
            return True
    return False


def _available_dimension_values(
    index: dict[str, Any],
    dimension: str,
    *,
    include_block_rows: bool,
) -> list[str]:
    values = list((index.get("dimensions", {}).get(dimension) or {}).get("values", []))
    if include_block_rows:
        for sheet in (index.get("sheets") or {}).values():
            for block in (sheet.get("blocks") or {}).values():
                if block.get("dimension") == dimension:
                    values.extend((block.get("rows") or {}).keys())
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _resolve_dimension_from_value(
    slot: dict[str, Any],
    requested_dimension: str,
    index: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    selectors = slot.get("selector_dimensions") or slot.get("dimensions") or {}
    if not isinstance(selectors, dict):
        raise SkillError("invalid_fact_slot", "selector_dimensions 必须是对象")
    raw = selectors.get(requested_dimension)
    if raw is None and len(selectors) == 1:
        raw = next(iter(selectors.values()))
    if raw is None:
        resolved = _source_dimension(requested_dimension, index)
        return {
            "requested_dimension": requested_dimension,
            "resolved_dimension": resolved,
            "requested_values": [],
            "members": [],
            "candidates": [resolved],
            "selection_basis": "dimension_name",
        }

    candidates: list[dict[str, Any]] = []
    uses_named_set = _uses_named_dimension_set(
        requested_dimension, raw, registry
    )
    for dimension, metadata in index.get("dimensions", {}).items():
        try:
            resolution = resolve_dimension_domain(
                str(dimension),
                raw,
                _available_dimension_values(
                    index,
                    str(dimension),
                    include_block_rows=(
                        uses_named_set
                        and normalize_token(dimension)
                        == normalize_token(requested_dimension)
                    ),
                ),
                registry,
                value_aliases=BUILTIN_ALIASES,
            )
        except DimensionDomainError:
            continue
        candidates.append(resolution)

    raw_values = raw if isinstance(raw, list) else [raw]
    if not candidates:
        raise SkillError(
            "unknown_dimension_value",
            f"竞品维度元信息中不存在维度值：{raw_values}",
            {"requested_values": raw_values, "candidates": []},
        )
    candidate_dimensions = [str(item["dimension"]) for item in candidates]
    if len(candidates) > 1:
        raise SkillError(
            "ambiguous_dimension_value",
            f"维度值 {raw_values} 同时匹配多个维度",
            {
                "requested_values": raw_values,
                "candidates": candidate_dimensions,
            },
        )

    resolution = candidates[0]
    resolved_dimension = str(resolution["dimension"])
    try:
        requested_source_dimension = _source_dimension(requested_dimension, index)
    except SkillError:
        requested_source_dimension = None
    if requested_source_dimension != resolved_dimension:
        raise SkillError(
            "dimension_resolution_required",
            f"维度值 {raw_values} 唯一属于 {resolved_dimension}，不是 {requested_dimension}",
            {
                "requested_values": raw_values,
                "candidates": candidate_dimensions,
                "resolution_patch": {
                    "from_dimension": requested_dimension,
                    "to_dimension": resolved_dimension,
                    "value": raw,
                },
            },
        )
    return {
        "requested_dimension": requested_dimension,
        "resolved_dimension": resolved_dimension,
        "requested_values": list(resolution.get("requested_values", raw_values)),
        "members": list(resolution.get("members", [])),
        "candidates": candidate_dimensions,
        "selection_basis": "source_metadata_unique",
    }


def _selector_values(
    slot: dict[str, Any],
    dimension: str,
    index: dict[str, Any],
    registry: dict[str, Any] | None = None,
) -> list[str | None]:
    resolution = _selector_resolution(
        slot,
        dimension,
        index,
        registry or load_dimension_set_registry(),
    )
    return resolution["members"]


def _selector_resolution(
    slot: dict[str, Any],
    dimension: str,
    index: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, Any]:
    raw = _selector_raw(slot, dimension, index)
    source_domains = slot.get("source_dimension_domains") or {}
    if not isinstance(source_domains, dict):
        raise SkillError("invalid_fact_slot", "source_dimension_domains 必须是对象")
    full_domain_ref = source_domains.get(dimension)
    if full_domain_ref is not None:
        expected_ref = source_dimension_domain_ref(dimension)
        if full_domain_ref != expected_ref:
            raise SkillError(
                "invalid_dimension_domain_ref",
                f"物理维度 {dimension} 的全域引用无效",
                {"expected": expected_ref, "actual": full_domain_ref},
            )
        if raw is not None:
            raise SkillError(
                "conflicting_dimension_domain",
                f"物理维度 {dimension} 不能同时声明全域和具体选择值",
            )
    logical_dimension = str(
        (slot.get("dimension_projection") or {}).get(dimension) or dimension
    )
    available = _available_dimension_values(
        index,
        dimension,
        include_block_rows=(
            full_domain_ref is not None
            or (raw is not None and _uses_named_dimension_set(logical_dimension, raw, registry))
        ),
    )
    if raw is None:
        result = {
            "dimension": logical_dimension,
            "requested_values": [],
            "members": available,
            "set_ids": [],
            "registry_hash": registry_hash(registry),
            "domain_id": full_domain_ref,
        }
        if full_domain_ref is not None:
            result["domain_kind"] = "source_dimension_all"
        return result
    try:
        return resolve_dimension_domain(
            logical_dimension,
            raw,
            available,
            registry,
            value_aliases=BUILTIN_ALIASES,
        )
    except DimensionDomainError as exc:
        code = (
            "unknown_dimension_value"
            if exc.code == "unavailable_dimension_domain_member"
            and not isinstance(raw, list)
            else exc.code
        )
        raise SkillError(code, exc.message, exc.details) from exc


def _period_slot(slot: dict[str, Any], index: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    raw_period = str(slot.get("period") or "")
    parsed = normalize_period(raw_period)
    if not parsed:
        raise SkillError("invalid_period", f"无法识别周期：{raw_period}")
    granularity, period = parsed
    sheet = index.get("sheets", {}).get(granularity)
    if not sheet or not sheet.get("available"):
        raise SkillError("granularity_unavailable", f"竞品数据源不支持 {granularity} 粒度")
    if period not in sheet.get("periods", {}):
        raise SkillError("period_unavailable", f"竞品数据源不存在周期：{period}")
    return granularity, period, sheet




def _overall_row_for_no_dimension_block(block: dict[str, Any], metric: str) -> int | None:
    rows = block.get("rows") or {}
    if not isinstance(rows, dict):
        raise SkillError(
            "invalid_block_rows",
            f"指标 {metric} 的数据行索引必须是对象",
            {"metric": metric, "rows_type": type(rows).__name__},
        )

    if block.get("dimension") != "无":
        return block.get("header_row")

    # 新版源表约定：维度=无时，rows 中唯一一行就是整体数据行。
    if len(rows) == 1:
        return int(next(iter(rows.values())))

    # 兼容未来显式命名整体行的无维度块。
    for overall_label in ("整体", "合计", "全部"):
        if overall_label in rows:
            return int(rows[overall_label])

    # 兼容行名直接等于指标名的无维度块。
    if metric in rows:
        return int(rows[metric])

    # 兼容极少数旧表：标题行本身就是数据行。
    if not rows:
        return block.get("header_row")

    raise SkillError(
        "ambiguous_no_dimension_rows",
        f"指标 {metric} 维度为无，但存在多行数据，无法确定整体行",
        {"metric": metric, "rows": list(rows.keys())},
    )


def _requested_cells(
    request: dict[str, Any],
    index: dict[str, Any],
    registry: dict[str, Any],
    dimension_resolutions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    demands = request.get("fact_demands")
    if demands is None:
        slots = request.get("fact_slots", [])
        if not isinstance(slots, list):
            raise SkillError("invalid_fact_slot", "fact_slots 必须是数组")
        demands = build_fact_demands(slots)
    if not isinstance(demands, list):
        raise SkillError("invalid_fact_demand", "fact_demands 必须是数组")
    for demand in demands:
        if not isinstance(demand, dict) or not demand.get("fact_demand_id"):
            raise SkillError("invalid_fact_demand", "每个物理事实需求必须有 fact_demand_id")
        metric = _source_metric(demand, index)
        dimensions = demand.get("dimension_refs", [])
        if not isinstance(dimensions, list):
            raise SkillError("invalid_fact_slot", "dimension_refs 必须是数组")
        if len(dimensions) > 1:
            raise SkillError(
                "unsupported_dimension_grain",
                "竞品源表当前只支持单维度事实槽位",
                {"dimensions": dimensions},
            )
        if dimensions:
            requested_dimension = str(dimensions[0])
            projection = demand.get("dimension_projection") or {}
            if requested_dimension in projection:
                domain = _selector_resolution(
                    demand, requested_dimension, index, registry
                )
                resolution = {
                    **domain,
                    "requested_dimension": str(projection[requested_dimension]),
                    "resolved_dimension": requested_dimension,
                    "candidates": [requested_dimension],
                    "selection_basis": "compiled_metric_dimension_binding",
                }
            else:
                resolution = _resolve_dimension_from_value(
                    demand, requested_dimension, index, registry
                )
            dimension = str(resolution["resolved_dimension"])
            values = list(resolution["members"]) or _selector_values(
                demand, dimension, index, registry
            )
            if dimension_resolutions is not None:
                resolution_id = stable_id(
                    "dimension_resolution",
                    {
                        "requested_dimension": resolution["requested_dimension"],
                        "resolved_dimension": dimension,
                        "requested_values": resolution["requested_values"],
                    },
                )
                dimension_resolutions[resolution_id] = {
                    "resolution_id": resolution_id,
                    **resolution,
                }
            block_dimension = dimension
        else:
            dimension = None
            values = [None]
            block_dimension = "无"
        granularity, period, sheet = _period_slot(demand, index)
        block = sheet.get("blocks", {}).get(metric)
        if not block:
            raise SkillError(
                "metric_unavailable_at_granularity",
                f"指标 {metric} 在 {granularity} 粒度表中不可用",
            )
        if block.get("dimension") != block_dimension:
            raise SkillError(
                "dimension_block_mismatch",
                f"指标 {metric} 数据块维度为 {block.get('dimension')}，不是 {block_dimension}",
            )
        column = sheet["periods"][period]
        for value in values:
            row = (
                _overall_row_for_no_dimension_block(block, metric)
                if value is None
                else block.get("rows", {}).get(value)
            )
            if row is None:
                raise SkillError(
                    "dimension_value_row_unavailable",
                    f"无法定位 {metric}/{value or '整体'} 的数据行",
                    {"metric": metric, "dimension": dimension, "value": value},
                )
            cells.append(
                {
                    "demand": demand,
                    "metric": metric,
                    "dimension": dimension,
                    "dimension_value": value,
                    "granularity": granularity,
                    "period": period,
                    "sheet": sheet,
                    "row": int(row),
                    "col": column_number(column),
                }
            )
    if not cells:
        raise SkillError("missing_fact_demands", "请求没有可读取的物理事实需求")
    return cells


def _matching_bindings(
    cell: dict[str, Any],
    index: dict[str, Any],
    registry: dict[str, Any],
    resolved_domains: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    bindings = cell["demand"].get("consumer_bindings") or []
    if cell["dimension"] is None:
        return [deepcopy(item) for item in bindings]
    matches: list[dict[str, Any]] = []
    for binding in bindings:
        source_binding = deepcopy(binding)
        if binding.get("source_selector_dimensions") is not None:
            source_binding["selector_dimensions"] = deepcopy(
                binding.get("source_selector_dimensions") or {}
            )
        resolution = _selector_resolution(
            source_binding, cell["dimension"], index, registry
        )
        allowed = resolution["members"]
        if cell["dimension_value"] in allowed:
            copied = deepcopy(binding)
            domain_id = resolution.get("domain_id")
            if domain_id:
                domain = {
                    "domain_id": domain_id,
                    "dimension": resolution["dimension"],
                    "requested_values": resolution["requested_values"],
                    "members": resolution["members"],
                    "set_ids": resolution["set_ids"],
                    "registry_hash": resolution["registry_hash"],
                }
                if resolution.get("domain_kind"):
                    domain["domain_kind"] = resolution["domain_kind"]
                previous = resolved_domains.get(domain_id)
                if previous is not None and previous != domain:
                    raise SkillError(
                        "conflicting_dimension_domain",
                        f"同一集合引用解析出了不同成员：{domain_id}",
                    )
                resolved_domains[domain_id] = domain
                copied.setdefault("dimension_domain_refs", {})[cell["dimension"]] = domain_id
            matches.append(copied)
    return matches


def _read_cells(client: LarkClient, token: str, cells: list[dict[str, Any]], revision: int) -> dict[tuple[str, int, int], Any]:
    output: dict[tuple[str, int, int], Any] = {}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for cell in cells:
        grouped.setdefault((cell["granularity"], cell["sheet"]["sheet_id"]), []).append(cell)
    for (_, sheet_id), group in grouped.items():
        rows = [item["row"] for item in group]
        cols = [item["col"] for item in group]
        payload = client.read_csv(
            token,
            sheet_id,
            f"{column_letter(min(cols))}{min(rows)}:{column_letter(max(cols))}{max(rows)}",
        )
        observed = int(payload.get("revision", -1))
        if observed != revision:
            raise SkillError(
                "concurrent_modification",
                "读取竞品事实时表格 revision 已变化",
                {"expected": revision, "observed": observed},
            )
        grid = parse_csv_payload(payload)
        row_indices = [int(value) for value in payload.get("row_indices", [])]
        col_indices = [
            column_number(value) if isinstance(value, str) else int(value)
            for value in payload.get("col_indices", [])
        ]
        if not row_indices:
            row_indices = list(range(min(rows), min(rows) + len(grid)))
        if not col_indices:
            col_indices = list(range(min(cols), min(cols) + max((len(row) for row in grid), default=0)))
        by_coordinate: dict[tuple[int, int], Any] = {}
        for row_offset, row_number in enumerate(row_indices):
            row = grid[row_offset] if row_offset < len(grid) else []
            for col_offset, col_number in enumerate(col_indices):
                by_coordinate[(row_number, col_number)] = row[col_offset] if col_offset < len(row) else ""
        for cell in group:
            output[(sheet_id, cell["row"], cell["col"])] = by_coordinate.get((cell["row"], cell["col"]), "")
    return output


def fetch_facts_from_index(
    request: dict[str, Any],
    index: dict[str, Any],
    client: LarkClient,
    cache_status: str,
    resolved_index_path: Path,
    *,
    dimension_set_registry_path: Path | None = None,
) -> dict[str, Any]:
    registry = load_dimension_set_registry(
        dimension_set_registry_path or DEFAULT_DIMENSION_SET_REGISTRY
    )
    current_registry_hash = registry_hash(registry)
    requested_registry_hash = request.get("dimension_set_registry_hash")
    if requested_registry_hash and requested_registry_hash != current_registry_hash:
        raise SkillError(
            "dimension_set_registry_changed",
            "编译与取数使用的集合注册表版本不一致",
            {"expected": requested_registry_hash, "actual": current_registry_hash},
        )
    dimension_resolutions: dict[str, dict[str, Any]] = {}
    cells = _requested_cells(request, index, registry, dimension_resolutions)
    revision = int(index["source"]["revision"])
    token = index["source"]["spreadsheet_token"]
    raw_values = _read_cells(client, token, cells, revision)
    facts_by_id: dict[str, dict[str, Any]] = {}
    bindings_by_id: dict[str, dict[str, Any]] = {}
    resolved_domains: dict[str, dict[str, Any]] = {}
    for cell in cells:
        demand = cell["demand"]
        raw = raw_values[(cell["sheet"]["sheet_id"], cell["row"], cell["col"])]
        metadata = index["metrics"][cell["metric"]]
        value = parse_source_number(raw, metadata.get("unit", demand.get("unit")))
        fact_identity = {
            "source_id": (request.get("source_binding") or {}).get(
                "source_id", request.get("source_id", "competitor_macro_sheet")
            ),
            "revision": revision,
            "sheet_id": cell["sheet"].get("sheet_id"),
            "row": cell["row"],
            "column": cell["col"],
            "source_metric_name": cell["metric"],
            "component": demand.get("component"),
            "scope": demand.get("scope"),
            "filters": demand.get("filters") or [],
        }
        fact_id = stable_id("fact", fact_identity)
        fact = {
                "fact_id": fact_id,
                "metric": cell["metric"],
                "source_metric_name": cell["metric"],
                "component": demand.get("component"),
                "scope": demand.get("scope"),
                "filters": deepcopy(demand.get("filters") or []),
                "period": cell["period"],
                "dimensions": {} if cell["dimension"] is None else {cell["dimension"]: cell["dimension_value"]},
                "value": value,
                "unit": metadata.get("unit", demand.get("unit", "")),
                "definition": metadata.get("notes") or cell["metric"],
                "additive": metadata.get("additive"),
                "aggregation": metadata.get("aggregation"),
                "missing": value is None,
                "raw_missing": value is None,
                "normalization_reason": "source_missing" if value is None else "unchanged",
                "value_derived_from_components": False,
                "source_request_id": request.get("request_id"),
                "source_ref": {
                    "sheet": cell["sheet"].get("sheet_name"),
                    "sheet_id": cell["sheet"].get("sheet_id"),
                    "row": cell["row"],
                    "column": column_letter(cell["col"]),
                    "revision": revision,
                    "schema_hash": index["source"].get("schema_hash"),
                },
                "coverage": "full",
        }
        existing = facts_by_id.get(fact_id)
        if existing is not None and existing != fact:
            raise SkillError("conflicting_physical_fact", f"同一物理事实返回冲突值：{fact_id}")
        facts_by_id[fact_id] = fact
        for binding in _matching_bindings(cell, index, registry, resolved_domains):
            binding["fact_id"] = fact_id
            binding_id = stable_id(
                "binding",
                [binding.get("binding_id"), binding.get("task_id"), binding.get("fact_slot_id"), fact_id],
            )
            binding["binding_id"] = binding_id
            previous = bindings_by_id.get(binding_id)
            if previous is not None and previous != binding:
                raise SkillError("conflicting_fact_binding", f"同一事实绑定发生冲突：{binding_id}")
            bindings_by_id[binding_id] = binding
    final_revision = revision
    if cache_status != "stale":
        final_revision = client.revision(token)
        if final_revision != revision:
            raise SkillError(
                "concurrent_modification",
                "计算前后竞品表格 revision 发生变化",
                {"before": revision, "after": final_revision},
            )
    return {
        "schema_version": SCENE_FACTS_V2,
        "facts": [facts_by_id[key] for key in sorted(facts_by_id)],
        "bindings": [bindings_by_id[key] for key in sorted(bindings_by_id)],
        "resolved_dimension_domains": {
            key: resolved_domains[key] for key in sorted(resolved_domains)
        },
        "dimension_resolutions": [
            dimension_resolutions[key] for key in sorted(dimension_resolutions)
        ],
        "source": {
            "url": index["source"].get("url"),
            "title": index["source"].get("title"),
            "revision": revision,
            "freshness": "stale" if cache_status == "stale" else "live",
            "cache_status": cache_status,
            "schema_hash": index["source"].get("schema_hash"),
            "source_binding": deepcopy(request.get("source_binding") or {}),
            "dimension_set_registry_hash": current_registry_hash,
            "warnings": index.get("warnings", []),
            "index_path": str(resolved_index_path),
            "call_attempts": deepcopy(getattr(client, "call_attempts", [])),
        },
    }


def fetch_facts(
    request: dict[str, Any],
    index_path: Path | None = None,
    *,
    source_url: str = DEFAULT_SOURCE_URL,
    identity: str = "user",
    allow_stale: bool = False,
    dimension_set_registry_path: Path | None = None,
) -> dict[str, Any]:
    """Diagnostic compatibility entrypoint; normal analysis uses DataGateway."""
    client = LarkClient(identity=identity)
    index, cache_status, resolved_index_path = ensure_shared_index(
        client,
        source_url,
        identity=identity,
        index_path=index_path,
        allow_stale=allow_stale,
    )
    return fetch_facts_from_index(
        request,
        index,
        client,
        cache_status,
        resolved_index_path,
        dimension_set_registry_path=dimension_set_registry_path,
    )
