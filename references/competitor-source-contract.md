# 竞品飞书源与 Provider 契约

## 源表边界

飞书源表保持手工维护的原始结构，不需要改造成 facts 表。Provider 只读 source config 指定的 Wiki URL，并且只识别配置中六个逻辑角色允许的 Sheet 名。当前默认名称为 `指标元信息`、`维度元信息`、`周度表`、`月度表`、`季度表`、`年度表`；其他 Sheet 即使包含相似周期或指标结构也必须忽略。

指标元信息中的 `可支持时间粒度`、`可支持拆解维度`、`聚合方式` 是事实能力判断的唯一源表依据。索引分别规范化为 `supported_grains`、`dimensions` 和 `aggregation_mode`；事实表仍需证明具体指标块和周期存在。`指标公式`、`使用说明` 仅保留在源表原始结构之外，不参与自动能力判断或口径推断。

默认源：

```text
https://bytedance.larkoffice.com/wiki/TrBAw0rDXiBrcUkJlbgcjsyYnkg?sheet=ESXBdZ&table=tbl5Ny6EgnsBwEBK&view=vew6r3PPJm
```

允许标准 Sheet 换序、插入行列、指标块移动、周期新增以及名称中的空格/全半角变化；不允许通过内容猜测标准 Sheet。元信息 Sheet 缺失或同一角色匹配多张表时拒绝。指标或维度存在保护词冲突、候选不唯一、维度不支持或事实块重复时必须澄清或拒绝。配置维护见 [data-sources/competitor-macro/source-guide.md](data-sources/competitor-macro/source-guide.md)。

## Gateway 请求

```json
{
  "request_id": "fetch_unified_1",
  "source_binding": {
    "schema_version": "source_binding/1.0",
    "provider_id": "feishu_competitor",
    "source_id": "competitor_macro_sheet",
    "config_hash": "...",
    "revision": 448,
    "schema_hash": "..."
  },
  "fact_demands": [],
  "match_overrides": []
}
```

Provider 不接收自然语言查询，不调用其他 Skill，不调用小策 CLI，不使用竞品 `query.py` 的派生结果。

源表已有的范围维度按物理维度处理。例如 `TOP6平台` 的成员来自当前 revision 的维度元信息和事实块，不在 [dimension-set-registry.json](dimension-set-registry.json) 维护 `TOP6` 别名或六个平台名单。编译器用稳定的 `source_dimension_all` 域引用声明全域，Provider 按每个 `consumer_binding` 解析当前成员。注册表只保留未来其他命名集合的扩展能力；成员缺失、别名冲突或维度不一致时阻断，不做部分展开。

## 标准 facts

Provider 读取基础单元格后直接生成 `scene_facts/2.0`。`facts` 中每个物理事实只出现一次，包含稳定 `fact_id`、指标、周期、维度、值、单位、源表可聚合性、定义、缺失状态和 `source_ref`；根级 `bindings` 将 `fact_id` 绑定到 `task_id`、`fact_slot_id`、`period_role`、`view_id` 和需求引用。需求中的 `source_dimension_domains` 显式携带物理维度全域意图，`resolved_dimension_domains` 记录对应 `domain_id` 在当前 source revision 的具体成员。执行器兼容层只做确定性投影，不重新解析业务结果。物理事实中的具体单位是运行时权威值；binding 中的单位只作为预期值校验，不得用占位值覆盖事实单位，两个具体单位冲突时必须阻断。

单值维度选择器在读取前必须跨当前维度元信息反向匹配。唯一匹配记录在根级 `dimension_resolutions`；多匹配返回澄清候选；候选维度与唯一元信息结果不一致时返回结构化 `resolution_patch`，不得按模型候选继续读取。

缺失值保持 `value=null`，不得补零或估算。Provider 只返回基础指标；结算率、同比、占比和归因由场景分析执行器完成。

## 实时与缓存

每次真实分析由 Gateway resolve 一次远端 revision，并把 index 固定到后续 fetch。结构索引按有效 source config、URL 和 identity 存放在稳定共享缓存；跨进程文件锁保证同一数据源只有一个重建者，锁内二次检查 revision。飞书调用执行跨进程最小间隔，只对超时、限频、429 和 5xx 做最多三次有界退避；语义、元信息和歧义错误不重试。结构索引 revision 与读取值的 revision 必须一致。只有显式允许 stale 且所需坐标完整时才使用快照，并标记 `freshness=stale`。

## 口径优先级

若源表直接提供与请求完全一致的官方组合指标，优先使用直接指标；否则按 [metric-composition-registry.json](metric-composition-registry.json) 从基础指标组合。直接指标与组合指标定义、单位、维度或范围不一致时进入澄清。
