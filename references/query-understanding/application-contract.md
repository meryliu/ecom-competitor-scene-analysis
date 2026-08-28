# Query Policy 应用契约

Query Policy 是标准 IR 生成前的可旁路业务增强层。它只补充业务默认、受控展开和必要澄清，不解析物理指标、维度、坐标或执行能力，也不替代现有候选引擎与业务参数预检。

## 运行边界

1. 每个用户显式独立问题先形成一个不可变 `raw_query` task；规则展开留在同一 task 内，不把展开项改造成新的用户问题。
2. 调用 `scripts/select_query_policy.py` 只取得与当前 Query 相关的有界规则包。`no_match`、`fallback_raw`、命令非零、超时或输出不可解析时，禁止再次尝试 Policy，直接以原始 Query 按 [../analysis-request-contract.md](../analysis-request-contract.md) 生成 IR。
3. `selected` 时，在临时语义帧中应用规则。`analysis_task.query` 始终保留原始 Query；默认和展开写入 `analysis_goal`、指标、范围、requirements 与可读业务 assumptions。
4. 写出 `query_policy_decision/1.0` 和候选 IR 后调用 `scripts/validate_query_policy_application.py`。带 `ir_effect_contract` 的已应用 action 必须用 `produced_refs` 绑定其实际生成的 IR 目标。校验器在同一事务内只对这些目标执行契约允许的协议字段规范化，并在 `commit` 或 `commit_pending_confirmation` 时写出 `committed_ir_path`；后续统一 runner 只消费该文件，不再消费校验前候选 IR。`commit_clarification` 直接向用户询问规则正常产生的业务问题。
5. `fallback_raw` 表示增强故障，不是业务状态。按 task 丢弃临时语义帧、默认、展开、assumptions、clarifications 和 action 记录，再从原始 Query 生成一次基础 IR。不得因此输出 `blocked`、`waiting_confirmation` 或非零分析状态。

Policy 降级不放宽主流程校验。回退后的原始 Query 仍可被现有 Query 理解、业务参数预检、候选解析或数据质量闸门正常澄清或阻断。

## 应用事务

规则可以重复评估，但每个动作对同一语义作用域最多提交一次。应用唯一键为：

```text
(task_id, rule_id, action_id, target_scope_fingerprint)
```

- `preserve`：先固定用户明确的指标、平台、时期、视角、拆解、口径和输出，后续默认不得覆盖。
- `set_default`：只补空字段；已有相同值视为完成，已有不同显式值时不应用。
- `expand_sub_queries`：按指标、范围、时期和输出类型形成语义指纹，已存在的 requirement 不追加。
- `rewrite`：规范化结果与现有语义相同时不提交。
- `clarify`：同一缺失字段和语义作用域只产生一个问题。

选择器先按原始 Query 高召回相关领域规则，再展开显式依赖。`depends_on` 仅确保依赖规则进入规则包，不代表无条件执行；依赖规则仍须独立满足 applicability。对已加载规则做有界不动点判断，最多使用 packet 中的 `max_application_rounds`。没有新语义变化时立即结束。

## 决策格式

```json
{
  "schema_version": "query_policy_decision/1.0",
  "policy_version": "query-policy/1.0.0",
  "raw_query": "用户原始 Query",
  "status": "applied|needs_clarification",
  "application_rounds": 1,
  "applied_actions": [
    {
      "task_id": "default",
      "rule_id": "gmv-defaults",
      "action_id": "default_metric_to_payment_gmv",
      "target_scope_fingerprint": "稳定的语义作用域指纹",
      "produced_refs": []
    }
  ],
  "clarifications": []
}
```

业务默认以自然语言写入 `analysis_task.assumptions`；rule ID、Policy 版本、失败码和耗时只保留在 `/workspace/runtime` 下的 packet/validation 诊断产物中，不进入最终业务口径。

`produced_refs` 只在 action 带 `ir_effect_contract` 时必填，每项使用 `{"collection":"attribution_targets","id":"实际 target_id"}`。它只声明该 action 新增或拥有的目标，不得绑定用户显式目标或其他 action 的产物。同一目标最多由一个 action 绑定。校验器只补齐缺失或非协议枚举的 `target_semantics`；若 `scenario`、合法语义枚举、拆解方式或公式结构与 action contract 冲突，仍返回 `fallback_raw`，不得覆盖。

## 规则到 IR

- TOP6 支付 GMV 表现：事实值、`yoy_growth` 和 `selected_set_share` 为平行需求。事实值 Requirement 使用 `operation=aggregate_level`、`scope_kind=source_dimension_all` 和 `dimension_hint=TOP6平台`；同比与归因引用同一逻辑指标，但各自保持 Requirement 局部时期与拆解。
- 单平台支付 GMV 归因：按规则声明完整因子和公式 AST；规则不指定归因算子。对比关系不明确时保留给现有业务参数预检确认。
- 单平台结算 GMV：结算 GMV 水平/同比、支付 GMV 归因、结算率水平/同比为平行需求。
- TOP6 结算 GMV：结算 GMV 和支付 GMV 的合计水平使用各自的 `aggregate_level`；结算率不可求和，仍按直接事实、注册组合和平台拆解的既有路径履约。
- 京东佣金：单平台未指定口径时并列两个事实需求；多平台时不增加京东专属 3P 口径。

不要把 Query Policy 规则写入 `business-intent-policy-registry.json`、`resolution-policy-registry.json`、派生注册表或归因算子注册表。

归因协议字段必须使用规范枚举：`metric_change` 对应 `absolute_delta`，`yoy_trend_change` 对应 `relative_yoy_trend`；公式和维度拆解分别使用 `formula` 与 `dimension`。业务文案只写入 `semantic_text`、目标说明或 assumptions，不得写入 `target_semantics`、`decomposition` 或 provenance。规则对这些字段有唯一、经审核的约束时，用 action 的 `ir_effect_contract` 声明，不在 instruction 中另造协议别名。

只有用户明确全域合计或已审核 `set_default` 产生全域范围时才提交 `aggregate_level`。模糊“大盘/整体”且范围仍有多个合理解释时保留给业务参数预检或 Resolve 澄清，不根据 `analysis_task.scope` 自由文本直接生成物理聚合。增强 IR 中该 intent 结构非法时应用校验必须 `fallback_raw`，不得把 Policy 故障变成分析阻断。
