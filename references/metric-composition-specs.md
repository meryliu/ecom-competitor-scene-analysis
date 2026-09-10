# 指标组合定义与维护

本文件说明同周期业务指标组合的注册和执行边界。机器可执行定义唯一来自 [metric-composition-registry.json](metric-composition-registry.json)；本说明与注册表不一致时，以机器注册表为准并修正文档。

每个组合必须显式维护 `period_aggregation`：`recompute` 表示跨非固有跨度时分别安全累计输入后执行一次原公式；`sum` 表示组合输出自身可以跨期求和；`period_only` 表示只能按源支持粒度逐期执行。该字段只决定已选中组合的时间物化方式，不参与组合召回或指标候选评分。缺失字段按 `period_only` 处理，防止未知公式被误聚合。

组合可以使用可选 `display={"unit": string, "multiplier": positive_number}` 声明最终答案的线性展示转换。`unit` 仍是计算和校验单位；答案组装阶段只在成功结果的计算单位与注册定义一致时生成 `display_value=value*multiplier` 和 `display_unit`，不得覆盖原始 `value/unit`。`display` 缺失、非法或无法转换时沿用原始结果，不得改变 Query 状态。该配置不进入候选、Resolve、Prepare、Compile 或 Execution；非线性转换不在本机制范围内。

## 适用边界

指标组合定义“指标是什么”，例如收入除以 GMV，或已登记的两个业务指标构成的跨指标占比。同比、环比、期间变化和同指标选择集占比仍由 [derived-metric-registry.json](derived-metric-registry.json) 管理。组合结果可以继续作为通用派生的输入，例如“综合支付TR同比”先计算综合支付TR，再复用 `yoy_growth`。

正常查询只声明用户要求的业务指标。Gateway 先解析同名直接事实；直接事实明确不可用时，Prepare 才按注册表展开基础指标。不得为了使用组合公式而绕过可用的同名源事实。

## TR 定义

| `composition_id` | 指标 | 公式 |
|---|---|---|
| `competitor_ad_payment_tr` | 广告支付TR | 闭环电商广告收入 / 支付GMV |
| `competitor_ad_settlement_tr` | 广告结算TR | 闭环电商广告收入 / 结算GMV |
| `competitor_commission_payment_tr` | 佣金支付TR | 闭环电商佣金收入 / 支付GMV |
| `competitor_commission_settlement_tr` | 佣金结算TR | 闭环电商佣金收入 / 结算GMV |
| `competitor_comprehensive_payment_tr` | 综合支付TR | 广告收入 / 支付GMV + 佣金收入 / 支付GMV |
| `competitor_comprehensive_settlement_tr` | 综合结算TR | 广告收入 / 结算GMV + 佣金收入 / 结算GMV |

六个指标均为 `metric_object=ratio`、`unit=rate`，并登记 `display={"unit":"%","multiplier":100}`。例如计算结果 `value=0.15, unit=rate` 在答案载荷中保留原值，同时生成 `display_value=15, display_unit=%`。计算继承需求的时期、范围、过滤、视角和拆解维度；事实缺失、叶子不可唯一解析、维度不兼容或分母为零时不得输出成功结果。

## 已注册占比

| `composition_id` | 指标 | 公式 |
|---|---|---|
| `douyin_express_market_share` | 抖音快递占比/市占率 | 抖音包裹量 / 邮政快递揽收量 |

该定义只用于占比水平，不覆盖“抖音包裹市占率-同比增速”。若源表未来提供语义、时期和口径完全匹配的直接占比事实，直接事实仍优先。分子和分母是注册时明确的不同范围，不使用 `same_scope` 校验。

## 注册表达式

每个定义使用非空 `inputs` 和 `expression`：

- `inputs[].role` 在定义内唯一，`inputs[].metric` 是需要从源表解析的业务指标。
- `{"input_role":"role"}` 引用一个已声明输入；重复引用同一角色复用同一事实槽。
- `{"literal":number}` 声明有限数值常量。
- 表达式只允许 `add`、`subtract`、`multiply`、`divide`、`sum` 和 `negate`。
- 每个声明的输入必须被表达式引用，表达式不得引用事实选择器、其他结果节点或未声明角色。

示例：

```json
{
  "inputs": [
    {"role": "ad_revenue", "metric": "闭环电商广告收入"},
    {"role": "commission_revenue", "metric": "闭环电商佣金收入"},
    {"role": "payment_gmv", "metric": "支付GMV"}
  ],
  "expression": {
    "op": "add",
    "args": [
      {
        "op": "divide",
        "args": [
          {"input_role": "ad_revenue"},
          {"input_role": "payment_gmv"}
        ]
      },
      {
        "op": "divide",
        "args": [
          {"input_role": "commission_revenue"},
          {"input_role": "payment_gmv"}
        ]
      }
    ]
  }
}
```

旧版 `operator=divide` 加两个输入的定义暂时兼容；新增和修订定义统一使用 `expression`。

## 维护步骤

1. 确认基础指标在实时源元信息中的标准名称、单位、时间粒度和拆解维度。
2. 在机器注册表新增唯一 `composition_id`、精确触发词、输入角色、表达式、输出单位和最小校验。
3. 公式或口径变化时递增定义版本；注册表内容变化时递增注册表版本。
4. 增加 Gateway 叶子投影、解析状态、Prepare 回退、编译表达式和执行数值测试。
5. 若公式不能用现有白名单表达，再评估编译器和执行器扩展；不要把业务特例写入主流程。
