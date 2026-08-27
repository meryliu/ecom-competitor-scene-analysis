# Data Gateway 契约

`DataGateway` 是分析编排与具体数据源之间唯一的运行时边界。第一阶段只提供 Feishu 实现，不建立插件发现、动态注册或多 Provider 状态机。

## 稳定接口

```python
capabilities = gateway.resolve(resolve_request)
facts = gateway.fetch(fetch_request)
```

`resolve()` 接收语义指标名、维度名、时期及任务级语义上下文，返回 `resolved_capabilities/1.0`。能力对象包含 provider/source 身份与版本、名称 bindings、按指标隔离的维度 bindings、指标/维度业务元信息、联合可用性及必要的逻辑候选摘要。它禁止包含 token、sheet ID、行列、单元格范围或指标块坐标；完整候选坐标只留在 Provider 内部。

候选解析沿用 legacy-first。只有约束候选无可行 binding 且拒绝证据包含 `core_semantics_below_floor` 时，才用当前请求文本做一次本地核心语义回退；成功 legacy binding 不变，回退不发起额外 Provider/飞书请求，回退失败仍返回原确认/阻断状态。

能力对象可包含 `task_resolutions`、`requirement_bindings` 和 `task_metric_dimension_bindings`。它们是按 `task_id` 投影的权威解析结果，包含任务级核心指标绑定、需求级源侧派生或维度约束 binding、指标状态、组合回退结果和确认 cases。`metric_constraints`、局部 `semantic_text`、`derived_metric_id` 和 `resolution_intent` 随 consumer 进入 resolve；每个 consumer 的时期、拆解维度、约束和指标对象是本 Requirement 的硬门，不得先合并为共享指标条件。根级 `required_periods`、`required_dimensions` 和 `required_breakdown_dimensions` 只用于兼容和诊断。

候选在同一固定索引上完成多通道元信息召回、核心语义、指标对象/单位 provenance、维度枚举、派生履约及结构粒度判断，不读取坐标、事实块或单元格，也不产生候选级远端调用。标准名和源别名都属于核心语义证据；当拆分维度已由同一索引证明时，`breakdown_scoped` 核心证据可隔离名称中的维度标签，但不能绕过核心门槛。`primary` 和降低一层的 `compatible_alternative` 进入同一 Requirement 排序；模型推断对象的模糊“表现”可使用唯一兼容增长事实，完整主事实仍优先，权威对象声明不放宽。候选摘要最多保留三个逻辑候选；`confidence` 是结构化完整语义置信度，`lexical_confidence` 是原始名称分，`match_evidence` 只保留命中通道和各正交轴的紧凑状态，不复制完整源索引。实际事实块、时期覆盖、物理选择器和安全 AST 由 Prepare 校验并物化。根级 `metric_bindings` 仅表示所有 Requirement 选择同一核心物理指标时的兼容摘要；Requirement 局部 binding 不得覆盖共享逻辑指标。请求上下文可包含当前任务实际关联的 `composition_intents`，Gateway 只递归检查这些组合的叶子，不展开整个注册表。

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
  "business_intent_policy_hash": "...",
  "resolution_engine_version": "2.13.0",
  "fact_provider_version": "1.1.0"
}
```

Gateway 必须拒绝与本次 resolve 不一致的 binding。config、revision、schema、resolution policy、business intent policy、resolution engine 或 fact provider 版本变化都会改变请求 hash，旧 checkpoint 不得误复用。Provider 版本只标识取数与源单元格规范化语义，不进入 Intent Resolution 候选判断。

## 固定快照与失败策略

Feishu Gateway 在 `resolve()` 中刷新并固定一次物理索引、client、cache status 和 index path；`fetch()` 必须复用它，不能再次调用索引刷新。正常热路径调用预算为：resolve 一次 revision 检查；fetch 每个实际 sheet/grain 一次矩形批量读取，再做一次最终 revision 检查；bundle 中所有 task 共享同一 binding 和取数。

事实读取期间出现 `concurrent_modification` 时本轮 fail closed，不在 runner 内部重新 prepare/compile。重新运行统一入口会从新 revision 完整 resolve，避免能力决策和事实来自不同快照。

## 产物、上下文与适配

`resolved-capabilities.json` 每个 run 只保存一份，属于诊断产物。其他产物只保存 binding 或 hash，不复制完整能力对象。正常分析仍只读取顶层 `answer-payload.json`，不要把能力、配置、计划或 facts 加入模型上下文。

更换取数实现时保持 `resolved_capabilities/1.0`、`source_binding/1.0` 和 `scene_facts/2.0`，新增 Gateway 实现并替换 runner 组装点。Provider 内部负责认证、物理元信息、坐标解析、批量读取和 revision 一致性；准备、编译、计算、归因和最终输出不感知物理源。精确单义请求不生成业务意图候选包；复合语义和非标准路径分别最多输出三个逻辑候选。完整失败证据只保存在 `resolved-capabilities.json`，顶层澄清只携带有界候选，因此不增加飞书调用并控制正常路径上下文。
