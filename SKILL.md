---
name: ecom-competitor-scene-analysis-compliant
description: WHEN 用户需要基于竞品飞书宏观表格做竞品事实查询、指标组合、派生指标、时期适配、TOP6平台拆解或可选归因分析时使用；触发关键词：竞品数据、宏观竞品、TOP6平台、竞品指标、竞品归因。DO NOT USE WHEN 用户要求修改源表、创建/发布看板、调用其他 Skill/小策 CLI、查询非竞品宏观数据或绕过飞书权限。
metadata:
  version: 1.2.0
  source_skill: ecom-competitor-scene-analysis
  compliance: das-databp
---
# 竞品场景分析（DAS 合规版）

本 Skill 直接从既有飞书源表生成标准 `facts`，再由本地编译器和执行器完成计算。统一 runner 在取数前规范化时期，并基于实时结构索引和指标元信息（`可支持时间粒度`、`可支持拆解维度`、`聚合方式`）一次决定直接事实、细粒度聚合或注册派生路径；不经过竞品 `query.py` 的非标准结果转换，不调用其他 Skill 或小策 CLI。指标元信息中的 `指标公式`、`使用说明` 不参与事实能力判断或自动口径推断；用户 Query 明确给出的归因公式仍按归因契约执行。


## DAS 合规运行约束

- 使用 DAS 内置 `execute` 调用 `scripts/run_analysis.py`，不要调用其他 Skill、子 agent、小策 CLI 或远端 SQL 工具。
- 所有输入、运行目录、缓存和最终产物必须放在 `/workspace/runtime` 下；建议把 `ECOM_COMPETITOR_SCENE_CACHE_DIR` 设置为 `/workspace/runtime/ecom-competitor-scene-cache`。
- 若缺少飞书授权或 `lark-cli` 无法读取源表，停止并按平台授权流程处理，不得伪造或回填竞品数据。
- 正常分析只读取 `answer-payload.json` 组织业务答案；诊断时才读取 `run-state.json` 与对应 reference。

## 请求路由

先区分任务类型，不要把业务复杂度当成文档加载条件：

- **正常分析请求**：用户要查询或分析竞品数据。无论最终档位是 `fast_fact`、`fast_derived`、`standard` 还是 `orchestrated`，都以 [references/analysis-request-contract.md](references/analysis-request-contract.md) 作为唯一人工查询契约，生成 IR 并调用统一 runner。只有命中相应业务语义时才额外读取机器注册表，执行档位由编译器决定。
- **诊断或维护**：用户明确要求定位错误、修改协议/代码/注册定义，或统一 runner 返回结构化失败后需要修复。此时才按“诊断路由”读取详细契约或源码。

正常分析不得预读 `scripts/`、`tests/`、完整执行契约或校验契约。不要通过检查实现代码来确认能力；以编译结果、结构化状态和 runner 产物为准。

## 正常分析

1. 读取统一分析请求契约，从 Query 生成精简 `analysis_ir/1.0`；同一轮多个独立问题生成一个 `analysis_bundle/1.0`。
2. 只声明用户要求的业务指标、时期、范围、派生和归因目标。不要为标准指标组合补写基础指标，不要为年/季/月粒度降级手写 `input_adaptations` 或计算 AST；统一 runner 根据源表能力生成这些执行细节。
3. 统一 runner 在编译前按 Query 级事实能力规划生成 canonical selector。复合语义先按 [references/business-intent-policy-registry.json](references/business-intent-policy-registry.json) 生成最多三个原始意图假设，再用同一份实时结构索引联合校验名称、指标对象、元信息粒度/维度/聚合方式、实际事实块和周期；候选的词面分仅用于召回，保护词按 [references/resolution-policy-registry.json](references/resolution-policy-registry.json) 归一化为比较方式和指标对象，只有完整可执行候选参与自动选择或一次性澄清。规划顺序固定为：规范化任务过滤并继承到所有事实叶子 -> 有界意图假设 -> 指标/维度绑定 -> 元信息能力校验 -> 实际事实块和周期校验 -> `direct_fact` -> 安全 `aggregate_fact` -> 用户明确公式的目标 `formula_computed`。Compile 只消费唯一规划结果，不重新猜指标、粒度、维度或路径；指标因子按普通事实取数，归因目标源事实优先。任务过滤与局部维度冲突时阻断；自动聚合按规范化物理时期复用内部角色；公式目标回退只在乘除公式、直接事实因子和单位量级均可证明时生成显式换算契约。
4. 只运行一次统一入口：

```bash
python3 scripts/run_analysis.py \
  --input <analysis-ir-or-bundle.json> \
  --work-dir <run-directory>
```

5. 成功执行后只读取顶层精简 `answer-payload.json` 组织业务答案。任务目录保留完整公式、事实、`fact_capability_plan` 和 canonical selectors，正常成功路径不要读取。
6. 同一输入重跑默认使用 `--resume auto` 复用已校验 facts checkpoint；只有明确要求忽略 checkpoint 时使用 `--fresh`。

编译器根据实际 IR 自动选择四种执行档位；模型不预选档位，也不单独调用 `compile_plan.py`、`run_fetch.py`、`run_fast_query.py` 或 `validate_execution.py`。Provider 直接生成 `scene_facts/2.0`，runner 内部完成编译、需求合并、一次取数、计算和校验。


## NEVER 清单

1. NEVER 调用其他 Skill、子 agent、小策 CLI 或远端 SQL 工具补算竞品结果。
2. NEVER 在缺少飞书授权、源表读取失败或质量闸门未通过时编造数据。
3. NEVER 把缓存、运行产物或正式交付产物写到 `/workspace/runtime` 之外。
4. NEVER 在正常分析路径读取大量中间产物或源码来替代 `answer-payload.json`。
5. NEVER 修改竞品飞书源表、发布看板或执行生产变更。

## 结果处理

- `success`：直接输出通过质量闸门的结果、口径和必要质量说明。
- `partial_success`：输出已保留的有效结果并明确未完成范围；归因残差超阈值时同时保留 rows、summary、残差、warning 和边界信息。若 `model_completion` 给出的成功 facts、唯一公式和校验条件足够，可做低风险补算并披露口径，但不得把节点、任务或顶层状态包装成 `success`。
- `waiting_confirmation`：只针对会改变结果的指标、维度、时期、范围、分母或公式候选向用户澄清。
- `blocked` 或命令退出码非零：读取 `run-state.json` 定位阶段，再进入诊断路由；不得猜测结果。
- Provider 返回 `resolution_patch` 时按补丁更新 IR 后重跑，保持需求 ID 稳定并复用未变化事实。

结论输出遵循以下通用原则：

- 归因结果完整展示通过质量闸门的业务结果。除非用户明确只要摘要，不得只输出相对贡献率；同时输出结果中有效的周期值、变化表现、绝对贡献、相对贡献、贡献方向，以及必要的整体摘要和边界说明。完整展示不等于额外扩展 Query 未要求的时期或计算。
- 最终答案优先按指标组织。同一指标且指标定义、视角、范围或分母域、维度粒度、父级边界、时期语义和单位兼容的事实、派生与归因结果尽量放在同一表格；不能安全合并时分开并说明口径差异。
- 每个数值必须有明确单位，可放在列名、表名或紧邻表格的口径说明中。单位未解析时明确披露，不得根据指标名称推测；百分比、百分点和指标原始单位不得混用。
- 用户公式中的全部显式因子都属于归因输入，包括来源指标、常量和派生子表达式。因子值跨期不变或贡献为 0 时仍按 runner 结果展示，不得在答案组织阶段删除。

只输出通过质量闸门的事实和计算结果。不得编造数据、指标定义、维度层级、聚合资格或归因能力。没有归因要求时不要输出贡献率章节。

## 诊断路由

只读取与失败或维护目标直接相关的材料：

- IR 字段、引用或需求分类：[references/analysis-ir-contract.md](references/analysis-ir-contract.md)
- 指标、维度、维度值或集合解析：[references/fact-resolution.md](references/fact-resolution.md) 与 [references/competitor-source-contract.md](references/competitor-source-contract.md)
- 解析决策阈值或候选策略维护：[references/resolution-policy-registry.json](references/resolution-policy-registry.json)；该文件只描述有限决策逻辑，不登记具体名称映射
- 复合业务意图候选策略维护：[references/business-intent-policy-registry.json](references/business-intent-policy-registry.json)；只维护语义触发、指标对象和名称模板，不登记标准指标映射
- 直接取数、粒度降级或派生回退：[references/source-resolution-policy.md](references/source-resolution-policy.md)
- 派生或指标组合定义维护：[references/derived-metric-specs.md](references/derived-metric-specs.md) 及对应机器注册表
- DAG、输入适配或节点执行：[references/executor-contract.md](references/executor-contract.md)
- 取数、checkpoint 或 runner 阶段：[references/execution-contract.md](references/execution-contract.md)
- 取数实现替换或 Gateway 边界：[references/data-gateway-contract.md](references/data-gateway-contract.md)
- 数据源 URL、Sheet 角色或 stale 策略维护：[references/data-sources/competitor-macro/source-guide.md](references/data-sources/competitor-macro/source-guide.md)
- 计划或最终质量错误：[references/validation-contract.md](references/validation-contract.md)
- 归因内核升级：[references/attribution-engine-integration.md](references/attribution-engine-integration.md)

先看顶层结果、`run-state.json` 和结构化错误，再读取对应契约；只有契约和产物无法解释问题，或用户明确要求修改实现时，才检查相关脚本和测试。源码按组件定向读取，不遍历整个项目。

## 业务定义边界

以下文件是唯一维护来源，不在 `SKILL.md` 或查询契约复制定义：

- 命名集合：[references/dimension-set-registry.json](references/dimension-set-registry.json)
- 非标准名称与源结构的解析决策逻辑：[references/resolution-policy-registry.json](references/resolution-policy-registry.json)
- 复合语义到可执行业务意图的有限展开：[references/business-intent-policy-registry.json](references/business-intent-policy-registry.json)
- 指标组合：[references/metric-composition-registry.json](references/metric-composition-registry.json)
- 派生指标：[references/derived-metric-registry.json](references/derived-metric-registry.json)
- 数据源连接与 Sheet 角色：[references/data-sources/competitor-macro/source-config.json](references/data-sources/competitor-macro/source-config.json)

指标、物理维度、别名、单位、定义和可聚合性只使用源表元信息，Skill 不维护第二份副本。`TOP6平台` 等源表范围维度直接使用其实时成员，不在命名集合注册表维护同义逻辑集合。归因只在 Query 明确要求原因、贡献、拉动或拖累量化时创建，并与派生需求独立。

`waiting_confirmation` 时只读取顶层 `resolution_cases`，把用户选中的 `candidate_id` 连同 case 中的 `case_id`、`source_revision`、`schema_hash`、`resolution_policy_hash`、`resolution_engine_version` 及适用的业务意图 policy hash、语义/组合指纹写入对应任务 IR 的 `resolution_patches` 后重跑。补丁只对该请求生效；不得写入策略注册表、集合注册表或共享结构索引。Gateway 返回的任务级 capability 是 Bundle 中同名指标的权威绑定；组合叶子 case 只有在 prepare 确认直接路径不可用后才从 deferred 激活。
