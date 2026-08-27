# 派生指标定义注册表

本竞品 Skill 将计算分为两个逻辑层：指标组合定义见 `metric-composition-registry.json`，本文件只维护跨期和范围通用派生。组合层定义“指标是什么”，通用派生定义“指标如何比较”。组合结果可以作为通用派生的输入，但不能把组合指标伪装成源表事实。

注册表版本：`1.4.0`

本文件说明 `scene-analysis` 的派生语义和维护规则；机器可执行定义唯一来自 [derived-metric-registry.json](derived-metric-registry.json)。它与 `ecom-attribution-calculation-engine` 的归因算子注册表分离：派生指标只描述事实之间的确定性计算，不负责维度贡献、残差或复杂边界校验。两者不一致时先修正机器注册表和本说明，禁止让编译器内置第二份公式。

## 已注册定义索引

| `derived_metric_id` | 触发描述 | 指标类型 | 必需时期事实 | 计算方式 |
|---|---|---|---|---|
| `yoy_growth` | 同比、年同比、同期同比 | `volume` / `ratio` | 当前期、去年同期 | 比例指标做差；量级指标做比值减一 |
| `yoy_trend_change` | 同比趋势、同比波动、同比增速差值 | `volume` / `ratio` | 分析期、对比期及各自去年同期 | 先算两期同比，再做分析期同比减对比期同比 |
| `mom_growth` | 环比、较上月/上期 | `volume` / `ratio` | 分析期、对比期 | 比例指标做差；量级指标做比值减一 |
| `period_change` | 分析期对比期变化、绝对变化 | `volume` / `ratio` | 分析期、对比期 | 分析期值减对比期值 |

索引只用于快速路由，具体事实字段、公式、单位和校验以对应定义为准。

## 竞品组合示例

`competitor_settlement_rate` 由 `结算GMV / 支付GMV` 组合得到。用户询问“结算率同比”时，先得到两个时期的组合指标，再使用本注册表中的 `yoy_growth`；不新增同义的 `settlement_rate_yoy_pp`。用户明确要求两个时期同比表现的差值时，使用 `yoy_trend_change`。

`selected_set_share` 是范围派生，分母为用户明确选择的维度域，适用于飞书指标元信息标记为可聚合的指标。对于 `TOP6平台` 这类物理范围维度，编译器生成不含成员名单的 `source_dimension_all` 域引用，Provider 从当前 revision 动态解析全量成员；对于显式选择域或其他已注册命名集合，仍绑定对应 `domain_ref`。注册表不维护 TOP6 成员或原子指标聚合性。输出必须披露分母域，TOP6 平台内占比不能自动称为全市场市占率。

## 使用原则

1. 先从用户 Query 识别派生描述，再从本注册表解析定义；不要把派生结果当成取数事实。
2. 注册定义优先于模型记忆。注册定义需要反推其完整事实时期、指标和单位，事实取回后再做本地二次计算。
3. “同比”是固定的年同比，不得解释成环比或“分析期对比期变化”。对 Query 明确要求看同比的每个时期分别实例化 `yoy_growth`；只有 Query 要求比较多个时期同比或看同比趋势时，才实例化相应的多个同比及趋势定义。
4. 派生计算保持轻量：检查时期完整、单位一致、必要分母非零和缺失状态即可，不套用归因引擎的复杂贡献率、稀疏组合或残差校验。
5. 若 Query 使用了未注册但语义明确、公式简单的派生描述，允许模型生成 `inferred` 定义继续执行；记录公式、事实输入、单位、推理依据和不确定性。若存在多个合理公式或会改变业务口径，才进入 clarification。
6. 结果必须保留 `derived_metric_id`、`definition_source`、`definition_version`、`input_refs` 和 `formula`，以便复算和审计。
7. 派生公式只描述指标间计算关系，自动继承需求的集合域或分组维度；新增业务集合不修改派生定义，新增现有白名单算子可表达的派生不修改编译器。

## 定义维护协议

机器注册表是派生计算的唯一可执行定义层，和 `SKILL.md` 的编排流程、`ecom-attribution-calculation-engine` 的归因算子注册表相互独立。新增或修订定义时：

1. 在 `derived-metric-registry.json` 新增唯一的 `derived_metric_id`，补充触发表述、适用的 `metric_object`、时期角色、结构化表达式、输出单位和最小校验。
2. 若公式或口径发生变化，递增注册表 `version`，并在定义上更新 `definition_version`；不要在主流程中追加同一公式的特例。
3. `required_facts` 必须描述取数事实，不要把另一个派生结果写成事实输入；若可由其他派生组合得到，写明依赖和执行顺序。
4. 只有确定性、低成本的算术派生放在本注册表；贡献率、残差、稀疏分组和复杂边界校验仍归 `ecom-attribution-calculation-engine` 负责。

定义最小接口示例：

```yaml
derived_metric_id: example_metric
trigger_phrases: ["示例描述"]
metric_objects: [volume, ratio]
period_roles: [analysis, comparison]
required_facts:
  - {period_role: analysis, metric: "目标指标"}
  - {period_role: comparison, metric: "目标指标"}
formula: "analysis - comparison"
output_unit: "与指标类型一致"
minimal_validation: [facts_present, unit_consistent]
execution_mode: lightweight_executor/derived
definition_version: "<registry-version>"
```

主流程读取该接口即可自动完成识别、事实投影和本地计算；定义未注册但关系唯一时使用同一接口生成 `status=inferred` 实例，不因注册表缺项阻断分析。

## 时间角色

场景分析使用以下标准角色：

| 角色 | 含义 |
|---|---|
| `analysis` | 用户指定的分析期 |
| `comparison` | 用户指定的对比期 |
| `analysis_last_year` | 分析期的去年同期 |
| `comparison_last_year` | 对比期的去年同期 |

“同比”只使用 `analysis` 对 `analysis_last_year`，或 `comparison` 对 `comparison_last_year`；“分析期对比期变化”只使用 `analysis` 对 `comparison`。两组关系必须在计划中分开记录。

## 定义 `yoy_growth`

- **业务名称**：年同比 / 同比增长
- **触发表述**：同比、年同比、同期同比、同比增长
- **输入事实**：Query 要求看同比的一个当前周期值和其去年同期值；多个时期只有在 Query 要求各自同比时才分别实例化。若 Query 指定维度，则对要求覆盖的每个 `view_id × group` 分别实例化。
- **量级指标公式**：`yoy_rate = analysis_value / analysis_last_year_value - 1`
- **比例指标公式**：`yoy_rate = analysis_value - analysis_last_year_value`
- **数值与展示单位**：源事实遵循声明单位数值契约，例如 `28.1%` 记为 `value=28.1, unit=%`；比例指标做差后按百分点输出，例如 `28.1 - 27.5 = 0.6pp`。量级指标的比值派生仍使用 `rate` / `rate_delta` 小数，集合占比使用 `share` 小数，展示为百分比时才乘以 100。`unit_scale` 中 `%` / `pp` 的 `1e-2` 只用于公式单位代数，不表示源事实按基础比例存储。
- **输出**：`analysis_yoy` 或 `comparison_yoy`、原始输入、公式、单位、`view_id`/分组标签和解释标签。
- **最小校验**：当前值和去年同期值存在；量级指标去年同期值不为 0；单位和口径一致。

## 定义 `yoy_trend_change`

- **业务名称**：同比增速趋势 / 同比增速差值
- **触发表述**：同比趋势、同比增速差值、同比的环比变化、同比波动、两期同比表现变化
- **输入事实**：默认变体使用 `analysis`、`comparison`、`analysis_last_year`、`comparison_last_year` 四期完整基础值。若源表唯一提供同口径的预计算同比序列，可使用 `source_precomputed_yoy_series` 变体，只读取 `analysis` 和 `comparison` 两期同比事实，再按 `period_change` 做差。
- **粒度**：整体或每个独立 `view_id × group`；不同视角不得混合计算。
- **中间计算**：先按 `yoy_growth` 分别计算分析期同比和对比期同比。
- **公式**：`yoy_trend_delta = analysis_yoy - comparison_yoy`
- **输出**：分析期同比、对比期同比、同比趋势差值、实际采用的输入事实和公式，并记录 `variant_id`；不得把预计算同比再做一次同比。
- **归因关系**：是否对该趋势差值归因由 Query 决定；派生定义本身不创建或改变归因目标。
- **最小校验**：基础值变体要求四期事实存在且量级指标两个去年同期值不为 0；预计算同比序列变体要求分析期和对比期同比事实存在；两者都要求单位和指标定义一致。
- **解释**：差值大于 0 表示分析期同比表现相对对比期加速；小于 0 表示相对放缓。

## 定义 `mom_growth`

- **业务名称**：环比增长 / 环比变化
- **触发表述**：环比、相比上期、较上月、较上周
- **输入事实**：`analysis`、`comparison` 两期值。
- **量级指标公式**：`mom_rate = analysis_value / comparison_value - 1`，对比期不为 0。
- **比例指标公式**：`mom_rate = analysis_value - comparison_value`，展示为 pp。
- **注意**：Query 只说“7 月对比 6 月”时，可以计算两期直接变化，但不得自动把它命名为“同比”；只有出现“环比”或等价表述时才登记 `mom_growth`。

## 定义 `period_change`

- **业务名称**：分析期对比期变化
- **触发表述**：较对比期提升/下降、分析期对比期变化、绝对变化；不要因出现宽泛的“原因”或“增长”自动创建。
- **输入事实**：`analysis`、`comparison` 两期值。
- **输出**：`change_value = analysis_value - comparison_value`；量级指标可额外输出 `relative_change = analysis_value / comparison_value - 1`（对比期不为 0），比例指标输出 pp 变化。
- **注意**：该定义只描述分析期和对比期本身的变化，不替代其他派生定义，也不自动成为归因目标。

## 定义实例模板

```json
{
  "derived_metric_id": "yoy_trend_change",
  "definition_source": "scene-analysis/references/derived-metric-registry.json",
  "definition_version": "1.4.0",
  "metric": "目标指标",
  "metric_object": "ratio",
  "period_roles": ["analysis", "comparison", "analysis_last_year", "comparison_last_year"],
  "formula": "(analysis - analysis_last_year) - (comparison - comparison_last_year)",
  "required_facts": [
    {"period_role": "analysis", "metric": "目标指标"},
    {"period_role": "comparison", "metric": "目标指标"},
    {"period_role": "analysis_last_year", "metric": "目标指标"},
    {"period_role": "comparison_last_year", "metric": "目标指标"}
  ],
  "execution_mode": "lightweight_executor/derived",
  "status": "registered"
}
```

未注册派生的实例将 `status` 设为 `inferred`，并额外记录 `inference_basis`；禁止把 inferred 定义伪装成已注册口径。
