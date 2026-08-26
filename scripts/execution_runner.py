#!/usr/bin/env python3
"""Execute deterministic scene-analysis nodes from a validated plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from fact_contract import SCENE_FACTS_V2, project_scene_facts

EXECUTOR_NAME = "scene-analysis-lightweight-executor"
EXECUTOR_VERSION = "1.10.0"
STORAGE_SCHEMA_VERSION = "2.0"
RUNNABLE_HANDLERS = {"fact_artifact", "derived", "attribution"}
PERIOD_VALUE_FIELDS = {
    "analysis": "analysis_value",
    "analysis_last_year": "analysis_last_year_value",
    "comparison": "comparison_value",
    "comparison_last_year": "comparison_last_year_value",
}


class ExecutionError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"cannot read JSON {path}: {exc}") from exc


class FactInput:
    def __init__(
        self,
        *,
        layout: str,
        rows: list[dict[str, Any]],
        fact_mappings: dict[str, dict[str, Any]],
        shared: dict[str, Any],
        raw_bytes: int | None,
        parse_ms: float,
    ) -> None:
        self.layout = layout
        self.rows = rows
        self.fact_mappings = fact_mappings
        self.shared = shared
        self.raw_bytes = raw_bytes
        self.parse_ms = parse_ms


def load_fact_input(path: Path) -> FactInput:
    started = time.perf_counter()
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except OSError as exc:
        raise ExecutionError(f"cannot read facts {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ExecutionError(f"facts are not valid UTF-8: {path}") from exc
    fact_mappings: dict[str, dict[str, Any]] = {}
    shared: dict[str, Any] = {}
    if path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        layout = "long"
    else:
        value = json.loads(text)
        if isinstance(value, dict) and value.get("fact_format") == "wide_facts/1.0":
            rows = value.get("rows")
            fact_mappings = value.get("fact_mappings")
            shared = value.get("shared") or {}
            layout = "wide"
            if not isinstance(rows, list):
                raise ExecutionError("wide facts require a rows array")
            if not isinstance(fact_mappings, dict) or not fact_mappings:
                raise ExecutionError("wide facts require a non-empty fact_mappings object")
            if not isinstance(shared, dict):
                raise ExecutionError("wide facts shared must be an object")
        elif isinstance(value, list):
            rows = value
            layout = "long"
        elif isinstance(value, dict) and isinstance(value.get("facts"), list):
            rows = (
                project_scene_facts(value)
                if value.get("schema_version") == SCENE_FACTS_V2
                else value["facts"]
            )
            layout = "long"
        elif isinstance(value, dict) and isinstance(value.get("normalized_facts"), list):
            rows = value["normalized_facts"]
            layout = "long"
        else:
            raise ExecutionError(
                "facts must be wide_facts/1.0, a JSON list, JSONL rows, or an object containing facts/normalized_facts"
            )
    if not all(isinstance(row, dict) for row in rows):
        raise ExecutionError("every fact row must be an object")
    return FactInput(
        layout=layout,
        rows=rows,
        fact_mappings=fact_mappings,
        shared=shared,
        raw_bytes=len(raw),
        parse_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def wide_facts_to_long(fact_input: FactInput) -> list[dict[str, Any]]:
    if fact_input.layout != "wide":
        return fact_input.rows
    output: list[dict[str, Any]] = []
    for mapping_id, mapping in fact_input.fact_mappings.items():
        if not isinstance(mapping_id, str) or not mapping_id or not isinstance(mapping, dict):
            raise ExecutionError("wide fact mappings require non-empty string IDs and object definitions")
        metric = mapping.get("metric", mapping_id)
        if not isinstance(metric, str) or not metric:
            raise ExecutionError(f"wide fact mapping {mapping_id!r} requires a metric")
        columns = {
            name: mapping.get(name)
            for name in ("value_column", "numerator_column", "denominator_column")
            if mapping.get(name) is not None
        }
        if not columns or any(not isinstance(column, str) or not column for column in columns.values()):
            raise ExecutionError(f"wide fact mapping {mapping_id!r} has invalid column references")
        has_components = "numerator_column" in columns or "denominator_column" in columns
        if has_components and not {"numerator_column", "denominator_column"}.issubset(columns):
            raise ExecutionError(f"wide fact mapping {mapping_id!r} must declare both numerator and denominator")
        for field in ("unit", "definition"):
            if field not in mapping and field not in fact_input.shared:
                raise ExecutionError(f"wide fact mapping {mapping_id!r} requires {field}")

    for row_index, physical in enumerate(fact_input.rows):
        dimensions = physical.get("dimensions", fact_input.shared.get("dimensions", {}))
        if not isinstance(dimensions, dict):
            raise ExecutionError(f"wide facts rows[{row_index}].dimensions must be an object")
        missing_map = physical.get("raw_missing", {})
        if missing_map is None:
            missing_map = {}
        if not isinstance(missing_map, dict):
            raise ExecutionError(f"wide facts rows[{row_index}].raw_missing must be an object")
        for mapping_id, mapping in fact_input.fact_mappings.items():
            referenced = {
                name: mapping[name]
                for name in ("value_column", "numerator_column", "denominator_column")
                if mapping.get(name) is not None
            }
            absent = [column for column in referenced.values() if column not in physical]
            if absent:
                raise ExecutionError(
                    f"wide facts rows[{row_index}] is missing columns for {mapping_id!r}: {sorted(absent)!r}"
                )
            raw_missing = missing_map.get(mapping_id)
            if raw_missing is not None and not isinstance(raw_missing, bool):
                raise ExecutionError(
                    f"wide facts rows[{row_index}].raw_missing[{mapping_id!r}] must be boolean or null"
                )
            logical = {
                "metric": mapping.get("metric", mapping_id),
                "view_id": physical.get(
                    "view_id", mapping.get("view_id", fact_input.shared.get("view_id"))
                ),
                "period": physical.get("period", fact_input.shared.get("period")),
                "period_role": physical.get("period_role", fact_input.shared.get("period_role")),
                "dimensions": dimensions,
                "value": physical.get(mapping.get("value_column")) if mapping.get("value_column") else None,
                "numerator": physical.get(mapping.get("numerator_column")) if mapping.get("numerator_column") else None,
                "denominator": physical.get(mapping.get("denominator_column")) if mapping.get("denominator_column") else None,
                "unit": mapping.get("unit", fact_input.shared.get("unit")),
                "definition": mapping.get("definition", fact_input.shared.get("definition")),
                "raw_missing": raw_missing,
                "source_request_id": physical.get(
                    "source_request_id", mapping.get("source_request_id", fact_input.shared.get("source_request_id"))
                ),
                "source_ref": physical.get(
                    "source_ref", fact_input.shared.get("source_ref", f"$.rows[{row_index}]")
                ),
                "wide_row_index": row_index,
                "wide_mapping_id": mapping_id,
            }
            if raw_missing is not None:
                logical["missing"] = raw_missing
            output.append(logical)
    return output


def load_fact_rows(path: Path) -> list[dict[str, Any]]:
    """Backward-compatible loader returning logical long-form rows."""
    return wide_facts_to_long(load_fact_input(path))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with temp.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def node_result_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    node_record = deepcopy(item)
    value = node_record.get("result")
    children: list[dict[str, Any]] = []
    if isinstance(value, dict) and isinstance(value.get("children"), list):
        children = value.pop("children")
        value["children_count"] = len(children)
    records = [{"record_type": "node", "data": node_record}]
    records.extend(
        {
            "record_type": "child",
            "node_id": item.get("node_id"),
            "child_index": index,
            "data": child,
        }
        for index, child in enumerate(children)
    )
    return records


def restore_node_result(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records or records[0].get("record_type") != "node" or not isinstance(records[0].get("data"), dict):
        raise ExecutionError("node result artifact must start with a node record")
    item = deepcopy(records[0]["data"])
    child_records = [record for record in records[1:] if record.get("record_type") == "child"]
    if child_records:
        child_records.sort(key=lambda record: int(record.get("child_index", -1)))
        result = item.get("result")
        if not isinstance(result, dict):
            raise ExecutionError("node result children require an object result")
        expected = result.pop("children_count", len(child_records))
        if expected != len(child_records):
            raise ExecutionError("node result child count does not match artifact records")
        result["children"] = [deepcopy(record.get("data")) for record in child_records]
    return item


class ArtifactStore:
    def __init__(self, output_path: Path, root: Path | None = None) -> None:
        self.base_dir = output_path.parent.resolve()
        if root is None:
            selected_root = output_path.with_name(output_path.name + ".artifacts")
        else:
            selected_root = root if root.is_absolute() else output_path.parent / root
        self.root = selected_root.resolve()
        if self.root == self.base_dir:
            raise ExecutionError("artifact directory must be a child of the execution manifest directory")
        try:
            self.root.relative_to(self.base_dir)
        except ValueError as exc:
            raise ExecutionError("artifact directory must be inside the execution manifest directory") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.node_hashes: dict[str, str] = {}
        self.cleanup_candidates: set[Path] = set()
        if output_path.exists():
            try:
                previous = load_json(output_path)
                previous_artifacts = previous.get("artifacts") if isinstance(previous, dict) else None
                for metadata in previous_artifacts.values() if isinstance(previous_artifacts, dict) else []:
                    if not isinstance(metadata, dict):
                        continue
                    path = resolve_artifact_path(self.base_dir, metadata)
                    path.relative_to(self.root)
                    self.cleanup_candidates.add(path)
            except (ExecutionError, ValueError):
                self.cleanup_candidates.clear()

    def relative_path(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.base_dir))

    def _write_records(
        self,
        artifact_id: str,
        filename: str,
        records: Iterable[dict[str, Any]],
        schema_version: str,
    ) -> dict[str, Any]:
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        digest = hashlib.sha256()
        byte_count = 0
        record_count = 0
        with temp.open("wb") as stream:
            for record in records:
                encoded = (canonical_json(record) + "\n").encode("utf-8")
                stream.write(encoded)
                digest.update(encoded)
                byte_count += len(encoded)
                record_count += 1
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        self.cleanup_candidates.add(path.resolve())
        metadata = {
            "artifact_id": artifact_id,
            "path": self.relative_path(path),
            "format": "jsonl",
            "schema_version": schema_version,
            "records": record_count,
            "bytes": byte_count,
            "sha256": digest.hexdigest(),
        }
        self.artifacts[artifact_id] = metadata
        return metadata

    def _write_json(self, artifact_id: str, filename: str, value: Any, schema_version: str) -> dict[str, Any]:
        encoded = (canonical_json(value) + "\n").encode("utf-8")
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temp.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
        self.cleanup_candidates.add(path.resolve())
        metadata = {
            "artifact_id": artifact_id,
            "path": self.relative_path(path),
            "format": "json",
            "schema_version": schema_version,
            "records": len(value) if isinstance(value, (dict, list)) else 1,
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }
        self.artifacts[artifact_id] = metadata
        return metadata

    def write_facts(self, facts: list[dict[str, Any]]) -> dict[str, Any]:
        content_id = sha256_value(facts)
        return self._write_records(
            "normalized_facts",
            f"normalized-facts-{content_id}.jsonl",
            facts,
            "normalized_fact/1.0",
        )

    def write_node_result(self, item: dict[str, Any]) -> dict[str, Any]:
        node_id = str(item["node_id"])
        item_hash = sha256_value(item)
        artifact_id = f"node_result:{node_id}"
        if self.node_hashes.get(node_id) == item_hash and artifact_id in self.artifacts:
            return self.artifacts[artifact_id]
        node_key = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]
        filename = f"nodes/{node_key}-{item_hash}.jsonl"
        metadata = self._write_records(artifact_id, filename, node_result_records(item), "node_result/2.0")
        metadata["content_hash"] = item_hash
        self.node_hashes[node_id] = item_hash
        return metadata

    def sync_results(
        self,
        ordered: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
        summaries: list[dict[str, Any]] = []
        result_index: dict[str, Any] = {}
        counts = {"derived_results": 0, "attribution_results": 0}
        for item in ordered:
            metadata = self.write_node_result(item)
            node_id = str(item["node_id"])
            summary = {key: deepcopy(value) for key, value in item.items() if key != "result"}
            summary["result_ref"] = {"artifact_id": metadata["artifact_id"], "line": 0}
            summaries.append(summary)
            result_index[node_id] = {
                "status": item["status"],
                "result_ref": {"artifact_id": metadata["artifact_id"], "line": 0},
                "input_hash": item.get("input_hash"),
            }
            if item.get("handler") == "derived":
                counts["derived_results"] += 1
            value = item.get("result")
            if item.get("handler") == "attribution":
                if isinstance(value, dict) and isinstance(value.get("children"), list):
                    for child_index, child in enumerate(value["children"]):
                        key = parent_result_key(node_id, child.get("parent") or {})
                        result_index[key] = {
                            "node_id": node_id,
                            "parent_dimensions": child.get("parent") or {},
                            "status": child.get("status"),
                            "result_ref": {"artifact_id": metadata["artifact_id"], "line": child_index + 1},
                            "input_hash": child.get("input_hash"),
                        }
                        counts["attribution_results"] += 1
                else:
                    counts["attribution_results"] += 1
        index_hash = sha256_value(result_index)
        index_meta = self._write_json(
            "result_index",
            f"result-index-{index_hash}.json",
            result_index,
            "result_index/2.0",
        )
        return summaries, {"artifact_id": index_meta["artifact_id"]}, counts

    def manifest(self) -> dict[str, dict[str, Any]]:
        return {artifact_id: deepcopy(metadata) for artifact_id, metadata in sorted(self.artifacts.items())}

    def prune_unreferenced(self) -> None:
        referenced = {
            resolve_artifact_path(self.base_dir, metadata)
            for metadata in self.artifacts.values()
        }
        for path in self.cleanup_candidates - referenced:
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    continue
        for path in sorted((item for item in self.root.rglob("*") if item.is_dir()), reverse=True):
            try:
                path.rmdir()
            except OSError:
                continue


def resolve_artifact_path(base_dir: Path, metadata: dict[str, Any]) -> Path:
    raw_path = metadata.get("path")
    if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
        raise ExecutionError("artifact path must be a non-empty relative path")
    base = base_dir.resolve()
    path = (base / raw_path).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ExecutionError("artifact path escapes the execution directory") from exc
    return path


def read_artifact_records(base_dir: Path, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    path = resolve_artifact_path(base_dir, metadata)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExecutionError(f"cannot read artifact {path}: {exc}") from exc
    if len(raw) != metadata.get("bytes") or hashlib.sha256(raw).hexdigest() != metadata.get("sha256"):
        raise ExecutionError(f"artifact integrity check failed: {path}")
    try:
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ExecutionError(f"artifact is not valid JSONL: {path}") from exc
    if len(records) != metadata.get("records") or not all(isinstance(record, dict) for record in records):
        raise ExecutionError(f"artifact record count or shape is invalid: {path}")
    return records


def cached_reference_results(document: dict[str, Any], base_dir: Path) -> dict[str, dict[str, Any]]:
    artifacts = document.get("artifacts")
    summaries = document.get("node_results")
    if not isinstance(artifacts, dict) or not isinstance(summaries, list):
        return {}
    output: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        if not isinstance(summary, dict) or summary.get("status") != "success":
            continue
        ref = summary.get("result_ref")
        artifact_id = ref.get("artifact_id") if isinstance(ref, dict) else None
        metadata = artifacts.get(artifact_id) if artifact_id is not None else None
        if not isinstance(metadata, dict):
            continue
        try:
            item = restore_node_result(read_artifact_records(base_dir, metadata))
        except ExecutionError:
            continue
        node_id = item.get("node_id")
        if isinstance(node_id, str):
            output[node_id] = item
    return output


class EventLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stream = None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open("w", encoding="utf-8")

    def add(self, event: str, **fields: Any) -> None:
        row = {"timestamp": utc_now(), "event": event, **fields}
        with self.lock:
            self.rows.append(row)
            if self.stream is not None:
                self.stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                self.stream.flush()

    def flush(self) -> None:
        if self.stream is not None:
            self.stream.flush()
            self.stream.close()
            self.stream = None


def normalize_facts(
    rows: list[dict[str, Any]],
    periods: dict[str, str],
    dimension_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    if len({str(value) for value in periods.values()}) != len(periods):
        raise ExecutionError("execution_runtime.periods must map roles to unique period values")
    period_to_role = {str(value): str(role) for role, value in periods.items()}
    normalized: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for index, raw in enumerate(rows):
        row = deepcopy(raw)
        dimensions = row.get("dimensions")
        if dimensions is None:
            dimensions = {
                field: row.get(field)
                for field in dimension_fields or []
                if field in row and row.get(field) is not None
            }
        if not isinstance(dimensions, dict):
            raise ExecutionError(f"facts[{index}].dimensions must be an object")
        row["dimensions"] = dimensions
        period = row.get("period")
        supplied_role = row.get("period_role")
        mapped_role = period_to_role.get(str(period)) if period is not None else None
        if supplied_role is not None and mapped_role is not None and str(supplied_role) != mapped_role:
            raise ExecutionError(
                f"facts[{index}] period_role {supplied_role!r} conflicts with period {period!r} mapped to {mapped_role!r}"
            )
        if supplied_role is not None and str(supplied_role) in periods and period is not None:
            expected_period = str(periods[str(supplied_role)])
            if str(period) != expected_period:
                raise ExecutionError(
                    f"facts[{index}] period {period!r} conflicts with period_role {supplied_role!r} mapped to {expected_period!r}"
                )
        if supplied_role is None and mapped_role is not None:
            row["period_role"] = mapped_role
        numerator = row.get("numerator")
        denominator = row.get("denominator")
        raw_missing = row.get("raw_missing", row.get("missing"))

        def finite_number(value: Any) -> float | None:
            if value is None or isinstance(value, bool):
                return None
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return None
            return parsed if math.isfinite(parsed) else None

        parsed_numerator = finite_number(numerator)
        parsed_denominator = finite_number(denominator)
        try:
            zero_denominator = denominator is not None and float(denominator) == 0
        except (TypeError, ValueError):
            zero_denominator = False
        source_missing = row.get("missing", raw_missing) is True
        value = row.get("value")
        has_any_component = numerator is not None or denominator is not None
        has_complete_components = parsed_numerator is not None and parsed_denominator is not None
        value_derived = False
        if source_missing:
            normalized_missing = True
            normalization_reason = "source_missing"
        elif zero_denominator:
            normalized_missing = True
            normalization_reason = "zero_denominator"
        elif value is None and has_complete_components:
            derived_value = parsed_numerator / parsed_denominator
            if not math.isfinite(derived_value):
                normalized_missing = True
                normalization_reason = "incomplete_components"
            else:
                row["value"] = derived_value
                normalized_missing = False
                normalization_reason = "value_derived_from_components"
                value_derived = True
        elif value is None and has_any_component:
            normalized_missing = True
            normalization_reason = "incomplete_components"
        elif value is None:
            normalized_missing = True
            normalization_reason = "value_missing"
        else:
            normalized_missing = False
            normalization_reason = "unchanged"
        row["raw_missing"] = raw_missing
        row["missing"] = normalized_missing
        row["normalization_reason"] = normalization_reason
        row["value_derived_from_components"] = value_derived
        if normalized_missing:
            row["value"] = None
        if row.get("metric") is None:
            raise ExecutionError(f"facts[{index}].metric is required")
        if row.get("period") is None and row.get("period_role") is None:
            raise ExecutionError(f"facts[{index}] requires period or period_role")
        if row.get("fact_id") is None:
            identity = {
                "metric": row.get("metric"),
                "view_id": row.get("view_id"),
                "period": row.get("period"),
                "period_role": row.get("period_role"),
                "dimensions": dimensions,
                "source_request_id": row.get("source_request_id"),
            }
            row["fact_id"] = sha256_value(identity)[:24]
        fact_id = str(row["fact_id"])
        if fact_id in seen_ids:
            existing = normalized[seen_ids[fact_id]]
            comparable_fields = (
                "metric_ref", "metric", "view_id", "period", "period_role",
                "dimensions", "value", "numerator", "denominator", "unit",
                "definition", "missing", "raw_missing", "normalization_reason",
                "value_derived_from_components", "source_request_id", "source_ref",
                "additive", "aggregation",
            )
            conflicts = [
                field for field in comparable_fields
                if existing.get(field) != row.get(field)
            ]
            if conflicts:
                raise ExecutionError(
                    f"duplicate fact_id has conflicting fields {conflicts}: {fact_id}"
                )
            slot_ids = existing.setdefault(
                "fact_slot_ids",
                [existing["fact_slot_id"]] if existing.get("fact_slot_id") else [],
            )
            incoming_slot = row.get("fact_slot_id")
            if incoming_slot and incoming_slot not in slot_ids:
                slot_ids.append(incoming_slot)
                slot_ids.sort()
            continue
        seen_ids[fact_id] = len(normalized)
        normalized.append(row)
    return normalized


def selector_matches(row: dict[str, Any], selector: dict[str, Any], parent: dict[str, Any] | None = None) -> bool:
    for key in ("metric_ref", "metric", "view_id", "period", "period_role", "unit"):
        if key in selector and row.get(key) != selector[key]:
            return False
    dimensions = row.get("dimensions") or {}
    selected_dimensions = selector.get("dimensions") or {}
    if not isinstance(selected_dimensions, dict):
        raise ExecutionError("selector.dimensions must be an object")
    expected_dimensions = {**selected_dimensions, **(parent or {})}
    for key, value in expected_dimensions.items():
        if dimensions.get(key) != value:
            return False
    if selector.get("dimensions_exact") is True and dimensions != expected_dimensions:
        return False
    return True


class FactStore:
    _PHYSICAL_EQUIVALENCE_FIELDS = (
        "physical_fact_id",
        "metric",
        "metric_object",
        "view_id",
        "period",
        "period_role",
        "dimensions",
        "component",
        "value",
        "numerator",
        "denominator",
        "unit",
        "missing",
        "raw_missing",
        "normalization_reason",
        "value_derived_from_components",
        "definition",
        "additive",
        "aggregation",
        "source_request_id",
        "source_ref",
    )

    def __init__(
        self,
        rows: list[dict[str, Any]],
        resolved_domains: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.rows = list(rows)
        self.resolved_domains = resolved_domains or {}
        self.indexes: dict[tuple[str, str], set[int]] = {}
        for index, row in enumerate(self.rows):
            self._index_row(index, row)

    def _index_row(self, index: int, row: dict[str, Any]) -> None:
        for key in ("metric_ref", "metric", "view_id", "period", "period_role", "unit"):
            if row.get(key) is not None:
                self.indexes.setdefault((key, canonical_json(row[key])), set()).add(index)
        for key, value in (row.get("dimensions") or {}).items():
            self.indexes.setdefault((f"dimension:{key}", canonical_json(value)), set()).add(index)

    def add_rows(self, rows: list[dict[str, Any]]) -> None:
        existing_ids = {str(row.get("fact_id")) for row in self.rows if row.get("fact_id")}
        for row in rows:
            fact_id = str(row.get("fact_id") or "")
            if not fact_id:
                raise ExecutionError("materialized fact requires fact_id")
            if fact_id in existing_ids:
                continue
            index = len(self.rows)
            self.rows.append(deepcopy(row))
            self._index_row(index, self.rows[index])
            existing_ids.add(fact_id)

    def select(self, selector: dict[str, Any], parent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if not isinstance(selector, dict):
            raise ExecutionError("fact selector must be an object")
        index_keys: list[tuple[str, str]] = []
        for key in ("metric_ref", "metric", "view_id", "period", "period_role", "unit"):
            if key in selector:
                index_keys.append((key, canonical_json(selector[key])))
        selected_dimensions = selector.get("dimensions") or {}
        if not isinstance(selected_dimensions, dict):
            raise ExecutionError("selector.dimensions must be an object")
        for key, value in {**selected_dimensions, **(parent or {})}.items():
            index_keys.append((f"dimension:{key}", canonical_json(value)))
        matching_sets = [self.indexes.get(index_key, set()) for index_key in index_keys]
        if any(not matching for matching in matching_sets):
            return []
        matching_sets.sort(key=len)
        candidates: set[int] | None = set(matching_sets[0]) if matching_sets else None
        for matching in matching_sets[1:]:
            assert candidates is not None
            candidates.intersection_update(matching)
            if not candidates:
                return []
        candidate_indexes = range(len(self.rows)) if candidates is None else sorted(candidates)
        return [self.rows[index] for index in candidate_indexes if selector_matches(self.rows[index], selector, parent)]

    @classmethod
    def _collapse_equivalent_physical_rows(
        cls,
        rows: list[dict[str, Any]],
        selector: dict[str, Any],
    ) -> list[dict[str, Any]]:
        collapsed: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            physical_fact_id = row.get("physical_fact_id")
            if not isinstance(physical_fact_id, str) or not physical_fact_id:
                collapsed.append(row)
                continue
            grouped.setdefault(physical_fact_id, []).append(row)
        for physical_fact_id in sorted(grouped):
            group = grouped[physical_fact_id]
            representative = min(
                group,
                key=lambda row: (
                    str(row.get("binding_id") or ""),
                    str(row.get("fact_id") or ""),
                ),
            )
            conflicts = sorted({
                field
                for row in group
                for field in cls._PHYSICAL_EQUIVALENCE_FIELDS
                if row.get(field) != representative.get(field)
            })
            if conflicts:
                raise ExecutionError(
                    "logical facts sharing physical_fact_id have conflicting fields "
                    f"{conflicts}: physical_fact_id={physical_fact_id}, selector={selector}"
                )
            collapsed.append(representative)
        return collapsed

    def one_fact(
        self,
        selector: dict[str, Any],
        *,
        parent: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        return self._one_fact_from_rows(
            self.select(selector, parent), selector, allow_missing=allow_missing
        )

    @classmethod
    def _one_fact_from_rows(
        cls,
        rows: list[dict[str, Any]],
        selector: dict[str, Any],
        *,
        allow_missing: bool = False,
    ) -> dict[str, Any]:
        matches = cls._collapse_equivalent_physical_rows(rows, selector)
        if len(matches) != 1:
            raise ExecutionError(
                f"selector must match exactly one fact, got {len(matches)}: {selector}"
            )
        row = matches[0]
        if row.get("missing") and not allow_missing:
            raise ExecutionError(
                f"selector must match exactly one non-missing fact, got 0: {selector}"
            )
        return row

    def unique_value(
        self,
        selector: dict[str, Any],
        *,
        field: str = "value",
        parent: dict[str, Any] | None = None,
    ) -> float:
        value = self.one_fact(selector, parent=parent).get(field)
        if value is None:
            raise ExecutionError(f"selected fact field {field} is null: {selector}")
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ExecutionError(f"selected fact field {field} is not numeric: {value!r}") from exc

    def parent_values(self, selector: dict[str, Any], dimensions: list[str]) -> list[dict[str, Any]]:
        if not dimensions:
            return [{}]
        values: dict[str, dict[str, Any]] = {}
        discovery_selector = deepcopy(selector)
        discovery_selector.pop("dimensions_exact", None)
        for row in self.select(discovery_selector):
            current = row.get("dimensions") or {}
            if any(key not in current for key in dimensions):
                continue
            parent = {key: current[key] for key in dimensions}
            values[canonical_json(parent)] = parent
        return [values[key] for key in sorted(values)]

    def aggregate_value(
        self,
        selector: dict[str, Any],
        domain_ref: str,
    ) -> float:
        domain = self.resolved_domains.get(domain_ref)
        if not isinstance(domain, dict):
            raise ExecutionError(f"aggregate references unresolved dimension domain: {domain_ref}")
        dimension = domain.get("dimension")
        values = domain.get("members")
        if not isinstance(dimension, str) or not isinstance(values, list) or not values:
            raise ExecutionError(f"resolved dimension domain is invalid: {domain_ref}")
        requested = {str(value) for value in values}
        selected = [
            row for row in self.select(selector)
            if str((row.get("dimensions") or {}).get(dimension)) in requested
        ]
        selected = self._collapse_equivalent_physical_rows(selected, selector)
        if not selected:
            raise ExecutionError("selected set denominator has no usable facts")
        covered = {
            str((row.get("dimensions") or {}).get(dimension)) for row in selected
        }
        if covered != requested:
            raise ExecutionError(
                f"selected set is incomplete: missing {sorted(requested - covered)}"
            )
        if any(row.get("missing") for row in selected):
            raise ExecutionError("selected set contains missing facts")
        if any(row.get("additive") is not True for row in selected):
            raise ExecutionError(
                "selected set metric is not marked additive by source metadata"
            )
        distinct = {(str((row.get("dimensions") or {}).get(dimension)), row.get("period_role")) for row in selected}
        if len(distinct) != len(selected):
            raise ExecutionError("selected set denominator contains duplicate facts")
        total = sum(float(row["value"]) for row in selected)
        if total == 0:
            raise ExecutionError("selected set denominator is 0")
        return total


def period_roles_for_scenario(scenario: str) -> list[str]:
    if scenario == "metric_change":
        return ["analysis", "comparison"]
    if scenario == "yoy_trend_change":
        return ["analysis", "analysis_last_year", "comparison", "comparison_last_year"]
    raise ExecutionError(f"unsupported scenario: {scenario!r}")


def selector_for_period(selector: dict[str, Any], role: str, periods: dict[str, str]) -> dict[str, Any]:
    output = deepcopy(selector)
    if role not in periods:
        raise ExecutionError(f"period role {role!r} is missing from binding.periods")
    output["period"] = periods[role]
    output["period_role"] = role
    return output


def bind_metric_values(
    store: FactStore,
    config: dict[str, Any],
    roles: list[str],
    periods: dict[str, str],
    parent: dict[str, Any] | None,
    node_results: dict[str, dict[str, Any]] | None = None,
    *,
    components: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        key: value
        for key, value in config.items()
        if key not in {"selector", "values_by_period_role", "expressions_by_period_role"}
    }
    values_by_role = config.get("values_by_period_role")
    if isinstance(values_by_role, dict):
        for role in roles:
            try:
                result[PERIOD_VALUE_FIELDS[role]] = float(values_by_role[role])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionError(
                    f"binding factor values_by_period_role.{role} must be numeric"
                ) from exc
        return result
    expressions_by_role = config.get("expressions_by_period_role")
    if isinstance(expressions_by_role, dict):
        for role in roles:
            expression = expressions_by_role.get(role)
            if not isinstance(expression, dict):
                raise ExecutionError(
                    f"binding factor expressions_by_period_role.{role} must be an object"
                )
            result[PERIOD_VALUE_FIELDS[role]] = evaluate_expression(
                expression, store, node_results or {}, parent
            )
        return result
    if "literal" in config:
        try:
            literal = float(config["literal"])
        except (TypeError, ValueError) as exc:
            raise ExecutionError("binding factor literal must be numeric") from exc
        for role in roles:
            if components:
                result[f"{role}_numerator"] = literal
                result[f"{role}_denominator"] = literal
            else:
                result[PERIOD_VALUE_FIELDS[role]] = literal
        return result
    selector = config.get("selector") or {"metric": config.get("name")}
    for role in roles:
        selected = selector_for_period(selector, role, periods)
        if components:
            result[f"{role}_numerator"] = store.unique_value(selected, field="numerator", parent=parent)
            result[f"{role}_denominator"] = store.unique_value(selected, field="denominator", parent=parent)
        else:
            result[PERIOD_VALUE_FIELDS[role]] = store.unique_value(selected, parent=parent)
    return result


def group_name(dimensions: dict[str, Any], keys: list[str]) -> str:
    if len(keys) == 1:
        return str(dimensions[keys[0]])
    return " | ".join(f"{key}={dimensions[key]}" for key in keys)


def bind_groups(
    store: FactStore,
    config: dict[str, Any],
    roles: list[str],
    periods: dict[str, str],
    metric_object: str,
    parent: dict[str, Any] | None,
    sparse_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    selector = config.get("selector") or {}
    keys = config.get("group_dimensions")
    if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
        raise ExecutionError("binding.groups.group_dimensions must be a non-empty string list")
    combinations: dict[str, dict[str, Any]] = {}
    discovery_selector = deepcopy(selector)
    discovery_selector.pop("dimensions_exact", None)
    for row in store.select(discovery_selector, parent):
        dimensions = row.get("dimensions") or {}
        if any(key not in dimensions for key in keys):
            continue
        values = {key: dimensions[key] for key in keys}
        combinations[canonical_json(values)] = values
    if not combinations:
        raise ExecutionError("group binding matched no dimension combinations")
    output: list[dict[str, Any]] = []
    allow_structural_absence = bool((sparse_policy or {}).get("structural_absence_is_zero"))
    for encoded in sorted(combinations):
        dimensions = combinations[encoded]
        child_parent = {**(parent or {}), **dimensions}
        item: dict[str, Any] = {"name": group_name(dimensions, keys), "dimensions": child_parent}
        structural_absence_periods: list[str] = []
        for role in roles:
            selected = selector_for_period(selector, role, periods)
            if metric_object == "ratio":
                matches = store.select(selected, child_parent)
                if matches:
                    row = store._one_fact_from_rows(
                        matches, selected, allow_missing=True
                    )
                else:
                    row = None
                if row is not None and not row.get("missing"):
                    numerator = row.get("numerator")
                    denominator = row.get("denominator")
                    try:
                        item[f"{role}_numerator"] = float(numerator)
                        item[f"{role}_denominator"] = float(denominator)
                    except (TypeError, ValueError) as exc:
                        raise ExecutionError(f"ratio group components must be numeric: {selected}") from exc
                elif row is not None and row.get("normalization_reason") == "zero_denominator":
                    numerator = row.get("numerator")
                    denominator = row.get("denominator")
                    if numerator not in (0, 0.0) or denominator not in (0, 0.0):
                        raise ExecutionError(f"zero-denominator structural fact must be 0/0: {selected}")
                    item[f"{role}_numerator"] = 0.0
                    item[f"{role}_denominator"] = 0.0
                    structural_absence_periods.append(role)
                elif not matches and allow_structural_absence:
                    item[f"{role}_numerator"] = 0.0
                    item[f"{role}_denominator"] = 0.0
                    structural_absence_periods.append(role)
                else:
                    raise ExecutionError(
                        f"ratio group period is missing and is not certified as structural absence: {selected}"
                    )
            else:
                item[PERIOD_VALUE_FIELDS[role]] = store.unique_value(selected, parent=child_parent)
        if structural_absence_periods:
            item["structural_absence_periods"] = structural_absence_periods
        output.append(item)
    return output


def build_attribution_payload(
    store: FactStore,
    execution: dict[str, Any],
    parent: dict[str, Any] | None = None,
    node_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if isinstance(execution.get("payload"), dict):
        return deepcopy(execution["payload"])
    binding = execution.get("binding")
    if not isinstance(binding, dict):
        raise ExecutionError("attribution execution requires payload or binding")
    scenario = str(binding.get("scenario", ""))
    roles = period_roles_for_scenario(scenario)
    periods = binding.get("periods")
    if not isinstance(periods, dict):
        raise ExecutionError("binding.periods must be an object")
    metric_object = str(binding.get("metric_object", ""))
    payload: dict[str, Any] = {
        "operator": execution.get("operator"),
        "scenario": scenario,
        "metric_object": metric_object,
        "decomposition": binding.get("decomposition"),
        "periods": {role: periods[role] for role in roles},
    }
    if isinstance(binding.get("metric"), dict):
        payload["metric"] = bind_metric_values(
            store,
            binding["metric"],
            roles,
            periods,
            parent,
            components=(metric_object == "ratio" and execution.get("operator") in {"structure_change", "structure_yoy_trend"}),
        )
    if isinstance(binding.get("factors"), list):
        payload["factors"] = [
            bind_metric_values(store, factor, roles, periods, parent, node_results)
            for factor in binding["factors"]
        ]
    if isinstance(binding.get("groups"), dict):
        sparse_policy = binding.get("sparse_policy")
        if sparse_policy is not None and not isinstance(sparse_policy, dict):
            raise ExecutionError("binding.sparse_policy must be an object")
        payload["groups"] = bind_groups(
            store, binding["groups"], roles, periods, metric_object, parent, sparse_policy
        )
    if "factors" not in payload and "groups" not in payload:
        raise ExecutionError("attribution binding requires factors or groups")
    for key in ("partial_coverage", "sparse_strategy", "epsilon", "reference_rate_policy", "coverage", "sparse_policy"):
        if key in binding:
            payload[key] = binding[key]
    for key in (
        "metric_semantics",
        "parent_metric_semantics",
        "relation_to_parent",
        "ranking",
        "semantic_warnings",
        "formula",
        "formula_shape",
        "factor_order",
        "formula_fingerprint",
    ):
        if key in binding:
            payload[key] = deepcopy(binding[key])
    return payload


def collect_expression_refs(expression: Any) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(expression, dict):
        raise ExecutionError("derived expression must be an object")
    if "literal" in expression:
        try:
            float(expression["literal"])
        except (TypeError, ValueError) as exc:
            raise ExecutionError("expression.literal must be numeric") from exc
        return [], set()
    if "fact" in expression:
        fact = expression["fact"]
        if not isinstance(fact, dict):
            raise ExecutionError("expression.fact must be an object")
        selector = {key: value for key, value in fact.items() if key != "field"}
        if not selector:
            raise ExecutionError("expression.fact selector must not be empty")
        return [fact], set()
    if "aggregate" in expression:
        aggregate = expression["aggregate"]
        if not isinstance(aggregate, dict) or not isinstance(aggregate.get("selector"), dict):
            raise ExecutionError("expression.aggregate requires a selector")
        return [{"fact": aggregate["selector"], "aggregate_dimension": aggregate.get("dimension")}], set()
    if "result" in expression:
        result_ref = expression["result"]
        if not isinstance(result_ref, dict) or not result_ref.get("node_id"):
            raise ExecutionError("expression.result requires node_id")
        return [], {str(result_ref["node_id"])}
    op = expression.get("op")
    args = expression.get("args")
    arity = {"add": (1, None), "subtract": (2, 2), "multiply": (1, None), "divide": (2, 2), "sum": (1, None), "negate": (1, 1)}
    if op not in arity or not isinstance(args, list):
        raise ExecutionError(f"unsupported derived operation: {op!r}")
    minimum, maximum = arity[op]
    if len(args) < minimum or (maximum is not None and len(args) > maximum):
        raise ExecutionError(f"invalid arity for derived operation {op!r}: {len(args)}")
    facts: list[dict[str, Any]] = []
    results: set[str] = set()
    for arg in args:
        arg_facts, arg_results = collect_expression_refs(arg)
        facts.extend(arg_facts)
        results.update(arg_results)
    return facts, results


def validate_period_mapping(binding_periods: Any, runtime_periods: dict[str, str], roles: list[str]) -> dict[str, str]:
    if not isinstance(binding_periods, dict):
        raise ExecutionError("binding.periods must be an object")
    resolved: dict[str, str] = {}
    for role in roles:
        if role not in binding_periods:
            raise ExecutionError(f"period role {role!r} is missing from binding.periods")
        if role not in runtime_periods:
            raise ExecutionError(f"period role {role!r} is missing from execution_runtime.periods")
        binding_value = str(binding_periods[role])
        runtime_value = str(runtime_periods[role])
        if binding_value != runtime_value:
            raise ExecutionError(
                f"binding period {role!r}={binding_value!r} conflicts with execution_runtime value {runtime_value!r}"
            )
        resolved[role] = runtime_periods[role]
    return resolved


def values_for_contract_path(value: Any, path: str) -> list[Any]:
    current = [value]
    for part in path.split("."):
        is_array = part.endswith("[]")
        key = part[:-2] if is_array else part
        next_values: list[Any] = []
        for item in current:
            if not isinstance(item, dict) or key not in item:
                continue
            child = item[key]
            if is_array:
                if isinstance(child, list):
                    next_values.extend(child)
            else:
                next_values.append(child)
        current = next_values
    return current


def validate_payload_contract(
    payload: dict[str, Any],
    expected_operator: str,
    get_operator: Callable[[str], dict[str, Any] | None],
    operator_matches: Callable[[str, str | None, str | None, str | None], bool],
) -> None:
    definition = get_operator(expected_operator)
    if not isinstance(definition, dict):
        raise ExecutionError(f"unsupported attribution operator: {expected_operator!r}")
    scenario = payload.get("scenario")
    metric_object = payload.get("metric_object")
    decomposition = payload.get("decomposition")
    if not operator_matches(expected_operator, scenario, metric_object, decomposition):
        raise ExecutionError(
            f"operator {expected_operator!r} does not match scenario={scenario!r}, "
            f"metric_object={metric_object!r}, decomposition={decomposition!r}"
        )
    for field in definition.get("required_inputs", []):
        path = field.get("path") if isinstance(field, dict) else None
        if not isinstance(path, str):
            continue
        values = values_for_contract_path(payload, path)
        if not values or any(value is None for value in values):
            raise ExecutionError(f"attribution payload is missing required input: {path}")


def parent_result_key(node_id: str, parent: dict[str, Any]) -> str:
    return f"{node_id}::{canonical_json(parent)}"


def evaluate_expression(
    expression: Any,
    store: FactStore,
    node_results: dict[str, dict[str, Any]],
    parent: dict[str, Any] | None = None,
) -> float:
    if not isinstance(expression, dict):
        raise ExecutionError("derived expression must be an object")
    if "literal" in expression:
        return float(expression["literal"])
    if "fact" in expression:
        fact = expression["fact"]
        if not isinstance(fact, dict):
            raise ExecutionError("expression.fact must be an object")
        field = str(fact.get("field", "value"))
        selector = {key: value for key, value in fact.items() if key != "field"}
        return store.unique_value(selector, field=field, parent=parent)
    if "aggregate" in expression:
        aggregate = expression["aggregate"]
        if not isinstance(aggregate, dict):
            raise ExecutionError("expression.aggregate must be an object")
        selector = aggregate.get("selector")
        domain_ref = aggregate.get("domain_ref")
        if not isinstance(selector, dict) or not isinstance(domain_ref, str) or not domain_ref:
            raise ExecutionError("aggregate requires selector and domain_ref")
        return store.aggregate_value(selector, domain_ref)
    if "result" in expression:
        result_ref = expression["result"]
        if not isinstance(result_ref, dict) or not result_ref.get("node_id"):
            raise ExecutionError("expression.result requires node_id")
        value: Any = node_results.get(str(result_ref["node_id"]))
        for key in str(result_ref.get("path", "result")).split("."):
            if not isinstance(value, dict) or key not in value:
                raise ExecutionError(f"result path is missing: {result_ref}")
            value = value[key]
        return float(value)
    op = expression.get("op")
    args = expression.get("args", [])
    if not isinstance(args, list):
        raise ExecutionError("expression.args must be a list")
    values = [evaluate_expression(arg, store, node_results, parent) for arg in args]
    if op == "add":
        return sum(values)
    if op == "subtract" and len(values) == 2:
        return values[0] - values[1]
    if op == "multiply":
        return math.prod(values)
    if op == "divide" and len(values) == 2:
        if values[1] == 0:
            raise ExecutionError("derived expression denominator is 0")
        return values[0] / values[1]
    if op == "sum":
        return sum(values)
    if op == "negate" and len(values) == 1:
        return -values[0]
    raise ExecutionError(f"unsupported operation or arity: {op!r}")


def materialize_intermediate_facts(
    node_id: str,
    execution: dict[str, Any],
    value: Any,
    store: FactStore,
) -> list[dict[str, Any]]:
    target = execution.get("materialize_as")
    if not isinstance(target, dict):
        return []
    required = ("metric_ref", "metric", "period_role", "period", "unit")
    if any(not isinstance(target.get(field), str) or not target[field] for field in required):
        raise ExecutionError("materialize_as is missing metric, period, role, or unit")
    validation = target.get("validation") or []
    if not isinstance(validation, list):
        raise ExecutionError("materialize_as.validation must be a list")
    output_rows = value if isinstance(value, list) else [{"dimensions": {}, "value": value}]
    expression = execution.get("expression")
    fact_refs, _ = collect_expression_refs(expression)
    materialized: list[dict[str, Any]] = []
    for index, item in enumerate(output_rows):
        if not isinstance(item, dict) or "value" not in item:
            raise ExecutionError("materialized grouped result must contain dimensions and value")
        dimensions = {**(target.get("dimensions") or {}), **(item.get("dimensions") or {})}
        input_rows: list[dict[str, Any]] = []
        for ref in fact_refs:
            if "fact" in ref and isinstance(ref.get("fact"), dict):
                selector = deepcopy(ref["fact"])
            else:
                selector = {key: value for key, value in ref.items() if key != "field"}
            matches = store.select(selector, item.get("dimensions") or {})
            if "facts_present" in validation and (
                not matches or any(row.get("missing") for row in matches)
            ):
                raise ExecutionError(
                    f"input adaptation has missing source facts for output row {index}: {selector}"
                )
            input_rows.extend(matches)
        if "unit_consistent" in validation:
            units = {row.get("unit") for row in input_rows if row.get("unit") is not None}
            if not units or units != {target["unit"]}:
                raise ExecutionError(
                    f"input adaptation units do not match target unit {target['unit']!r}: {sorted(units)}"
                )
        if "unit_scale_verified" in validation:
            conversion = target.get("unit_conversion")
            if not isinstance(conversion, dict):
                raise ExecutionError("unit_scale_verified requires unit_conversion")
            scale_factor = conversion.get("scale_factor")
            if (
                conversion.get("target_unit") != target.get("unit")
                or isinstance(scale_factor, bool)
                or not isinstance(scale_factor, (int, float))
                or not math.isfinite(float(scale_factor))
                or float(scale_factor) == 0
            ):
                raise ExecutionError("unit_conversion target or scale factor is invalid")
            expected_units = conversion.get("expected_input_units")
            if not isinstance(expected_units, dict) or not expected_units:
                raise ExecutionError("unit_conversion.expected_input_units must be non-empty")
            for row in input_rows:
                metric_name = str(row.get("metric") or "")
                expected_unit = expected_units.get(metric_name)
                if expected_unit is None or row.get("unit") != expected_unit:
                    raise ExecutionError(
                        f"input adaptation unit mismatch for {metric_name!r}: "
                        f"expected {expected_unit!r}, got {row.get('unit')!r}"
                    )
        if "metric_additive" in validation and (
            not input_rows or any(row.get("additive") is not True for row in input_rows)
        ):
            raise ExecutionError(
                "input adaptation requires source metric metadata additive=true"
            )
        revisions = {
            (row.get("source_ref") or {}).get("revision")
            for row in input_rows
            if (row.get("source_ref") or {}).get("revision") is not None
        }
        if len(revisions) > 1:
            raise ExecutionError("input adaptation source facts span multiple revisions")
        parsed_value = float(item["value"])
        if "unit_scale_verified" in validation:
            parsed_value *= float(target["unit_conversion"]["scale_factor"])
        if not math.isfinite(parsed_value):
            raise ExecutionError("materialized fact value must be finite")
        input_fact_ids = sorted({str(row.get("fact_id")) for row in input_rows})
        input_physical_fact_ids = sorted({
            str(physical_fact_id)
            for row in input_rows
            for physical_fact_id in (
                [row.get("physical_fact_id")]
                if row.get("physical_fact_id")
                else (row.get("source_ref") or {}).get("input_physical_fact_ids") or []
            )
            if physical_fact_id
        })
        identity = {
            "node_id": node_id,
            "metric": target["metric"],
            "period": target["period"],
            "view_id": target.get("view_id"),
            "dimensions": dimensions,
        }
        materialized.append({
            "fact_id": f"intermediate_{sha256_value(identity)[:20]}",
            "metric_ref": target["metric_ref"],
            "metric": target["metric"],
            "metric_object": target.get("metric_object"),
            "view_id": target.get("view_id"),
            "period": target["period"],
            "period_role": target["period_role"],
            "dimensions": dimensions,
            "value": parsed_value,
            "unit": target["unit"],
            "definition": f"input adaptation from {target.get('rule_source')}",
            "additive": bool(input_rows) and all(row.get("additive") is True for row in input_rows),
            "missing": False,
            "raw_missing": False,
            "normalization_reason": "unchanged",
            "value_derived_from_components": False,
            "source_request_id": "input_adaptation",
            "source_ref": {
                "type": "input_adaptation",
                "node_id": node_id,
                "input_fact_ids": input_fact_ids,
                "input_physical_fact_ids": input_physical_fact_ids,
                "revisions": sorted(revisions),
                "rule_source": target.get("rule_source"),
                **(
                    {"rollup": deepcopy(target.get("rollup"))}
                    if target.get("rollup") is not None
                    else {}
                ),
            },
            "coverage": "full",
            "intermediate": True,
        })
    return materialized


def load_attribution_api() -> tuple[
    Callable[..., dict[str, Any]],
    Callable[[str], dict[str, Any] | None],
    Callable[[str, str | None, str | None, str | None], bool],
]:
    try:
        from _vendor.attribution_core import get_operator, identity, operator_matches, run
    except ImportError as exc:
        raise ExecutionError(f"embedded attribution engine is unavailable: {exc}") from exc
    if not all(callable(item) for item in (run, get_operator, operator_matches)):
        raise ExecutionError("attribution engine API is incomplete")
    lock_path = Path(__file__).resolve().parents[1] / "attribution-core.lock.json"
    try:
        lock = load_json(lock_path)
    except Exception as exc:  # noqa: BLE001 - normalize lock failures as execution errors.
        raise ExecutionError(f"cannot load embedded attribution lock: {exc}") from exc
    runtime_identity = identity()
    if lock.get("identity") != runtime_identity:
        raise ExecutionError("embedded attribution engine does not match attribution-core.lock.json")
    return run, get_operator, operator_matches


def validate_attribution_result(
    result: dict[str, Any], expected_operator: str | None, tolerance: float
) -> str | None:
    if result.get("ok") is not True:
        raise ExecutionError("attribution result ok is not true")
    if expected_operator and result.get("operator") != expected_operator:
        raise ExecutionError(
            f"attribution operator mismatch: expected {expected_operator!r}, got {result.get('operator')!r}"
        )
    if not isinstance(result.get("summary"), dict) or not isinstance(result.get("rows"), list):
        raise ExecutionError("attribution result requires summary and rows")
    for key in ("warnings", "boundary_cases"):
        if not isinstance(result.get(key), list):
            raise ExecutionError(f"attribution result {key} must be a list")
    residual = result["summary"].get("residual")
    scale_values = [
        abs(float(result["summary"][key]))
        for key in ("analysis_value", "comparison_value", "change_value")
        if result["summary"].get(key) is not None
    ]
    effective_tolerance = max(tolerance, max(scale_values, default=0.0) * tolerance)
    if residual is not None and abs(float(residual)) > effective_tolerance:
        return (
            f"attribution residual {residual} exceeds tolerance "
            f"{effective_tolerance} (base={tolerance}, scale-aware)"
        )
    return None


def prepare_attribution(
    store: FactStore,
    execution: dict[str, Any],
    runtime_periods: dict[str, str],
    get_operator: Callable[[str], dict[str, Any] | None],
    operator_matches: Callable[[str, str | None, str | None, str | None], bool],
    node_results: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    expected_operator = execution.get("operator")
    if not isinstance(expected_operator, str) or not expected_operator:
        raise ExecutionError("attribution execution requires an explicit operator")
    expansion = execution.get("expansion") or {"mode": "none"}
    mode = expansion.get("mode", "none")
    if mode == "for_each_parent_group" and isinstance(execution.get("payload"), dict):
        raise ExecutionError("for_each_parent_group requires dynamic binding; static payload is not allowed")
    if mode not in {"none", "for_each_parent_group"}:
        raise ExecutionError(f"unsupported expansion mode: {mode!r}")

    if isinstance(execution.get("payload"), dict):
        payload = deepcopy(execution["payload"])
        roles = period_roles_for_scenario(str(payload.get("scenario", "")))
        payload["periods"] = validate_period_mapping(payload.get("periods"), runtime_periods, roles)
        validate_payload_contract(payload, expected_operator, get_operator, operator_matches)
        return {"mode": "none", "items": [{"parent": None, "payload": payload}]}

    binding = execution.get("binding")
    if not isinstance(binding, dict):
        raise ExecutionError("attribution execution requires payload or binding")
    roles = period_roles_for_scenario(str(binding.get("scenario", "")))
    binding_periods = validate_period_mapping(binding.get("periods"), runtime_periods, roles)
    prepared_execution = deepcopy(execution)
    prepared_execution["binding"]["periods"] = binding_periods
    if mode == "none":
        payload = build_attribution_payload(
            store, prepared_execution, node_results=node_results
        )
        validate_payload_contract(payload, expected_operator, get_operator, operator_matches)
        return {"mode": "none", "items": [{"parent": None, "payload": payload}]}

    parent_dimensions = expansion.get("parent_dimensions")
    if not isinstance(parent_dimensions, list) or not parent_dimensions:
        raise ExecutionError("for_each_parent_group requires parent_dimensions")
    group_config = binding.get("groups") or {}
    parent_selector = expansion.get("parent_selector", group_config.get("selector"))
    if not isinstance(parent_selector, dict) or not parent_selector:
        raise ExecutionError("for_each_parent_group requires expansion.parent_selector or binding.groups.selector")
    parents = store.parent_values(parent_selector, parent_dimensions)
    if not parents:
        raise ExecutionError("parent expansion matched no groups")
    items: list[dict[str, Any]] = []
    for parent in parents:
        payload = build_attribution_payload(
            store, prepared_execution, parent, node_results
        )
        validate_payload_contract(payload, expected_operator, get_operator, operator_matches)
        items.append({"parent": parent, "payload": payload})
    return {"mode": mode, "items": items}


def execute_attribution(
    prepared: dict[str, Any],
    expected_operator: str,
    run_attribution: Callable[..., dict[str, Any]],
    max_workers: int,
    residual_tolerance: float,
    node_id: str,
    events: EventLog,
) -> dict[str, Any]:
    mode = prepared["mode"]
    items = prepared["items"]
    if mode == "none":
        payload = items[0]["payload"]
        result = run_attribution(payload, explain_routing=True)
        residual_warning = validate_attribution_result(
            result, expected_operator, residual_tolerance
        )
        output = {"input_hash": sha256_value(payload), "result": result}
        if residual_warning is not None:
            output.update(status="partial_success", warnings=[residual_warning])
        return output

    def one(item: dict[str, Any]) -> dict[str, Any]:
        parent = item["parent"]
        payload = item["payload"]
        queued_at = utc_now()
        events.add("parent_queued", node_id=node_id, parent=parent)
        started_at = utc_now()
        started = time.perf_counter()
        events.add("parent_started", node_id=node_id, parent=parent)
        try:
            result = run_attribution(payload, explain_routing=True)
            residual_warning = validate_attribution_result(
                result, expected_operator, residual_tolerance
            )
            output = {
                "parent": parent,
                "status": "partial_success" if residual_warning else "success",
                "input_hash": sha256_value(payload),
                "result": result,
            }
            if residual_warning:
                output["warnings"] = [residual_warning]
        except Exception as exc:  # noqa: BLE001 - isolate each parent task.
            output = {"parent": parent, "status": "failed", "error": str(exc), "error_type": exc.__class__.__name__}
        output.update({
            "queued_at": queued_at,
            "started_at": started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        events.add(
            "parent_finished",
            node_id=node_id,
            parent=parent,
            status=output["status"],
            duration_ms=output["duration_ms"],
        )
        return output

    children: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {pool.submit(one, item): item["parent"] for item in items}
        for future in as_completed(futures):
            children.append(future.result())
    children.sort(key=lambda item: canonical_json(item["parent"]))
    succeeded = sum(item["status"] == "success" for item in children)
    partial = sum(item["status"] == "partial_success" for item in children)
    failed = sum(item["status"] == "failed" for item in children)
    status = (
        "success"
        if succeeded == len(children)
        else "partial_success"
        if succeeded or partial
        else "failed"
    )
    return {
        "status": status,
        "parents_total": len(children),
        "parents_succeeded": succeeded,
        "parents_partial": partial,
        "parents_failed": failed,
        "children": children,
    }


def executable_node(node: dict[str, Any]) -> bool:
    execution = node.get("execution") or {}
    return (
        node.get("status") != "blocked"
        and execution.get("mode") == "lightweight_executor"
        and execution.get("handler") in RUNNABLE_HANDLERS
    )


def dependency_succeeded(
    dependency: str,
    plan_nodes: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> bool:
    if dependency in results:
        return results[dependency].get("status") == "success"
    return plan_nodes.get(dependency, {}).get("status") == "success"


def compute_run_status(nodes: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]]) -> str:
    if any(node.get("criticality") == "core" and node.get("status") == "blocked" for node in nodes.values()):
        return "blocked"
    if any(node.get("criticality") == "required" and node.get("status") == "blocked" for node in nodes.values()):
        return "partial_success"
    for node_id, result in results.items():
        if (
            nodes[node_id].get("criticality") == "core"
            and result.get("status") in {"failed", "blocked"}
        ):
            return "blocked"
    for node_id, result in results.items():
        if nodes[node_id].get("criticality") == "required" and result.get("status") != "success":
            return "partial_success"
    if any(result.get("status") == "partial_success" for result in results.values()):
        return "partial_success"
    return "success"


def dry_bind_nodes(
    runnable: dict[str, dict[str, Any]],
    store: FactStore,
    runtime_periods: dict[str, str],
    get_operator: Callable[[str], dict[str, Any] | None] | None,
    operator_matches: Callable[[str, str | None, str | None, str | None], bool] | None,
    max_workers: int,
) -> tuple[dict[str, Any], dict[str, Exception]]:
    prepared: dict[str, Any] = {}
    errors: dict[str, Exception] = {}

    def one(node_id: str, node: dict[str, Any]) -> tuple[str, Any, Exception | None]:
        execution = node.get("execution") or {}
        handler = execution.get("handler")
        try:
            if handler == "fact_artifact":
                return node_id, None, None
            if handler == "derived":
                if not isinstance(execution.get("unit"), str) or not execution.get("unit"):
                    raise ExecutionError("derived execution requires unit")
                expressions = execution.get("expressions")
                expression_items = expressions.values() if isinstance(expressions, dict) else [execution.get("expression")]
                intermediate_expressions = execution.get("intermediate_expressions") or {}
                if isinstance(expressions, dict) and not expressions:
                    raise ExecutionError("derived expressions must not be empty")
                if not isinstance(intermediate_expressions, dict):
                    raise ExecutionError("derived intermediate_expressions must be an object")
                expression_items = [*expression_items, *intermediate_expressions.values()]
                fact_refs: list[dict[str, Any]] = []
                result_refs: set[str] = set()
                for expression in expression_items:
                    facts, refs = collect_expression_refs(expression)
                    fact_refs.extend(facts)
                    result_refs.update(refs)
                undeclared = result_refs - set(node.get("depends_on") or [])
                if undeclared:
                    raise ExecutionError(f"derived result references must be declared dependencies: {sorted(undeclared)}")
                for fact in fact_refs:
                    field = str(fact.get("field", "value"))
                    selector = {key: value for key, value in fact.items() if key != "field"}
                    group_dimensions = execution.get("group_dimensions") or []
                    if group_dimensions:
                        if not store.parent_values(selector, group_dimensions):
                            raise ExecutionError(f"group binding matched no dimension combinations: {selector}")
                    else:
                        store.unique_value(selector, field=field)
                return node_id, {"fact_refs": fact_refs, "result_refs": sorted(result_refs)}, None
            if handler == "attribution":
                if get_operator is None or operator_matches is None:
                    raise ExecutionError("attribution engine contract API is unavailable")
                value = prepare_attribution(
                    store,
                    execution,
                    runtime_periods,
                    get_operator,
                    operator_matches,
                )
                return node_id, value, None
            raise ExecutionError(f"unsupported handler: {handler!r}")
        except Exception as exc:  # noqa: BLE001 - report every node binding error before execution.
            return node_id, None, exc

    with ThreadPoolExecutor(max_workers=min(max_workers, len(runnable))) as pool:
        futures = [pool.submit(one, node_id, node) for node_id, node in sorted(runnable.items())]
        for future in as_completed(futures):
            node_id, value, error = future.result()
            if error is None:
                prepared[node_id] = value
            else:
                errors[node_id] = error
    return prepared, errors


def calculation_results(
    nodes: dict[str, dict[str, Any]],
    ordered_results: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    derived_results: list[dict[str, Any]] = []
    attribution_results: list[dict[str, Any]] = []
    for item in ordered_results:
        node_id = item["node_id"]
        execution = nodes[node_id].get("execution") or {}
        handler = item.get("handler")
        if handler == "derived":
            expression = execution.get("expressions", execution.get("expression"))
            try:
                fact_refs, result_refs = collect_expression_refs_for_output(expression)
            except ExecutionError:
                fact_refs, result_refs = [], []
            derived = {
                "derived_metric_id": execution.get("derived_metric_id", node_id),
                "node_id": node_id,
                "status": item["status"],
                "input_refs": fact_refs + result_refs,
                "formula": execution.get("formula", expression),
                "unit": execution.get("unit"),
                "value": item.get("result"),
                "input_hash": item.get("input_hash"),
            }
            for key in (
                "definition_source",
                "definition_version",
                "definition_status",
                "inference_basis",
                "metric",
                "metric_object",
                "view_id",
                "period_roles",
            ):
                if key in execution:
                    derived[key] = execution[key]
            if "intermediate_results" in item:
                derived["intermediate_results"] = item["intermediate_results"]
            if "error" in item:
                derived.update(error=item["error"], error_type=item.get("error_type"))
            derived_results.append(derived)
        elif handler == "attribution":
            value = item.get("result")
            if isinstance(value, dict) and isinstance(value.get("children"), list):
                for child in value["children"]:
                    child_result = {
                        "result_id": parent_result_key(node_id, child.get("parent") or {}),
                        "node_id": node_id,
                        "parent_dimensions": child.get("parent") or {},
                        "status": child.get("status"),
                        "input_hash": child.get("input_hash"),
                    }
                    for key in ("metric", "metric_object", "unit"):
                        if key in execution:
                            child_result[key] = execution[key]
                    if "result" in child:
                        child_result["result"] = child["result"]
                    if isinstance(child.get("warnings"), list):
                        child_result["warnings"] = deepcopy(child["warnings"])
                    if "error" in child:
                        child_result.update(error=child["error"], error_type=child.get("error_type"))
                    attribution_results.append(child_result)
            else:
                attribution = {
                    "result_id": node_id,
                    "node_id": node_id,
                    "status": item["status"],
                    "input_hash": item.get("input_hash"),
                }
                for key in ("metric", "metric_object", "unit"):
                    if key in execution:
                        attribution[key] = execution[key]
                if isinstance(value, dict) and isinstance(value.get("result"), dict):
                    attribution["result"] = value["result"]
                if isinstance(value, dict) and isinstance(value.get("warnings"), list):
                    attribution["warnings"] = deepcopy(value["warnings"])
                if "error" in item:
                    attribution.update(error=item["error"], error_type=item.get("error_type"))
                attribution_results.append(attribution)
    return derived_results, attribution_results


def collect_expression_refs_for_output(expression: Any) -> tuple[list[Any], list[Any]]:
    if isinstance(expression, dict) and "literal" not in expression and "fact" not in expression and "result" not in expression:
        facts: list[Any] = []
        results: list[Any] = []
        if isinstance(expression.get("args"), list):
            for arg in expression["args"]:
                arg_facts, arg_results = collect_expression_refs_for_output(arg)
                facts.extend(arg_facts)
                results.extend(arg_results)
        else:
            for value in expression.values():
                arg_facts, arg_results = collect_expression_refs_for_output(value)
                facts.extend(arg_facts)
                results.extend(arg_results)
        return facts, results
    if isinstance(expression, dict) and "fact" in expression:
        return [{"fact": expression["fact"]}], []
    if isinstance(expression, dict) and "result" in expression:
        return [], [{"result": expression["result"]}]
    if isinstance(expression, dict):
        facts: list[Any] = []
        results: list[Any] = []
        for value in expression.values():
            arg_facts, arg_results = collect_expression_refs_for_output(value)
            facts.extend(arg_facts)
            results.extend(arg_results)
        return facts, results
    return [], []


def aggregate_fetch_timing(plan: dict[str, Any]) -> tuple[float | None, bool, int]:
    results = [item for item in plan.get("fetch_results", []) if isinstance(item, dict)]
    if not results:
        return None, False, 0
    intervals: list[tuple[datetime, datetime]] = []
    durations: list[float] = []
    for result in results:
        duration = result.get("duration_ms")
        try:
            parsed_duration = float(duration)
        except (TypeError, ValueError):
            parsed_duration = -1
        if math.isfinite(parsed_duration) and parsed_duration >= 0:
            durations.append(parsed_duration)
        try:
            started = datetime.fromisoformat(str(result["started_at"]).replace("Z", "+00:00"))
            ended = datetime.fromisoformat(str(result["ended_at"]).replace("Z", "+00:00"))
            if started.tzinfo is None or ended.tzinfo is None:
                raise ValueError("timestamps require timezones")
            if ended < started:
                raise ValueError("end precedes start")
            intervals.append((started, ended))
        except (KeyError, TypeError, ValueError):
            continue
    if len(intervals) == len(results):
        elapsed = (max(item[1] for item in intervals) - min(item[0] for item in intervals)).total_seconds() * 1000
        return round(elapsed, 3), True, len(results)
    if len(durations) == len(results):
        return round(sum(durations), 3), True, len(results)
    return None, False, len(results)


def assemble_execution_document(
    plan: dict[str, Any],
    normalized: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    *,
    plan_hash: str,
    facts_hash: str,
    engine_hash: str | None,
    started_at: str,
    duration_ms: float,
    performance_metrics: dict[str, Any],
    include_facts: bool,
    include_calculation_results: bool = True,
    artifact_store: ArtifactStore | None = None,
) -> dict[str, Any]:
    document = deepcopy(plan)
    ordered = [results[node_id] for node_id in sorted(results)]
    result_by_id = {item["node_id"]: item for item in ordered}
    assembled_nodes: list[dict[str, Any]] = []
    for node in document.get("nodes", []):
        assembled = deepcopy(node)
        result = result_by_id.get(str(node.get("node_id")))
        if result is not None:
            assembled["status"] = result["status"]
        assembled_nodes.append(assembled)
    document["nodes"] = assembled_nodes
    node_map = {str(node["node_id"]): node for node in assembled_nodes if node.get("node_id")}
    runnable_ids = {node_id for node_id, node in node_map.items() if executable_node(node)}
    run_status = compute_run_status(node_map, results) if len(results) == len(runnable_ids) else "running"
    succeeded = sorted(node_id for node_id, node in node_map.items() if node.get("status") == "success")
    failed = sorted(node_id for node_id, node in node_map.items() if node.get("status") == "failed")
    partial = sorted(node_id for node_id, node in node_map.items() if node.get("status") == "partial_success")
    skipped = sorted(node_id for node_id, node in node_map.items() if node.get("status") == "skipped")
    blocked = sorted(node_id for node_id, node in node_map.items() if node.get("status") == "blocked")
    if artifact_store is not None:
        summaries, result_index_ref, collection_counts = artifact_store.sync_results(ordered)
        document.update({
            "executor": {"name": EXECUTOR_NAME, "version": EXECUTOR_VERSION},
            "storage": {
                "mode": "reference",
                "schema_version": STORAGE_SCHEMA_VERSION,
                "artifact_root": artifact_store.relative_path(artifact_store.root),
            },
            "artifacts": artifact_store.manifest(),
            "result_collections": {
                "normalized_facts": {"artifact_id": "normalized_facts", "records": len(normalized)},
                "node_results": {"artifact_ids": [item["result_ref"]["artifact_id"] for item in summaries], "records": len(summaries)},
                "derived_results": {"source": "node_results", "records": collection_counts["derived_results"]},
                "attribution_results": {"source": "node_results", "records": collection_counts["attribution_results"]},
            },
            "plan_hash": plan_hash,
            "facts_hash": facts_hash,
            "engine_hash": engine_hash,
            "started_at": started_at,
            "ended_at": utc_now(),
            "status": run_status,
            "declared_status": run_status,
            "computed_status": run_status,
            "normalized_fact_summary": {
                "records": len(normalized),
                "missing_records": sum(bool(row.get("missing")) for row in normalized),
            },
            "performance_metrics": performance_metrics,
            "node_results": summaries,
            "result_index": result_index_ref,
            "execution_summary": {
                "succeeded_nodes": succeeded,
                "failed_nodes": failed,
                "partial_nodes": partial,
                "skipped_nodes": skipped,
                "blocked_nodes": blocked,
                "duration_ms": duration_ms,
            },
        })
        document.pop("normalized_facts", None)
        document.pop("derived_results", None)
        document.pop("attribution_results", None)
        return document
    result_index: dict[str, Any] = {}
    for index, item in enumerate(ordered):
        result_index[item["node_id"]] = {
            "status": item["status"],
            "result_ref": f"$.node_results[{index}]",
            "input_hash": item.get("input_hash"),
        }
        value = item.get("result")
        if isinstance(value, dict) and isinstance(value.get("children"), list):
            for child_index, child in enumerate(value["children"]):
                key = parent_result_key(item["node_id"], child.get("parent") or {})
                result_index[key] = {
                    "node_id": item["node_id"],
                    "parent_dimensions": child.get("parent") or {},
                    "status": child.get("status"),
                    "result_ref": f"$.node_results[{index}].result.children[{child_index}]",
                    "input_hash": child.get("input_hash"),
                }
    derived_results, attribution_results = calculation_results(node_map, ordered) if include_calculation_results else ([], [])
    document.update({
        "executor": {"name": EXECUTOR_NAME, "version": EXECUTOR_VERSION},
        "plan_hash": plan_hash,
        "facts_hash": facts_hash,
        "engine_hash": engine_hash,
        "started_at": started_at,
        "ended_at": utc_now(),
        "status": run_status,
        "declared_status": run_status,
        "computed_status": run_status,
        "normalized_fact_summary": {
            "records": len(normalized),
            "missing_records": sum(bool(row.get("missing")) for row in normalized),
        },
        "performance_metrics": performance_metrics,
        "node_results": ordered,
        "result_index": result_index,
        "execution_summary": {
            "succeeded_nodes": succeeded,
            "failed_nodes": failed,
            "partial_nodes": partial,
            "skipped_nodes": skipped,
            "blocked_nodes": blocked,
            "duration_ms": duration_ms,
        },
    })
    if include_calculation_results:
        document["derived_results"] = derived_results
        document["attribution_results"] = attribution_results
    else:
        document.pop("derived_results", None)
        document.pop("attribution_results", None)
    if include_facts:
        document["normalized_facts"] = normalized
    else:
        document.pop("normalized_facts", None)
    return document


def execute_plan(
    plan: dict[str, Any],
    facts: list[dict[str, Any]] | FactInput,
    output_path: Path,
    events_path: Path | None,
    storage_mode: str = "inline",
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    started_wall = time.perf_counter()
    started_at = utc_now()
    events = EventLog(events_path)
    runtime = plan.get("execution_runtime") or {}
    if runtime and runtime.get("version") not in {None, "1.0"}:
        raise ExecutionError(f"unsupported execution_runtime.version: {runtime.get('version')!r}")
    periods = runtime.get("periods") or {}
    dimension_fields = runtime.get("dimension_fields") or []
    if not isinstance(dimension_fields, list) or not all(isinstance(item, str) and item for item in dimension_fields):
        raise ExecutionError("execution_runtime.dimension_fields must be a string list")
    if isinstance(facts, FactInput):
        fact_input = facts
    else:
        fact_input = FactInput(
            layout="long",
            rows=facts,
            fact_mappings={},
            shared={},
            raw_bytes=None,
            parse_ms=0.0,
        )
    normalize_started = time.perf_counter()
    logical_facts = wide_facts_to_long(fact_input)
    normalized = normalize_facts(logical_facts, periods, dimension_fields)
    normalize_ms = round((time.perf_counter() - normalize_started) * 1000, 3)
    total_fetch_ms, fetch_timing_complete, fetch_attempts = aggregate_fetch_timing(plan)
    performance_metrics = {
        "input_layout": fact_input.layout,
        "raw_bytes": fact_input.raw_bytes,
        "physical_rows": len(fact_input.rows),
        "logical_facts": len(logical_facts),
        "parse_ms": fact_input.parse_ms,
        "normalize_ms": normalize_ms,
        "total_fetch_ms": total_fetch_ms,
        "fetch_timing_complete": fetch_timing_complete,
        "fetch_attempts": fetch_attempts,
    }
    resolved_domains = plan.get("resolved_dimension_domains") or {}
    if not isinstance(resolved_domains, dict):
        raise ExecutionError("resolved_dimension_domains must be an object")
    store = FactStore(normalized, resolved_domains)
    plan_hash = sha256_value(plan)
    facts_hash = sha256_value(normalized)
    max_workers = int(runtime.get("max_workers", 4))
    if max_workers < 1 or max_workers > 32:
        raise ExecutionError("execution_runtime.max_workers must be between 1 and 32")
    residual_tolerance = float(runtime.get("residual_tolerance", 1e-8))
    if residual_tolerance < 0:
        raise ExecutionError("execution_runtime.residual_tolerance must be >= 0")
    nodes_raw = plan.get("nodes")
    if not isinstance(nodes_raw, list):
        raise ExecutionError("plan.nodes must be a list")
    nodes = {str(node.get("node_id")): node for node in nodes_raw if isinstance(node, dict) and node.get("node_id")}
    runnable = {node_id: node for node_id, node in nodes.items() if executable_node(node)}
    if not runnable:
        raise ExecutionError("plan contains no lightweight_executor nodes")
    if storage_mode not in {"auto", "inline", "reference"}:
        raise ExecutionError("storage_mode must be auto, inline, or reference")
    if storage_mode == "auto":
        has_parent_fanout = any(
            ((node.get("execution") or {}).get("expansion") or {}).get("mode") == "for_each_parent_group"
            for node in runnable.values()
        )
        storage_mode = "reference" if len(normalized) > 1000 or has_parent_fanout else "inline"
    artifact_store = ArtifactStore(output_path, artifact_dir) if storage_mode == "reference" else None
    if artifact_store is not None:
        artifact_store.write_facts(normalized)
    has_attribution = any((node.get("execution") or {}).get("handler") == "attribution" for node in runnable.values())
    engine_hash = None
    if has_attribution:
        expected_identity = plan.get("attribution_engine")
        if not isinstance(expected_identity, dict):
            raise ExecutionError("attribution plan is missing embedded engine identity")
        attribution_api = load_attribution_api()
        from _vendor.attribution_core import identity

        runtime_identity = identity()
        if expected_identity != runtime_identity:
            raise ExecutionError("compiled plan attribution engine identity does not match embedded runtime")
        engine_hash = str(runtime_identity["core_sha256"])
    else:
        attribution_api = (None, None, None)
    run_attribution, get_operator, operator_matches = attribution_api
    events.add("run_started", plan_hash=plan_hash, facts_hash=facts_hash, runnable_nodes=len(runnable))
    deferred_preflight = {
        node_id
        for node_id, node in runnable.items()
        if any(
            dependency in runnable and dependency != "fact_artifact"
            for dependency in (node.get("depends_on") or [])
        )
    }
    prepared, preflight_errors = dry_bind_nodes(
        {
            node_id: node
            for node_id, node in runnable.items()
            if node_id not in deferred_preflight
        },
        store,
        periods,
        get_operator,
        operator_matches,
        max_workers,
    )
    events.add(
        "preflight_finished",
        nodes=len(runnable),
        failed_nodes=sorted(preflight_errors),
    )
    results: dict[str, dict[str, Any]] = {}
    if output_path.exists():
        try:
            previous = load_json(output_path)
            previous_executor = previous.get("executor") if isinstance(previous, dict) else None
            if (
                isinstance(previous, dict)
                and isinstance(previous_executor, dict)
                and previous_executor.get("version") == EXECUTOR_VERSION
                and previous.get("plan_hash") == plan_hash
                and previous.get("facts_hash") == facts_hash
                and previous.get("engine_hash") == engine_hash
            ):
                cached = (
                    cached_reference_results(previous, output_path.parent)
                    if (previous.get("storage") or {}).get("mode") == "reference"
                    else {
                        str(item.get("node_id")): item
                        for item in previous.get("node_results", [])
                        if isinstance(item, dict) and item.get("node_id")
                    }
                )
                for item in cached.values():
                    if item.get("node_id") in runnable and item.get("status") == "success":
                        results[item["node_id"]] = item
        except ExecutionError:
            pass
    pending = set(runnable) - set(results)
    for node_id, result in sorted(results.items()):
        materialized = result.get("materialized_facts")
        if isinstance(materialized, list):
            store.add_rows(materialized)
    for node_id in sorted(results):
        events.add("node_cache_hit", node_id=node_id, input_hash=results[node_id].get("input_hash"))

    def snapshot(include_facts: bool = False) -> dict[str, Any]:
        return assemble_execution_document(
            plan,
            normalized,
            results,
            plan_hash=plan_hash,
            facts_hash=facts_hash,
            engine_hash=engine_hash,
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started_wall) * 1000, 3),
            performance_metrics=performance_metrics,
            include_facts=include_facts,
            include_calculation_results=include_facts,
            artifact_store=artifact_store,
        )

    def run_one(node_id: str, queued_at: str) -> dict[str, Any]:
        node = nodes[node_id]
        execution = node.get("execution") or {}
        handler = execution.get("handler")
        dependencies = node.get("depends_on") or []
        node_started_at = utc_now()
        node_started = time.perf_counter()
        input_hash = sha256_value({
            "execution": execution,
            "facts_hash": facts_hash,
            "engine_hash": engine_hash if handler == "attribution" else None,
            "dependencies": {
                dependency: results.get(dependency, {}).get("input_hash")
                for dependency in dependencies
            },
        })
        events.add("node_started", node_id=node_id, handler=handler, input_hash=input_hash)
        try:
            if node_id in preflight_errors:
                raise preflight_errors[node_id]
            if handler == "fact_artifact":
                value: Any = {"records": len(normalized), "facts_hash": facts_hash}
                status = "success"
            elif handler == "derived":
                group_dimensions = execution.get("group_dimensions") or []
                group_parents: list[dict[str, Any]] = []
                if group_dimensions:
                    expression_for_groups = execution.get("expression")
                    group_fact_refs, _ = collect_expression_refs(expression_for_groups)
                    if not group_fact_refs:
                        raise ExecutionError("grouped derived expression requires a fact selector")
                    group_selector = {
                        key: value
                        for key, value in group_fact_refs[0].items()
                        if key != "field"
                    }
                    group_parents = store.parent_values(group_selector, group_dimensions)
                    if not group_parents:
                        raise ExecutionError("grouped derived expression matched no dimension combinations")
                if isinstance(execution.get("expressions"), dict):
                    if group_parents:
                        value = [
                            {
                                "dimensions": parent,
                                "values": {
                                    name: evaluate_expression(expression, store, results, parent)
                                    for name, expression in execution["expressions"].items()
                                },
                            }
                            for parent in group_parents
                        ]
                    else:
                        value = {
                            name: evaluate_expression(expression, store, results)
                            for name, expression in execution["expressions"].items()
                        }
                else:
                    if group_parents:
                        value = [
                            {
                                "dimensions": parent,
                                "value": evaluate_expression(execution.get("expression"), store, results, parent),
                            }
                            for parent in group_parents
                        ]
                    else:
                        value = evaluate_expression(execution.get("expression"), store, results)
                status = "success"
            elif handler == "attribution":
                if run_attribution is None:
                    raise ExecutionError("attribution engine is unavailable")
                prepared_value = prepared.get(node_id)
                if prepared_value is None:
                    if get_operator is None or operator_matches is None:
                        raise ExecutionError("attribution engine contract API is unavailable")
                    prepared_value = prepare_attribution(
                        store,
                        execution,
                        periods,
                        get_operator,
                        operator_matches,
                        results,
                    )
                value = execute_attribution(
                    prepared_value,
                    str(execution.get("operator")),
                    run_attribution,
                    max_workers,
                    residual_tolerance,
                    node_id,
                    events,
                )
                status = value.get("status", "success") if isinstance(value, dict) else "success"
            else:
                raise ExecutionError(f"unsupported handler: {handler!r}")
            result = {
                "node_id": node_id,
                "handler": handler,
                "status": status,
                "input_hash": input_hash,
                "result": value,
            }
            if status == "success" and execution.get("materialize_as") is not None:
                result["materialized_facts"] = materialize_intermediate_facts(
                    node_id, execution, value, store
                )
            if handler == "derived" and execution.get("intermediate_expressions"):
                intermediate = execution["intermediate_expressions"]
                if execution.get("group_dimensions"):
                    intermediate = [
                        {
                            "dimensions": item.get("dimensions", {}),
                            "values": {
                                name: evaluate_expression(expression, store, results, item.get("dimensions", {}))
                                for name, expression in execution["intermediate_expressions"].items()
                            },
                        }
                        for item in value
                    ]
                else:
                    intermediate = {
                        name: evaluate_expression(expression, store, results)
                        for name, expression in execution["intermediate_expressions"].items()
                    }
                result["intermediate_results"] = intermediate
        except Exception as exc:  # noqa: BLE001 - preserve node failure and continue independent nodes.
            result = {
                "node_id": node_id,
                "handler": handler,
                "status": "failed",
                "input_hash": input_hash,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }
        result.update({
            "queued_at": queued_at,
            "started_at": node_started_at,
            "ended_at": utc_now(),
            "duration_ms": round((time.perf_counter() - node_started) * 1000, 3),
        })
        return result

    while pending:
        ready = [
            node_id
            for node_id in sorted(pending)
            if not any(dependency in pending for dependency in (nodes[node_id].get("depends_on") or []))
        ]
        if not ready:
            raise ExecutionError(f"cannot make progress; unresolved executor nodes: {sorted(pending)}")
        runnable_ready: list[tuple[str, str]] = []
        for node_id in ready:
            node = nodes[node_id]
            dependencies = node.get("depends_on") or []
            failed_dependencies = [dep for dep in dependencies if not dependency_succeeded(dep, nodes, results)]
            if failed_dependencies:
                queued_at = utc_now()
                results[node_id] = {
                    "node_id": node_id,
                    "handler": (node.get("execution") or {}).get("handler"),
                    "status": "skipped",
                    "error": f"dependencies did not succeed: {failed_dependencies}",
                    "queued_at": queued_at,
                    "started_at": utc_now(),
                    "ended_at": utc_now(),
                    "duration_ms": 0,
                }
                events.add("node_skipped", node_id=node_id, failed_dependencies=failed_dependencies)
                pending.remove(node_id)
                atomic_write_json(output_path, snapshot())
                continue
            queued_at = utc_now()
            events.add("node_queued", node_id=node_id, handler=(node.get("execution") or {}).get("handler"))
            runnable_ready.append((node_id, queued_at))
        if runnable_ready:
            completed_results: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=min(max_workers, len(runnable_ready))) as pool:
                futures = {pool.submit(run_one, node_id, queued_at): node_id for node_id, queued_at in runnable_ready}
                for future in as_completed(futures):
                    result = future.result()
                    node_id = result["node_id"]
                    results[node_id] = result
                    completed_results.append(result)
                    pending.remove(node_id)
                    events.add("node_finished", node_id=node_id, status=result["status"], duration_ms=result["duration_ms"])
            for result in completed_results:
                materialized = result.get("materialized_facts")
                if result.get("status") == "success" and isinstance(materialized, list):
                    store.add_rows(materialized)
            atomic_write_json(output_path, snapshot())

    final = snapshot(include_facts=True)
    events.add("run_finished", status=final["status"], duration_ms=final["execution_summary"]["duration_ms"])
    atomic_write_json(output_path, final)
    if artifact_store is not None:
        artifact_store.prune_unreferenced()
    events.flush()
    return final


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="validated scene-analysis plan JSON")
    parser.add_argument("--facts", required=True, type=Path, help="normalized fact JSON or JSONL")
    parser.add_argument("--output", required=True, type=Path, help="executor result JSON")
    parser.add_argument("--events", type=Path, help="optional JSONL timing event path")
    parser.add_argument("--storage-mode", choices=("auto", "inline", "reference"), default="auto")
    parser.add_argument("--artifact-dir", type=Path, help="artifact directory inside the output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = load_json(args.plan)
        if not isinstance(plan, dict):
            raise ExecutionError("plan top level must be an object")
        facts = load_fact_input(args.facts)
        result = execute_plan(
            plan,
            facts,
            args.output,
            args.events,
            storage_mode=args.storage_mode,
            artifact_dir=args.artifact_dir,
        )
        print(json.dumps({
            "status": result["status"],
            "nodes": len(result["node_results"]),
            "duration_ms": result["execution_summary"]["duration_ms"],
            "output": str(args.output),
        }, ensure_ascii=False))
        return 0 if result["status"] == "success" else 1
    except Exception as exc:  # noqa: BLE001 - CLI returns a concise structured failure.
        print(json.dumps({"status": "blocked", "error": str(exc), "error_type": exc.__class__.__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
