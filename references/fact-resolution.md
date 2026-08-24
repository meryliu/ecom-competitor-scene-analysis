# 事实与元信息解析

竞品表是手工维护的原始表，指标名、维度名、Sheet、指标块和周期列可能有轻微变化。解析由本 Skill 内嵌的竞品 Provider 完成，不调用其他 Skill，不把自然语言或 Markdown 当作事实中间格式。

## 映射发生位置

1. Gateway resolve 对复合 Query 按 `business-intent-policy-registry.json` 生成有限原始假设；策略只提供语义模板，不提供标准名称映射。
2. 同一次 resolve 将核心指标语义与派生语义分开评分，并用指标元信息做轻量结构粒度判断。结构不可执行候选只留在诊断拒绝信息，不进入自动选择或澄清；实际事实块与时期覆盖由 prepare 校验。
3. 源索引构建保留未决物理指标块；请求级 resolver 联合评估“物理块、指标、维度、行域”，不得先独立确定指标再猜维度。业务意图选定后不再重新猜业务语义。
4. prepare/compile 保留逻辑指标和维度，同时生成按指标隔离的 `source_metric_name`、`source_dimension_refs`、`source_selector_dimensions`、`source_dimension_domains` 与 `dimension_projection`。
5. Provider 用物理字段定位事实，并用实时枚举确认维度值、物理维度全域或命名集合。`TOP6平台` 直接走物理维度全域，不经过逻辑集合注册表。
6. `project_scene_facts()` 按 consumer binding 把物理维度投影回逻辑维度，执行器不感知源表名称变化。

## 解析边界

- **唯一匹配**：规范化空格、全半角、常见标点和已登记别名后只有一个候选，直接绑定，并记录匹配证据。
- **可疑匹配**：编辑距离、别名或元信息备注只能提供低置信度候选时，返回候选、证据和影响的 fact slots，请用户澄清。
- **冲突匹配**：多个指标/维度/维度值、重复指标块、周期粒度不一致或官方定义冲突时阻断，不按位置猜测。
- **缺失**：源单元格为空、`-`、`N/A` 等保持 `value=null` 和 `missing=true`，不得补 0。

Query 仅提供维度值时，模型可以给出维度名候选，但最终绑定必须由 Provider 使用当前结构索引反向确认。单值只命中一个维度时自动确认；命中多个维度时返回候选；唯一命中与候选维度不同时返回 `resolution_patch`。解析证据写入 `dimension_resolutions`，不为单个业务值维护静态归属表。

## Provider 输入和输出

编译器生成结构化逻辑 `fact_slots`，再把物理身份兼容的槽位合并为 `fact_demands`，原 task、时期角色、视角和选择器保留为 `consumer_bindings`。Gateway resolve 首轮刷新共享结构索引并输出不含坐标的逻辑能力；fetch 复用同一固定索引，批量读取唯一物理事实并输出 `scene_facts/2.0`。一次请求只读基础指标；组合指标和通用派生在本地执行器中计算。

结构索引至少缓存：源 revision、schema hash、指标和维度规范名、别名证据、Sheet/指标块坐标、周期粒度、匹配置信度和告警。即使某张表只有未决块，也保留已识别的粒度、周期行和周期列。共享缓存只保存原始索引；请求级 overlay 和 `resolution_patches` 不写回缓存。

## 容错和确认

表格插入行列、Sheet 改名、指标块移动、周期新增以及大小写/空格/标点变化不应破坏坐标解析。名称近似但定义、单位、范围或维度不一致时不自动合并。若源表新增未知指标或表块，先按 [resolution-policy-registry.json](resolution-policy-registry.json) 的有限决策逻辑生成最多三个联合候选。唯一候选满足硬门槛和强证据时自动绑定；有可行候选但不足以自动决定时仅暂停受影响任务；无可行候选时阻断该任务。其他任务继续运行。

只有会改变结果的歧义才提问，问题必须列出候选名称、元信息证据、置信度和受影响的指标/周期/维度。澄清后通过 IR 的 `resolution_patches` 更新并重编译。patch 必须匹配 case ID、候选 ID、source revision、schema hash 和 policy hash；任一变化均视为 stale，重新生成候选。用户确认不得沉淀为全局别名或规则。

决策注册表不是名称映射枚举。它只能使用实现白名单中的 hard gates、strong evidence、阈值和候选上限；模型推断本身不能单独触发自动绑定。

业务意图注册表同样不得登记标准指标或维度映射，只能维护需求级派生触发、指标对象、名称模板、优先级和候选上限。完整 Query 只在没有结构化 consumer 的兼容路径使用；有 consumer 时仅使用其 `derived_metric_id` 或局部语义片段。规则与 source revision、schema hash、resolution policy hash 一同进入审计指纹；策略变更后旧确认补丁失效。普通事实和派生可参与意图展开，归因目标、用户公式和注册组合叶子不允许被自动改写指标对象。

## 实时性

resolve 检查 revision，fetch 读取后再次检查。两次不一致时返回 `concurrent_modification` 并终止本轮，避免在旧计划中混入新能力；重新运行会完整 resolve。允许 stale 时必须显式标记 `freshness=stale`，并且所需坐标已在缓存中；默认不使用 stale。
