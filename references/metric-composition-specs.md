# 指标组合定义与维护

本文件说明同周期业务指标组合的注册和执行边界。机器可执行定义唯一来自 [metric-composition-registry.json](metric-composition-registry.json)；本说明与注册表不一致时，以机器注册表为准并修正文档。

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

六个指标均为 `metric_object=ratio`、`unit=rate`。`rate` 使用小数值契约，例如 `value=0.15` 展示为 `15%`。计算继承需求的时期、范围、过滤、视角和拆解维度；事实缺失、叶子不可唯一解析、维度不兼容或分母为零时不得输出成功结果。

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
