# 通用分析 IR 与计划编译契约

本文件定义模型与 `compile_plan.py` 的接口。只有生成、检查或扩展编译计划时读取。

`scripts/analysis_ir_normalizer.py` 是 Resolve 前唯一的确定性规范化入口。它只处理可机械确定的结构：时期格式、归因时期角色别名、可由字段形态唯一判断的 factor `kind`，以及将多时期注册组合拆成编译器消费的单时期 Requirement。规范化必须幂等，不得改写用户指标、范围、筛选、公式或有歧义的业务含义。结构基线见 [analysis-ir.schema.json](analysis-ir.schema.json)，跨引用、时期角色和公式完整性继续由 `ir_contract_guard.py` 统一校验。Query Policy 应用校验失败仍返回 `fallback_raw`，增强环节不得形成业务阻断。

## 目录

1. 设计边界
2. IR 顶层结构
3. 平行需求类型
4. 计算分类规则
5. 编译流程
6. 编译输出与状态
7. 执行档位
8. 重编译和性能字段

## 设计边界

模型理解原始 Query 并输出精简 IR；编译器不解析自然语言、不发明业务口径、不计算事实。编译器确定性完成能力解析、事实槽位合并、节点模板、DAG、统一取数请求和计划校验。执行器只消费编译后的计划与事实。

事实观察、派生、自定义计算和归因是平行需求。共享事实不建立计算依赖；只有显式结果引用才建立依赖。`attribution_targets=[]` 是合法计划，不得查询算子或加载归因引擎。

## IR 顶层结构

多个独立问题可使用 bundle，共享一次物理取数和 revision；每个 `task_id` 必须唯一，且归一化后的 artifact 目录名也不得冲突：

```json
{
  "schema_version": "analysis_bundle/1.0",
  "tasks": [
    {"task_id": "question_1", "analysis_ir": {}},
    {"task_id": "question_2", "analysis_ir": {}}
  ]
}
```

单任务与 bundle 的版本字段是互斥协议：单任务根对象使用 `ir_version`，bundle 根对象使用 `schema_version`，bundle 内每个 `analysis_ir` 仍使用 `ir_version`。runner 在 source resolve 前执行协议预检，错误返回 `INPUT_PROTOCOL_INVALID`，不得进入取数。

```json
{
  "ir_version": "analysis_ir/1.0",
  "analysis_task": {
    "query": "用户原始 Query",
    "analysis_goal": "最终目标",
    "execution_mode": "direct_query|analysis_orchestration",
    "metrics": [
      {
        "metric_id": "metric_ref",
        "name": "运行时指标名称",
        "metric_object": "volume|ratio",
        "unit": "运行时单位",
        "definition": "已确认口径或待竞品 Provider 元信息解析"
      }
    ],
    "periods": {"analysis": "period_a"},
    "scope": "用户指定范围",
    "filters": [],
    "assumptions": []
  },
  "views": [{"view_id": "view_1", "dimension_tree_ref": "tree_1"}],
  "dimension_trees": [
    {
      "tree_id": "tree_1",
      "levels": [{"level_id": "level_1", "dimension_ref": "dimension_ref"}]
    }
  ],
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

每项需求使用唯一 `requirement_id`；归因保留唯一 `target_id`。通用字段为 `view_id`、`apply_to`、`criticality`、`period_roles` 和 `required_outputs`。`criticality` 只能是 `core|required|optional`。

`analysis_task.periods` 是事实观察、组合、通用派生和自定义计算的任务级默认时期映射。每个归因目标的 `periods` 是该目标的权威局部映射，不反写任务级映射；不同归因目标可以复用同一角色名并指向不同物理时期。例如同一 Query 中，同比增速使用 `analysis=2026-07, analysis_last_year=2025-07`，同比归因可同时使用 `analysis=2026-07, comparison=2025-07`。两个归因目标也可分别声明 `comparison=2025-07` 与 `comparison=2026-06`。编译器按“目标 + 角色 + 物理时期”生成消费槽位，不因角色同名合并不同物理时期。

归因目标可附带可选 `metric_semantics`、`parent_target_ref`、`relation_to_parent` 和 `ranking`。这些字段只进入 binding/结果元数据，不生成事实槽位；缺失、unknown 或低置信度不阻断归因。

模型必须先完整声明用户要求的事实、派生和归因，再声明输入适配。输入适配不得替代或隐藏最终业务需求。

`dimension_trees` 和 `apply_to` 只表达用户指定的层级及计算挂载位置，不自动产生派生或归因需求。指标、维度和时期都是运行时引用，编译器不得包含业务专用字段。

## 平行需求类型

### 输入适配

当计算需要的输入无法由源表直接提供，但存在唯一、可追溯的安全转换时，使用 `input_adaptations`。表达式复用自定义计算的白名单 AST；目标由指标、时期角色、视角和维度确定。相同目标只允许一个适配节点。

Prepare 自动生成的适配可额外携带 `target_period`，用于目标局部时期不在 `analysis_task.periods` 中，或同名角色在不同归因目标中指向不同物理时期的情况。模型不得为粒度上卷手写该字段；Prepare 根据目标时期和源能力生成并校验。

```json
{
  "requirement_id": "adapt_1",
  "metric_ref": "metric_ref",
  "target_period_role": "analysis",
  "view_id": "view_1",
  "dimension_refs": ["dimension_ref"],
  "dimensions": {"dimension_ref": ["named_set"]},
  "expression": {
    "op": "sum",
    "args": [
      {"fact": {"metric_ref": "metric_ref", "period_role": "source_1"}},
      {"fact": {"metric_ref": "metric_ref", "period_role": "source_2"}}
    ]
  },
  "rule_source": "source_metric_metadata",
  "validation": ["facts_present", "unit_consistent", "metric_additive"],
  "criticality": "core"
}
```

适配可用于时间或维度上卷、范围聚合及其他确定性输入形态转换。聚合时必须使用 `metric_additive`，其值只从 Provider 返回的飞书指标元信息校验。指标组合继续使用 `metric_compositions`，业务派生继续使用 `derived_requirements`，不要在适配层复制定义。

源数据只有周标签时，周标签按源配置中的 ISO 8601 语义解释（周一至周日）。Prepare 生成周上卷适配时可在 `input_adaptations.rollup` 保存 `calendar`、`target_period` 和逐周 `components`；每个组件包含 `period`、`overlap_days` 和 `weight=overlap_days/7`。直接事实优先，且不得混合月份/季度组件与周组件。

### 事实观察

```json
{
  "requirement_id": "observe_1",
  "metric_ref": "metric_ref",
  "period_roles": ["analysis"],
  "view_id": "view_1",
  "dimension_refs": [],
  "criticality": "core"
}
```

需求级逻辑指标约束使用可选 `metric_constraints`：

```json
{
  "constraint_id": "platform_jd",
  "kind": "dimension_filter",
  "operator": "eq",
  "values": ["京东"],
  "dimension_hint": "平台",
  "provenance": "model_inferred"
}
```

`operator` 只允许 `eq|in|exclude`，同一 requirement 的多项约束按 AND 组合。该结构表达逻辑口径，不代表物理选择器已可用；Resolve 使用指标支持维度和实时枚举判断可得性，Prepare 才写入 `dimensions` 或生成维度成员适配。约束身份必须包含 `task_id + requirement_id + metric_ref + normalized constraints`，所有 binding 均按 requirement 隔离，不能覆盖同一逻辑指标的无约束需求。

安全自动路径限于：现成完整口径事实、基础指标单成员选择、可加指标的多成员求和、同一指标全域减同一指标排除成员，以及既有注册组合/派生。两个独立指标名称看似可相减时最多生成需确认假设；只有用户显式公式、注册定义或结构化同指标成员关系才能生成 AST。

当 Query 已明确要求一个运算结果、但 Query 层无法判断它最终由现成事实、成员选择还是注册公式履约时，可在对应 Requirement 暂存路径无关的 `resolution_intent`。当前只允许 `operation=share_level`，并必须声明 `output_metric_object=ratio`、`operands.numerator` 和 `operands.denominator`。其中维度条件仍是逻辑语义，不是物理 selector。

```json
{
  "requirement_id": "douyin_share",
  "metric_ref": "express_volume",
  "period_roles": ["analysis"],
  "semantic_text": "抖音快递占比",
  "resolution_intent": {
    "operation": "share_level",
    "output_metric_object": "ratio",
    "operands": {
      "numerator": {
        "concept_ref": "express_volume",
        "metric_constraints": [{
          "kind": "dimension_filter",
          "operator": "eq",
          "dimension_hint": "平台",
          "values": ["抖音"],
          "provenance": "user_explicit"
        }]
      },
      "denominator": {
        "concept_ref": "express_volume",
        "scope_kind": "market_total"
      }
    }
  }
}
```

`resolution_intent` 与 `derived_metric_id`、`composition_id` 互斥。Gateway 按 Requirement 选择直接占比事实、同指标成员占比或注册组合；Prepare 必须删除 intent 并物化为既有事实、派生或 `metric_compositions`。未物化 intent 不得进入 Compile，Fast Query 也必须转交统一 Prepare 流程。旧 IR 不含该字段时行为不变。

### 派生需求

注册定义使用 `definition_status=registered` 和 `derived_metric_id`。未注册但唯一可推理的简单关系使用 `definition_status=inferred`，并提供 `definition.expression`、`required_period_roles`、`unit` 和 `inference_basis`。存在多个合理公式、分母或范围时生成 clarification，不得放入自定义计算兜底。

```json
{
  "requirement_id": "derived_1",
  "derived_metric_id": "yoy_growth",
  "definition_status": "registered",
  "metric_ref": "metric_ref",
  "metric_object": "volume",
  "view_id": "view_1",
  "criticality": "required"
}
```

### 指标组合需求

指标组合用 `metric_compositions` 表达“指标是什么”，由 [metric-composition-registry.json](metric-composition-registry.json) 展开为基础事实；通用同比、环比和期间变化仍放在 `derived_requirements`。

注册组合使用 `inputs + expression` 安全 AST。`inputs[].role` 在定义内唯一，表达式通过 `input_role` 引用输入并可重复复用同一事实槽；允许的算子与自定义计算一致，但组合注册表不得引用任意事实选择器或其他结果节点。完整维护规则见 [metric-composition-specs.md](metric-composition-specs.md)。旧版两输入 `operator=divide` 定义仅用于兼容。

```json
{
  "requirement_id": "settlement_rate_1",
  "metric_ref": "competitor_settlement_rate",
  "composition_id": "competitor_settlement_rate",
  "period_roles": ["analysis"],
  "view_id": "view_1",
  "dimension_refs": ["TOP6平台"],
  "dimensions": {},
  "criticality": "required"
}
```

`dimensions` 中标量表示精确维度值，数组表示显式选择域；`dimension_refs` 决定是否按维度成员分别返回。`TOP6平台` 等源表范围维度直接写入 `dimension_refs`，其全量成员由 Provider 按当前 revision 解析，不在 IR 或注册表枚举。其他命名集合仅在 [dimension-set-registry.json](dimension-set-registry.json) 实际有定义时解析。数组维度不在 `dimension_refs` 时，计算需求必须显式形成集合聚合；编译后的精确事实选择器不得包含数组。

### 自定义计算

只在用户明确给出操作数和运算关系时使用。`definition_source` 固定为 `user_query`，表达式使用执行器白名单 AST：`add`、`subtract`、`multiply`、`divide`、`sum`、`negate`、`literal`、`fact` 和 `result`。

```json
{
  "requirement_id": "custom_1",
  "definition_source": "user_query",
  "expression": {
    "op": "divide",
    "args": [
      {"fact": {"metric_ref": "metric_a", "period_role": "analysis"}},
      {"fact": {"metric_ref": "metric_b", "period_role": "analysis"}}
    ]
  },
  "unit": "ratio",
  "view_id": "view_1",
  "criticality": "required"
}
```

### 归因目标

归因目标只来自用户明确的原因、贡献、拉动或拖累量化要求。每个目标声明 `scenario`、`target_semantics`、`metric_object`、`decomposition`、时期角色、视角及公式因子或分组维度。编译器按需查询内嵌 `attribution_core`。TopN、稳定组合等部分覆盖目标还必须声明 `coverage.mode=auto_residual`，并保证可通过父节点完整事实计算 `其他/未覆盖`；不得把部分组合重新归一化为完整父节点。

```json
{
  "target_id": "target_1",
  "metric_ref": "metric_ref",
  "metric_object": "volume",
  "scenario": "metric_change",
  "target_semantics": "absolute_delta",
  "decomposition": "dimension",
  "periods": {"analysis": "period_a", "comparison": "period_b"},
  "view_id": "view_1",
  "group_dimensions": ["dimension_ref"],
  "metric_semantics": {
    "metric_id": "stable_metric_id",
    "direction": "higher_is_better|lower_is_better|unknown",
    "direction_source": "user|official_metadata|registry|parent_derivation|inference|unknown",
    "direction_confidence": "high|medium|low|unknown"
  },
  "parent_target_ref": "optional_parent_target",
  "relation_to_parent": {"monotonicity": "positive|negative|conditional|unknown"},
  "ranking": {"metric": "contribution_rate", "filter": "positive|negative|all", "order": "asc|desc|abs_asc|abs_desc", "top_k": 3},
  "criticality": "required"
}
```

公式归因使用以下通用因子契约，不绑定具体指标、维度、时期或常量语义：

```json
{
  "decomposition": "formula",
  "dimensions": {"dimension_ref": "scalar_value"},
  "factors": [
    {"factor_id": "factor_a", "kind": "metric", "metric_ref": "metric_a"},
    {
      "factor_id": "factor_b",
      "kind": "literal",
      "values_by_period_role": {"analysis": 1.0, "comparison": 1.0}
    },
    {
      "factor_id": "factor_c",
      "kind": "derived",
      "expressions_by_period_role": {
        "analysis": {"op": "divide", "args": [{"fact": {"metric_ref": "metric_c", "period_role": "analysis"}}, {"literal": 2}]},
        "comparison": {"op": "divide", "args": [{"fact": {"metric_ref": "metric_c", "period_role": "comparison"}}, {"literal": 2}]}
      }
    }
  ],
  "formula": {
    "op": "multiply",
    "args": [
      {"factor_ref": "factor_a"},
      {"factor_ref": "factor_b"},
      {"factor_ref": "factor_c"}
    ]
  }
}
```

`factor_id` 在同一目标内唯一且稳定；`factor_order`、`formula_fingerprint` 由编译器生成。公式 AST 必须恰好引用每个因子一次，顺序和乘除位置决定因子角色；声明角色与公式冲突时在取数前阻断。`literal` 和 `derived` 必须覆盖场景要求的全部时期角色。值不变或贡献为 0 不允许删除因子。当前公式归因只支持纯乘法或乘除组合；含加减的混合形态若没有匹配的现有算子，返回 `FORMULA_SHAPE_UNSUPPORTED`，不得改写用户公式。

runner 在 Provider resolve 前先调用 `scripts/business_parameter_preflight.py`，再执行归因 IR contract guard。预检是 task-local、纯内存的业务完整性层：只对 `attribution_targets` 中缺失且可由当前 Query/IR 唯一确定的时期角色、`target_semantics` 或拆解类型做确定性补齐；多个合理解释生成不超过三个候选的 `kind=business_parameter` resolution case。它不读取源元信息、不参与指标/维度候选评分、不校验推断单位，也不从历史对话补充范围。

目标及 metric 因子必须使用已声明的 `metric_ref`，`formula` 必须是对象 AST，每个 `factor_ref` 必须命中唯一 `factor_id`，literal 必须覆盖场景角色。`metric_change` 的规范角色固定为 `analysis/comparison`；目标局部的旧 `analysis_last_year` 仅可在没有冲突时归一为 `comparison`。同比派生可继续在任务级保留 `analysis_last_year`，不会被归因角色归一删除。结构错误使用 `ATTR-IR-000` 至 `ATTR-IR-006` 在远端调用前失败，不得被业务预检改写为澄清，不解析自由文本公式或模糊匹配指标名。

归因算子仍由 `scenario + metric_object + target_semantics + formula shape + factor roles` 共同确定。例如同一乘除公式在 `metric_change` 与 `yoy_trend_change` 下分别路由到变化算子和同比趋势算子；公式形态不越权覆盖场景。

测试或离线编译可以在目标中提供完整 `operator_contract`；正式执行由编译器查询内嵌归因内核，不接受模型自行构造的算子能力声明。

### 输出要求

每个输出要求用 `source_requirement_refs` 指向实际承接结果的事实、组合、派生或归因需求。编译器据此生成最终组织依赖，禁止只有输出文案而没有计算生产者。

```json
{
  "requirement_id": "output_1",
  "source_requirement_refs": ["derived_1", "target_1"],
  "criticality": "core"
}
```

比例结构归因可在目标中声明以下 Query 级策略：

```json
{
  "parent_dimensions": ["category"],
  "group_dimensions": ["presale"],
  "coverage": {"mode": "auto_residual", "residual_name": "其他"},
  "sparse_policy": {
    "strategy": "merge_other_then_epsilon",
    "merge_rules": [{"target_name": "其他", "members": ["指定组合"], "is_other": true}],
    "rollup_path": [["category"]],
    "epsilon": 1e-9,
    "reference_rate_policy": "paired_observed_self_rate",
    "structural_absence_is_zero": true,
    "approximation_note_required": true
  }
}
```

只填写 Query 实际指定的 `merge_rules` 或 `rollup_path`；未指定时保留空数组并执行默认“其他 → 残差 → ε”。`parent_dimensions` 表示闭合与合并的父边界，非空时编译器自动生成逐父展开；不得用一个 payload 混合多个父节点。`merge_rules` 和 `rollup_path` 处理后仍有任一必要周期分母缺失或为 0 的组合，继续走默认处理。当前只实现上例中的固定 `strategy` 和 `reference_rate_policy`，不得声明其他枚举值。

## 计算分类规则

按以下顺序分类，且并行识别归因目标：

1. 用户明确给出因子和运算关系：`custom_calculation`。
2. 语义命中机器注册表：`registered derived_requirement`。
3. 未命中但语义、分母、范围和时期唯一：`inferred derived_requirement`。
4. 存在多个合理定义：`clarification`。
5. 明确要求贡献或原因量化：另建 `attribution_target`，不改变前四项分类。

“当前分组指标占同口径大盘指标的比例”在指标、视角根范围和过滤条件唯一时属于语义派生；公式只是执行形式，不属于用户自定义公式。业务指标名称本身没有公式时，交给竞品 Provider 元信息解析，禁止当成自定义计算。

## 编译流程

`scripts/compile_plan.py` 依次执行：

1. 校验 IR 版本、ID、指标、视角、维度树、时期和引用。
2. 将四类需求标准化为带类型的 `work_item`。
3. 编译输入适配并注册其目标中间事实；适配源事实进入统一取数，适配目标不进入物理请求。
4. 独立解析派生注册表、自定义公式和归因能力；没有对应需求时跳过该 resolver。
5. 对不支持的归因目标执行目标语义闸门并局部阻断，不替换目标。
6. 将每项支持需求投影为事实槽位，并按指标、时期、视角、粒度、组件和全局范围去重。
7. 仅在执行器支持对应通用展开时根据 `apply_to` 生成节点模板；否则以 `CALCULATION_SCOPE_UNSUPPORTED` 局部阻断，禁止退化为单次计算。
8. 生成一条统一结构化取数请求；事实解析不足时标记 `waiting_resolution`，不伪造定义。
9. 生成 `requirement_compilation`，确保每项需求都有节点、阻断或 clarification。
10. 运行现有计划校验器后输出计划。

编译器生成的事实引用必须包含逻辑 `metric_ref` 和 `view_id`，并用 `dimensions` 与 `dimensions_exact=true` 固定粒度。`metric_ref` 用于在同一物理事实服务多个逻辑需求时优先选择对应绑定；它不改变 Provider 的物理取数身份。分组归因仅在部分覆盖或显式要求整体值时绑定独立大盘事实。

Prepare 在事实能力判断前形成统一过滤上下文：`analysis_task.filters` 中可确定性下推的 `eq/in` 条件先规范化为任务级 `selector_dimensions`，再传播到需求、归因目标、每个 metric 因子、derived 因子的事实叶子和所有时期角色；局部维度只可补充不冲突的约束。Compile 使用同一幂等规范化逻辑并验证所有物理事实槽位均继承任务选择器。`selector_dimensions` 的每个键必须进入物理 `dimension_refs`。标量过滤不得自动转成逐成员 fanout，只有显式 `group_dimensions`/父级展开才生成分组输出；无法下推的过滤操作必须在 Prepare 阻断，不能作为无效审计字段继续执行。

编译器不得调用取数服务、执行计算、组织业务结论或为单次 Query 生成脚本。新增计算类型通过独立 resolver 接入，不在编译器核心添加业务条件分支。

语义字段只进入 attribution binding 和结果元数据，不进入 `fact_slots`。方向缺失、unknown、低置信度或父子关系无法推导时，计划仍可执行；业务价值词不输出，数学贡献和排序照常执行。ranking 配置异常按安全默认降级并记录 warning，不为此阻断整体计划。

## 编译输出与状态

编译计划继续遵循 [output-contract.md](output-contract.md)，并增加：

```json
{
  "execution_profile": "fast_fact|fast_derived|standard|orchestrated",
  "fast_query_admission": {
    "schema_version": "fast_query_admission/1.0",
    "eligible": true,
    "validation_profile": "minimal|full",
    "features": {},
    "limits": {},
    "reasons": [],
    "fallback_triggers": []
  },
  "compiler": {
    "name": "scene-analysis-plan-compiler",
    "version": "1.8.0",
    "source_ir_version": "analysis_ir/1.0",
    "source_ir_sha256": "...",
    "timings": {
      "operator_resolution_ms": 0,
      "compile_ms": 0,
      "plan_validation_ms": 0
    }
  },
  "requirement_compilation": [
    {
      "requirement_id": "requirement_ref",
      "kind": "fact_observation|registered_derived|inferred_derived|custom_calculation|attribution",
      "status": "compiled|blocked|waiting_confirmation",
      "node_ids": [],
      "fact_slot_ids": []
    }
  ]
}
```

每个计划节点通过 `requirement_refs` 回指 IR。`attribution_targets=[]` 时不生成 operator query、contract、归因节点或归因事实。运行时派生与自定义计算可共享 `handler=derived`，但节点类型、定义来源和审计信息必须保留。

阻断范围遵循 `criticality`：core 阻断整体；required 允许其他独立需求继续并在最终形成 partial success；optional 只产生披露。阻断节点不能产生专属事实或成功结论。

## 执行档位

编译器调用 `scripts/fast_query_admission.py`，根据编译后的事实槽位、表达式、层级、确认项和归因目标确定档位；`analysis_task.execution_mode` 只保留兼容性，不作为权威裁决。详细准入、预算和升级规则见 [fast-query-contract.md](fast-query-contract.md)。

所有档位使用 `scripts/run_analysis.py` 单命令执行。编译器的 admission 仍决定本地执行强度，但不再选择不同的取数入口。`run_fast_query.py` 仅为兼容委托入口。

## 重编译和性能字段

竞品 Provider 的指标、维度或 Schema 解析结果使用根级 `resolution_patches` 更新 IR 后确定性重编译。每项包含 `case_id`、`candidate_id`、`source_revision`、`schema_hash`、`resolution_policy_hash`、`resolution_engine_version` 和指标语义 fingerprint；组合叶子 case 还包含 `task_id`、`metric_ref`、`composition_id`、`input_role` 和 `composition_registry_hash`。patch 是请求级确认，不得修改逻辑指标/维度、全局注册表或共享索引；版本、语义或组合定义字段不匹配时返回 stale case。保持未变化的 `requirement_id`、事实槽位 ID 和节点 ID 稳定，复用请求 hash 未变化的成功事实，不重新理解 Query。

业务参数 case 使用同一根级 `resolution_patches`，但固定 `kind=business_parameter`，通过 `case_id + candidate_id + context_fingerprint` 校验；`requires_value=true` 的候选额外携带 `value`。预检消费并从送往 Provider 的 IR 中移除该补丁，只保留补齐后的结构化字段。完整 IR 不写入预检诊断字段，因此编译输入、节点 ID 和 fetch request hash 不变。

prepare 阶段可生成根级 `resolution_blocks`。每项绑定 `requirement_id`、`criticality` 和 active resolution cases；`core` case 暂停任务确认，`required`/`optional` case 由 compiler 物化为对应的 blocked requirement node，使其他独立需求继续执行。

至少采集 `ir_generation_ms`（由调用方记录）、`operator_resolution_ms`、`compile_ms`、`plan_validation_ms` 和 `recompile_ms`。编译器自身目标为秒级，外部算子契约查询单独计时。
