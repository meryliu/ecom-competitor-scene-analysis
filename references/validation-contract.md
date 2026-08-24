# 轻量执行校验契约

本文件定义 `scene-analysis` 的只读确定性校验。需要生成计划、执行外部调用或输出最终结论时读取。校验器不推断业务口径、不调用外部服务、不修改输入。

## 调用阶段

Fast path 的 `validation_profile=minimal` 表示只生成与本次事实和简单派生相关的证明义务，不表示跳过本地校验器。`run_analysis.py` 在同一工作流内运行计划和最终校验；无关的归因、父分组和引用式规则因产物中不存在对应能力而自然跳过。

```bash
python3 scripts/validate_execution.py --input <plan.json> --phase plan --report <plan-validation.json>
python3 scripts/validate_execution.py --input <execution.json> --phase final --report <final-validation.json>
```

- `plan`：在首次外部调用前执行。退出码 `2/3` 时不得开始取数或归因。
- `final`：在组织最终业务结论后、交付前执行。存在 `ERROR` 时不得声明 `success`；根据失败范围改为 `partial_success`、`waiting_confirmation` 或 `blocked`。
- 校验输入和报告必须落盘并纳入执行追溯；校验本身不创建 `fetch_request`。

退出码：`0` 通过，`1` 仅警告，`2` 校验错误，`3` 输入不可读。

## 节点机器字段

每个节点除原字段外必须包含：

```json
{
  "criticality": "core|required|optional",
  "requirement_refs": ["用户需求或算子/派生需求ID"]
}
```

- `core`：失败后核心目标不可回答，顶层为 `blocked`。
- `required`：用户要求的独立视角或结果，失败后顶层为 `partial_success`。
- `optional`：仅校验、展示增强或非必要推荐项，失败不自动降低业务完成状态，但必须披露 warning。

不得通过解析自然语言 `failure_strategy` 推断关键性。

## 校验范围

1. 顶层结构、节点和请求 ID 唯一。
2. 节点依赖存在、DAG 无环、状态转换一致。
3. 最终校验时所有节点到达终态。
4. 事实需求、派生输入和归因输入有可追溯来源。
5. `fact_demand_id` 唯一；Provider 中同一物理事实只有一个物理 `fact_id`，所有 binding 引用存在的 fact、task 和 slot；投影后保留 `physical_fact_id`、`binding_id`，逻辑 `fact_id` 唯一且由前两者稳定生成。
6. 每次真实调用使用唯一 `attempt_id`，重试关系可追溯，成功后不重试；恢复复用不伪造新 attempt。
7. 标准事实保留 `raw_missing`，并生成受支持的 `normalization_reason`；`missing=true` 时保持 `value=null`，`denominator=0` 时必须缺失。
8. 成功的派生结果保留输入引用、公式、单位和值。
9. 成功的归因结果保留 `ok`、summary、rows、warnings 和 boundary_cases。
10. 未指定层级的维度若采用复合映射或存在多候选，必须有结构化选择依据、assumption 或 clarification。
11. 顶层声明状态与节点状态推导结果一致。
12. 使用轻量执行器时，运行时版本、时期角色、并发数和残差阈值合法。
13. 可执行节点声明受支持的 handler；派生节点提供合法安全表达式、单位和显式结果依赖，归因节点提供显式算子和非空声明式输入绑定。
14. 父分组展开只引用运行时维度字段，不使用业务专用展开模式；静态 payload 不能用于父展开。
15. 归因时期角色与 `execution_runtime.periods` 完全一致，不允许同一事实的 `period` 与 `period_role` 冲突。
16. 从 `numerator/denominator` 恢复 `value` 时，组成事实必须为有限数值、分母非零，派生标记必须一致且结果在容差内闭合。
17. 宽表请求声明非空行键和事实映射；最终执行结果记录原始字节数、物理行数、逻辑事实数、解析耗时、规范化耗时和总取数耗时。新版结果声明 `success` 时，每个取数请求必须有且仅有一条带计时的执行记录。
18. 引用式结果逐块校验路径边界、格式、记录数、字节数和 SHA-256；事实与节点 JSONL 流式读取，结果索引必须完整且唯一地指向存在的节点或父任务行。任一 artifact 缺失、损坏或越界均阻断最终成功。
19. `attribution_targets` 的 ID 唯一，场景、目标语义和时期角色完整；每个归因节点都有有效 `target_ref` 和 `operator_contract_ref`。
20. 权威算子契约必须包含非空 `supported_target_semantics`。不匹配节点使用 `ATTRIBUTION_TARGET_UNSUPPORTED`，能力缺失节点使用 `ATTRIBUTION_CAPABILITY_UNRESOLVED`；两者都必须为 `blocked` 且不可执行。
21. blocked 节点没有成功结果，依赖它的节点不能成功，成功结论不能引用其结果；原有贡献闭合、残差和覆盖校验保持不变。
22. 编译计划保留 `analysis_ir/1.0`、编译器版本、来源哈希和非负耗时；每个事实观察、派生、自定义计算和归因目标都有唯一 `requirement_compilation`。
23. 每条需求编译记录引用存在的节点和事实槽位，节点通过 `requirement_refs` 回指需求；需求不得静默遗漏或重复编译。
24. `attribution_targets=[]` 时，计划不得包含 operator query、operator contract、归因节点或归因专属事实。
25. 存在 `execution_profile` 时必须同时存在 `fast_query_admission/1.0`；顶层档位、eligible 和 validation_profile 必须一致，fast 档位不得包含归因目标。
26. 比例结构归因的 `sparse_policy` 必须是对象；仅允许已实现的 strategy/reference policy，`epsilon` 必须为有限正数，`merge_rules`、`rollup_path` 和 `parent_dimensions` 类型合法。
27. 归因目标声明非空 `parent_dimensions` 时必须使用 `for_each_parent_group` 且两处父维度一致；每个运行时归因 payload 只能包含一个父节点。
28. 归因结果中的 `ranking` 是可选展示视图；正/负过滤、贡献率/绝对值排序和 TopK 稳定性只产生 `WARNING`，不得因排序视图异常掩盖完整 `rows` 或阻断整体输出。
29. 输入适配目标不进入物理取数请求；适配节点必须保存安全表达式、目标事实、规则来源和显式依赖。`metric_additive` 只能由输入事实元信息证明。
30. 周上卷适配若声明 `rollup`，必须使用 `calendar=iso8601`；每个来源周唯一且存在，`overlap_days` 必须为 1 到 7，`weight` 必须等于 `overlap_days/7`，并与目标期间实际日期交集一致。缺失周不得补零，计划不得混合不同上卷路径。
30. 最终组织节点即使存在失败依赖也可成功完成“整理可用结果”职责；manifest 保留失败/跳过节点和原始 Query，供模型检查并补足低风险计算。模型补足不改变节点执行状态。
31. 每个事实槽位的 `selector_dimensions` 键必须全部进入物理 `dimension_refs`；计算节点是否逐成员返回仍由其显式 `group_dimensions` 决定。
32. 公式归因的每个因子必须有唯一稳定 `factor_id` 和合法 `kind`；`factor_order` 必须与 binding 顺序一致，公式 AST 必须恰好引用全部因子一次。
33. 目标标量维度必须传播到目标、metric 因子、derived 因子事实叶子及全部时期角色；因子维度与目标维度冲突、selector grain 不完整或重复 selector 在 fetch 前报错。标量过滤不得隐式生成 fanout。
34. 分析 IR 不得覆盖 runner-owned 残差阈值。残差超阈值的有效引擎结果必须保存为 `partial_success`；`execution_summary.partial_nodes` 与节点终态完全一致。
35. `success` 节点只能依赖 `success`；结论组织、任务汇总和 bundle 汇总不得把 `partial_success`、失败、跳过或阻断依赖升级为 `success`。

## 轻量执行器校验

`execution.mode=lightweight_executor` 时，`handler` 只能是 `fact_artifact`、`derived`、`attribution` 或 `model_owned`。归因节点必须提供 `operator`，并提供 `payload` 或 `binding`；派生节点必须提供 `expression` 或 `expressions`。

父分组展开使用动态 binding；公式节点或其他没有 `groups.selector` 的配置同时提供通用 `parent_selector`：

```json
{
  "expansion": {
    "mode": "for_each_parent_group",
    "parent_dimensions": ["运行时维度引用"],
    "parent_selector": {"metric": "运行时指标引用"}
  }
}
```

禁止增加按具体业务维度命名的展开模式。执行器字段错误在计划阶段直接阻断外部取数，避免取数完成后才发现计划不可执行。

归因节点先引用顶层目标和权威算子契约。目标匹配时才允许 `execution.mode=lightweight_executor`；不匹配时使用：

```json
{
  "target_ref": "target_ref",
  "operator_contract_ref": "operator_query_ref",
  "status": "blocked",
  "reason_code": "ATTRIBUTION_TARGET_UNSUPPORTED",
  "required_target_semantics": "point_yoy_trend",
  "supported_target_semantics": ["relative_yoy_trend"],
  "execution": {"mode": "blocked", "handler": "attribution"}
}
```

## 维度解析记录

未指定层级的维度使用结构化记录，供校验器检查决策是否披露；校验器不判断映射业务上是否正确：

```json
{
  "requested": "用户原始维度",
  "explicit_level": false,
  "candidates": ["候选A", "候选B"],
  "candidates_semantically_equivalent": false,
  "selected_fields": ["字段A", "字段B"],
  "selection_basis": "user_confirmed|resolver_unique|recommended|default",
  "resolution_ref": "取数响应引用",
  "assumption_ref": "采用推荐或默认映射时的assumption ID",
  "clarification_ref": "存在未裁决候选时的clarification ID"
}
```

`resolver_unique` 表示竞品 Provider 元信息解析出唯一可执行映射；`recommended/default` 必须同时引用 assumption。多个非等价候选必须引用 blocking clarification。

## 状态收口

- 存在未解决的 blocking clarification：`waiting_confirmation`。
- 任一 `core` 节点为 `failed`、`skipped`、`partial_success` 或 `blocked`：`blocked`。
- 任一 `required` 节点未成功：`partial_success`。
- 所有 core/required 节点成功且无校验错误：`success`。

最终输出保留：

```json
{
  "validation_reports": {
    "plan": {"artifact": "...", "exit_code": 0},
    "final": {"artifact": "...", "exit_code": 0}
  },
  "declared_status": "success",
  "computed_status": "success"
}
```

校验器报告问题但仍能回答部分问题时，只输出已通过事实和质量闸门的结论，并列出失败、跳过节点和未覆盖范围。禁止为通过校验而删除失败节点、修改原始事实或把缺失值补为 0。
