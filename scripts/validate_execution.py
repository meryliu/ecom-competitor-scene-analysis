#!/usr/bin/env python3
"""Validate a scene-analysis plan or execution result without mutating it."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from fact_contract import stable_id
from time_rollup import normalize_period, overlap_days


NODE_STATUSES = {
    "planned",
    "waiting_resolution",
    "waiting_confirmation",
    "running",
    "success",
    "partial_success",
    "failed",
    "skipped",
    "blocked",
}
TERMINAL_STATUSES = {"success", "partial_success", "failed", "skipped", "blocked"}
CRITICALITIES = {"core", "required", "optional"}
ATTRIBUTION_SCENARIOS = {
    "metric_change": ("analysis", "comparison"),
    "yoy_trend_change": ("analysis", "analysis_last_year", "comparison", "comparison_last_year"),
}
TARGET_SEMANTICS = {"absolute_delta", "relative_yoy_trend", "point_yoy_trend"}
FINAL_STATUSES = {"success", "partial_success", "waiting_confirmation", "blocked"}
PLAN_STATUSES = {
    "ready_for_resolution",
    "ready_for_confirmation",
    "ready_for_fetch",
    "blocked",
}
NORMALIZATION_REASONS = {
    "source_missing",
    "zero_denominator",
    "value_missing",
    "incomplete_components",
    "value_derived_from_components",
    "unchanged",
}
REFERENCE_STORAGE_VERSION = "2.0"
REFERENCE_EXECUTOR_VERSIONS = {"1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0", "1.8.0", "1.8.1", "1.9.0", "1.10.0"}


def compiler_uses_fact_slots(document: dict[str, Any]) -> bool:
    raw_version = str((document.get("compiler") or {}).get("version") or "")
    try:
        version = tuple(int(part) for part in raw_version.split("."))
    except ValueError:
        return False
    return version >= (1, 9, 0)


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


@dataclass(frozen=True)
class Issue:
    rule_id: str
    severity: str
    path: str
    message: str
    suggested_action: str
    node_id: str | None = None
    request_id: str | None = None


class Validator:
    def __init__(self, document: dict[str, Any], phase: str, base_dir: Path | None = None) -> None:
        self.document = document
        self.phase = phase
        self.base_dir = base_dir.resolve() if base_dir is not None else None
        self.issues: list[Issue] = []
        self.nodes: list[dict[str, Any]] = []
        self.node_map: dict[str, dict[str, Any]] = {}
        self.attribution_targets: dict[str, dict[str, Any]] = {}
        self.operator_contracts: dict[str, dict[str, Any]] = {}

    def add(
        self,
        rule_id: str,
        severity: str,
        path: str,
        message: str,
        suggested_action: str,
        *,
        node_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.issues.append(
            Issue(
                rule_id,
                severity,
                path,
                message,
                suggested_action,
                node_id,
                request_id,
            )
        )

    def validate(self) -> dict[str, Any]:
        self._validate_top_level()
        self._validate_compiler_contract()
        self._validate_execution_profile()
        self._validate_attribution_targets()
        self._validate_nodes()
        self._validate_executor_contract()
        self._validate_graph()
        self._validate_execution_summary()
        self._validate_requests()
        self._validate_artifacts()
        self._validate_facts()
        self._validate_calculation_results()
        self._validate_conclusions()
        self._validate_dimension_decisions()
        self._validate_performance_metrics()

        computed_status = self._compute_status()
        declared_status = self.document.get("status")
        if declared_status and computed_status and declared_status != computed_status:
            self.add(
                "STATUS-001",
                "ERROR",
                "$.status",
                f"声明状态 {declared_status!r} 与校验器计算状态 {computed_status!r} 不一致",
                "根据节点终态和阻断确认项更新顶层状态",
            )

        errors = sum(issue.severity == "ERROR" for issue in self.issues)
        warnings = sum(issue.severity == "WARNING" for issue in self.issues)
        counts = {status: 0 for status in sorted(NODE_STATUSES)}
        for node in self.nodes:
            status = node.get("status")
            if status in counts:
                counts[status] += 1

        return {
            "valid": errors == 0,
            "phase": self.phase,
            "declared_status": declared_status,
            "computed_status": computed_status,
            "summary": {
                "errors": errors,
                "warnings": warnings,
                "nodes_total": len(self.nodes),
                "node_status_counts": counts,
            },
            "issues": [asdict(issue) for issue in self.issues],
        }

    def _validate_execution_profile(self) -> None:
        profile = self.document.get("execution_profile")
        admission = self.document.get("fast_query_admission")
        if profile is None and admission is None:
            return
        allowed = {"fast_fact", "fast_derived", "standard", "orchestrated"}
        if profile not in allowed:
            self.add(
                "FAST-001",
                "ERROR",
                "$.execution_profile",
                f"execution_profile 非法: {profile!r}",
                "使用 fast_fact、fast_derived、standard 或 orchestrated",
            )
            return
        if not isinstance(admission, dict):
            self.add("FAST-002", "ERROR", "$.fast_query_admission", "缺少 fast query 准入证据", "保存确定性 admission 结果")
            return
        if admission.get("schema_version") != "fast_query_admission/1.0":
            self.add("FAST-003", "ERROR", "$.fast_query_admission.schema_version", "fast query admission 版本非法", "使用 fast_query_admission/1.0")
        if admission.get("execution_profile") != profile:
            self.add("FAST-004", "ERROR", "$.fast_query_admission.execution_profile", "准入结果与顶层 profile 不一致", "重新运行确定性准入")
        expected_eligible = profile in {"fast_fact", "fast_derived"}
        if admission.get("eligible") is not expected_eligible:
            self.add("FAST-005", "ERROR", "$.fast_query_admission.eligible", "eligible 与 execution_profile 不一致", "重新运行确定性准入")
        validation_profile = admission.get("validation_profile")
        expected_validation = "minimal" if expected_eligible else "full"
        if validation_profile != expected_validation:
            self.add("FAST-006", "ERROR", "$.fast_query_admission.validation_profile", "validation_profile 与准入结果不一致", "使用匹配的校验档位")
        if expected_eligible and self.document.get("attribution_targets"):
            self.add("FAST-007", "ERROR", "$.attribution_targets", "fast query 不得包含归因目标", "升级为 orchestrated")
        for field in ("features", "limits", "reasons", "fallback_triggers"):
            value = admission.get(field)
            if field in {"reasons", "fallback_triggers"}:
                valid = isinstance(value, list)
            else:
                valid = isinstance(value, dict)
            if not valid:
                self.add("FAST-008", "ERROR", f"$.fast_query_admission.{field}", f"{field} 结构非法", "重新运行确定性准入")

    def _validate_top_level(self) -> None:
        for key in ("analysis_task", "attribution_targets", "nodes", "fetch_requests", "clarifications", "status"):
            if key not in self.document:
                self.add(
                    "DOC-001",
                    "ERROR",
                    f"$.{key}",
                    f"缺少顶层字段 {key}",
                    "按 scene-analysis 输出契约补齐字段",
                )
        if not isinstance(self.document.get("analysis_task", {}), dict):
            self.add("DOC-002", "ERROR", "$.analysis_task", "analysis_task 必须是对象", "改为 JSON 对象")
        if not isinstance(self.document.get("nodes", []), list):
            self.add("DOC-003", "ERROR", "$.nodes", "nodes 必须是数组", "改为节点数组")
        if not isinstance(self.document.get("attribution_targets", []), list):
            self.add("DOC-006", "ERROR", "$.attribution_targets", "attribution_targets 必须是数组", "改为归因目标数组；无归因需求时使用空数组")
        if self.phase == "final" and not isinstance(self.document.get("execution_summary"), dict):
            self.add(
                "DOC-004",
                "ERROR",
                "$.execution_summary",
                "最终执行结果缺少 execution_summary",
                "补充 succeeded_nodes、failed_nodes 和 skipped_nodes",
            )
        declared_status = self.document.get("status")
        allowed_statuses = PLAN_STATUSES if self.phase == "plan" else FINAL_STATUSES
        if declared_status not in allowed_statuses:
            self.add(
                "DOC-005",
                "ERROR",
                "$.status",
                f"{self.phase} 阶段的顶层状态非法: {declared_status!r}",
                "使用当前阶段允许的顶层状态",
            )

    def _validate_compiler_contract(self) -> None:
        compiler = self.document.get("compiler")
        if compiler is None:
            return
        if not isinstance(compiler, dict):
            self.add("COMP-001", "ERROR", "$.compiler", "compiler 必须是对象", "保留编译器名称、版本、IR 版本和耗时")
            return
        for field in ("name", "version", "source_ir_version", "source_ir_sha256"):
            if not isinstance(compiler.get(field), str) or not compiler[field]:
                self.add("COMP-002", "ERROR", f"$.compiler.{field}", f"编译器缺少 {field}", "由 compile_plan.py 生成该字段")
        if compiler.get("name") != "scene-analysis-plan-compiler":
            self.add("COMP-003", "ERROR", "$.compiler.name", "不支持的计划编译器", "使用 scene-analysis-plan-compiler")
        source_ir = self.document.get("analysis_ir")
        if not isinstance(source_ir, dict) or source_ir.get("ir_version") != "analysis_ir/1.0":
            self.add("COMP-004", "ERROR", "$.analysis_ir", "编译计划缺少 analysis_ir/1.0 来源", "保留精简 IR 以便复编译和审计")
            return
        actual_ir_hash = hashlib.sha256(
            json.dumps(source_ir, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if compiler.get("source_ir_sha256") != actual_ir_hash:
            self.add("COMP-022", "ERROR", "$.compiler.source_ir_sha256", "源 IR 哈希与计划不一致", "修改 IR 后使用 compile_plan.py 重新编译")
        timings = compiler.get("timings")
        if not isinstance(timings, dict):
            self.add("COMP-005", "ERROR", "$.compiler.timings", "编译器缺少耗时对象", "记录能力解析、编译和计划校验耗时")
        else:
            for field in ("operator_resolution_ms", "compile_ms", "plan_validation_ms"):
                value = timings.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                    self.add("COMP-006", "ERROR", f"$.compiler.timings.{field}", f"{field} 必须是非负数", "由编译器记录毫秒耗时")

        requirement_ids: set[str] = set()
        for collection, id_field in (
            ("input_adaptations", "requirement_id"),
            ("fact_observations", "requirement_id"),
            ("metric_compositions", "requirement_id"),
            ("derived_requirements", "requirement_id"),
            ("custom_calculations", "requirement_id"),
            ("attribution_targets", "target_id"),
            ("output_requirements", "requirement_id"),
        ):
            values = source_ir.get(collection, [])
            if not isinstance(values, list):
                self.add("COMP-007", "ERROR", f"$.analysis_ir.{collection}", f"{collection} 必须是数组", "修正源 IR")
                continue
            for index, item in enumerate(values):
                requirement_id = item.get(id_field) if isinstance(item, dict) else None
                if not isinstance(requirement_id, str) or not requirement_id:
                    self.add("COMP-008", "ERROR", f"$.analysis_ir.{collection}[{index}].{id_field}", "需求缺少唯一 ID", "修正源 IR")
                elif requirement_id in requirement_ids:
                    self.add("COMP-009", "ERROR", f"$.analysis_ir.{collection}[{index}].{id_field}", f"需求 ID {requirement_id!r} 重复", "使用跨类型唯一需求 ID")
                else:
                    requirement_ids.add(requirement_id)

        compilation = self.document.get("requirement_compilation")
        if not isinstance(compilation, list):
            self.add("COMP-010", "ERROR", "$.requirement_compilation", "缺少需求编译映射", "为每个 IR 需求记录节点、阻断或确认状态")
            return
        compiled_ids: set[str] = set()
        plan_node_ids = {
            node.get("node_id") for node in self.document.get("nodes", []) if isinstance(node, dict)
        }
        fact_slot_ids = {
            slot.get("fact_slot_id")
            for slot in (self.document.get("analysis_task") or {}).get("fact_requirements", [])
            if isinstance(slot, dict)
        }
        node_by_id = {
            node.get("node_id"): node for node in self.document.get("nodes", []) if isinstance(node, dict)
        }
        for index, item in enumerate(compilation):
            path = f"$.requirement_compilation[{index}]"
            if not isinstance(item, dict):
                self.add("COMP-011", "ERROR", path, "需求编译记录必须是对象", "重新编译计划")
                continue
            requirement_id = item.get("requirement_id")
            if not isinstance(requirement_id, str) or requirement_id not in requirement_ids:
                self.add("COMP-012", "ERROR", f"{path}.requirement_id", "需求编译记录没有对应源 IR 需求", "移除多余记录或修正需求引用")
                continue
            if requirement_id in compiled_ids:
                self.add("COMP-013", "ERROR", f"{path}.requirement_id", f"需求 {requirement_id!r} 被重复编译", "每个需求只保留一条编译记录")
            compiled_ids.add(requirement_id)
            status = item.get("status")
            if status not in {"compiled", "blocked", "waiting_confirmation"}:
                self.add("COMP-014", "ERROR", f"{path}.status", "非法需求编译状态", "使用 compiled、blocked 或 waiting_confirmation")
            node_ids = item.get("node_ids")
            if not isinstance(node_ids, list) or not node_ids:
                self.add("COMP-015", "ERROR", f"{path}.node_ids", "需求没有映射到任何节点", "生成执行、阻断或确认节点")
                continue
            for node_id in node_ids:
                if node_id not in plan_node_ids:
                    self.add("COMP-016", "ERROR", f"{path}.node_ids", f"引用不存在节点 {node_id!r}", "重新编译节点图")
                    continue
                refs = node_by_id[node_id].get("requirement_refs", [])
                if requirement_id not in refs:
                    self.add("COMP-017", "ERROR", f"{path}.node_ids", f"节点 {node_id!r} 未回指需求", "将 requirement_id 写入节点 requirement_refs")
                if status == "blocked" and node_by_id[node_id].get("status") != "blocked":
                    self.add("COMP-018", "ERROR", f"{path}.status", "阻断需求映射到非阻断节点", "保持需求与节点状态一致")
            slots = item.get("fact_slot_ids", [])
            if not isinstance(slots, list) or any(slot not in fact_slot_ids for slot in slots):
                self.add("COMP-019", "ERROR", f"{path}.fact_slot_ids", "需求引用不存在的事实槽位", "重新执行事实槽位合并")
        missing = sorted(requirement_ids - compiled_ids)
        if missing:
            self.add("COMP-020", "ERROR", "$.requirement_compilation", f"IR 需求未编译: {missing!r}", "为每个需求生成节点、阻断或确认记录")

        source_targets = source_ir.get("attribution_targets", [])
        if isinstance(source_targets, list) and not source_targets:
            task = self.document.get("analysis_task") or {}
            has_operator_state = bool(task.get("operator_queries")) or bool(task.get("operator_contracts"))
            has_attribution_node = any(self._is_attribution_node(node) for node in self.document.get("nodes", []) if isinstance(node, dict))
            if has_operator_state or has_attribution_node:
                self.add("COMP-021", "ERROR", "$.analysis_ir.attribution_targets", "无归因目标时仍生成了归因能力或节点", "跳过 attribution resolver 并移除归因专属状态")

    def _validate_attribution_targets(self) -> None:
        raw_targets = self.document.get("attribution_targets", [])
        if not isinstance(raw_targets, list):
            return
        engine_identity = self.document.get("attribution_engine")
        if raw_targets:
            required_identity_fields = {
                "name",
                "engine_api_version",
                "contract_schema_version",
                "registry_version",
                "core_version",
                "core_sha256",
            }
            if not isinstance(engine_identity, dict) or not required_identity_fields.issubset(engine_identity):
                self.add(
                    "ATTR-026",
                    "ERROR",
                    "$.attribution_engine",
                    "归因计划缺少完整的内嵌引擎 identity",
                    "使用内嵌 query_operator 重新编译计划",
                )
        elif engine_identity is not None:
            self.add(
                "ATTR-027",
                "ERROR",
                "$.attribution_engine",
                "无归因目标时不应记录或加载归因引擎",
                "移除归因引擎状态并重新编译",
            )
        for index, target in enumerate(raw_targets):
            path = f"$.attribution_targets[{index}]"
            if not isinstance(target, dict):
                self.add("ATTR-001", "ERROR", path, "归因目标必须是对象", "删除或改正该目标")
                continue
            target_id = target.get("target_id")
            if not isinstance(target_id, str) or not target_id:
                self.add("ATTR-002", "ERROR", f"{path}.target_id", "target_id 必须是非空字符串", "提供唯一目标 ID")
                continue
            if target_id in self.attribution_targets:
                self.add("ATTR-003", "ERROR", f"{path}.target_id", f"target_id {target_id!r} 重复", "为目标分配唯一 ID")
            self.attribution_targets[target_id] = target
            for field in ("metric", "metric_object", "scenario", "target_semantics", "periods", "view_id"):
                if field not in target:
                    self.add("ATTR-004", "ERROR", f"{path}.{field}", f"归因目标缺少 {field}", "补齐目标契约")
            scenario = target.get("scenario")
            metric = target.get("metric")
            if not isinstance(metric, (str, dict)) or not metric:
                self.add("ATTR-022", "ERROR", f"{path}.metric", "metric 必须是非空指标引用", "提供指标 ID 或指标引用对象")
            if target.get("metric_object") not in {"volume", "ratio"}:
                self.add("ATTR-023", "ERROR", f"{path}.metric_object", "metric_object 必须为 volume 或 ratio", "明确指标对象")
            if not isinstance(target.get("view_id"), str) or not target["view_id"]:
                self.add("ATTR-024", "ERROR", f"{path}.view_id", "view_id 必须是非空字符串", "提供独立视角 ID")
            if scenario not in ATTRIBUTION_SCENARIOS:
                self.add("ATTR-005", "ERROR", f"{path}.scenario", "scenario 必须为 metric_change 或 yoy_trend_change", "根据 Query 锁定归因现象")
            semantics = target.get("target_semantics")
            if semantics not in TARGET_SEMANTICS:
                self.add("ATTR-006", "ERROR", f"{path}.target_semantics", "target_semantics 非法", "使用已定义的目标语义标签")
            elif scenario == "metric_change" and semantics != "absolute_delta":
                self.add("ATTR-007", "ERROR", f"{path}.target_semantics", "metric_change 必须归因 analysis - comparison", "使用 absolute_delta")
            elif scenario == "yoy_trend_change" and semantics not in {"relative_yoy_trend", "point_yoy_trend"}:
                self.add("ATTR-007", "ERROR", f"{path}.target_semantics", "yoy_trend_change 必须声明同比趋势定义", "使用 relative_yoy_trend 或 point_yoy_trend")
            periods = target.get("periods")
            if not isinstance(periods, dict):
                self.add("ATTR-008", "ERROR", f"{path}.periods", "periods 必须是时期角色映射", "按归因场景补齐时期")
            elif scenario in ATTRIBUTION_SCENARIOS:
                for role in ATTRIBUTION_SCENARIOS[scenario]:
                    if not isinstance(periods.get(role), str) or not periods[role]:
                        self.add("ATTR-009", "ERROR", f"{path}.periods.{role}", "缺少必需时期角色", "补齐非空时期引用")

        task = self.document.get("analysis_task")
        contracts = task.get("operator_contracts", []) if isinstance(task, dict) else []
        if not isinstance(contracts, list):
            self.add("ATTR-010", "ERROR", "$.analysis_task.operator_contracts", "operator_contracts 必须是数组", "保存 query_operator 的完整响应")
            return
        for index, contract in enumerate(contracts):
            path = f"$.analysis_task.operator_contracts[{index}]"
            if not isinstance(contract, dict):
                self.add("ATTR-011", "ERROR", path, "operator_contract 必须是对象", "保存完整能力响应")
                continue
            contract_id = contract.get("query_id")
            if not isinstance(contract_id, str) or not contract_id:
                self.add("ATTR-012", "ERROR", f"{path}.query_id", "operator_contract 缺少 query_id", "使用对应 operator_query 的 query_id")
                continue
            if contract_id in self.operator_contracts:
                self.add("ATTR-013", "ERROR", f"{path}.query_id", f"operator_contract {contract_id!r} 重复", "每次能力查询只保留一个权威响应")
            self.operator_contracts[contract_id] = contract
            contract_identity = contract.get("engine_identity")
            if raw_targets and contract_identity != engine_identity:
                self.add(
                    "ATTR-028",
                    "ERROR",
                    f"{path}.engine_identity",
                    "算子契约与计划锁定的归因引擎 identity 不一致",
                    "使用同一内嵌运行时重新查询算子并编译计划",
                )

    def _artifact_path(self, artifact_id: str, metadata: dict[str, Any]) -> Path | None:
        raw_path = metadata.get("path")
        if self.base_dir is None:
            self.add("ART-001", "ERROR", f"$.artifacts.{artifact_id}.path", "校验引用式产物需要 manifest 所在目录", "通过 CLI 校验或提供 base_dir")
            return None
        if not isinstance(raw_path, str) or not raw_path or Path(raw_path).is_absolute():
            self.add("ART-002", "ERROR", f"$.artifacts.{artifact_id}.path", "artifact path 必须是非空相对路径", "使用 manifest 目录内的相对路径")
            return None
        path = (self.base_dir / raw_path).resolve()
        try:
            path.relative_to(self.base_dir)
        except ValueError:
            self.add("ART-003", "ERROR", f"$.artifacts.{artifact_id}.path", "artifact path 越出 manifest 目录", "将产物放在 manifest 同级子目录")
            return None
        return path

    def _validate_artifacts(self) -> None:
        storage = self.document.get("storage")
        if not isinstance(storage, dict) or storage.get("mode") != "reference":
            return
        if storage.get("schema_version") != REFERENCE_STORAGE_VERSION:
            self.add("ART-004", "ERROR", "$.storage.schema_version", "不支持的引用存储版本", "使用 execution manifest 2.0")
        artifacts = self.document.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            self.add("ART-005", "ERROR", "$.artifacts", "引用式执行结果缺少 artifacts", "重新执行引用式存储")
            return
        for artifact_id, metadata in artifacts.items():
            path_label = f"$.artifacts.{artifact_id}"
            if not isinstance(metadata, dict) or metadata.get("artifact_id") != artifact_id:
                self.add("ART-006", "ERROR", path_label, "artifact ID 与 manifest key 不一致", "重新生成 artifact manifest")
                continue
            path = self._artifact_path(str(artifact_id), metadata)
            if path is None:
                continue
            try:
                actual_bytes = path.stat().st_size
                digest = hashlib.sha256()
                with path.open("rb") as raw_stream:
                    for chunk in iter(lambda: raw_stream.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                self.add("ART-007", "ERROR", path_label, "引用的 artifact 不可读", "恢复文件或重新执行对应节点")
                continue
            expected_bytes = metadata.get("bytes")
            expected_hash = metadata.get("sha256")
            if expected_bytes != actual_bytes or expected_hash != digest.hexdigest():
                self.add("ART-008", "ERROR", path_label, "artifact 字节数或 SHA-256 不闭合", "恢复未损坏文件或重新执行")
                continue
            artifact_format = metadata.get("format")
            try:
                if artifact_format == "jsonl":
                    actual_records = 0
                    with path.open("r", encoding="utf-8") as stream:
                        for line in stream:
                            if line.strip():
                                json.loads(line, object_pairs_hook=reject_duplicate_keys)
                                actual_records += 1
                elif artifact_format == "json":
                    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
                    actual_records = len(value) if isinstance(value, (dict, list)) else 1
                else:
                    raise ValueError("unsupported format")
            except (json.JSONDecodeError, ValueError):
                self.add("ART-009", "ERROR", path_label, "artifact 格式或 JSON 内容非法", "按声明格式重新生成文件")
                continue
            if metadata.get("records") != actual_records:
                self.add("ART-010", "ERROR", path_label, "artifact 记录数与 manifest 不一致", "重新生成记录数摘要")

    def _iter_artifact_records(self, artifact_id: str) -> Iterator[tuple[int, Any]]:
        artifacts = self.document.get("artifacts") or {}
        metadata = artifacts.get(artifact_id) if isinstance(artifacts, dict) else None
        if not isinstance(metadata, dict) or metadata.get("format") != "jsonl":
            return
        path = self._artifact_path(artifact_id, metadata)
        if path is None:
            return
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if line.strip():
                        yield line_number - 1, json.loads(line, object_pairs_hook=reject_duplicate_keys)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return

    def _is_reference_storage(self) -> bool:
        storage = self.document.get("storage")
        return isinstance(storage, dict) and storage.get("mode") == "reference"

    def _load_json_artifact(self, artifact_id: str) -> Any:
        artifacts = self.document.get("artifacts") or {}
        metadata = artifacts.get(artifact_id) if isinstance(artifacts, dict) else None
        if not isinstance(metadata, dict) or metadata.get("format") != "json":
            return None
        path = self._artifact_path(artifact_id, metadata)
        if path is None:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    def _validate_nodes(self) -> None:
        raw_nodes = self.document.get("nodes", [])
        if not isinstance(raw_nodes, list):
            return
        self.nodes = [node for node in raw_nodes if isinstance(node, dict)]
        seen: set[str] = set()
        required = {
            "node_id",
            "type",
            "status",
            "criticality",
            "requirement_refs",
            "depends_on",
            "inputs",
            "outputs",
            "execution",
            "quality_gate",
            "failure_strategy",
        }
        for index, node in enumerate(raw_nodes):
            path = f"$.nodes[{index}]"
            if not isinstance(node, dict):
                self.add("NODE-001", "ERROR", path, "节点必须是对象", "删除或改正该节点")
                continue
            node_id = node.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                self.add("NODE-002", "ERROR", f"{path}.node_id", "node_id 必须是非空字符串", "提供唯一 node_id")
                continue
            if node_id in seen:
                self.add("NODE-003", "ERROR", f"{path}.node_id", f"node_id {node_id!r} 重复", "为节点分配唯一 ID", node_id=node_id)
            seen.add(node_id)
            self.node_map[node_id] = node
            for key in sorted(required - node.keys()):
                self.add("NODE-004", "ERROR", f"{path}.{key}", f"节点缺少字段 {key}", "补齐节点执行契约", node_id=node_id)
            if node.get("status") not in NODE_STATUSES:
                self.add("NODE-005", "ERROR", f"{path}.status", f"非法节点状态 {node.get('status')!r}", "使用约定的节点状态", node_id=node_id)
            if node.get("criticality") not in CRITICALITIES:
                self.add("NODE-006", "ERROR", f"{path}.criticality", "criticality 必须为 core、required 或 optional", "明确节点对最终目标的重要性", node_id=node_id)
            for key in ("requirement_refs", "depends_on", "outputs", "quality_gate"):
                if key in node and not isinstance(node[key], list):
                    self.add("NODE-007", "ERROR", f"{path}.{key}", f"{key} 必须是数组", "改为数组", node_id=node_id)
            if self.phase == "final" and node.get("status") not in TERMINAL_STATUSES:
                self.add("NODE-008", "ERROR", f"{path}.status", "最终校验时节点仍未到达终态", "完成节点或明确标记 failed/skipped/blocked", node_id=node_id)
            if self._is_attribution_node(node):
                self._validate_attribution_node(node, path)

    def _is_attribution_node(self, node: dict[str, Any]) -> bool:
        execution = node.get("execution")
        return (
            isinstance(execution, dict) and execution.get("handler") == "attribution"
        ) or str(node.get("type", "")).endswith("_attribution") or "target_ref" in node

    def _validate_attribution_node(self, node: dict[str, Any], path: str) -> None:
        node_id = node.get("node_id")
        target_ref = node.get("target_ref")
        if not isinstance(target_ref, str) or target_ref not in self.attribution_targets:
            self.add("ATTR-014", "ERROR", f"{path}.target_ref", "归因节点必须引用有效 attribution target", "提供 target_ref", node_id=node_id)
            return
        target = self.attribution_targets[target_ref]
        contract_ref = node.get("operator_contract_ref")
        contract = self.operator_contracts.get(contract_ref) if isinstance(contract_ref, str) else None
        if contract is None:
            self.add("ATTR-025", "ERROR", f"{path}.operator_contract_ref", "归因节点必须引用已保存的权威算子契约", "保存 query_operator 响应并提供有效引用", node_id=node_id)
        execution = node.get("execution") if isinstance(node.get("execution"), dict) else {}
        is_executable = execution.get("mode") == "lightweight_executor"
        supported_semantics = contract.get("supported_target_semantics") if isinstance(contract, dict) else None
        engine_identity = self.document.get("attribution_engine")
        engine_name = engine_identity.get("name") if isinstance(engine_identity, dict) else None
        capability_resolved = (
            isinstance(contract, dict)
            and contract.get("supported") is True
            and contract.get("contract_source") == engine_name
            and isinstance(supported_semantics, list)
            and bool(supported_semantics)
            and all(isinstance(item, str) and item for item in supported_semantics)
        )
        if not capability_resolved:
            if node.get("status") != "blocked" or node.get("reason_code") != "ATTRIBUTION_CAPABILITY_UNRESOLVED" or is_executable:
                self.add("ATTR-015", "ERROR", path, "归因能力契约缺失或未声明目标语义时必须阻断且不可执行", "设置 blocked、ATTRIBUTION_CAPABILITY_UNRESOLVED，并移除可执行模式", node_id=node_id)
            return
        if contract.get("scenario") != target.get("scenario"):
            self.add("ATTR-016", "ERROR", f"{path}.operator_contract_ref", "算子场景与归因目标场景不一致", "重新查询匹配目标场景的算子", node_id=node_id)
        required_semantics = target.get("target_semantics")
        mismatch = required_semantics not in supported_semantics
        if mismatch:
            if (
                node.get("status") != "blocked"
                or node.get("reason_code") != "ATTRIBUTION_TARGET_UNSUPPORTED"
                or node.get("required_target_semantics") != required_semantics
                or node.get("supported_target_semantics") != supported_semantics
                or is_executable
            ):
                self.add("ATTR-017", "ERROR", path, "目标语义超出算子能力时必须按契约阻断且不可执行", "记录目标/能力语义和 ATTRIBUTION_TARGET_UNSUPPORTED", node_id=node_id)
            return
        if node.get("status") == "blocked":
            self.add("ATTR-018", "ERROR", path, "目标语义与算子能力匹配，不应按能力原因阻断", "恢复正常计划状态", node_id=node_id)
        if is_executable and execution.get("operator") != contract.get("operator"):
            self.add("ATTR-019", "ERROR", f"{path}.execution.operator", "执行算子与权威能力契约不一致", "使用 operator_contract.operator", node_id=node_id)

    def _validate_executor_contract(self) -> None:
        runtime = self.document.get("execution_runtime")
        runtime_periods: dict[str, Any] = {}
        if runtime is not None:
            if not isinstance(runtime, dict):
                self.add("EXEC-001", "ERROR", "$.execution_runtime", "execution_runtime 必须是对象", "按轻量执行器契约提供运行时配置")
            else:
                if runtime.get("version") not in {None, "1.0"}:
                    self.add("EXEC-002", "ERROR", "$.execution_runtime.version", "不支持的轻量执行器契约版本", "使用 version=1.0")
                if "periods" in runtime and not isinstance(runtime["periods"], dict):
                    self.add("EXEC-003", "ERROR", "$.execution_runtime.periods", "periods 必须是时期角色到实际时期的对象", "改为 JSON 对象")
                elif isinstance(runtime.get("periods"), dict):
                    runtime_periods = runtime["periods"]
                dimension_fields = runtime.get("dimension_fields")
                if dimension_fields is not None and (
                    not isinstance(dimension_fields, list)
                    or not all(isinstance(item, str) and item for item in dimension_fields)
                ):
                    self.add("EXEC-013", "ERROR", "$.execution_runtime.dimension_fields", "dimension_fields 必须是非空字符串数组", "提供运行时维度字段引用")
                workers = runtime.get("max_workers")
                if workers is not None and (not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 32):
                    self.add("EXEC-004", "ERROR", "$.execution_runtime.max_workers", "max_workers 必须是 1 到 32 的整数", "设置有界本地并发数")
                tolerance = runtime.get("residual_tolerance")
                if tolerance is not None and (not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance < 0):
                    self.add("EXEC-005", "ERROR", "$.execution_runtime.residual_tolerance", "残差阈值必须是非负数", "设置非负数值阈值")

        allowed_handlers = {"fact_artifact", "derived", "attribution", "model_owned"}
        for index, node in enumerate(self.nodes):
            execution = node.get("execution")
            if not isinstance(execution, dict) or execution.get("mode") != "lightweight_executor":
                continue
            node_id = node.get("node_id")
            path = f"$.nodes[{index}].execution"
            handler = execution.get("handler")
            if handler not in allowed_handlers:
                self.add("EXEC-006", "ERROR", f"{path}.handler", f"不支持的执行器 handler: {handler!r}", "使用 fact_artifact、derived、attribution 或 model_owned", node_id=node_id)
                continue
            if handler == "derived":
                expression = execution.get("expression")
                expressions = execution.get("expressions")
                if not isinstance(expression, dict) and not isinstance(expressions, dict):
                    self.add("EXEC-007", "ERROR", path, "derived handler 缺少 expression 或 expressions", "提供安全表达式对象", node_id=node_id)
                if not isinstance(execution.get("unit"), str) or not execution.get("unit"):
                    self.add("EXEC-014", "ERROR", f"{path}.unit", "derived handler 缺少结果单位", "提供明确单位", node_id=node_id)
                expression_items = expressions.values() if isinstance(expressions, dict) else [expression]
                intermediate_expressions = execution.get("intermediate_expressions", {})
                if isinstance(expressions, dict) and not expressions:
                    self.add("EXEC-015", "ERROR", f"{path}.expressions", "expressions 不能为空", "提供至少一个派生表达式", node_id=node_id)
                if not isinstance(intermediate_expressions, dict):
                    self.add("EXEC-030", "ERROR", f"{path}.intermediate_expressions", "intermediate_expressions 必须是对象", "使用名称到安全表达式的映射", node_id=node_id)
                    intermediate_expressions = {}
                for expression_item in expression_items:
                    self._validate_expression(expression_item, path, node)
                for name, expression_item in intermediate_expressions.items():
                    self._validate_expression(expression_item, f"{path}.intermediate_expressions.{name}", node)
                materialize_as = execution.get("materialize_as")
                if materialize_as is not None:
                    if not isinstance(materialize_as, dict):
                        self.add("EXEC-038", "ERROR", f"{path}.materialize_as", "materialize_as 必须是对象", "提供中间事实目标契约", node_id=node_id)
                    else:
                        required_fields = [
                            "metric_ref", "metric", "period_role", "period", "unit", "rule_source"
                        ]
                        if compiler_uses_fact_slots(self.document):
                            required_fields.append("fact_slot_id")
                        for field in required_fields:
                            if not isinstance(materialize_as.get(field), str) or not materialize_as[field]:
                                self.add("EXEC-039", "ERROR", f"{path}.materialize_as.{field}", f"materialize_as 缺少 {field}", "补齐中间事实目标和规则来源", node_id=node_id)
                        target_slots = materialize_as.get("fact_slot_ids")
                        if compiler_uses_fact_slots(self.document) and not (
                            isinstance(target_slots, list)
                            and target_slots
                            and all(isinstance(item, str) and item for item in target_slots)
                            and len(target_slots) == len(set(target_slots))
                            and materialize_as.get("fact_slot_id") in target_slots
                        ):
                            self.add(
                                "EXEC-045", "ERROR", f"{path}.materialize_as.fact_slot_ids",
                                "中间事实必须声明完整且包含主 slot 的目标 slot 集合",
                                "重新编译 input adaptation 的目标绑定", node_id=node_id,
                            )
                        validation = materialize_as.get("validation")
                        allowed_validation = {
                            "facts_present", "unit_consistent", "metric_additive", "unit_scale_verified"
                        }
                        if (
                            not isinstance(validation, list)
                            or not validation
                            or any(item not in allowed_validation for item in validation)
                        ):
                            self.add("EXEC-040", "ERROR", f"{path}.materialize_as.validation", "输入适配校验项非法", "只使用受支持的输入适配校验项", node_id=node_id)
                        if isinstance(validation, list) and "unit_scale_verified" in validation:
                            conversion = materialize_as.get("unit_conversion")
                            valid_expected = isinstance(conversion, dict) and isinstance(
                                conversion.get("expected_input_units"), dict
                            ) and bool(conversion.get("expected_input_units"))
                            scale_factor = conversion.get("scale_factor") if isinstance(conversion, dict) else None
                            if (
                                not valid_expected
                                or isinstance(scale_factor, bool)
                                or not isinstance(scale_factor, (int, float))
                                or not math.isfinite(float(scale_factor))
                                or float(scale_factor) == 0
                                or conversion.get("target_unit") != materialize_as.get("unit")
                            ):
                                self.add(
                                    "EXEC-041", "ERROR", f"{path}.materialize_as.unit_conversion",
                                    "单位量级换算契约非法", "提供输入单位、目标单位和有限非零换算因子",
                                    node_id=node_id,
                                )
                        rollup = materialize_as.get("rollup")
                        if rollup is not None:
                            components = rollup.get("components") if isinstance(rollup, dict) else None
                            if not isinstance(rollup, dict) or rollup.get("calendar") != "iso8601":
                                self.add("ROLLUP-001", "ERROR", f"{path}.materialize_as.rollup", "周上卷必须使用 ISO 8601 日历", "声明 calendar=iso8601", node_id=node_id)
                            elif not isinstance(components, list) or not components:
                                self.add("ROLLUP-002", "ERROR", f"{path}.materialize_as.rollup.components", "上卷组件不能为空", "提供完整的来源期间列表", node_id=node_id)
                            else:
                                seen: set[str] = set()
                                target_period = materialize_as.get("period")
                                for component_index, component in enumerate(components):
                                    component_path = f"{path}.materialize_as.rollup.components[{component_index}]"
                                    period = component.get("period") if isinstance(component, dict) else None
                                    if not isinstance(period, str) or normalize_period(period) is None or period in seen:
                                        self.add("ROLLUP-003", "ERROR", component_path, "上卷组件期间非法或重复", "使用规范化且唯一的来源期间", node_id=node_id)
                                        continue
                                    seen.add(period)
                                    weight = component.get("weight")
                                    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(float(weight)) or not 0 < float(weight) <= 1:
                                        self.add("ROLLUP-004", "ERROR", f"{component_path}.weight", "上卷权重非法", "使用 overlap_days/7", node_id=node_id)
                                    days = component.get("overlap_days")
                                    if days is not None:
                                        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 7:
                                            self.add("ROLLUP-005", "ERROR", f"{component_path}.overlap_days", "交集天数非法", "使用 1 到 7 的整数", node_id=node_id)
                                        elif isinstance(target_period, str) and normalize_period(target_period) is not None:
                                            expected = overlap_days(period, target_period)
                                            if expected != days or abs(float(weight) - days / 7.0) > 1e-12:
                                                self.add("ROLLUP-006", "ERROR", component_path, "上卷覆盖天数与权重不一致", "重新按 ISO 周和目标区间计算", node_id=node_id)
            if handler == "attribution":
                if not isinstance(execution.get("operator"), str) or not execution.get("operator"):
                    self.add("EXEC-008", "ERROR", f"{path}.operator", "attribution handler 缺少显式算子", "引用计划阶段已解析的算子", node_id=node_id)
                if not isinstance(execution.get("payload"), dict) and not isinstance(execution.get("binding"), dict):
                    self.add("EXEC-009", "ERROR", path, "attribution handler 缺少 payload 或 binding", "提供声明式归因输入绑定", node_id=node_id)
                expansion = execution.get("expansion", {"mode": "none"})
                if not isinstance(expansion, dict) or expansion.get("mode", "none") not in {"none", "for_each_parent_group"}:
                    self.add("EXEC-010", "ERROR", f"{path}.expansion", "不支持的父分组展开配置", "使用 none 或 for_each_parent_group", node_id=node_id)
                elif expansion.get("mode") == "for_each_parent_group":
                    parent_dimensions = expansion.get("parent_dimensions")
                    if not isinstance(parent_dimensions, list) or not parent_dimensions or not all(isinstance(item, str) and item for item in parent_dimensions):
                        self.add("EXEC-011", "ERROR", f"{path}.expansion.parent_dimensions", "父分组展开需要非空维度引用数组", "提供动态父维度引用", node_id=node_id)
                    if isinstance(execution.get("payload"), dict):
                        self.add("EXEC-016", "ERROR", f"{path}.payload", "静态 payload 不能用于父分组展开", "改用动态 binding", node_id=node_id)
                    binding = execution.get("binding")
                    groups = binding.get("groups") if isinstance(binding, dict) else None
                    parent_selector = expansion.get("parent_selector")
                    group_selector = groups.get("selector") if isinstance(groups, dict) else None
                    if not isinstance(parent_selector, dict) and not isinstance(group_selector, dict):
                        self.add("EXEC-017", "ERROR", f"{path}.expansion.parent_selector", "父分组展开缺少父值发现选择器", "提供 parent_selector 或 groups.selector", node_id=node_id)
                source_key = "binding" if isinstance(execution.get("binding"), dict) else "payload"
                source = execution.get(source_key)
                if isinstance(source, dict):
                    if self.document.get("compiler"):
                        selectors: list[tuple[str, Any]] = []
                        metric_binding = source.get("metric")
                        if isinstance(metric_binding, dict):
                            selectors.append((f"{path}.binding.metric.selector", metric_binding.get("selector")))
                        for factor_index, factor in enumerate(source.get("factors", []) if isinstance(source.get("factors"), list) else []):
                            if isinstance(factor, dict) and "literal" not in factor:
                                if factor.get("kind") == "metric" or "selector" in factor:
                                    selectors.append((f"{path}.binding.factors[{factor_index}].selector", factor.get("selector")))
                                expressions = factor.get("expressions_by_period_role")
                                if isinstance(expressions, dict):
                                    for role, expression in expressions.items():
                                        self._validate_expression(
                                            expression,
                                            f"{path}.binding.factors[{factor_index}].expressions_by_period_role.{role}",
                                            node,
                                        )
                        groups = source.get("groups")
                        if isinstance(groups, dict):
                            selectors.append((f"{path}.binding.groups.selector", groups.get("selector")))
                        for selector_path, selector in selectors:
                            self._validate_compiled_selector(selector, selector_path, node)
                    scenario = source.get("scenario")
                    roles = ATTRIBUTION_SCENARIOS.get(scenario)
                    if roles is None:
                        self.add("EXEC-018", "ERROR", f"{path}.binding.scenario", "归因场景必须为 metric_change 或 yoy_trend_change", "使用算子契约中的场景", node_id=node_id)
                    periods = source.get("periods")
                    if not isinstance(periods, dict):
                        self.add("EXEC-019", "ERROR", f"{path}.binding.periods", "归因绑定缺少时期映射", "提供算子需要的时期角色", node_id=node_id)
                    elif roles is not None:
                        for role in roles:
                            if role not in periods:
                                self.add("EXEC-020", "ERROR", f"{path}.binding.periods.{role}", "归因绑定缺少必需时期角色", "补齐时期映射", node_id=node_id)
                            elif not isinstance(periods[role], str) or not periods[role]:
                                self.add("EXEC-021", "ERROR", f"{path}.binding.periods.{role}", "归因绑定时期必须是非空字符串", "补齐目标局部时期映射", node_id=node_id)
                        if all(role in periods for role in roles):
                            role_periods = [str(periods[role]) for role in roles]
                            if len(role_periods) != len(set(role_periods)):
                                self.add("EXEC-012", "ERROR", f"{path}.binding.periods", "同一归因算子的时期角色不能映射到同一个时期", "为归因角色提供不同物理时期", node_id=node_id)
                    has_factors = isinstance(source.get("factors"), list) and bool(source.get("factors"))
                    has_groups = isinstance(source.get("groups"), (dict, list)) and bool(source.get("groups"))
                    if not has_factors and not has_groups:
                        self.add("EXEC-022", "ERROR", path, "归因配置缺少 factors 或 groups", "提供声明式公式因子或分组绑定", node_id=node_id)
                    if has_factors and self.document.get("compiler"):
                        factors = source["factors"]
                        factor_ids = [
                            factor.get("factor_id")
                            for factor in factors
                            if isinstance(factor, dict)
                        ]
                        if (
                            len(factor_ids) != len(factors)
                            or any(not isinstance(item, str) or not item for item in factor_ids)
                            or len(set(factor_ids)) != len(factor_ids)
                        ):
                            self.add(
                                "EXEC-041", "ERROR", f"{path}.{source_key}.factors",
                                "公式归因因子必须有稳定且唯一的 factor_id",
                                "为每个因子生成稳定唯一 factor_id", node_id=node_id,
                            )
                        kinds = {
                            factor.get("kind")
                            for factor in factors
                            if isinstance(factor, dict)
                        }
                        if not kinds.issubset({"metric", "literal", "derived"}):
                            self.add(
                                "EXEC-042", "ERROR", f"{path}.{source_key}.factors",
                                "公式归因因子 kind 非法",
                                "只使用 metric、literal 或 derived", node_id=node_id,
                            )
                        factor_order = source.get("factor_order")
                        if factor_order != factor_ids:
                            self.add(
                                "EXEC-043", "ERROR", f"{path}.{source_key}.factor_order",
                                "factor_order 必须与 factors 顺序完整一致",
                                "按公式因子稳定顺序生成 factor_order", node_id=node_id,
                            )

                        def formula_refs(expression: Any) -> list[str]:
                            if not isinstance(expression, dict):
                                return []
                            if isinstance(expression.get("factor_ref"), str):
                                return [expression["factor_ref"]]
                            refs: list[str] = []
                            for arg in expression.get("args") or []:
                                refs.extend(formula_refs(arg))
                            return refs

                        refs = formula_refs(source.get("formula"))
                        if len(refs) != len(set(refs)) or set(refs) != set(factor_ids):
                            self.add(
                                "EXEC-044", "ERROR", f"{path}.{source_key}.formula",
                                "公式 AST 与归因因子集合不完整一致",
                                "让公式恰好引用每个 factor_id 一次", node_id=node_id,
                            )
                    if "sparse_policy" in source:
                        self._validate_sparse_policy(
                            source.get("sparse_policy"),
                            f"{path}.{source_key}.sparse_policy",
                            node,
                        )

    def _validate_sparse_policy(self, policy: Any, path: str, node: dict[str, Any]) -> None:
        node_id = node.get("node_id")

        def invalid(field: str, message: str) -> None:
            self.add(
                "EXEC-034",
                "ERROR",
                f"{path}{field}",
                message,
                "使用 merge_other_then_epsilon、配对自身率和合法的合并/上卷配置",
                node_id=node_id,
            )

        if not isinstance(policy, dict):
            invalid("", "sparse_policy 必须是对象")
            return
        if policy.get("strategy", "merge_other_then_epsilon") != "merge_other_then_epsilon":
            invalid(".strategy", "不支持的稀疏结构处理策略")
        if policy.get("reference_rate_policy", "paired_observed_self_rate") != "paired_observed_self_rate":
            invalid(".reference_rate_policy", "不支持的 epsilon 参考率策略")
        epsilon = policy.get("epsilon", 1e-9)
        if (
            isinstance(epsilon, bool)
            or not isinstance(epsilon, (int, float))
            or not math.isfinite(float(epsilon))
            or float(epsilon) <= 0
        ):
            invalid(".epsilon", "epsilon 必须是有限正数")
        merge_rules = policy.get("merge_rules", [])
        if not isinstance(merge_rules, list):
            invalid(".merge_rules", "merge_rules 必须是数组")
        else:
            for index, rule in enumerate(merge_rules):
                members = rule.get("members") if isinstance(rule, dict) else None
                if not isinstance(rule, dict) or not isinstance(members, list) or not members:
                    invalid(f".merge_rules[{index}]", "每条合并规则必须是含非空 members 的对象")
                    continue
                if not all(isinstance(member, (str, dict)) for member in members):
                    invalid(f".merge_rules[{index}].members", "合并成员必须是名称字符串或维度选择器对象")
        rollup_path = policy.get("rollup_path", [])
        if not isinstance(rollup_path, list):
            invalid(".rollup_path", "rollup_path 必须是数组")
        else:
            for index, level in enumerate(rollup_path):
                if not isinstance(level, list) or not level or not all(isinstance(item, str) and item for item in level):
                    invalid(f".rollup_path[{index}]", "每个上卷层级必须是非空字符串数组")
        parent_dimensions = policy.get("parent_dimensions", [])
        if not isinstance(parent_dimensions, list) or not all(isinstance(item, str) and item for item in parent_dimensions):
            invalid(".parent_dimensions", "parent_dimensions 必须是字符串数组")

    def _validate_expression(self, expression: Any, path: str, node: dict[str, Any]) -> None:
        node_id = node.get("node_id")
        if not isinstance(expression, dict):
            self.add("EXEC-023", "ERROR", path, "派生表达式必须是对象", "使用安全表达式 AST", node_id=node_id)
            return
        if "literal" in expression:
            value = expression["literal"]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                self.add("EXEC-024", "ERROR", path, "literal 必须是数值", "提供数值常量", node_id=node_id)
            return
        if "fact" in expression:
            if not isinstance(expression["fact"], dict) or not expression["fact"]:
                self.add("EXEC-025", "ERROR", path, "fact 必须提供非空选择器", "提供可唯一绑定的事实选择器", node_id=node_id)
            elif self.document.get("compiler"):
                self._validate_compiled_selector(expression["fact"], f"{path}.fact", node)
            return
        if "aggregate" in expression:
            aggregate = expression.get("aggregate")
            if not isinstance(aggregate, dict):
                self.add("EXEC-032", "ERROR", path, "aggregate 必须是对象", "提供集合聚合配置", node_id=node_id)
                return
            if not isinstance(aggregate.get("selector"), dict) or not aggregate.get("selector"):
                self.add("EXEC-033", "ERROR", f"{path}.aggregate.selector", "aggregate 缺少事实选择器", "提供可追溯事实选择器", node_id=node_id)
            if not isinstance(aggregate.get("dimension"), str) or not aggregate.get("dimension"):
                self.add("EXEC-034", "ERROR", f"{path}.aggregate.dimension", "aggregate 缺少聚合维度", "提供维度名称", node_id=node_id)
            domain_ref = aggregate.get("domain_ref")
            if not isinstance(domain_ref, str) or not domain_ref:
                self.add("EXEC-035", "ERROR", f"{path}.aggregate.domain_ref", "aggregate 缺少集合引用", "提供编译器生成的 domain_ref", node_id=node_id)
            elif self.phase == "final":
                domains = self.document.get("resolved_dimension_domains")
                if not isinstance(domains, dict) or domain_ref not in domains:
                    self.add("EXEC-036", "ERROR", f"{path}.aggregate.domain_ref", "集合引用未解析", "使用 Provider 返回的集合解析结果", node_id=node_id)
                elif aggregate.get("dimension") != domains[domain_ref].get("dimension"):
                    self.add("EXEC-037", "ERROR", f"{path}.aggregate.dimension", "聚合维度与集合定义不一致", "重新编译集合聚合", node_id=node_id)
            return
        if "result" in expression:
            result_ref = expression["result"]
            ref_node = result_ref.get("node_id") if isinstance(result_ref, dict) else None
            if not isinstance(ref_node, str) or not ref_node:
                self.add("EXEC-026", "ERROR", path, "result 必须引用 node_id", "提供结果节点引用", node_id=node_id)
            elif ref_node not in (node.get("depends_on") or []):
                self.add("EXEC-027", "ERROR", path, "result 引用未声明为节点依赖", "将结果节点加入 depends_on", node_id=node_id)
            return
        op = expression.get("op")
        args = expression.get("args")
        arity = {"add": (1, None), "subtract": (2, 2), "multiply": (1, None), "divide": (2, 2), "sum": (1, None), "negate": (1, 1)}
        if op not in arity or not isinstance(args, list):
            self.add("EXEC-028", "ERROR", path, f"不支持的派生操作 {op!r}", "使用白名单操作和参数数组", node_id=node_id)
            return
        minimum, maximum = arity[op]
        if len(args) < minimum or (maximum is not None and len(args) > maximum):
            self.add("EXEC-029", "ERROR", path, f"操作 {op!r} 的参数个数不合法", "按操作约束提供参数", node_id=node_id)
        for index, arg in enumerate(args):
            self._validate_expression(arg, f"{path}.args[{index}]", node)

    def _validate_compiled_selector(self, selector: Any, path: str, node: dict[str, Any]) -> None:
        node_id = node.get("node_id")
        if not isinstance(selector, dict):
            self.add("EXEC-031", "ERROR", path, "编译器生成的事实选择器必须是对象", "重新编译事实绑定", node_id=node_id)
            return
        expected_view = (node.get("inputs") or {}).get("view_id")
        if expected_view is not None and selector.get("view_id") != expected_view:
            self.add("EXEC-032", "ERROR", f"{path}.view_id", "事实选择器与节点视角不一致", "写入节点 view_id", node_id=node_id)
        if not isinstance(selector.get("dimensions"), dict) or selector.get("dimensions_exact") is not True:
            self.add("EXEC-033", "ERROR", path, "固定粒度选择器必须声明精确 dimensions", "提供 dimensions 对象并设置 dimensions_exact=true", node_id=node_id)
        elif any(isinstance(value, (list, dict)) for value in selector["dimensions"].values()):
            self.add("EXEC-038", "ERROR", f"{path}.dimensions", "精确事实选择器不能包含集合", "将集合放入 fact demand，并通过 group_dimensions 或 domain_ref 绑定", node_id=node_id)
        if compiler_uses_fact_slots(self.document):
            slot_id = selector.get("fact_slot_id")
            slot_ids = selector.get("fact_slot_ids")
            if not (isinstance(slot_id, str) and slot_id) and not (
                isinstance(slot_ids, list)
                and bool(slot_ids)
                and all(isinstance(item, str) and item for item in slot_ids)
                and len(slot_ids) == len(set(slot_ids))
            ):
                self.add(
                    "EXEC-045", "ERROR", path,
                    "新编译计划的事实选择器缺少 fact slot 范围",
                    "重新编译事实绑定，禁止退回全局语义选择器",
                    node_id=node_id,
                )

    def _validate_graph(self) -> None:
        graph: dict[str, list[str]] = {}
        for node_id, node in self.node_map.items():
            deps = node.get("depends_on", [])
            if not isinstance(deps, list):
                continue
            graph[node_id] = []
            for dep in deps:
                if dep not in self.node_map:
                    self.add("DAG-001", "ERROR", f"$.nodes[{node_id}].depends_on", f"依赖节点 {dep!r} 不存在", "补充依赖节点或修正引用", node_id=node_id)
                else:
                    graph[node_id].append(dep)
                    if node.get("status") == "success" and self.node_map[dep].get("status") != "success":
                        self.add(
                            "DAG-002",
                            "ERROR",
                            f"$.nodes[{node_id}].status",
                            f"节点成功，但依赖 {dep!r} 的状态为 {self.node_map[dep].get('status')!r}",
                            "修正节点状态或重新执行依赖链",
                            node_id=node_id,
                        )

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                self.add("DAG-003", "ERROR", "$.nodes", f"检测到包含 {node_id!r} 的循环依赖", "移除循环依赖", node_id=node_id)
                return
            if node_id in visited:
                return
            visiting.add(node_id)
            for dep in graph.get(node_id, []):
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in graph:
            visit(node_id)

    def _validate_execution_summary(self) -> None:
        summary = self.document.get("execution_summary")
        if self.phase != "final" or not isinstance(summary, dict):
            return
        expected = {
            "succeeded_nodes": {node_id for node_id, node in self.node_map.items() if node.get("status") == "success"},
            "failed_nodes": {node_id for node_id, node in self.node_map.items() if node.get("status") == "failed"},
            "partial_nodes": {node_id for node_id, node in self.node_map.items() if node.get("status") == "partial_success"},
            "skipped_nodes": {node_id for node_id, node in self.node_map.items() if node.get("status") == "skipped"},
            "blocked_nodes": {node_id for node_id, node in self.node_map.items() if node.get("status") == "blocked"},
        }
        for key, expected_ids in expected.items():
            actual = summary.get(key, [])
            if not isinstance(actual, list):
                self.add("STATUS-003", "ERROR", f"$.execution_summary.{key}", f"{key} 必须是数组", "按节点状态生成数组")
                continue
            actual_ids = set(actual)
            if len(actual_ids) != len(actual) or actual_ids != expected_ids:
                self.add(
                    "STATUS-004",
                    "ERROR",
                    f"$.execution_summary.{key}",
                    f"{key} 与节点终态不一致，期望 {sorted(expected_ids)!r}，实际 {sorted(actual_ids)!r}",
                    "重新生成 execution_summary",
                )

    def _request_signature(self, request: dict[str, Any]) -> str:
        fields = {
            key: request.get(key)
            for key in (
                "source_binding",
                "periods",
                "dimensions",
                "metrics",
                "scope",
                "filters",
                "fact_layout",
            )
            if key in request
        }
        payload = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _validate_requests(self) -> None:
        requests = self.document.get("fetch_requests", [])
        if not isinstance(requests, list):
            self.add("FETCH-001", "ERROR", "$.fetch_requests", "fetch_requests 必须是数组", "改为数组")
            return
        task_selectors = (self.document.get("analysis_task") or {}).get(
            "selector_dimensions", {}
        )
        if not isinstance(task_selectors, dict):
            self.add(
                "FETCH-024", "ERROR", "$.analysis_task.selector_dimensions",
                "任务事实选择器必须是对象", "使用规范化的维度到取值映射",
            )
            task_selectors = {}
        ids: set[str] = set()
        signatures: dict[str, str] = {}
        by_id: dict[str, dict[str, Any]] = {}
        for index, request in enumerate(requests):
            path = f"$.fetch_requests[{index}]"
            if not isinstance(request, dict):
                self.add("FETCH-002", "ERROR", path, "取数请求必须是对象", "删除或改正该请求")
                continue
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                self.add("FETCH-003", "ERROR", f"{path}.request_id", "request_id 必须是非空字符串", "提供唯一 request_id")
                continue
            if request_id in ids:
                self.add("FETCH-004", "ERROR", f"{path}.request_id", f"request_id {request_id!r} 重复", "使用新的 request_id", request_id=request_id)
            ids.add(request_id)
            by_id[request_id] = request
            binding = request.get("source_binding")
            if binding is not None:
                required_binding = {
                    "schema_version", "provider_id", "source_id", "config_hash",
                    "revision", "schema_hash",
                }
                if not isinstance(binding, dict) or binding.get("schema_version") != "source_binding/1.0" or any(
                    binding.get(field) in (None, "") for field in required_binding
                ):
                    self.add("FETCH-005", "ERROR", f"{path}.source_binding", "source_binding 不完整或版本不受支持", "由 DataGateway.resolve 注入完整绑定", request_id=request_id)
            if not isinstance(request.get("fact_slots"), list) or not request.get("fact_slots"):
                self.add("FETCH-006", "ERROR", f"{path}.fact_slots", "竞品 Provider 请求缺少事实槽位", "补充结构化 fact_slots", request_id=request_id)
            else:
                for slot_index, slot in enumerate(request["fact_slots"]):
                    if not isinstance(slot, dict):
                        continue
                    selectors = slot.get("selector_dimensions") or {}
                    dimension_refs = slot.get("dimension_refs") or []
                    if not isinstance(selectors, dict) or not isinstance(dimension_refs, list):
                        continue
                    missing_dimensions = sorted(
                        str(dimension)
                        for dimension in selectors
                        if str(dimension) not in dimension_refs
                    )
                    if missing_dimensions:
                        self.add(
                            "FETCH-023",
                            "ERROR",
                            f"{path}.fact_slots[{slot_index}].dimension_refs",
                            f"事实选择器维度未进入物理粒度：{missing_dimensions}",
                            "将 selector_dimensions 中的维度加入 dimension_refs",
                            request_id=request_id,
                        )
                    missing_task_selectors = {
                        dimension: value
                        for dimension, value in task_selectors.items()
                        if dimension not in selectors or selectors.get(dimension) != value
                    }
                    if missing_task_selectors:
                        self.add(
                            "FETCH-025", "ERROR",
                            f"{path}.fact_slots[{slot_index}].selector_dimensions",
                            f"事实槽位未继承任务选择器：{missing_task_selectors}",
                            "通过统一 selector context 生成事实槽位",
                            request_id=request_id,
                        )
            if isinstance(request.get("fact_demands"), list):
                demands = request.get("fact_demands")
                if not isinstance(demands, list) or not demands:
                    self.add("FETCH-016", "ERROR", f"{path}.fact_demands", "v2 Provider 请求缺少物理事实需求", "由编译器生成 fact_demands", request_id=request_id)
                else:
                    demand_ids = [item.get("fact_demand_id") for item in demands if isinstance(item, dict)]
                    if len(demand_ids) != len(demands) or any(not item for item in demand_ids) or len(set(demand_ids)) != len(demand_ids):
                        self.add("FETCH-017", "ERROR", f"{path}.fact_demands", "fact_demand_id 缺失或重复", "为每个物理需求生成稳定唯一 ID", request_id=request_id)
                    slot_ids = {
                        item.get("fact_slot_id") for item in request.get("fact_slots", [])
                        if isinstance(item, dict) and item.get("fact_slot_id")
                    }
                    binding_ids: set[str] = set()
                    for demand_index, demand in enumerate(demands):
                        if not isinstance(demand, dict):
                            continue
                        bindings = demand.get("consumer_bindings")
                        if not isinstance(bindings, list) or not bindings:
                            self.add("FETCH-020", "ERROR", f"{path}.fact_demands[{demand_index}].consumer_bindings", "物理需求没有消费者绑定", "至少绑定一个事实槽位", request_id=request_id)
                            continue
                        for binding_index, binding in enumerate(bindings):
                            binding_path = f"{path}.fact_demands[{demand_index}].consumer_bindings[{binding_index}]"
                            if not isinstance(binding, dict) or binding.get("fact_slot_id") not in slot_ids:
                                self.add("FETCH-021", "ERROR", binding_path, "consumer binding 引用了不存在的事实槽位", "重新从 fact_slots 生成 binding", request_id=request_id)
                                continue
                            binding_id = binding.get("binding_id")
                            if not isinstance(binding_id, str) or not binding_id or binding_id in binding_ids:
                                self.add("FETCH-022", "ERROR", f"{binding_path}.binding_id", "binding_id 缺失或重复", "生成稳定唯一 binding_id", request_id=request_id)
                            else:
                                binding_ids.add(binding_id)
            layout = request.get("fact_layout")
            if layout is not None:
                if not isinstance(layout, dict) or layout.get("type") not in {"long", "wide_by_grain"}:
                    self.add("FETCH-013", "ERROR", f"{path}.fact_layout", "fact_layout 类型非法", "使用 long 或 wide_by_grain", request_id=request_id)
                elif layout.get("type") == "wide_by_grain":
                    row_keys = layout.get("row_keys")
                    mappings = layout.get("fact_mappings")
                    if not isinstance(row_keys, list) or not row_keys or not all(isinstance(item, str) and item for item in row_keys):
                        self.add("FETCH-014", "ERROR", f"{path}.fact_layout.row_keys", "宽表必须声明非空行键", "声明时期和维度粒度字段", request_id=request_id)
                    if not isinstance(mappings, dict) or not mappings:
                        self.add("FETCH-015", "ERROR", f"{path}.fact_layout.fact_mappings", "宽表必须声明事实映射", "将每个逻辑事实映射到值列或分子分母列", request_id=request_id)
            signature = self._request_signature(request)
            previous = signatures.get(signature)
            if previous and request.get("purpose") != "structured_refetch" and request.get("retry_of") is None:
                self.add("FETCH-007", "ERROR", path, f"请求事实范围与 {previous!r} 重复", "复用成功响应或缩小为明确缺口", request_id=request_id)
            signatures[signature] = request_id
            retry_of = request.get("retry_of")
            if retry_of and retry_of not in by_id:
                self.add("FETCH-008", "ERROR", f"{path}.retry_of", f"retry_of {retry_of!r} 不存在或顺序错误", "引用已存在的原请求", request_id=request_id)

        results = self.document.get("fetch_results", [])
        if not isinstance(results, list):
            return
        attempts: dict[str, int] = {}
        attempt_ids: set[str] = set()
        attempt_rows: dict[str, list[dict[str, Any]]] = {}
        successful: set[str] = set()
        require_fetch_metrics = (
            self.phase == "final"
            and (self.document.get("executor") or {}).get("version") in REFERENCE_EXECUTOR_VERSIONS
        )
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                continue
            result_path = f"$.fetch_results[{index}]"
            request_id = result.get("request_id")
            if isinstance(request_id, str):
                attempts[request_id] = attempts.get(request_id, 0) + 1
                attempt_rows.setdefault(request_id, []).append(result)
                if result.get("status") in {"success", "partial_success"}:
                    successful.add(request_id)
            attempt_id = result.get("attempt_id")
            if attempt_id is not None:
                if not isinstance(attempt_id, str) or not attempt_id or attempt_id in attempt_ids:
                    self.add("FETCH-018", "ERROR", f"{result_path}.attempt_id", "attempt_id 缺失、非法或重复", "为每次真实调用生成唯一 attempt_id", request_id=request_id)
                else:
                    attempt_ids.add(attempt_id)
            if require_fetch_metrics:
                for field in ("raw_bytes", "duration_ms"):
                    value = result.get(field)
                    try:
                        valid = not isinstance(value, bool) and float(value) >= 0 and math.isfinite(float(value))
                    except (TypeError, ValueError):
                        valid = False
                    if not valid:
                        self.add("FETCH-011", "ERROR", f"{result_path}.{field}", f"取数结果缺少有效 {field}", "使用 run_fetch.py 采集取数性能", request_id=request_id)
                for field in ("started_at", "ended_at"):
                    if not isinstance(result.get(field), str) or not result[field]:
                        self.add("FETCH-012", "ERROR", f"{result_path}.{field}", f"取数结果缺少 {field}", "使用 run_fetch.py 记录起止时间", request_id=request_id)
                try:
                    started = datetime.fromisoformat(str(result["started_at"]).replace("Z", "+00:00"))
                    ended = datetime.fromisoformat(str(result["ended_at"]).replace("Z", "+00:00"))
                    valid_interval = started.tzinfo is not None and ended.tzinfo is not None and ended >= started
                except (KeyError, TypeError, ValueError):
                    valid_interval = False
                if not valid_interval:
                    self.add("FETCH-016", "ERROR", result_path, "取数起止时间不是有效的带时区区间", "使用 run_fetch.py 记录起止时间", request_id=request_id)
        for request_id, rows in attempt_rows.items():
            if len(rows) <= 1:
                continue
            succeeded_at: int | None = None
            for position, row in enumerate(rows):
                if not row.get("attempt_id"):
                    self.add("FETCH-009", "ERROR", "$.fetch_results", f"同一 request_id {request_id!r} 的多次调用缺少 attempt_id", "为每次调用记录唯一 attempt_id 和重试关系", request_id=request_id)
                if position > 0 and not row.get("retry_of") and not row.get("refresh_of"):
                    self.add("FETCH-019", "ERROR", "$.fetch_results", f"request_id {request_id!r} 的后续 attempt 缺少 lineage", "使用 retry_of 或显式 refresh_of", request_id=request_id)
                if succeeded_at is not None and not row.get("refresh_of"):
                    self.add("FETCH-010", "ERROR", "$.fetch_results", f"request_id {request_id!r} 成功后仍自动重试", "复用成功响应；显式刷新使用 refresh_of", request_id=request_id)
                if row.get("status") in {"success", "partial_success"}:
                    succeeded_at = position
        if require_fetch_metrics and self.document.get("status") == "success":
            for request_id in sorted(set(by_id) - set(attempts)):
                self.add("FETCH-017", "ERROR", "$.fetch_results", f"成功结果缺少取数请求 {request_id!r} 的执行记录", "使用 run_fetch.py 执行并追加结果", request_id=request_id)
        for request_id, request in by_id.items():
            retry_of = request.get("retry_of")
            if retry_of in successful:
                self.add("FETCH-010", "ERROR", f"$.fetch_requests[{request_id}]", "业务响应成功后仍创建了重试请求", "复用已成功响应", request_id=request_id)

    def _validate_facts(self) -> None:
        reference_mode = self._is_reference_storage()
        if reference_mode:
            facts: Any = self._iter_artifact_records("normalized_facts")
        else:
            inline_facts = self.document.get("normalized_facts", [])
            if not isinstance(inline_facts, list):
                return
            facts = enumerate(inline_facts)
        request_ids = {
            request.get("request_id")
            for request in self.document.get("fetch_requests", [])
            if isinstance(request, dict)
        }
        compiled_fact_requirements = [
            slot
            for slot in (self.document.get("analysis_task") or {}).get("fact_requirements", [])
            if isinstance(slot, dict)
        ]
        compiled_view_ids = {slot.get("view_id") for slot in compiled_fact_requirements}
        executor_version = str((self.document.get("executor") or {}).get("version") or "")
        require_projected_identity = executor_version in {"1.9.0", "1.10.0"}
        logical_fact_ids: set[str] = set()
        fact_count = 0
        missing_count = 0
        for index, fact in facts:
            fact_count += 1
            path = (
                f"artifact://normalized_facts#{index}"
                if reference_mode
                else f"$.normalized_facts[{index}]"
            )
            if not isinstance(fact, dict):
                self.add("FACT-001", "ERROR", path, "标准化事实必须是对象", "删除或改正该事实")
                continue
            fact_id = fact.get("fact_id")
            if not isinstance(fact_id, str) or not fact_id:
                self.add("FACT-017", "ERROR", f"{path}.fact_id", "事实缺少稳定逻辑 fact_id", "由事实投影层生成逻辑事实身份")
            elif fact_id in logical_fact_ids:
                self.add("FACT-018", "ERROR", f"{path}.fact_id", "逻辑 fact_id 重复", "按物理事实与消费 binding 生成唯一逻辑 ID")
            else:
                logical_fact_ids.add(fact_id)
            is_intermediate = fact.get("intermediate") is True or (
                isinstance(fact.get("source_ref"), dict)
                and fact["source_ref"].get("type") == "input_adaptation"
            )
            if require_projected_identity and not is_intermediate:
                physical_fact_id = fact.get("physical_fact_id")
                binding_id = fact.get("binding_id")
                if not isinstance(physical_fact_id, str) or not physical_fact_id:
                    self.add("FACT-019", "ERROR", f"{path}.physical_fact_id", "源事实缺少物理事实 ID", "保留 Provider 生成的物理 fact_id")
                if not isinstance(binding_id, str) or not binding_id:
                    self.add("FACT-020", "ERROR", f"{path}.binding_id", "源事实缺少消费 binding ID", "保留 Fact Contract 投影使用的 binding_id")
                if (
                    isinstance(fact_id, str) and fact_id
                    and isinstance(physical_fact_id, str) and physical_fact_id
                    and isinstance(binding_id, str) and binding_id
                    and fact_id != stable_id("fact", [physical_fact_id, binding_id])
                ):
                    self.add("FACT-021", "ERROR", f"{path}.fact_id", "逻辑事实 ID 与物理事实和 binding 不闭合", "按稳定身份公式重新投影事实")
            missing = fact.get("missing")
            missing_count += missing is True
            value = fact.get("value")
            if not isinstance(missing, bool):
                self.add("FACT-006", "ERROR", f"{path}.missing", "missing 必须是布尔值", "由事实标准化器生成明确缺失状态")
            if "raw_missing" not in fact:
                self.add("FACT-007", "ERROR", f"{path}.raw_missing", "标准化事实缺少 raw_missing", "保留数据源原始缺失标记")
            reason = fact.get("normalization_reason")
            if reason not in NORMALIZATION_REASONS:
                self.add("FACT-008", "ERROR", f"{path}.normalization_reason", "缺少或不支持的标准化原因", "使用事实标准化器生成原因")
            if missing is True and value is not None:
                self.add("FACT-002", "ERROR", f"{path}.value", "missing=true 时 value 必须为 null", "保留缺失状态，不补0或估算")
            if missing is False and value is None:
                self.add("FACT-003", "ERROR", f"{path}.value", "missing=false 时 value 不能为 null", "修正缺失标记或补充真实值")
            if missing is False and value is not None:
                try:
                    parsed_value = float(value)
                    value_is_numeric = not isinstance(value, bool) and math.isfinite(parsed_value)
                except (TypeError, ValueError):
                    value_is_numeric = False
                if not value_is_numeric:
                    self.add("FACT-009", "ERROR", f"{path}.value", "missing=false 时 value 必须是有限数值", "修正数值或标记缺失")
            denominator = fact.get("denominator")
            try:
                denominator_is_zero = denominator is not None and float(denominator) == 0
            except (TypeError, ValueError):
                denominator_is_zero = False
            if denominator_is_zero and missing is not True:
                self.add("FACT-010", "ERROR", f"{path}.missing", "denominator=0 时必须标记 missing=true", "按零分母规则重新标准化")
            value_derived = fact.get("value_derived_from_components") is True
            if (reason == "value_derived_from_components") != value_derived:
                self.add("FACT-011", "ERROR", f"{path}.value_derived_from_components", "派生原因与派生标记不一致", "由标准化器统一生成派生状态")
            if value_derived:
                numerator = fact.get("numerator")
                try:
                    parsed_numerator = float(numerator)
                    parsed_denominator = float(denominator)
                    components_valid = (
                        not isinstance(numerator, bool)
                        and not isinstance(denominator, bool)
                        and math.isfinite(parsed_numerator)
                        and math.isfinite(parsed_denominator)
                        and parsed_denominator != 0
                    )
                except (TypeError, ValueError):
                    components_valid = False
                if not components_valid or missing is not False or value is None:
                    self.add("FACT-012", "ERROR", path, "组成事实不足以支持派生 value", "补齐有效分子和非零分母，或标记缺失")
                else:
                    expected_value = parsed_numerator / parsed_denominator
                    try:
                        derived_value = float(value)
                        value_closes = math.isfinite(derived_value) and math.isclose(
                            derived_value, expected_value, rel_tol=1e-12, abs_tol=1e-12
                        )
                    except (TypeError, ValueError):
                        value_closes = False
                    if not value_closes:
                        self.add("FACT-013", "ERROR", f"{path}.value", "value 与 numerator/denominator 不闭合", "重新执行事实标准化")
            source_id = fact.get("source_request_id", fact.get("request_id"))
            if source_id not in request_ids:
                self.add("FACT-004", "ERROR", path, f"事实来源请求 {source_id!r} 不存在", "补充可追溯的 source_request_id")
            for key in ("metric", "period", "unit", "definition", "missing"):
                if key not in fact:
                    self.add("FACT-005", "ERROR", f"{path}.{key}", f"事实缺少字段 {key}", "按标准化事实契约补齐")
            if compiled_fact_requirements and fact.get("view_id") not in compiled_view_ids:
                self.add("FACT-016", "ERROR", f"{path}.view_id", "事实缺少有效编译视角", "返回事实槽位对应的 view_id")
        if reference_mode:
            summary = self.document.get("normalized_fact_summary")
            expected = summary.get("records") if isinstance(summary, dict) else None
            if expected != fact_count:
                self.add(
                    "FACT-014",
                    "ERROR",
                    "$.normalized_fact_summary.records",
                    f"事实摘要记录数 {expected!r} 与流式读取记录数 {fact_count} 不一致",
                    "重新生成引用式执行结果",
                )
            expected_missing = summary.get("missing_records") if isinstance(summary, dict) else None
            if expected_missing != missing_count:
                self.add(
                    "FACT-015",
                    "ERROR",
                    "$.normalized_fact_summary.missing_records",
                    f"缺失事实摘要 {expected_missing!r} 与流式读取结果 {missing_count} 不一致",
                    "重新生成引用式执行结果",
                )

    def _validate_performance_metrics(self) -> None:
        if (
            self.phase != "final"
            or (self.document.get("executor") or {}).get("version") not in REFERENCE_EXECUTOR_VERSIONS
        ):
            return
        metrics = self.document.get("performance_metrics")
        if not isinstance(metrics, dict):
            self.add("PERF-001", "ERROR", "$.performance_metrics", "新执行器结果缺少性能指标", "重新执行 execution_runner.py")
            return
        for field in ("physical_rows", "logical_facts"):
            value = metrics.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                self.add("PERF-002", "ERROR", f"$.performance_metrics.{field}", f"{field} 必须是非负整数", "由执行器自动采集")
        physical = metrics.get("physical_rows")
        logical = metrics.get("logical_facts")
        if isinstance(physical, int) and isinstance(logical, int) and logical < physical:
            self.add("PERF-003", "ERROR", "$.performance_metrics.logical_facts", "logical_facts 不能小于 physical_rows", "检查宽表事实映射")
        for field in ("parse_ms", "normalize_ms"):
            value = metrics.get(field)
            try:
                valid = not isinstance(value, bool) and float(value) >= 0 and math.isfinite(float(value))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                self.add("PERF-004", "ERROR", f"$.performance_metrics.{field}", f"{field} 必须是非负有限数值", "由执行器自动采集")
        raw_bytes = metrics.get("raw_bytes")
        if raw_bytes is not None and (not isinstance(raw_bytes, int) or isinstance(raw_bytes, bool) or raw_bytes < 0):
            self.add("PERF-005", "ERROR", "$.performance_metrics.raw_bytes", "raw_bytes 必须是非负整数或 null", "由事实加载器读取文件字节数")
        fetch_results = [item for item in self.document.get("fetch_results", []) if isinstance(item, dict)]
        total_fetch_ms = metrics.get("total_fetch_ms")
        if fetch_results:
            try:
                valid_total = not isinstance(total_fetch_ms, bool) and float(total_fetch_ms) >= 0 and math.isfinite(float(total_fetch_ms))
            except (TypeError, ValueError):
                valid_total = False
            if not valid_total or metrics.get("fetch_timing_complete") is not True:
                self.add("PERF-006", "ERROR", "$.performance_metrics.total_fetch_ms", "存在取数记录但总取数耗时不完整", "补齐每次取数起止时间后重新执行")

    def _validate_calculation_results(self) -> None:
        node_results = self.document.get("node_results", [])
        if isinstance(node_results, list):
            for index, result in enumerate(node_results):
                if not isinstance(result, dict) or result.get("status") not in {"success", "partial_success"}:
                    continue
                node_id = result.get("node_id")
                if node_id in self.node_map and self.node_map[node_id].get("status") == "blocked":
                    self.add("ATTR-020", "ERROR", f"$.node_results[{index}]", "blocked 归因节点不能包含成功结果", "移除结果并保持节点阻断", node_id=node_id)
        if self._is_reference_storage():
            self._validate_reference_results()
            return
        for collection, result_type in (("derived_results", "派生"), ("attribution_results", "归因")):
            results = self.document.get(collection, [])
            if not isinstance(results, list):
                continue
            for index, result in enumerate(results):
                if not isinstance(result, dict) or (
                    result.get("status") != "success"
                    and not (result_type == "归因" and result.get("status") == "partial_success")
                ):
                    continue
                path = f"$.{collection}[{index}]"
                node_id = result.get("node_id")
                if node_id in self.node_map and self.node_map[node_id].get("status") == "blocked":
                    self.add("ATTR-020", "ERROR", path, "blocked 归因节点不能包含成功结果", "移除结果并保持节点阻断", node_id=node_id)
                if result_type == "派生":
                    for key in ("input_refs", "formula", "unit", "value"):
                        if key not in result:
                            self.add("CALC-001", "ERROR", f"{path}.{key}", f"成功的派生结果缺少 {key}", "补充可复现计算信息")
                else:
                    payload = result.get("result", result)
                    if payload.get("ok") is not True:
                        self.add("CALC-002", "ERROR", path, "归因结果标记有效，但引擎结果 ok 不为 true", "修正状态或重新执行归因")
                    for key in ("summary", "rows", "warnings", "boundary_cases"):
                        if key not in payload:
                            self.add("CALC-003", "ERROR", f"{path}.{key}", f"有效归因结果缺少 {key}", "保留完整归因引擎输出")

    def _validate_conclusions(self) -> None:
        conclusions = self.document.get("conclusions")
        if not isinstance(conclusions, list):
            return
        blocked = {node_id for node_id, node in self.node_map.items() if node.get("status") == "blocked"}
        for index, conclusion in enumerate(conclusions):
            if not isinstance(conclusion, dict) or conclusion.get("status") != "success":
                continue
            refs = conclusion.get("result_refs", [])
            if isinstance(refs, list):
                invalid = sorted({ref for ref in refs if ref in blocked})
                if invalid:
                    self.add("ATTR-021", "ERROR", f"$.conclusions[{index}].result_refs", f"成功结论引用 blocked 节点 {invalid!r}", "移除被阻断结果并披露未完成范围")

    def _validate_attribution_payload(self, payload: Any, path: str) -> None:
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            self.add("CALC-002", "ERROR", path, "归因结果标记 success，但引擎结果 ok 不为 true", "修正状态或重新执行归因")
            return
        for key in ("summary", "rows", "warnings", "boundary_cases"):
            if key not in payload:
                self.add("CALC-003", "ERROR", f"{path}.{key}", f"成功的归因结果缺少 {key}", "保留完整归因引擎输出")
        ranking = payload.get("ranking")
        if ranking is not None:
            self._validate_attribution_ranking(ranking, f"{path}.ranking")

    def _validate_attribution_ranking(self, ranking: Any, path: str) -> None:
        if not isinstance(ranking, dict):
            self.add("ATTR-RANK-001", "WARNING", path, "ranking 不是对象；不影响完整归因结果", "忽略排序视图并保留 rows")
            return
        filter_value = ranking.get("filter")
        order = ranking.get("order")
        top_k = ranking.get("top_k")
        rows = ranking.get("rows")
        if filter_value not in {"positive", "negative", "all"}:
            self.add("ATTR-RANK-002", "WARNING", f"{path}.filter", "ranking.filter 非法", "使用 positive、negative 或 all")
        if order not in {"asc", "desc", "abs_asc", "abs_desc"}:
            self.add("ATTR-RANK-003", "WARNING", f"{path}.order", "ranking.order 非法", "使用贡献率或绝对值升降序")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            self.add("ATTR-RANK-004", "WARNING", f"{path}.top_k", "ranking.top_k 必须为正整数", "忽略排序视图或使用正整数")
        if not isinstance(rows, list):
            self.add("ATTR-RANK-005", "WARNING", f"{path}.rows", "ranking.rows 不是数组", "忽略排序视图并使用完整 rows")
            return

        values: list[float] = []
        for index, row in enumerate(rows):
            row_path = f"{path}.rows[{index}]"
            rate = row.get("contribution_rate") if isinstance(row, dict) else None
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not math.isfinite(float(rate)):
                self.add("ATTR-RANK-006", "WARNING", row_path, "排序行缺少有限 contribution_rate", "从排序视图移除该行")
                continue
            value = float(rate)
            values.append(value)
            if filter_value == "positive" and value <= 0:
                self.add("ATTR-RANK-007", "WARNING", row_path, "正向贡献 TopN 混入非正贡献", "仅保留 contribution_rate > 0")
            if filter_value == "negative" and value >= 0:
                self.add("ATTR-RANK-008", "WARNING", row_path, "负向贡献 TopN 混入非负贡献", "仅保留 contribution_rate < 0")
            if isinstance(row, dict) and row.get("rank") != index + 1:
                self.add("ATTR-RANK-009", "WARNING", f"{row_path}.rank", "rank 不是从 1 开始的连续序号", "重新生成排序视图")

        comparable = [abs(value) for value in values] if order in {"abs_asc", "abs_desc"} else values
        descending = order in {"desc", "abs_desc"}
        if any(
            (left < right if descending else left > right)
            for left, right in zip(comparable, comparable[1:])
        ):
            self.add("ATTR-RANK-010", "WARNING", f"{path}.rows", "ranking.rows 与声明顺序不一致", "按声明的贡献率口径重新排序")

    def _validate_reference_results(self) -> None:
        summaries = self.document.get("node_results")
        artifacts = self.document.get("artifacts")
        if not isinstance(summaries, list) or not isinstance(artifacts, dict):
            self.add("REF-001", "ERROR", "$.node_results", "引用式结果缺少节点摘要或 artifact 清单", "重新生成引用式执行结果")
            return

        # The catalog keeps only indexable metadata; result payloads are validated and discarded line by line.
        catalog: dict[tuple[str, int], dict[str, Any]] = {}
        seen_artifacts: set[str] = set()
        derived_count = 0
        attribution_count = 0
        for summary_index, summary in enumerate(summaries):
            summary_path = f"$.node_results[{summary_index}]"
            if not isinstance(summary, dict):
                self.add("REF-002", "ERROR", summary_path, "节点摘要必须是对象", "重新生成节点摘要")
                continue
            ref = summary.get("result_ref")
            artifact_id = ref.get("artifact_id") if isinstance(ref, dict) else None
            line = ref.get("line") if isinstance(ref, dict) else None
            if not isinstance(artifact_id, str) or not isinstance(line, int) or isinstance(line, bool) or line != 0:
                self.add("REF-003", "ERROR", f"{summary_path}.result_ref", "节点摘要必须引用节点 artifact 第 0 行", "修正节点结果引用")
                continue
            metadata = artifacts.get(artifact_id)
            if not isinstance(metadata, dict) or metadata.get("format") != "jsonl":
                self.add("REF-004", "ERROR", f"{summary_path}.result_ref", "节点摘要引用的 JSONL artifact 不存在", "恢复 artifact 或重新执行节点")
                continue
            if artifact_id in seen_artifacts:
                self.add("REF-005", "ERROR", f"{summary_path}.result_ref", "多个节点摘要引用同一节点 artifact", "为每个节点生成独立 artifact")
                continue
            seen_artifacts.add(artifact_id)
            node_id = summary.get("node_id")
            handler = summary.get("handler")
            child_indexes: list[int] = []
            record_count = 0
            node_data: dict[str, Any] | None = None
            for record_line, record in self._iter_artifact_records(artifact_id):
                record_count += 1
                record_path = f"artifact://{artifact_id}#{record_line}"
                if not isinstance(record, dict):
                    self.add("REF-006", "ERROR", record_path, "节点 artifact 记录必须是对象", "重新生成节点 artifact")
                    continue
                record_type = record.get("record_type")
                data = record.get("data")
                if record_line == 0:
                    if record_type != "node" or not isinstance(data, dict):
                        self.add("REF-007", "ERROR", record_path, "节点 artifact 第 0 行必须是 node 记录", "重新生成节点 artifact")
                        continue
                    node_data = data
                    for field in ("node_id", "handler", "status", "input_hash"):
                        if data.get(field) != summary.get(field):
                            self.add("REF-008", "ERROR", record_path, f"节点记录的 {field} 与 manifest 摘要不一致", "重新生成 manifest")
                    catalog[(artifact_id, record_line)] = {
                        "record_type": "node",
                        "node_id": data.get("node_id"),
                        "status": data.get("status"),
                        "input_hash": data.get("input_hash"),
                    }
                    if handler == "derived":
                        derived_count += 1
                        if data.get("status") == "success" and "result" not in data:
                            self.add("CALC-001", "ERROR", record_path, "成功的派生节点缺少 value", "重新执行派生节点")
                    elif handler == "attribution":
                        value = data.get("result")
                        has_children = isinstance(value, dict) and isinstance(value.get("children_count"), int)
                        if not has_children:
                            attribution_count += 1
                            if data.get("status") == "success":
                                payload = value.get("result") if isinstance(value, dict) else None
                                self._validate_attribution_payload(payload, f"{record_path}.data.result")
                else:
                    if record_type != "child" or not isinstance(data, dict):
                        self.add("REF-009", "ERROR", record_path, "节点 artifact 后续行必须是 child 记录", "重新生成 fanout 节点 artifact")
                        continue
                    if record.get("node_id") != node_id or not isinstance(record.get("child_index"), int):
                        self.add("REF-010", "ERROR", record_path, "child 记录的 node_id 或 child_index 非法", "重新生成 fanout 节点 artifact")
                    child_index = record.get("child_index")
                    child_indexes.append(child_index)
                    catalog[(artifact_id, record_line)] = {
                        "record_type": "child",
                        "node_id": record.get("node_id"),
                        "child_index": child_index,
                        "parent_dimensions": data.get("parent") or {},
                        "status": data.get("status"),
                        "input_hash": data.get("input_hash"),
                    }
                    attribution_count += 1
                    if data.get("status") == "success":
                        self._validate_attribution_payload(data.get("result"), f"{record_path}.data.result")
            if record_count == 0:
                self.add("REF-011", "ERROR", f"$.artifacts.{artifact_id}", "节点 artifact 为空", "重新执行对应节点")
                continue
            if node_data is not None:
                value = node_data.get("result")
                expected_children = value.get("children_count") if isinstance(value, dict) else None
                if child_indexes and handler != "attribution":
                    self.add("REF-012", "ERROR", f"$.artifacts.{artifact_id}", "非归因节点不能包含 fanout child", "重新生成节点 artifact")
                if child_indexes != list(range(len(child_indexes))) or expected_children not in {None, len(child_indexes)}:
                    self.add("REF-013", "ERROR", f"$.artifacts.{artifact_id}", "fanout child 行号或数量不连续", "重新生成节点 artifact")

        declared_node_artifacts = {
            artifact_id for artifact_id in artifacts if isinstance(artifact_id, str) and artifact_id.startswith("node_result:")
        }
        if declared_node_artifacts != seen_artifacts:
            self.add("REF-014", "ERROR", "$.artifacts", "节点 artifact 与 node_results 摘要不是一一对应", "移除孤立产物或补齐节点摘要")
        collections = self.document.get("result_collections")
        expected_counts = {
            "node_results": len(summaries),
            "derived_results": derived_count,
            "attribution_results": attribution_count,
        }
        if not isinstance(collections, dict):
            self.add("REF-015", "ERROR", "$.result_collections", "引用式结果缺少集合计数", "重新生成结果集合摘要")
        else:
            for collection, actual in expected_counts.items():
                declared = collections.get(collection)
                records = declared.get("records") if isinstance(declared, dict) else None
                if records != actual:
                    self.add("REF-016", "ERROR", f"$.result_collections.{collection}.records", f"声明记录数 {records!r} 与实际 {actual} 不一致", "重新生成集合计数")

        index_ref = self.document.get("result_index")
        index_artifact_id = index_ref.get("artifact_id") if isinstance(index_ref, dict) else None
        index = self._load_json_artifact(index_artifact_id) if isinstance(index_artifact_id, str) else None
        if not isinstance(index, dict):
            self.add("REF-017", "ERROR", "$.result_index", "结果索引引用不可读或不是对象", "恢复 result-index.json 或重新执行")
            return
        indexed_refs: set[tuple[str, int]] = set()
        for key, entry in index.items():
            entry_path = f"artifact://{index_artifact_id}/{key}"
            result_ref = entry.get("result_ref") if isinstance(entry, dict) else None
            artifact_id = result_ref.get("artifact_id") if isinstance(result_ref, dict) else None
            line = result_ref.get("line") if isinstance(result_ref, dict) else None
            if not isinstance(artifact_id, str) or not isinstance(line, int) or isinstance(line, bool) or line < 0:
                self.add("REF-018", "ERROR", entry_path, "结果索引引用格式非法", "重建结果索引")
                continue
            reference = (artifact_id, line)
            target = catalog.get(reference)
            if target is None:
                self.add("REF-018", "ERROR", entry_path, "结果索引指向不存在的 artifact 行", "重建结果索引")
                continue
            if reference in indexed_refs:
                self.add("REF-019", "ERROR", entry_path, "多个结果 key 指向同一 artifact 行", "为每个节点或父分组保留唯一 key")
            indexed_refs.add(reference)
            expected_key = target.get("node_id")
            if target.get("record_type") == "child":
                parent = target.get("parent_dimensions") or {}
                expected_key = f"{target.get('node_id')}::{json.dumps(parent, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
                if entry.get("node_id") != target.get("node_id") or entry.get("parent_dimensions") != parent:
                    self.add("REF-020", "ERROR", entry_path, "索引父节点或父维度与 child 记录不一致", "重建结果索引")
            if key != expected_key:
                self.add("REF-021", "ERROR", entry_path, "结果索引 key 与目标记录不一致", "按节点 ID 和父维度重建 key")
            for field in ("status", "input_hash"):
                if entry.get(field) != target.get(field):
                    self.add("REF-022", "ERROR", entry_path, f"索引 {field} 与目标记录不一致", "重建结果索引")
        if indexed_refs != set(catalog):
            self.add("REF-023", "ERROR", "$.result_index", "结果索引未完整覆盖节点和 fanout child 记录", "重建完整结果索引")

    def _validate_dimension_decisions(self) -> None:
        task = self.document.get("analysis_task", {})
        dimensions = task.get("dimensions", []) if isinstance(task, dict) else []
        assumptions = {
            item.get("id")
            for item in task.get("assumptions", [])
            if isinstance(item, dict) and item.get("id")
        } if isinstance(task, dict) else set()
        clarifications = self.document.get("clarifications", [])
        clarification_ids = {
            item.get("id")
            for item in clarifications
            if isinstance(item, dict) and item.get("id")
        } if isinstance(clarifications, list) else set()
        if not isinstance(dimensions, list):
            return
        for index, dimension in enumerate(dimensions):
            if not isinstance(dimension, dict) or dimension.get("explicit_level") is not False:
                continue
            path = f"$.analysis_task.dimensions[{index}]"
            candidates = dimension.get("candidates", [])
            equivalent = dimension.get("candidates_semantically_equivalent") is True
            clarification_ref = dimension.get("clarification_ref")
            if isinstance(candidates, list) and len(candidates) > 1 and not equivalent and clarification_ref not in clarification_ids:
                self.add("DIM-001", "ERROR", path, "未指定层级且存在多个非等价候选，但没有有效 clarification", "创建阻断确认项或提供等价性证据")
            selected_fields = dimension.get("selected_fields", [])
            basis = dimension.get("selection_basis")
            if isinstance(selected_fields, list) and len(selected_fields) > 1 and basis not in {"user_confirmed", "resolver_unique"}:
                assumption_ref = dimension.get("assumption_ref")
                if assumption_ref not in assumptions:
                    self.add("DIM-002", "ERROR", path, "采用复合维度映射但没有可追溯 assumption", "披露选定映射、来源和影响范围")

    def _has_unresolved_blocking_clarification(self) -> bool:
        clarifications = self.document.get("clarifications", [])
        if not isinstance(clarifications, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("severity") == "blocking"
            and item.get("status", "unresolved") != "resolved"
            for item in clarifications
        )

    def _compute_status(self) -> str | None:
        if self.phase == "plan":
            if any(issue.severity == "ERROR" for issue in self.issues):
                return "blocked"
            if any(node.get("criticality") == "core" and node.get("status") == "blocked" for node in self.nodes):
                return "blocked"
            if self._has_unresolved_blocking_clarification():
                return "ready_for_confirmation"
            if any(node.get("status") == "waiting_resolution" for node in self.nodes):
                return "ready_for_resolution"
            return "ready_for_fetch"

        if self._has_unresolved_blocking_clarification():
            return "waiting_confirmation"
        core_failed = any(
            node.get("criticality") == "core" and node.get("status") in {"failed", "skipped", "blocked"}
            for node in self.nodes
        )
        if core_failed:
            return "blocked"
        required_incomplete = any(
            node.get("criticality") == "required" and node.get("status") != "success"
            for node in self.nodes
        )
        if required_incomplete:
            return "partial_success"
        if any(node.get("status") == "partial_success" for node in self.nodes):
            return "partial_success"
        optional_incomplete = any(
            node.get("criticality") == "optional" and node.get("status") in {"failed", "skipped", "partial_success", "blocked"}
            for node in self.nodes
        )
        if optional_incomplete:
            self.add(
                "STATUS-005",
                "WARNING",
                "$.nodes",
                "optional 节点未成功，但不影响核心结论状态",
                "在最终 warnings 中披露该节点和影响范围",
            )
        if any(node.get("status") not in TERMINAL_STATUSES for node in self.nodes):
            return "blocked"
        return "success"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="scene-analysis JSON document")
    parser.add_argument("--phase", required=True, choices=("plan", "final"))
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    return parser.parse_args()


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        f"valid: {report['valid']}",
        f"phase: {report['phase']}",
        f"status: {report['declared_status']} -> {report['computed_status']}",
        f"issues: {summary['errors']} errors, {summary['warnings']} warnings",
    ]
    for issue in report["issues"]:
        lines.append(f"[{issue['severity']}] {issue['rule_id']} {issue['path']}: {issue['message']}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        document = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("顶层 JSON 必须是对象")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"无法读取校验输入: {exc}", file=sys.stderr)
        return 3

    report = Validator(document, args.phase, base_dir=args.input.parent).validate()
    output = json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else render_text(report)
    print(output)
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report["summary"]["errors"]:
        return 2
    if report["summary"]["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
