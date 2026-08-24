# 完整执行契约

本文件定义竞品场景分析从计划、飞书取数到确定性计算和交付的运行协议。计划字段见 [output-contract.md](output-contract.md)，源表和 Provider 细节见 [competitor-source-contract.md](competitor-source-contract.md)。

## 固定组件

- `scripts/compile_plan.py`：把 `analysis_ir/1.0` 编译成可校验计划、事实槽位和唯一 Provider 请求。
- `scripts/dimension_domain_registry.py`：读取业务集合注册表，将命名集合解析为实时源表中的具体维度成员。
- `scripts/data_gateway.py`：定义 provider-neutral 的 resolve/fetch 与 source binding 契约。
- `scripts/resolution_policy.py`：在 Gateway 内执行请求级联合候选决策和确认补丁校验；不读取事实值、不修改共享索引。
- `scripts/run_analysis.py`：统一能力解析、编译、需求合并、Gateway 取数、checkpoint 恢复、执行和校验；支持单任务及 bundle。
- `scripts/run_fetch.py`：仅用于 Provider 诊断；Provider 直接写出 `scene_facts/2.0`。
- `scripts/execution_runner.py`：消费标准 facts，执行事实绑定、组合指标、注册派生、自定义安全表达式和可选归因。
- `scripts/validate_execution.py`：计划阶段和最终阶段的确定性校验，不调用外部服务。
- `scripts/_vendor/attribution_core`：仅在存在受支持归因目标时懒加载的内嵌归因内核。

不得调用其他 Skill、远端数据查询 CLI、竞品 `query.py` 或临时生成计算脚本。飞书表格仍是手工维护的原始结构；只有 Provider 输出转换为标准 facts，源表不被改写。

## 执行顺序

1. 模型从 Query 生成精简 `analysis_ir/1.0`，完整保留事实观察、指标组合、通用派生、自定义计算和归因目标；目标输入不能直接取得时另建输入适配，不能删除最终需求。
2. 运行统一入口；编译器为每个 task 生成事实槽位，再跨消费者、跨 task 合并兼容物理需求：

   ```bash
   python3 scripts/run_analysis.py \
     --input <analysis-ir-or-bundle.json> \
     --work-dir <run-directory> \
     --resume auto
   ```

3. runner 先校验单任务/bundle 输入协议，再通过 Gateway resolve 一次逻辑能力并固定 source binding。编译器统一传播目标过滤上下文并完成公式因子、维度冲突和 selector grain 校验；每个计划存在 `ERROR` 时不取数。Gateway 在同一 revision 下批量读取唯一物理事实，直接输出 `scene_facts/2.0`。
4. runner 按 bindings 为每个 task 投影逻辑事实，再按 DAG 波次运行事实、输入适配、组合/通用派生、自定义计算和归因。适配结果在波次之间作为共享中间事实注入，下游不重复计算。
5. `run-state.json` 在每个阶段原子落盘。成功 facts checkpoint 后的本地失败自动恢复，不再次取数；真实 fetch attempt 只追加不覆盖。
6. 单独调试执行器时可运行：

   ```bash
   python3 scripts/execution_runner.py \
     --plan <compiled-plan.json> --facts <facts.json> \
     --output <execution-manifest.json> --events <execution-events.jsonl> \
     --storage-mode auto
   ```

7. 结论组织前对照原始 Query 检查结果完整性。节点失败但成功 facts 足以支持唯一、低风险计算时，模型可补足最终答案；不得修改事实、删除失败节点或把模型补足伪装成执行成功。随后对同一 manifest 运行 `validate_execution.py --phase final`。

## 标准 facts

Provider 输出的根对象为：

```json
{
  "schema_version": "scene_facts/2.0",
  "facts": [{
    "fact_id": "fact_abc",
    "metric_ref": "payment_gmv",
    "metric": "支付GMV",
    "period": "2026-05",
    "dimensions": {"TOP6平台": "拼多多"},
    "value": 123.4,
    "unit": "亿元",
    "definition": "元信息中的指标定义",
    "additive": true,
    "aggregation": "源表聚合规则",
    "missing": false,
    "raw_missing": false,
    "normalization_reason": "unchanged",
    "value_derived_from_components": false,
    "source_request_id": "fetch_unified_1",
    "source_ref": {"sheet": "月度表", "row": 12, "column": "H", "revision": 448}
  }],
  "bindings": [{
    "binding_id": "binding_abc",
    "fact_id": "fact_abc",
    "task_id": "query_1",
    "fact_slot_id": "slot_1",
    "period_role": "analysis",
    "view_id": "platform_view",
    "dimension_domain_refs": {"TOP6平台": "domain_abc"}
  }],
  "resolved_dimension_domains": {
    "domain_abc": {"domain_kind": "source_dimension_all", "dimension": "TOP6平台", "members": ["淘系", "抖音", "拼多多", "京东", "快手", "视频号"]}
  },
  "source": {"revision": 448, "schema_hash": "...", "freshness": "live"}
}
```

`value=null` 时必须保持 `missing=true`；不能用 0 或模型估算替代。Provider 只返回基础指标，结算率、同比、占比和归因都由本 Skill 的确定性执行器完成。执行器可以重算缺失标记，但不得抹掉 `raw_missing`。

根对象 `facts[].fact_id` 标识物理事实。按 binding 投影后的执行输入改用 `fact_id=stable_id(physical_fact_id,binding_id)`，并同时保留 `physical_fact_id` 与 `binding_id`。物理去重、消费绑定和逻辑执行身份分别由 Provider、Fact Contract 和 Executor 负责，禁止在 Intent Resolution 或 Executor 中读取源坐标后重新映射。

## 组合与通用派生

组合指标使用 [metric-composition-registry.json](metric-composition-registry.json)，例如结算率固定为同周期、同范围、同维度的 `结算GMV / 支付GMV`。维度域整体计算时先依据飞书元信息聚合可加的基础输入，再执行同一组合公式；Skill 不重复维护原子指标聚合性。`selected_set_share` 的分母通过 `domain_ref` 取得当前 revision 的明确成员，不自动解释为全市场份额。

通用派生使用 [derived-metric-registry.json](derived-metric-registry.json)，包括同比、环比、期间变化和同比增速差值。派生定义自动继承维度域的 `domain_ref` 或分组维度，不为 `TOP6平台` 等物理维度复制公式。同比增速差值使用当前期和去年同期的四个基础事实，输出百分点，不输出相对增长率。

两层可以共享事实但不互相隐式生成：用户未要求同比时不预取去年同期；用户未要求归因时不创建归因事实。

## 失败与确认

- 指标、维度、维度值或周期候选唯一时自动绑定；匹配置信度可疑、候选冲突、官方定义不一致或结构缺块时返回结构化 `clarification`。
- 有可行解析候选但不满足自动门槛时，受影响任务为 `waiting_confirmation` 且不编译、不取数；bundle 中其他任务继续执行，顶层为 `partial_success`。全部任务等待时顶层为 `waiting_confirmation`；无可行候选仍为 `blocked`。
- 用户补丁只在 revision、schema 和 policy hash 全部一致时应用。策略变化使 source binding 和 fetch request hash 改变，旧 checkpoint 不复用。
- Gateway 发现 revision 在事实读取期间变化时返回 `concurrent_modification`，本轮 runner fail closed，不在已编译计划内刷新能力或重试。重新运行会基于新 revision 完整 resolve；其他语义或结构错误同样不重试。
- 单元格缺失保留缺失状态；依赖缺失的派生节点失败或跳过，独立事实继续执行，成功事实仍进入最终模型组织。
- Provider 失败时保留错误码、请求 ID、revision 和已写入产物；不得回退到模型知识或旧非标准响应。
- 归因能力不支持时只阻断归因节点，独立事实和确定性派生仍可交付。
- 公式归因 binding 保留稳定 `factor_id`、`factor_order`、完整公式 AST 与 fingerprint。metric 因子绑定事实，literal 因子绑定各时期角色值，derived 因子绑定各时期角色安全表达式；因子贡献为 0 时仍保留结果行。
- 残差超过 runner 固定阈值时不得丢弃归因内核结果：节点标记 `partial_success`，保留 rows、summary、residual、warnings 和 boundary cases。分析 IR 不得覆盖残差阈值。
- 结论组织和 bundle 汇总按依赖状态传播；`partial_success`、失败或阻断依赖不得升级为 `success`。

最终状态只能是 `success`、`partial_success`、`waiting_confirmation` 或 `blocked`，并按 `core/required/optional` 关键性披露影响范围。

## 性能和审计

resolve 首轮刷新并固定数据源级共享原始结构索引，随后只在内存创建 policy-versioned 请求 overlay；fetch 不做第二次索引刷新，只按坐标批量读取单元格并最终校验 revision。候选计算不增加飞书调用，精确路径不输出候选；非标准路径每 case 最多三个候选。兼容 selectors 在 Provider 前求并集，同一物理事实只读取并保存一次。记录 `raw_bytes`、唯一物理事实数、各 task 逻辑绑定数、阶段耗时、config hash、revision、schema hash、policy hash、缓存状态和 freshness。跨进程 singleflight 防止并发重建，数据源级限流避免并行 run 触发飞书频控。完整 capabilities 每个 run 只落一份诊断产物，正常模型上下文只读取顶层答案。
