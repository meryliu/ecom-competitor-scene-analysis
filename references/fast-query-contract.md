# Fast Admission 与执行档位契约

本文件定义低风险场景的准入、最小证明义务和自动升级，供维护或诊断编译器档位选择、fast 性能和对应校验时读取。正常分析请求不预读本文件；统一按 [analysis-request-contract.md](analysis-request-contract.md) 生成 IR，并由编译器在执行中确定档位。

## 目录

1. 执行档位
2. 最小 IR
3. 确定性准入
4. 单命令执行
5. 响应规范化边界
6. 最小证明义务
7. 自动升级
8. 输出与性能

## 执行档位

- `fast_fact`：少量事实、固定时期和固定视角，不含计算节点。
- `fast_derived`：在 `fast_fact` 基础上包含注册派生、唯一推理派生或用户明确给出的简单安全表达式。
- `standard`：存在歧义、超预算、结果依赖链、阻断节点或需要完整编排。
- `orchestrated`：存在归因目标、自适应二次取数或动态父子展开。

不要根据模型写入的 `analysis_task.execution_mode` 直接裁决。编译器固定调用 `scripts/fast_query_admission.py`，依据实际事实槽位、计算表达式、维度层级、确认项和目标生成：

```json
{
  "execution_profile": "fast_fact",
  "fast_query_admission": {
    "schema_version": "fast_query_admission/1.0",
    "eligible": true,
    "execution_profile": "fast_fact",
    "validation_profile": "minimal",
    "features": {},
    "limits": {},
    "reasons": [],
    "fallback_triggers": []
  }
}
```

## 最小 IR

候选 fast query 只生成 `analysis_ir/1.0` 必需语义，不手写完整计划：

```json
{
  "ir_version": "analysis_ir/1.0",
  "analysis_task": {
    "query": "用户原始 Query",
    "analysis_goal": "返回用户要求的事实和简单计算",
    "execution_mode": "direct_query",
    "metrics": [
      {
        "metric_id": "metric_ref",
        "name": "业务指标",
        "metric_object": "volume",
        "unit": "待取数解析",
        "definition": "待取数解析"
      }
    ],
    "periods": {"analysis": "period_a"},
    "scope": "用户指定范围或 Provider 元信息确认的默认范围",
    "filters": [],
    "assumptions": []
  },
  "views": [{"view_id": "view_1"}],
  "dimension_trees": [],
  "fact_observations": [],
  "derived_requirements": [],
  "custom_calculations": [],
  "attribution_targets": [],
  "output_requirements": [],
  "clarifications": []
}
```

注册派生使用 `definition_status=registered`。唯一推理派生使用 `definition_status=inferred`，并提供安全 `definition.expression`、`definition.unit`、非空 `inference_basis`；存在候选定义时填充 `alternative_candidates` 并升级，不要强行进入 fast path。用户明确公式使用 `custom_calculations`，只要表达式有界且无结果链，也可进入 `fast_derived`。

## 确定性准入

默认预算：

```json
{
  "metrics": 3,
  "views": 3,
  "dimension_levels": 3,
  "max_dimension_depth": 2,
  "fact_slots": 16,
  "calculation_nodes": 4,
  "expression_depth": 4,
  "result_rows": 1000
}
```

需要调整时只在 IR `runtime.fast_query_limits` 中覆盖正整数。预算影响执行档位，不改变业务口径。

以下情况固定拒绝 fast path：

- 存在归因目标或 `runtime.adaptive_fetch=true`。
- 存在 blocking、capability 或 operator_input clarification。
- 编译后存在 blocked 节点。
- 推理派生缺少唯一依据或存在替代候选。
- 表达式引用另一个计算结果，形成结果依赖链。
- 任一预算超限。

## 单命令执行

候选 fast query 直接执行：

```bash
python3 scripts/run_analysis.py \
  --input <analysis-ir.json> \
  --work-dir <run-directory>
```

该入口确定性完成：编译、计划校验、合并物理事实需求、共享索引取数、按 bindings 投影逻辑事实、执行简单派生、收口展示节点、最终校验和生成 `answer-payload.json`。`run_fast_query.py` 保留相同参数作为兼容委托入口。

生产执行不得使用 `--response-file`；该参数只用于离线回放和测试。生产取数通过本 Skill 的 Data Gateway 边界调用当前 Feishu 实现，直接生成标准 facts；fast path 不感知物理 Provider。

退出码：

- `0`：fast query 成功，直接读取 `answer-payload.json` 组织用户答案。
- `2`：输入、计划、Provider、执行或最终校验失败；读取 `run-state.json` 判断失败阶段和可恢复 checkpoint。

## 响应规范化边界

生产 Provider 只返回 `scene_facts/2.0`，不运行自然语言、Markdown、列式数组或字段别名 adapter。`facts` 是唯一物理事实，`bindings` 保存 task、槽位、时期角色和视角；执行前投影属于契约转换，不是重新解析业务结果。v1 仅用于单任务历史 artifact 兼容。

## 最小证明义务

Fast path 不省略确定性正确性检查，只省略无关能力推理和 Agent 往返。固定检查：

1. 响应是 `scene_facts/2.0`，`fact_id`、`binding_id` 唯一且引用闭合。
2. 每个物理事实唯一；同一身份值冲突时阻断。每个槽位至少有一条绑定事实。
3. 时期、时期角色、视角和维度粒度与计划一致。
4. 非缺失值为有限数值；缺失值不补零。
5. 同一事实槽位的单位和定义唯一。
6. 请求范围明确要求全量时，响应不得声明 TopN、partial 或 `coverage_rate < 1`。
7. 派生继续由轻量执行器检查事实唯一性、安全 AST、单位和非零分母。
8. 编译计划和最终 manifest 仍运行本地确定性校验器；不要为减少毫秒级校验而牺牲追溯性。

## 失败与恢复

以下问题停止对应 task 或整个流程，并在 `run-state.json` 保留已完成阶段：

- 响应不可机器解析。
- 事实槽位未绑定或没有可用值。
- 时期、视角或粒度不匹配。
- 单位或定义冲突。
- 数值或缺失状态不受支持。
- 派生绑定、分母或计算失败。
- 返回行数超过预算。
- 全量请求只得到 TopN 或部分覆盖。
- 需要额外取数。

facts checkpoint 已成功且 input/request/artifact hash 与 revision 元数据一致时，`--resume auto` 从本地投影和执行阶段恢复，不调用 Provider。`--fresh` 才允许显式绕过。失败和成功 fetch attempt 均只追加，恢复不新增伪 attempt。

## 输出与性能

成功产物：

- `run-state.json`、`fetch-request.json` 与 `facts.json`
- `tasks/<task_id>/compiled-plan.json` 与 `plan-validation.json`
- `tasks/<task_id>/logical-facts.json`
- `tasks/<task_id>/execution-manifest.json` 与 `execution-events.jsonl`
- `tasks/<task_id>/final-validation.json` 与 `answer-payload.json`
- `answer-payload.json`

`answer-payload.json` 只保留用户回答所需的视角行、派生结果、质量摘要、范围和 assumptions。直接基于它输出业务结果，不再重新读取完整 manifest 或手工改写节点状态。

记录 `workflow_duration_ms`、`post_fetch_to_answer_ms`、`fast_query_runtime.pre_final_validation_ms` 和 `fast_query_runtime.post_fetch_pre_final_validation_ms`。性能目标为：排除外部取数后，fast query 编排耗时低于 10 秒；取数完成到 `answer-payload.json` 生成低于 5 秒。Fallback 也必须记录已发生阶段的墙钟耗时，便于区分外部取数、确定性本地执行和 Agent 接管成本。
