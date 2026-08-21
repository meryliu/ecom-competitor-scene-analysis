# 轻量执行器契约

本契约定义 `scene-analysis` 如何把已经通过计划校验的节点图交给确定性执行器。执行器只消费计划中的抽象节点、事实绑定和算子契约，不理解或内置任何业务指标、维度、日期及公式。

## 边界

执行器负责标准 facts 校验、依赖调度、输入适配、轻量派生、归因输入绑定、父分组扇出、结果索引、检查点和耗时记录。Query 理解、口径裁决、算子选择和业务结论仍由模型、竞品 Provider 及归因注册表负责。

禁止为单次 Query 生成新的 Python/JavaScript 计算脚本或执行轨迹脚本。新增业务指标、维度或父子层级时，只修改运行时计划和事实，不修改执行器源码。

## 入口

```bash
python3 scripts/execution_runner.py \
  --plan <validated-plan.json> \
  --facts <long-facts-or-wide-facts.json> \
  --output <execution-result.json> \
  --events <execution-events.jsonl> \
  --storage-mode auto
```

`--plan` 必须已通过 `validate_execution.py --phase plan`。`--facts` 接受标准长表事实或 `wide_facts/1.0`；取数响应的保存、重定向或格式转换不得触发新的外部请求。

## 宽表输入

相同来源、范围、过滤条件、时期和维度粒度的多个固定事实可共享一个物理行。第一阶段由执行器展开为现有逻辑长表，归因绑定无需改变：

```json
{
  "fact_format": "wide_facts/1.0",
  "shared": {"source_request_id": "fetch_request_id", "view_id": "view_ref"},
  "fact_mappings": {
    "ratio_ref": {
      "metric": "metric_ref",
      "numerator_column": "measure_ref_1",
      "denominator_column": "shared_measure_ref",
      "unit": "ratio",
      "definition": "取数响应返回的口径"
    },
    "value_ref": {
      "metric": "value_metric_ref",
      "value_column": "measure_ref_2",
      "unit": "运行时单位",
      "definition": "取数响应返回的口径"
    }
  },
  "rows": [
    {
      "period": "period_a",
      "dimensions": {"dimension_ref": "value_1"},
      "measure_ref_1": 1,
      "measure_ref_2": 2,
      "shared_measure_ref": 4,
      "raw_missing": {}
    }
  ]
}
```

每个映射必须声明 `value_column`，或同时声明 `numerator_column` 和 `denominator_column`；同一物理列可以被多个逻辑事实引用。缺列属于协议错误，空值按缺失归一化。宽表不得合并不同粒度或制造稀疏笛卡尔积；不满足条件时继续使用长表。

## 标准事实行

```json
{
  "fact_id": "稳定且唯一的事实ID",
  "metric": "运行时指标标签",
  "view_id": "分析视角ID",
  "period": "实际时期值",
  "period_role": "analysis",
  "dimensions": {"dimension_ref_1": "value_1"},
  "value": 0.0,
  "numerator": null,
  "denominator": null,
  "unit": "运行时单位",
  "definition": "取数响应返回的口径",
  "raw_missing": false,
  "missing": false,
  "normalization_reason": "unchanged",
  "value_derived_from_components": false,
  "source_request_id": "fetch_request_id",
  "source_ref": "原始响应引用"
}
```

`execution_runtime.periods` 是唯一时期角色映射。`period_role` 可根据 `period` 补充；两者同时存在时必须一致。编译计划产生的事实必须携带事实槽位的 `view_id`，该字段同时参与事实 ID、索引和选择器匹配。`dimensions` 可以包含任意动态字段；旧事实将维度保存在顶层字段时，可用通用 `execution_runtime.dimension_fields` 数组迁移。

执行器保留源端 `missing` 为 `raw_missing`，并按固定优先级重算标准状态：

1. 源端明确 `missing=true`：保持缺失，原因为 `source_missing`。
2. `denominator=0`：保持缺失，原因为 `zero_denominator`。
3. `value=null` 且有限 `numerator`、有限非零 `denominator` 完整：计算 `value=numerator/denominator`，原因为 `value_derived_from_components`，并设置 `value_derived_from_components=true`。
4. `value=null` 且组成事实不完整：保持缺失，原因为 `incomplete_components`；没有组成字段时使用 `value_missing`。
5. 已有有效值保持不变，原因为 `unchanged`。

所有缺失记录都保持 `value=null`，不得补 0 或估算。恢复值只属于可复现的确定性计算，最终校验必须检查组成事实有效性及公式闭合。`raw_missing` 与标准化后的 `missing` 分开保留，以便定位源端标记和本地规则不一致的问题。

事实选择器默认把 `dimensions` 作为子集条件；选择大盘或其他固定粒度事实时必须设置 `dimensions_exact=true`，避免整体事实与分组事实同时匹配：

```json
{"metric": "metric_ref", "view_id": "view_ref", "dimensions": {}, "dimensions_exact": true}
```

## 运行时配置

计划顶层可增加：

```json
{
  "execution_runtime": {
    "version": "1.0",
    "periods": {
      "analysis": "period_a",
      "analysis_last_year": "period_a_ly",
      "comparison": "period_b",
      "comparison_last_year": "period_b_ly"
    },
    "dimension_fields": [],
    "max_workers": 4,
    "residual_tolerance": 1e-8
  }
}
```

时期角色由 Query 和派生/归因需求决定，不固定要求四期。归因 `binding.periods` 或静态 `payload.periods` 必须逐角色等于该映射。`max_workers` 用于 dry-bind、同一 DAG 波次和父任务本地并发，不得制造重复取数请求。`residual_tolerance` 由 runner 固定注入，分析 IR 不得覆盖。

## 可执行节点

可执行节点在原有 `execution` 对象中增加声明式字段：

```json
{
  "target_ref": "target_ref",
  "operator_contract_ref": "operator_query_ref",
  "execution": {
    "mode": "lightweight_executor",
    "handler": "attribution",
    "operator": "operator_id_from_contract",
    "binding": {},
    "expansion": {"mode": "none"}
  }
}
```

`handler` 支持：

- `derived`：执行注册派生、推理派生或用户自定义计算的安全表达式；计划保留不同节点类型和 `definition_source`。
- `attribution`：调用版本锁定的内嵌 `attribution_core`。
- `fact_artifact`：确认并索引已落盘的标准事实。
- `model_owned`：保留给业务判断或结论组织，不由执行器改写。

输入适配复用 `handler=derived`，并通过 `materialize_as` 声明目标事实。节点成功后，执行器在下一 DAG 波次前把结果注入运行时 FactStore；它只在运行时供下游复用，不写回 Provider 原始 facts。`materialize_as.validation` 只支持 `facts_present`、`unit_consistent` 和 `metric_additive`；其中可聚合性必须来自输入事实的飞书元信息。

归因节点进入执行器前，校验器必须确认 `target_ref.target_semantics` 属于权威算子契约的 `supported_target_semantics`。能力不匹配的节点使用 `status=blocked` 和 `execution.mode=blocked`，不进入执行器；执行器保留其终态，并继续调度无依赖关系的支持节点。禁止在 binding 中改变目标或在执行器内补算不受支持的目标。

### 归因绑定

公式归因使用动态因子：

```json
{
  "binding": {
    "scenario": "metric_change",
    "metric_object": "volume",
    "decomposition": "multiplication",
    "periods": {"analysis": "period_a", "comparison": "period_b"},
    "metric": {"name": "metric_ref_y", "selector": {"metric": "metric_ref_y", "view_id": "view_ref", "dimensions": {}, "dimensions_exact": true}},
    "factor_order": ["factor_a", "factor_b"],
    "formula": {"op": "multiply", "args": [{"factor_ref": "factor_a"}, {"factor_ref": "factor_b"}]},
    "formula_fingerprint": "stable_hash",
    "factors": [
      {"factor_id": "factor_a", "kind": "metric", "name": "factor_a", "selector": {"metric": "factor_a", "view_id": "view_ref", "dimensions": {}, "dimensions_exact": true}, "role": "multiplier"},
      {"factor_id": "factor_b", "kind": "literal", "name": "factor_b", "values_by_period_role": {"analysis": 1.0, "comparison": 1.0}, "role": "multiplier"}
    ]
  }
}
```

维度或结构归因使用动态分组字段：

```json
{
  "binding": {
    "scenario": "metric_change",
    "metric_object": "ratio",
    "decomposition": "structure",
    "periods": {"analysis": "period_a", "comparison": "period_b"},
    "groups": {
      "selector": {"metric": "metric_ref_r", "view_id": "view_ref", "dimensions": {}, "dimensions_exact": true},
      "group_dimensions": ["dimension_ref_2"]
    }
  },
  "expansion": {
    "mode": "for_each_parent_group",
    "parent_dimensions": ["dimension_ref_1"],
    "parent_selector": {"metric": "metric_ref_r"}
  }
}
```

当 `groups` 只保留 TopN、稳定组合或其他不完整子集时，binding 必须声明父节点完整口径。推荐在归因绑定中加入：

```json
{
  "coverage": {
    "mode": "auto_residual",
    "residual_name": "其他/未覆盖",
    "parent_selector": {"metric": "metric_ref_r", "view_id": "view_ref", "dimensions": {}, "dimensions_exact": true}
  }
}
```

执行器按 `parent_selector` 绑定父节点的完整值（结构归因绑定分子、分母；量级归因绑定各期值），并将 `coverage` 传入归因引擎。引擎以父节点为分母自动追加残差行；禁止对不闭合的保留组合静默重归一化。成功结果必须在 `summary.coverage` 暴露保留覆盖率和残差口径。

比例结构 binding 同时传递 `sparse_policy`。执行顺序固定为 Query 合并、Query 上卷、剩余稀疏项并入“其他”、未覆盖残差并入同一“其他”、单边 `0/0` 配对级 ε。真实 source missing 不得转换为结构性 `0/0`；只有事实明确为零分母或组合在某期结构性不存在，且 `structural_absence_is_zero=true` 时才绑定 `0/0`。父总体任一必要周期分母 `<= 0`、分母 0 但分子非 0 均使该父任务失败。

执行器把指标、维度和值都作为不透明运行时标签。源代码不得对这些标签做业务条件分支。

### 派生表达式

派生节点使用白名单表达式，不执行字符串代码：

```json
{
  "execution": {
    "mode": "lightweight_executor",
    "handler": "derived",
    "expression": {
      "op": "subtract",
      "args": [
        {"op": "divide", "args": [{"fact": {"metric": "metric_ref_1", "period_role": "analysis"}}, {"fact": {"metric": "metric_ref_1", "period_role": "analysis_last_year"}}]},
        {"literal": 1}
      ]
    },
    "unit": "rate"
  }
}
```

第一版允许 `add`、`subtract`、`multiply`、`divide`、`sum` 和 `negate`。分母为 0、事实不唯一或缺失时节点失败，不猜测值。

## 父分组扇出

`for_each_parent_group` 从符合 `groups.selector` 的事实中取得 `parent_dimensions` 的唯一组合；发现父值时忽略精确粒度标记，绑定每个子组时恢复精确匹配。一个父任务失败不阻断其他父任务；节点根据成功和失败数量标记 `success`、`partial_success` 或 `failed`。

编译器在归因目标存在非空 `parent_dimensions` 时自动生成 `for_each_parent_group`，并把父维度同时投影到父事实与子事实槽位。归因引擎的单次 payload 只允许一个父维度组合，从执行层和计算层同时防止跨父合并“其他”或错误分摊残差。

父分组必须使用动态 `binding`；静态 `payload` 不能随父值变化，因此不得与 `for_each_parent_group` 同时使用。公式节点没有 `groups.selector` 时，通过 `expansion.parent_selector` 声明发现父值所需的事实范围。

结果键固定为 `node_id + parent_dimensions`，不按业务名称拼接代码。并发完成后按父键稳定排序，保证同一输入得到确定性输出。

## 状态与失败

执行器先 dry-bind 全部确定性节点，再按 DAG 拓扑波次运行。同一波次互不依赖的节点按 `max_workers` 并发；依赖未成功时，下游标记 `skipped`。dry-bind 失败只阻断对应节点及其下游，不启动该节点归因计算。

执行器不得为通过校验而修改计划、公式、事实值或边界策略。事实绑定失败和归因引擎失败保留结构化错误；引擎输出有效但残差超阈值时保留完整结果和 warning，并将节点标记为 `partial_success`。输出文件已存在且计划哈希、事实哈希均未变化时，重新运行复用已成功节点；失败、部分成功和跳过节点重新执行。

## 输出

执行器 `1.8.1` 支持 `inline` 和 `reference` 两种存储。CLI 默认 `auto`：标准事实超过 1000 行或存在父分组扇出时选择 `reference`，否则选择 `inline`；可显式传入 `--storage-mode`。引用模式的主文件是 `execution_manifest/2.0`，至少包含：

```json
{
  "executor": {"name": "scene-analysis-lightweight-executor", "version": "1.8.1"},
  "storage": {"mode": "reference", "schema_version": "2.0", "artifact_root": "execution-result.json.artifacts"},
  "plan_hash": "sha256",
  "facts_hash": "sha256",
  "engine_hash": "归因引擎及注册表sha256；没有归因节点时为null",
  "status": "success|partial_success|blocked",
  "artifacts": {
    "normalized_facts": {
      "artifact_id": "normalized_facts",
      "path": "execution-result.json.artifacts/normalized-facts.jsonl",
      "format": "jsonl",
      "schema_version": "normalized_fact/1.0",
      "records": 0,
      "bytes": 0,
      "sha256": "sha256"
    }
  },
  "node_results": [{"node_id": "node_ref", "status": "success", "result_ref": {"artifact_id": "node_result:node_ref", "line": 0}}],
  "result_index": {"artifact_id": "result_index"},
  "result_collections": {},
  "performance_metrics": {
    "input_layout": "long|wide",
    "raw_bytes": 0,
    "physical_rows": 0,
    "logical_facts": 0,
    "parse_ms": 0,
    "normalize_ms": 0,
    "total_fetch_ms": 0
  },
  "execution_summary": {
    "succeeded_nodes": [],
    "failed_nodes": [],
    "skipped_nodes": [],
    "blocked_nodes": [],
    "duration_ms": 0
  }
}
```

指标定义固定为：`raw_bytes` 是执行器实际读取的 Provider facts 文件字节数；`physical_rows` 是输入记录数；`logical_facts` 是宽表展开后进入执行器的事实数；`parse_ms` 包含文件读取、UTF-8 解码和 JSON/JSONL 解析；`normalize_ms` 仅记录必要的缺失状态校验和宽转长耗时；`total_fetch_ms` 来自计划中的 Provider `fetch_results`，并行请求按最早开始至最晚结束计算墙钟时间。Provider 自身的 revision、schema hash 和 freshness 保留在事实源元数据中。

引用模式只在主 manifest 保留原计划、节点终态、轻量节点摘要、集合计数、artifact 元数据、性能和执行摘要；不得再内联 `normalized_facts`、`derived_results` 或 `attribution_results`。标准事实写入一个 JSONL；每个节点写入独立 JSONL；父任务各占一行，不嵌入节点的 `children` 数组；结果索引写入独立 JSON。所有 artifact 元数据必须含相对路径、格式、schema 版本、记录数、字节数和 SHA-256，路径必须位于 manifest 目录内。`inline` 保留旧结构用于小任务和兼容调用。

artifact 必须先写入临时文件、刷新并原子替换，主 manifest 最后原子替换。进程中断时，旧 manifest 仍只引用已经完整提交的文件；缺失、越界、格式错误、字节数或哈希不闭合的引用不得作为成功结果读取。

每个节点和父任务记录 `queued_at`、`started_at`、`ended_at`、`duration_ms`、输入哈希、结果引用、warning 和错误。事件 JSONL 在事件发生时同步追加并刷新，进程中断时保留已有轨迹；检查点按 DAG 波次更新 artifact 后再提交 manifest。缓存复用必须同时匹配执行器版本、计划哈希、事实哈希和归因引擎哈希，并通过引用文件完整性检查。

按需查看结果，不加载全部 artifact：

```bash
python3 scripts/inspect_execution.py --manifest <execution-result.json> --list
python3 scripts/inspect_execution.py --manifest <execution-result.json> --key '<result-key>'
python3 scripts/inspect_execution.py --manifest <execution-result.json> --node-id '<node-id>'
```

## 质量闸门

执行顺序固定为：计划校验、事实标准化、全节点 dry-bind、DAG 执行、节点输出校验、最终校验。dry-bind 检查安全表达式、结果依赖、时期一致性、事实唯一绑定、算子与场景匹配及必填输入；比例结构归因在进入算子前检查各组必需分母。算子完成后检查 `ok`、实际算子、`summary`、`rows`、`warnings`、`boundary_cases` 和残差。最终事实校验还检查 `raw_missing`、`normalization_reason`、零分母缺失状态，以及从组成事实恢复的值是否闭合。

执行器结果不是业务结论。模型应通过 `inspect_execution.py` 按 `result_index` 读取成功节点，完成业务解读和 `conclusion_organization`；不得另写脚本重组确定性结果。完成模型节点后直接运行 `validate_execution.py --phase final`。
