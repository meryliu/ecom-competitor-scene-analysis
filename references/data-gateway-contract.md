# Data Gateway 契约

`DataGateway` 是分析编排与具体数据源之间唯一的运行时边界。第一阶段只提供 Feishu 实现，不建立插件发现、动态注册或多 Provider 状态机。

## 稳定接口

```python
capabilities = gateway.resolve(resolve_request)
facts = gateway.fetch(fetch_request)
```

`resolve()` 接收语义指标名、维度名、时期及任务级语义上下文，返回 `resolved_capabilities/1.0`。能力对象包含 provider/source 身份与版本、名称 bindings、按指标隔离的维度 bindings、指标/维度业务元信息、联合可用性及必要的逻辑候选摘要。它禁止包含 token、sheet ID、行列、单元格范围或指标块坐标；完整候选坐标只留在 Provider 内部。

能力对象可包含 `task_resolutions` 和 `task_metric_dimension_bindings`。它们是按 `task_id` 投影的权威解析结果，包含任务级绑定、指标状态、组合回退结果和确认 cases。根级 `metric_bindings` 仅表示所有任务结论一致的兼容绑定；任务之间结论不一致时不得使用根级字段覆盖任务投影。请求上下文可包含当前任务实际关联的 `composition_intents`，Gateway 只解析这些组合的叶子，不展开整个注册表。

`fetch()` 接收 provider-neutral 事实需求和 runner 注入的 `source_binding/1.0`：

```json
{
  "schema_version": "source_binding/1.0",
  "provider_id": "feishu_competitor",
  "source_id": "competitor_macro_sheet",
  "config_hash": "...",
  "revision": 448,
  "schema_hash": "...",
  "freshness": "live",
  "resolution_policy_hash": "...",
  "resolution_engine_version": "2.0.0"
}
```

Gateway 必须拒绝与本次 resolve 不一致的 binding。config、revision、schema、resolution policy 或 resolution engine 变化都会改变请求 hash，旧 checkpoint 不得误复用。

## 固定快照与失败策略

Feishu Gateway 在 `resolve()` 中刷新并固定一次物理索引、client、cache status 和 index path；`fetch()` 必须复用它，不能再次调用索引刷新。正常热路径调用预算为：resolve 一次 revision 检查；fetch 每个实际 sheet/grain 一次矩形批量读取，再做一次最终 revision 检查；bundle 中所有 task 共享同一 binding 和取数。

事实读取期间出现 `concurrent_modification` 时本轮 fail closed，不在 runner 内部重新 prepare/compile。重新运行统一入口会从新 revision 完整 resolve，避免能力决策和事实来自不同快照。

## 产物、上下文与适配

`resolved-capabilities.json` 每个 run 只保存一份，属于诊断产物。其他产物只保存 binding 或 hash，不复制完整能力对象。正常分析仍只读取顶层 `answer-payload.json`，不要把能力、配置、计划或 facts 加入模型上下文。

更换取数实现时保持 `resolved_capabilities/1.0`、`source_binding/1.0` 和 `scene_facts/2.0`，新增 Gateway 实现并替换 runner 组装点。Provider 内部负责认证、物理元信息、坐标解析、批量读取和 revision 一致性；准备、编译、计算、归因和最终输出不感知物理源。精确匹配不生成候选包；仅非标准路径输出最多三个候选，因此不增加飞书调用，正常路径上下文保持不变。
