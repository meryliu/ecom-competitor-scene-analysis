# 正常分析请求契约

本文件是所有正常竞品分析请求的模型侧入口。业务复杂度不改变入口：模型只生成语义 IR，`run_analysis.py` 内部编译后决定 `fast_fact`、`fast_derived`、`standard` 或 `orchestrated`。

## 1. 生成最小 IR

不要手写执行计划、事实坐标、DAG、标准聚合 AST、派生基础指标或算子能力。单问题使用：

```json
{
  "ir_version": "analysis_ir/1.0",
  "analysis_task": {
    "query": "用户原始问题",
    "analysis_goal": "完整回答目标",
    "metrics": [
      {
        "metric_id": "稳定的本任务引用",
        "name": "用户指标名称",
        "metric_object": "volume|ratio",
        "unit": "待元信息解析",
        "definition": "待元信息解析"
      }
    ],
    "periods": {"analysis": "用户目标时期"},
    "scope": "用户指定范围",
    "filters": [],
    "assumptions": []
  },
  "views": [{"view_id": "view_1"}],
  "dimension_trees": [],
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

同一轮多个独立问题使用一个 bundle，共享物理取数和 revision：

```json
{
  "schema_version": "analysis_bundle/1.0",
  "tasks": [
    {"task_id": "question_1", "analysis_ir": {}},
    {"task_id": "question_2", "analysis_ir": {}}
  ]
}
```

每项需求使用唯一 `requirement_id`；归因目标使用唯一 `target_id`。`criticality` 只使用 `core|required|optional`。只声明 Query 实际需要的时期角色和输出。

`unit=待元信息解析` 和 `definition=待元信息解析` 只是模型侧占位状态，不是可执行元信息。统一 runner 在编译前使用当前 source revision 的结构索引补全所有已匹配指标；IR 中已有具体单位但与源元信息冲突时阻断。后续 binding 只校验预期单位，不得覆盖 Provider 事实中的具体单位。

`metric_object` 是 Query 语义声明。默认视为 `model_inferred`；只有用户明确指定指标对象或公式约束时才写 `metric_object_source=user_explicit|user_formula`。复合“表现、增速、涨幅、相比上期”等语义由 runner 生成有界业务意图假设，并结合实时指标/维度元信息、事实块和时期一次筛成可执行候选；模型不要串行改写指标名称试取数据。

用户可使用非标准业务表述；不要为了迎合当前源表名称改写 Query 或预先枚举别名。Gateway 会基于当前 revision 生成候选。收到 `waiting_confirmation` 时只向用户展示 case 的逻辑候选和证据，确认后把所选候选写入该任务的 `resolution_patches`，保持原 requirement ID 后重跑。

单任务根对象只使用 `ir_version=analysis_ir/1.0`；bundle 根对象只使用 `schema_version=analysis_bundle/1.0`，且每个 `analysis_ir` 自身仍声明 `ir_version`。两种版本字段不得互换或同时出现；runner 在能力解析和取数前以 `INPUT_PROTOCOL_INVALID` 阻断错误协议。

## 2. 需求分类

先完整声明用户最终要求，再声明必要的输入适配。五类业务需求相互独立：

1. `fact_observations`：直接展示源表事实。
2. `metric_compositions`：仅用于用户明确指定组合口径或自动解析无法唯一匹配的维护场景。正常查询把“结算率”等用户指标声明为 `fact_observation`；runner 先查源表直接指标，缺失时自动命中注册组合并展开基础指标。
3. `derived_requirements`：同比、环比、期间变化、市占率等语义派生。存在派生语义时读取 [derived-metric-registry.json](derived-metric-registry.json) 取得 `derived_metric_id`、时期角色和对象约束；不要从展示名称猜公式。
4. `custom_calculations`：仅当用户明确给出操作数和运算关系时使用，`definition_source=user_query`。
5. `attribution_targets`：仅当用户明确要求原因、贡献、拉动或拖累量化时创建。归因与派生并行；“同比增速贡献”通常同时需要同比派生和贡献归因，不能互相替代。

用户明确给出归因公式时，公式因子按原顺序完整声明为 `factors`，每项使用稳定 `factor_id` 和 `kind=metric|literal|derived`；公式关系写入只引用 `factor_id` 的 `formula` AST。显式常量不是可省略的校准项：使用 `values_by_period_role` 声明各时期角色值，即使各期相同、最终贡献为 0，也必须参与执行并保留在结果中。派生子表达式使用 `expressions_by_period_role`。`decomposition` 可写 `formula`，由编译器结合场景、目标对象、目标语义、公式形态和因子角色解析已有归因算子；不能把公式形态直接替代场景判断。

同一指标同时要求水平和同比时只声明一个核心指标，例如 `线上化率`；水平写入 `fact_observations`，同比写入引用同一 `metric_ref` 的 `derived_requirements`。不要额外声明“线上化率同比变化”等来源式指标名。Resolve 可以将源侧预计算同比仅绑定到派生 requirement，不会替换水平事实。

标准年、季、月粒度降级不由模型创建 `input_adaptations`。runner 先检查目标粒度直接事实；缺失且指标可聚合时，按最近细粒度和完整覆盖规则自动生成适配。只有用户明确给出非标准输入转换时才在 IR 中声明适配。

存在多个合理指标、分母、范围或公式时写入 blocking clarification；不要用推测公式兜底。

各类需求项只填写与 Query 有关的字段：

| 数组 | 核心字段 |
|---|---|
| `fact_observations` | `requirement_id, metric_ref, period_roles, view_id, dimensions, dimension_refs, criticality` |
| `metric_compositions` | 上述通用字段加注册表中的 `composition_id` |
| `derived_requirements` | `requirement_id, derived_metric_id, definition_status=registered, metric_ref, metric_object, view_id, dimensions, dimension_refs, criticality` |
| `custom_calculations` | `requirement_id, definition_source=user_query, expression, unit, view_id, criticality` |
| `attribution_targets` | `target_id, metric_ref, metric_object, scenario, target_semantics, decomposition, periods, view_id, group_dimensions, criticality` |

例如，标量事实过滤使用 `{"dimensions":{"TOP6平台":"京东"},"dimension_refs":[]}`；TOP6 逐平台派生使用 `{"dimensions":{},"dimension_refs":["TOP6平台"]}`；TOP6 贡献归因的计算分组使用 `{"group_dimensions":["TOP6平台"]}`。`TOP6平台` 是当前源表的物理维度，不是 `平台` 维度下名为 `TOP6` 的逻辑集合。

占位字段必须从 Query 或机器注册表填写。没有某类需求时保持空数组。归因的 `scenario`、`target_semantics`、时期和拆解必须与用户目标一致，不能套用其他归因问题的固定值。

## 3. 时期和输入适配

- “同比”使用当前期与去年同期；一个时期同比只创建 `analysis`、`analysis_last_year`。时期可以保留用户表达，runner 会在编译和哈希前规范化为 `YYYY-MM`、`YYYY-Qn`、ISO 周或 `YYYY`。
- 两个时期的同比差或同比趋势才创建两组同期角色。
- “环比”按 Query 粒度选择直接上期。
- 季度、年度等目标粒度保留目标时期；不要预先展开月份。runner 根据实时结构索引优先读取目标粒度，缺失时自动生成并复用细粒度适配结果。
- 自动展开得到的相同物理时期在整个任务中只注册一个内部时期角色，供不同指标和适配共同引用；`__fact_` 是 runner 保留前缀，模型不得创建或依赖内部角色名称。
- 不为 Query 未要求的趋势或比较期额外取数。

季度求和适配包含 `requirement_id, metric_ref, target_period_role, view_id, dimensions, dimension_refs, expression, rule_source, validation, criticality`。其中 `expression` 使用 `sum` 引用对应三个月的事实角色，`rule_source=source_metric_metadata`，`validation` 至少包含 `facts_present, unit_consistent, metric_additive`。

去年同期需要独立的同期适配，但相同目标中间事实只生成一次，供多个下游结果复用。

用户归因公式只在目标事实和安全聚合均不可用时作为目标事实回退。P0 自动回退仅支持由直接事实 metric factor、无量纲 literal 和 `multiply/divide` 组成的公式；Prepare 根据源指标实际单位生成显式 `unit_conversion`，执行器核验输入实际单位后应用量级换算。未知单位、derived factor、加减公式或需要先聚合的 factor 不生成公式回退，不得通过删除单位校验继续执行。

## 4. 维度和值域

- `dimensions` 表示选择条件；标量是单值，数组是选择域。
- `dimension_refs` 表示结果是否按该维度逐成员返回。
- 单个标量过滤不需要为了物理取数补 `dimension_refs`；编译器会把 `dimensions` 的键加入事实物理粒度，同时保持计算分组不变。
- `dimensions={}` 且 `dimension_refs=["TOP6平台"]` 表示按当前源表 `TOP6平台` 的全部成员逐平台计算。
- 如果命名集合只作为整体范围，需求不添加该 `dimension_ref`，并通过集合聚合节点形成整体值。
- 对源表已存在的范围维度直接使用物理维度名，不再把范围名称写成另一维度的逻辑值；只有注册表中实际存在的其他命名集合才保留用户原词并由 Provider/编译器解析。
- Query 只有“京东”等维度值时，可把维度名写成模型候选；Provider 使用实时结构元信息反向确认。不得维护静态“值属于哪个维度”映射。
- “分别”“各平台”“每个平台”等表述必须添加相应 `dimension_ref`。

计算节点的 `group_dimensions` 使用真实分组维度，例如 `TOP6平台`。命名集合是选择域，不是独立维度值；源表中的 `TOP6平台` 则是独立物理维度。

## 5. 输出和依赖

每个 `output_requirement` 必须通过 `source_requirement_refs` 指向实际承接结果的事实、组合、派生或归因需求：

```json
{
  "requirement_id": "output_1",
  "source_requirement_refs": ["share_1", "contribution_1"],
  "criticality": "core"
}
```

共享事实只用于取数去重，不自动建立计算依赖。派生或归因只有引用适配目标或其他结果时才建立依赖。不得因为预计执行器不支持而删除用户需求或退化为只展示基础事实。

## 6. 执行与结果

IR 或 bundle 落盘后只执行：

```bash
python3 scripts/run_analysis.py \
  --input <analysis-ir-or-bundle.json> \
  --work-dir <run-directory>
```

不要在正常分析中单独运行编译、取数、fast runner 或校验脚本。统一 runner 会完成时期规范化、源路径解析、任务级预检、档位选择、一次合并取数、计算、最终校验和结果生成。单任务失败时保留其他独立任务结果并返回 `partial_success`。

命令成功后只读取顶层 `answer-payload.json`。按原始 Query 检查回答完整性，并按其中的 `success`、`partial_success`、`waiting_confirmation` 或 `blocked` 状态组织结果。不要读取全部中间 artifact 来重复解释成功节点。

### 结果组织

结果组织只改变通过质量闸门产物的展示方式，不增加事实需求、时期角色、派生或归因目标，也不重算成功节点。

`partial_success` 是终态而非可包装状态。残差超阈值或必需依赖未完成时，保留已计算的 rows、summary、残差、warning 和 boundary 信息，但任务、结论依赖和顶层状态不得声明 `success`。

**归因完整性**：除非用户明确只要摘要，归因回答同时展示成功结果中有效的整体摘要和明细。整体摘要包括算子实际返回的周期整体值、整体变化、残差、覆盖或边界信息；明细包括算子实际返回的周期值、变化值或变化率、绝对贡献、相对贡献和贡献方向。某字段不适用于当前算子、未返回或因边界条件不可用时不伪造，可省略该列或标记不可用并说明原因。内部哈希、路由和调试元数据不属于业务结果。

**按指标组织**：最终答案优先以指标为结果组。同一指标下的事实、指标组合、派生和归因结果，在指标定义、视角、范围或分母域、维度粒度、父级边界、时期语义和单位均兼容时，尽量以共同业务维度为行键合并到同一表格。只有名称相同但任一口径不兼容时不得合并；应分表并简要说明差异。不存在共同业务行键、合并后明显降低可读性或结果仅为单个标量时，可直接使用短表或文字，不为满足形式强制合表。

表格结构由本次成功结果决定，不预设特定指标、平台、算子或固定列。通用展示顺序为：业务维度、周期事实、派生表现、绝对变化或贡献、相对变化或贡献、方向或状态。没有对应成功结果的列不补齐，也不为组表额外取数或计算。同一数值由多个成功结果重复提供且口径和值一致时只展示一次；口径或值不一致时不得静默覆盖。

**单位与时期**：每个数值必须能在列名、表名或紧邻表格的口径说明中确定单位。同一列只能有一种单位；不同单位可分列展示。绝对贡献继承归因目标变化量的单位，相对贡献使用百分比；量级指标变化率使用百分比，比例指标的绝对变化按结果口径使用百分点或其他明确单位。源端单位为待解析、未知或缺失时明确标注“单位未解析”，不得推断为元、万元、亿元或其他单位。时期标签根据实际 period role 和时期值生成，不根据 `mom_value` 等内部字段名猜测同比或环比。

**输出顺序**：先给整体表现或直接答案，再按指标展示统一表格，最后解释主要变化、拉动或拖累及必要的范围、单位、缺失、覆盖和 freshness 说明。表格承担数据汇总，结论文字不机械重复全部单元格。

输出前检查以下行为不变量：

- 原始 Query 的每项要求均由成功结果回答，未完成范围已披露。
- 归因结果未被缩减为只有相对贡献率，且不可用字段没有被伪造。
- 口径兼容的同指标结果没有无理由拆散，口径不兼容的结果没有强行合并。
- 所有数值的单位和时期语义可确定；未解析单位已明确披露。
- 最终答案只使用顶层 payload 中通过质量闸门的结果，没有为展示目的重算或扩展 Query。

命令失败时先读取 `run-state.json` 和结构化错误以确定失败阶段；此时正常分析路径结束，转入 `SKILL.md` 的诊断路由。已成功 facts 可在同一输入重跑时通过默认 `--resume auto` 复用。
