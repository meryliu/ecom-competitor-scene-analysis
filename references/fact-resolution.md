# 事实与元信息解析

竞品表是手工维护的原始表，指标名、维度名、Sheet、指标块和周期列可能有轻微变化。解析由本 Skill 内嵌的竞品 Provider 完成，不调用其他 Skill，不把自然语言或 Markdown 当作事实中间格式。

## 映射发生位置

1. Gateway resolve 对复合 Query 按 `business-intent-policy-registry.json` 生成有限原始假设；策略只提供语义模板，不提供标准名称映射。语义归一化分别记录比较词、派生度量词、核心度量词和时间粒度词：订单量、订单价、GMV 保留在核心语义；日度、周度、月度、季度、年度只形成粒度提示，不增加派生得分。
2. 无约束需求保持既有召回路径。有 `metric_constraints` 的需求合并完整局部短语、核心指标、维度元信息子集及按需派生假设召回，按规范名去重后统一评价；完整口径候选不能被词面 TopK 提前丢弃。明确要求拆分，或原始路径没有候选时，可使用当前 Requirement 的 `semantic_text/query_fragment` 做一次有界上下文召回；它只扩大召回集合，所有候选仍按原核心语义、权威指标对象/单位、保护语义、结构粒度和维度硬门评价。拆分维度已经由源元信息唯一解析时，核心语义评分可从候选名称或单个别名中剥离已证明的“分/各 + 维度名”和请求侧大盘范围词；该证据只隔离指标核心与拆分标签，不能剥离任意低分词或挽救错误核心。业务意图假设携带 `semantic_role`：`primary` 候选位于主语义层；当“表现”的指标对象仅为模型推断时，`compatible_alternative` 可补充同核心增长事实并降低一层，无论是否存在 Breakdown。主事实满足结构要求时始终优先；主事实不可得且兼容增长事实唯一可履约时自动绑定。用户显式、公式、注册定义或源元信息声明的指标对象不进入该兼容展开。候选按 `semantic_tier -> constraint_tier -> derived_tier -> fulfillment_tier -> requires_confirmation -> core -> derived -> dimension evidence -> lexical -> candidate_id` 排序。维度、派生、上下文或粒度证据不能挽救错误核心语义。旧路径没有可行候选且存在 `core_semantics_below_floor` 时，才使用当前 Query/consumer `semantic_text` 做一次请求内核心提示回退；回退必须得到唯一可行 binding，否则保留原确认/阻断结果。粒度、维度歧义/缺失、不可加性和显式对象/单位冲突不由该回退挽救，也不触发远端重取。
3. 源索引构建保留未决物理指标块；请求级 resolver 联合评估“物理块、指标、维度、行域”，不得先独立确定指标再猜维度。业务意图选定后不再重新猜业务语义。
4. prepare/compile 保留逻辑指标和维度，同时生成按指标隔离的 `source_metric_name`、`source_dimension_refs`、`source_selector_dimensions`、`source_dimension_domains` 与 `dimension_projection`。
5. Provider 用物理字段定位事实，并用实时枚举确认维度值、物理维度全域或命名集合。`TOP6平台` 直接走物理维度全域，不经过逻辑集合注册表。
6. `project_scene_facts()` 按 consumer binding 把物理维度投影回逻辑维度，执行器不感知源表名称变化。

## 解析边界

- **唯一匹配**：规范化空格、全半角、常见标点和已登记别名后只有一个候选，直接绑定，并记录匹配证据。
- **可疑匹配**：编辑距离、别名或元信息备注只能提供低置信度候选时，返回候选、证据和影响的 fact slots，请用户澄清。
- **冲突匹配**：多个指标/维度/维度值、重复指标块、周期粒度不一致或官方定义冲突时阻断，不按位置猜测。
- **缺失**：源单元格为空、`-`、`N/A` 等保持 `value=null` 和 `missing=true`，不得补 0。

Query 仅提供维度值或通用逻辑维度 hint 时，最终绑定使用当前结构索引按以下顺序确定：候选指标支持的物理维度 -> 物理规范名精确匹配 -> 物理别名精确匹配 -> 请求值是实时枚举域子集。过滤后唯一时自动绑定；多个物理维度仍可满足时，为每个“指标 × 物理维度”生成可确认候选；无命中时阻断。该算法对平台、地区、类目及后续新增维度统一生效，不使用静态映射、物理坐标、事实值或名称模糊相似度。证据写入候选的 `dimension_resolution`，包括物理维度、方法和值。

当前 Query 是本次指标语义的权威上下文。正常候选绑定成功后不因其他上下文改写；仅当约束候选全部因 `core_semantics_below_floor` 被拒绝时，resolver 才可在同一请求内用当前 Query/consumer `semantic_text` 提取一次保守核心提示重评。该回退最多一次、复用内存索引且不调用 Provider；没有唯一可行结果时保留原确认/阻断。没有明确引用词时不得从历史指标继承核心语义。

## Provider 输入和输出

编译器生成结构化逻辑 `fact_slots`，再把物理身份兼容的槽位合并为 `fact_demands`，原 task、时期角色、视角和选择器保留为 `consumer_bindings`。Gateway resolve 首轮刷新共享结构索引并输出不含坐标的逻辑能力；fetch 复用同一固定索引，批量读取唯一物理事实并输出 `scene_facts/2.0`。一次请求只读基础指标；组合指标和通用派生在本地执行器中计算。

结构索引至少缓存：源 revision、schema hash、指标和维度规范名、别名证据、Sheet/指标块坐标、周期粒度、匹配置信度和告警。即使某张表只有未决块，也保留已识别的粒度、周期行和周期列。共享缓存只保存原始索引；请求级 overlay 和 `resolution_patches` 不写回缓存。

## 容错和确认

表格插入行列、Sheet 改名、指标块移动、周期新增以及大小写/空格/标点变化不应破坏坐标解析。名称近似但定义、单位、范围或维度不一致时不自动合并。若源表新增未知指标或表块，先按 [resolution-policy-registry.json](resolution-policy-registry.json) 的有限决策逻辑生成最多三个联合候选。唯一候选满足硬门槛和强证据时自动绑定；有可行候选但不足以自动决定时仅暂停受影响任务；无可行候选时阻断该任务。其他任务继续运行。

只有会改变结果的歧义才提问，问题必须列出候选名称、元信息证据、置信度和受影响的指标/周期/维度。澄清后通过 IR 的 `resolution_patches` 更新并重编译。patch 必须匹配 case ID、候选 ID、source revision、schema hash 和 policy hash；任一变化均视为 stale，重新生成候选。用户确认不得沉淀为全局别名或规则。

决策注册表不是名称映射枚举。它只能使用实现白名单中的 hard gates、strong evidence、阈值和候选上限；模型推断本身不能单独触发自动绑定。

约束候选优先级为：完全满足口径的现成事实、基础指标加已确认维度成员、可加成员集合、同指标全域减同指标排除成员、已注册组合/派生。Resolve 只根据名称、别名、维度及枚举、结构粒度和可加性证明逻辑路径，不检查坐标、事实块或具体单元格；Prepare 才验证这些物理条件并生成 `source_scoped_fact`、成员选择、成员求和或同指标排除 AST。两个独立指标（例如总量指标和名称相似的某平台独立指标）没有结构化关系时不得自动生成减法。

`eq|in|exclude` 使用同一通用约束签名比较器。完整口径证据必须由同一个规范名或单个别名同时覆盖全部 AND 约束和指标核心，不能把规范名中的约束证据与另一别名中的核心证据拼接；排除约束还必须同时命中排除算子和值。从该标签剥离结构化约束值、约束算子和既有非核心词后，若候选核心等于请求核心，则为 `exact`；若候选核心严格包含请求核心且仍有非空残余，则为 `overqualified`。该判断不维护 3P、自营、含税等业务限定词表。`overqualified` 完整口径仍是可行的 `source_scoped_fact`，但降至 `semantic_tier=1` 并要求确认：存在 tier 0 的基础指标维度选择路径时由既有排序自动胜出，仅有该候选时进入确认而非阻断；用户明确请求残余限定，或同一个精确元信息别名证明等价时，仍按 `exact` 自动绑定。非包含关系的既有模糊匹配逻辑保持不变。候选 `confidence` 表示核心与全部必需约束的瓶颈置信度，`lexical_confidence` 保留旧名称匹配分，`match_evidence` 紧凑记录各召回通道、核心关系及残余、约束、派生与粒度状态。源侧预计算派生只绑定对应派生 requirement；没有唯一源侧派生时使用注册本地派生。
`metric_constraint` case 还携带有界 `rejected_candidates`，包括候选名称、核心分/门槛、结构冲突和约束证据，便于解释阻断原因而不改变候选排序。

单位与指标对象按 provenance 处理：`model_inferred` 不作硬过滤且不获得排序收益，冲突作为软诊断交给 Prepare 用源元信息纠正；用户显式、用户公式、注册定义和源元信息声明仍是硬约束。Compile、Execution 和事实质量闸门不放宽单位一致性与量级校验。维度唯一域推断复用当前请求的内存索引和规范化枚举集合，不增加 Provider 调用；公开候选和拒绝证据继续受策略上限约束，不把完整源索引放入模型上下文。

业务意图注册表同样不得登记标准指标或维度映射，只能维护需求级派生触发、指标对象、名称模板、语义角色、优先级和候选上限。`compatible_alternative` 不是 Query 改写，也不能覆盖 `primary`，只把同一 Requirement 的可兼容履约形态送入统一候选排序。完整 Query 只在没有结构化 consumer 的兼容路径使用；有 consumer 时仅使用其 `derived_metric_id` 或局部语义片段。规则与 source revision、schema hash、resolution policy hash 一同进入审计指纹；策略变更后旧确认补丁失效。普通事实和派生可参与意图展开，归因目标、用户公式和注册组合叶子不允许被自动改写指标对象。

Requirement 履约使用一个统一候选序列，候选类型包括 `direct_fact`、`member_selector`、`set_aggregate`、`registered_composition`、`registered_derived` 和显式允许的 `safe_inference`。排序保持“完整直接事实 > 同指标安全选择/聚合 > 注册组合/派生 > 安全推断”。注册组合先按逻辑输出命中，再解析叶子；叶子不会拿组合输出名称重新评分。直接事实歧义仍须确认，不能用组合绕过；组合叶子确认只在组合成为最高可行路径后激活。该统一表示不放宽粒度、维度、可加性、单位、缺失值或执行质量校验。

`aggregate_level` 在统一序列中只追加一个 `source_dimension_all_sum` 路径，并继续归类为 `set_aggregate`。Resolve 从指标支持维度的规范名/别名中解析唯一物理维度，校验可加性和当前事实块的全域覆盖；完整范围直接事实仍优先。候选失败只记录在本 Requirement，不得删除或降权其他可行候选。Prepare 仅对最终选中路径按当前 source revision 物化 `SetSpec`，并复用既有 `sum` AST；成员列表不进入 IR 或模型上下文。

动态集合在 Prepare 中物化为 provider-neutral `SetSpec`：物理维度、成员类型、实时成员、source revision、消费意图和稳定指纹。实现不限定平台，可处理任意具备实时枚举的维度；高基数集合受上限保护。可加指标复用现有 `input_adaptations + sum/subtract`，比率必须聚合注册的分子/分母后重算；没有注册公式的非可加指标不自动集合聚合。集合成员改变或 source revision 变化时指纹变化，旧物化结果不得复用。

## 实时性

resolve 检查 revision，fetch 读取后再次检查。两次不一致时返回 `concurrent_modification` 并终止本轮，避免在旧计划中混入新能力；重新运行会完整 resolve。允许 stale 时必须显式标记 `freshness=stale`，并且所需坐标已在缓存中；默认不使用 stale。
