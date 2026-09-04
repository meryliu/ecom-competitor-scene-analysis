---
name: ecom-competitor-scene-analysis-compliant
description: WHEN 用户需要从飞书竞品宏观数据源查询、计算或分析电商平台经营指标时使用，覆盖单平台或 TOP6 事实、同比环比、指标组合、用户公式、时期或维度适配，以及用户明确要求的原因、贡献、拉动或拖累归因；平台名或别名（如淘系、京东、拼多多）与 GMV、订单价、结算率、闭环电商佣金等宏观指标共同出现时，即使未写“竞品”也应触发，具体支持范围由实时源表解析；也用于诊断和维护本 Skill 的 Query Policy、分析 IR、指标解析、取数、编译、执行和质量校验链路。DO NOT USE WHEN 用户查询其他数据源、进行不依赖该数据源的行业或竞品定性研究、要求修改飞书源表、创建或发布看板、调用其他 Skill、小策 CLI 或远端 SQL，或要求绕过飞书权限、结构化校验或质量闸门。
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

- **正常分析请求**：用户要查询或分析竞品数据。无论最终档位是 `fast_fact`、`fast_derived`、`standard` 还是 `orchestrated`，都以 [references/analysis-request-contract.md](references/analysis-request-contract.md) 作为唯一人工查询契约；先执行其中的可旁路 Query Policy 增强，再生成 IR 并调用统一 runner。只有命中相应业务语义时才额外读取机器注册表，执行档位由编译器决定。
- **诊断或维护**：用户明确要求定位错误、修改协议/代码/注册定义，或统一 runner 返回结构化失败后需要修复。此时才按“诊断路由”读取详细契约或源码。

正常分析不得预读 `scripts/`、`tests/`、完整执行契约或校验契约。不要通过检查实现代码来确认能力；以编译结果、结构化状态和 runner 产物为准。

## 正常分析

统一 runner 在 Resolve 前通过 `analysis_ir_normalizer.py` 做幂等结构规范化；它只处理时期、角色、factor 类型和多时期组合拆分，不推断业务指标、范围或公式含义。

1. 读取统一分析请求契约。对用户显式独立问题分别保留不可变原始 Query，在 `/workspace/runtime` 的 task 目录运行 `scripts/select_query_policy.py`。`no_match`、`fallback_raw`、超时、命令失败或输出不可解析时，本 task 禁止再次尝试 Policy，直接从原始 Query 生成 IR。

   选择器只接受文件参数。先写入内容为 `{"raw_query":"用户原始 Query"}` 的 JSON 文件，再使用规范命令；不要把 Query 文本直接作为 CLI 参数，也不要猜测 `--query` 等别名：

   ```bash
   python3 scripts/select_query_policy.py \
     --input <raw-query.json> \
     --output <query-policy-packet.json>
   ```
2. `selected` 时只读取返回的有界规则包和 [references/query-understanding/application-contract.md](references/query-understanding/application-contract.md)，在临时语义帧中应用规则并生成候选 IR，再运行 `scripts/validate_query_policy_application.py`。带 `ir_effect_contract` 的已应用 action 必须在 decision 中用 `produced_refs` 绑定其生成的 IR 目标。只有 `commit|commit_pending_confirmation` 才提交校验器返回的 `committed_ir_path`；不得继续使用校验前的候选 IR。`commit_clarification` 询问规则正常产生的业务问题；`fallback_raw` 按 task 丢弃全部默认、展开、assumptions、clarifications 和 action 记录，从原始 Query 生成一次基础 IR。Query Policy 故障不得成为 `blocked`、`waiting_confirmation` 或非零分析状态。
3. 从提交后的语义生成精简 `analysis_ir/1.0`；同一轮多个独立问题生成一个 `analysis_bundle/1.0`。`analysis_task.query` 必须保留原始 Query；规则只补缺失语义，用户明确指标、平台、时期、视角、拆解、口径和输出优先。
4. 只声明用户要求或通过 Query Policy 正常补充的业务指标、时期、范围、派生和归因目标。派生与归因并存时分别保留各自时期角色：归因使用目标内 `periods`，不得为了复用事实而覆盖任务级派生时期。不要为标准指标组合补写基础指标，不要为年/季/月粒度降级手写 `input_adaptations` 或计算 AST；统一 runner 根据源表能力生成这些执行细节。
   5. 统一 runner 在 Provider 调用前先执行任务级业务参数预检和时期协议预检，再执行原有归因 IR 严格校验。预检只使用当前 task 的 Query、结构化 IR 和请求级确认补丁；明确“同比/环比”且时期唯一时可确定性补齐，存在多个会改变结果的时期、场景、公式或拆解维度解释时返回 `waiting_confirmation`，不得从未结构化历史对话补充指标或范围。时期字段不得把 `analysis`、`comparison` 等角色名当作实际时期值，缺失或不可解析时期按 `INVALID_PERIOD` fail-fast；预检不猜测缺失时期。完整参数不改写，非法版本、引用、AST 和冲突补丁仍按原错误码 fail-fast。之后按需求生成有界候选。每个需求分别保留核心指标语义、时间粒度提示、注册派生意图和可选 `metric_constraints`；核心度量词（如订单量、订单价、GMV）不得被当成派生词剥离，时间粒度词不参与派生得分。无约束需求保留既有候选路径；有约束需求联合完整短语、核心指标、维度元信息子集及按需派生假设召回，再以核心语义硬门、约束满足、派生履约、粒度与可加性分层排序，维度或派生证据不能挽救错误核心指标。指标对象仅由模型推断的模糊“表现”需求可召回低一层的兼容增长事实：同核心且满足结构要求的主事实始终优先，只有兼容增长事实唯一可履约时才自动绑定；用户明确规模、金额、增长或比例时不得借此改写对象。完整语义置信度与原始词面分分别记录，完整口径候选不能被词面 TopK 提前丢弃。Resolve 只判断逻辑可得性，不读取物理坐标；完整口径事实优先。单位和指标对象按 provenance 分级：模型推断以及未附 Query 证据的 `user_formula/user_explicit` 只产生 soft conflict，不淘汰精确候选，也不增加排序分；Query 有可追溯证据、注册定义或业务意图规则才可作为强约束。Prepare 以源表元数据规范化实际单位和指标对象，保留声明值、源值和冲突诊断；明确用户约束冲突时进入确认或由 Compile 阻断。Compile/Execution 继续严格校验单位、对象、公式和量级。维度回退先在当前指标支持的物理维度中匹配规范名/别名，再按实时枚举域唯一性绑定；该规则适用于所有维度，多个可行物理维度必须确认，不维护值到维度的静态映射。binding 按需求隔离，不覆盖共享指标。若旧路径没有可行候选且拒绝原因包含核心语义门槛失败，才在同一请求内用当前 Query 提取一次保守核心提示并重评；成功绑定不走回退，不继承历史指标，不增加 Provider 调用。Prepare 再校验实际事实块、时期、维度和值域覆盖，并仅对同指标成员关系、用户显式公式或注册定义生成安全 AST；两个独立指标不得仅凭名称自动相减。其余粒度上卷、组合和源侧派生行为遵循 [references/fact-resolution.md](references/fact-resolution.md)。
   用户明确或 Query Policy 审核默认的源维度全域合计用 Requirement 级 `aggregate_level` 声明，成员由当前 revision 动态物化；模糊范围不得从自由文本 scope 强制聚合。
6. 只运行一次统一分析入口：

```bash
python3 scripts/run_analysis.py \
  --input <analysis-ir-or-bundle.json> \
  --work-dir <run-directory>
```

7. 成功执行后只读取顶层精简 `answer-payload.json` 组织业务答案。任务目录保留完整公式、事实、`fact_capability_plan` 和 canonical selectors，正常成功路径不要读取。
8. 同一输入重跑默认使用 `--resume auto` 复用已校验 facts checkpoint；只有明确要求忽略 checkpoint 时使用 `--fresh`。

编译器根据实际 IR 自动选择四种执行档位；模型不预选档位，也不单独调用 `compile_plan.py`、`run_fetch.py`、`run_fast_query.py` 或 `validate_execution.py`。Provider 直接生成 `scene_facts/2.0`，runner 内部完成编译、需求合并、一次取数、计算和校验。


## NEVER 清单

1. NEVER 调用其他 Skill、子 agent、小策 CLI 或远端 SQL 工具补算竞品结果。
2. NEVER 在缺少飞书授权、源表读取失败或质量闸门未通过时编造数据。
3. NEVER 把缓存、运行产物或正式交付产物写到 `/workspace/runtime` 之外。
4. NEVER 在正常分析路径读取大量中间产物或源码来替代 `answer-payload.json`。
5. NEVER 修改竞品飞书源表、发布看板或执行生产变更。
6. NEVER 因 Query Policy 资源、依赖、应用或校验故障阻断正常分析；必须按 task 原子回退原始 Query。

## 结果处理

- `success`：直接输出通过质量闸门的结果、口径和必要质量说明。
- `partial_success`：输出已保留的有效结果并明确未完成范围；归因残差超阈值时同时保留 rows、summary、残差、warning 和边界信息。若 `model_completion` 给出的成功 facts、唯一公式和校验条件足够，可做低风险补算并披露口径，但不得把节点、任务或顶层状态包装成 `success`。
- `waiting_confirmation`：只针对会改变结果的指标、维度、时期、范围、分母、归因场景、公式或拆解维度候选向用户澄清。业务参数 case 使用当前 Query/IR 的 `context_fingerprint`，旧 case 不得跨请求复用。
- `blocked` 或命令退出码非零：读取 `run-state.json` 定位阶段，再进入诊断路由；不得猜测结果。
- Provider 返回 `resolution_patch` 时按补丁更新 IR 后重跑，保持需求 ID 稳定并复用未变化事实。

结论输出遵循以下通用原则：

- `success` 和 `partial_success` 的最终答案必须以 `answer_basis` 为唯一依据，在末尾稳定输出以下结构；数据口径列出实际引用的逻辑指标、源指标、单位和源口径定义，维度口径只列实际筛选值与拆解维度，计算口径只列关键可读公式，归因方法只列算子名称及已有简介。不得从 Query 或指标名补写口径；`calculations` 或 `attribution` 为空时省略对应行，其他字段缺失时如实写“未提供”。

  ```markdown
  **口径说明**

  - 数据口径：……
  - 维度口径：……
  - 计算口径：……
  - 归因方法：……
  ```

- 归因结果完整展示通过质量闸门的业务结果。除非用户明确只要摘要，不得只输出相对贡献率；同时输出结果中有效的周期值、变化表现、绝对贡献、相对贡献、贡献方向，以及必要的整体摘要和边界说明。完整展示不等于额外扩展 Query 未要求的时期或计算。
- 最终答案优先按指标组织。同一指标且指标定义、视角、范围或分母域、维度粒度、父级边界、时期语义和单位兼容的事实、派生与归因结果尽量放在同一表格；不能安全合并时分开并说明口径差异。
- 每个数值必须有明确单位，可放在列名、表名或紧邻表格的口径说明中。单位未解析时明确披露，不得根据指标名称推测；百分比、百分点和指标原始单位不得混用。标准数值契约中 `%` 和 `pp` 均使用声明单位下的数值，例如 `28.1%` 为 `value=28.1, unit=%`，`+0.6pp` 为 `value=0.6, unit=pp`；不得将它们当作 `0.281%` 或 `0.006pp` 输出。
- 用户公式中的全部显式因子都属于归因输入，包括来源指标、常量和派生子表达式。因子值跨期不变或贡献为 0 时仍按 runner 结果展示，不得在答案组织阶段删除。

只输出通过质量闸门的事实和计算结果。不得编造数据、指标定义、维度层级、聚合资格或归因能力。没有归因要求时不要输出贡献率章节。

## 诊断路由

只读取与失败或维护目标直接相关的材料：

- IR 字段、引用或需求分类：[references/analysis-ir-contract.md](references/analysis-ir-contract.md)
- 指标、维度、维度值或集合解析：[references/fact-resolution.md](references/fact-resolution.md) 与 [references/competitor-source-contract.md](references/competitor-source-contract.md)
- 解析决策阈值或候选策略维护：[references/resolution-policy-registry.json](references/resolution-policy-registry.json)；该文件只描述有限决策逻辑，不登记具体名称映射
- 复合业务意图候选策略维护：[references/business-intent-policy-registry.json](references/business-intent-policy-registry.json)；只维护语义触发、指标对象和名称模板，不登记标准指标映射
- 直接取数、粒度降级或派生回退：[references/source-resolution-policy.md](references/source-resolution-policy.md)
- 指标组合定义维护：[references/metric-composition-specs.md](references/metric-composition-specs.md) 及 [references/metric-composition-registry.json](references/metric-composition-registry.json)
- 通用派生定义维护：[references/derived-metric-specs.md](references/derived-metric-specs.md) 及 [references/derived-metric-registry.json](references/derived-metric-registry.json)
- DAG、输入适配或节点执行：[references/executor-contract.md](references/executor-contract.md)
- 取数、checkpoint 或 runner 阶段：[references/execution-contract.md](references/execution-contract.md)
- 取数实现替换或 Gateway 边界：[references/data-gateway-contract.md](references/data-gateway-contract.md)
- 数据源 URL、Sheet 角色或 stale 策略维护：[references/data-sources/competitor-macro/source-guide.md](references/data-sources/competitor-macro/source-guide.md)
- 计划或最终质量错误：[references/validation-contract.md](references/validation-contract.md)
- 归因内核升级：[references/attribution-engine-integration.md](references/attribution-engine-integration.md)
- Query Policy 规则、依赖、应用或回退：[references/query-understanding/application-contract.md](references/query-understanding/application-contract.md)
- 飞书 Query Policy 高频更新、编译和 Review：[references/query-understanding/maintenance-sop.md](references/query-understanding/maintenance-sop.md)

先看顶层结果、`run-state.json` 和结构化错误，再读取对应契约；只有契约和产物无法解释问题，或用户明确要求修改实现时，才检查相关脚本和测试。源码按组件定向读取，不遍历整个项目。

## 业务定义边界

以下文件是唯一维护来源，不在 `SKILL.md` 或查询契约复制定义：

- 命名集合：[references/dimension-set-registry.json](references/dimension-set-registry.json)
- 非标准名称与源结构的解析决策逻辑：[references/resolution-policy-registry.json](references/resolution-policy-registry.json)
- 复合语义到可执行业务意图的有限展开：[references/business-intent-policy-registry.json](references/business-intent-policy-registry.json)
- Query 理解业务补充规则：[references/query-understanding/policy-manifest.json](references/query-understanding/policy-manifest.json) 与 `references/query-understanding/rules/`
- 指标组合：[references/metric-composition-registry.json](references/metric-composition-registry.json)
- 派生指标：[references/derived-metric-registry.json](references/derived-metric-registry.json)
- 数据源连接与 Sheet 角色：[references/data-sources/competitor-macro/source-config.json](references/data-sources/competitor-macro/source-config.json)

指标、物理维度、别名、单位、定义和可聚合性只使用源表元信息，Skill 不维护第二份副本。`TOP6平台` 等源表范围维度直接使用其实时成员，不在命名集合注册表维护同义逻辑集合。归因只在 Query 明确要求原因、贡献、拉动或拖累量化时创建，并与派生需求独立。

`waiting_confirmation` 时只读取顶层 `resolution_cases`。Provider case 把用户选中的 `candidate_id` 连同 `case_id`、source revision、schema/policy hash、引擎版本及适用的语义/组合指纹写入对应任务 IR 的 `resolution_patches`；`kind=business_parameter` 的 case 写入 `case_id`、`candidate_id` 和 `context_fingerprint`，候选要求自由值时再写 `value`。补丁只对该请求生效并保持需求 ID 稳定；不得写入策略注册表、集合注册表或共享结构索引。Gateway 返回的任务级 capability 是 Bundle 中同名指标的权威绑定；组合叶子 case 只有在 prepare 确认直接路径不可用后才从 deferred 激活。
