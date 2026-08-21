# 输出契约与模板

本文件定义 `analysis_ir/1.0`、编译计划和最终 manifest 的最小稳定结构。取数执行细节见 [execution-contract.md](execution-contract.md)。

## 分析 IR

模型只提交语义，不手写事实坐标或执行节点：

```json
{
  "ir_version": "analysis_ir/1.0",
  "analysis_task": {
    "query": "用户原始问题",
    "analysis_goal": "回答目标",
    "execution_mode": "direct_query",
    "metrics": [{
      "metric_id": "payment_gmv",
      "name": "支付GMV",
      "metric_object": "volume",
      "unit": "待元信息解析",
      "definition": "待元信息解析"
    }],
    "periods": {"analysis": "2026-05"},
    "scope": "TOP6平台",
    "filters": [],
    "assumptions": []
  },
  "views": [{"view_id": "platform_view", "dimension_tree_ref": "platform_tree"}],
  "dimension_trees": [{"tree_id": "platform_tree", "levels": [{"level_id": "platform", "dimension_ref": "平台"}]}],
  "input_adaptations": [],
  "fact_observations": [],
  "metric_compositions": [],
  "derived_requirements": [],
  "custom_calculations": [],
  "attribution_targets": [],
  "output_requirements": [],
  "clarifications": []
}
```

需求集合并列：`fact_observations` 表达用户要求的事实型指标，runner 再决定源表直接事实或注册组合；`derived_requirements` 是注册/唯一推理派生，`custom_calculations` 是用户明确公式，`attribution_targets` 是原因或贡献量化。标准时间粒度适配和注册组合输入由准备阶段生成，不要求模型手写。只有实际需要的目标时期角色才由模型进入 `analysis_task.periods`，自动展开的子周期使用内部角色。

每个非空 `output_requirement` 必须提供 `source_requirement_refs`，指向承接该输出的事实、组合、派生或归因需求。

## 计划

`compile_plan.py` 输出包含：

```json
{
  "execution_mode": "analysis_orchestration",
  "analysis_task": {
    "metrics": [],
    "periods": {},
    "fact_requirements": [],
    "input_adaptations": [],
    "metric_compositions": [],
    "derived_requirements": [],
    "custom_calculations": [],
    "attribution_requirements": []
  },
  "execution_runtime": {"version": "1.0", "periods": {}, "dimension_fields": []},
  "nodes": [],
  "fetch_requests": [{
    "request_id": "fetch_unified_1",
    "fact_slots": [],
    "fact_demands": [],
    "fact_layout": {"type": "long"}
  }],
  "requirement_compilation": [],
  "clarifications": [],
  "status": "ready_for_fetch"
}
```

编译器请求保持 provider-neutral。跨 task 合并后，runner 根据本次 `DataGateway.resolve()` 结果注入 `source_binding/1.0`；模型和编译器不选择物理 Provider。

每个节点必须含 `criticality`、`requirement_refs`、`depends_on`、`execution` 和质量闸门。每个 IR 需求必须有唯一 `requirement_compilation` 记录，状态只能是 `compiled`、`blocked`、`waiting_confirmation` 或 `skipped`。`attribution_targets=[]` 时计划不得包含归因算子查询、归因契约或归因专属事实。

## 归因目标（可选）

归因目标只在用户明确要求原因、贡献、拉动或拖累量化时生成：

```json
{
  "target_id": "platform_contribution",
  "metric": "支付GMV",
  "metric_object": "volume",
  "scenario": "metric_change",
  "target_semantics": "absolute_delta",
  "periods": {"analysis": "2026-05", "comparison": "2026-04"},
  "view_id": "platform_view",
  "decomposition": "dimension",
  "dimension": "平台",
  "ranking": {"metric": "contribution_rate", "order": "desc", "top_k": 6}
}
```

比例结构归因可增加 `parent_dimensions`、`group_dimensions` 和 `sparse_policy`；每个父节点独立闭合，稀疏项并入唯一“其他/未覆盖”。方向元信息只影响业务解释，不改变数学排序。

公式归因的编译目标和 execution binding 额外保留 `formula`、`formula_shape`、`formula_fingerprint`、`factor_order`，每个因子保留稳定 `factor_id`、`kind` 和角色。AST 因子集合、`factor_order` 与 binding 因子必须完整一致；零贡献因子不删除。

## 最终 manifest

小结果可内联，较大结果使用 artifact 引用：

```json
{
  "schema_version": "execution_manifest/2.0",
  "status": "success",
  "declared_status": "success",
  "computed_status": "success",
  "nodes": [],
  "execution_summary": {
    "succeeded_nodes": [],
    "failed_nodes": [],
    "partial_nodes": [],
    "skipped_nodes": [],
    "blocked_nodes": []
  },
  "result_collections": {
    "facts": {"artifact": "facts.jsonl", "schema": "normalized_facts/1.0"},
    "derived": {"artifact": "derived.jsonl"}
  },
  "performance_metrics": {
    "raw_bytes": 0,
    "physical_rows": 0,
    "logical_facts": 0,
    "parse_ms": 0,
    "normalize_ms": 0,
    "total_fetch_ms": 0
  },
  "validation_reports": {"plan": {}, "final": {}}
}
```

最终答案优先引用成功节点和通过质量闸门的事实；必须说明指标定义、时间粒度、范围（例如 TOP6 分母域）、缺失、覆盖和 freshness。最终组织阶段还要对照原始 Query 检查遗漏：成功 facts 足以支持唯一、低风险计算时，模型可以补足并披露口径；失败或阻断节点本身不得被改写成执行成功。

归因残差超阈值属于 `partial_success`，不是空失败：结果集合继续保留引擎 rows、summary、残差、warning 和 boundary data。结论节点、任务答案和 bundle 顶层状态只能保持或降低该状态，不能包装为 `success`。

统一 runner 的顶层 `answer-payload.json` 是正常分析唯一读取入口，只保留业务事实、派生值、归因摘要与明细、口径和质量状态。公式 AST、输入选择器、内部哈希、引擎身份和路由保留在 `tasks/<task>/` 诊断产物中。自动粒度适配的源周期事实不重复进入顶层视图，物化后的目标周期结果保留。

## 状态

- 计划：`ready_for_resolution`、`ready_for_confirmation`、`ready_for_fetch`、`blocked`。
- 最终：`success`、`partial_success`、`waiting_confirmation`、`blocked`。

存在会改变结果的指标、维度、周期、分母或公式歧义时，优先由 Provider 元信息匹配；只有多个候选或置信度不足才向用户澄清，并列出候选、证据和受影响的事实槽位。
