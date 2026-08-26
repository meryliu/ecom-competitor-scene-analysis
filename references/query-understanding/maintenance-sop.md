# Query Policy 维护 SOP

本 SOP 用于飞书业务规则高频修改后的提取、评审、编译和回归。飞书文档是业务来源，评审包是待审中间态，`references/query-understanding/` 中带 manifest 的资源才是 Skill 运行版本。

## 版本流转

1. 使用团队批准的飞书 CLI 读取指定文档，固定 `document_id`、revision、读取时间、URL 与原文 SHA-256。不得直接从浮动的最新文档覆盖运行规则。
2. 在 Skill 外的 `runtime/query-policy-review/<revision>/` 生成评审包，至少包含来源差异、覆盖矩阵、规则差异、正反例结果、待确认项，以及 `proposed/` 下的 source manifest、规则 bundle、索引和行为 fixtures。
3. Reviewer 按“来源差异 → 覆盖矩阵 → 逐规则差异 → 正反例 → 待确认项”的顺序评审。每条 active 规则都必须能回指 `source_ref.heading_path` 和 `source_ref.block_id`；未纳入内容必须记录为明确排除项，不能静默丢失。
4. 业务评审通过后只编译到新的空目录，不直接覆盖当前运行版本：

```bash
python3 scripts/compile_query_policy.py \
  --review-dir <runtime/query-policy-review/revision> \
  --output-dir <empty-build-dir> \
  --policy-version <query-policy/version>
python3 scripts/compile_query_policy.py --check <empty-build-dir>
```

5. 比较构建目录与拟替换资源的 canonical JSON 语义，确认索引、8 条或本次批准数量的规则、依赖、动作和 source refs 均符合评审结论。将构建产物作为同一次代码变更替换 `policy-index.json`、`policy-manifest.json` 与 `rules/`；应用契约和 schema 除非协议本身变化，否则不随业务规则改写。
6. 运行 Query Policy 行为测试、相关主流程回归、全量测试和 Skill 校验。代码评审至少检查 source revision、policy hash、规则/fixture 覆盖、候选引擎零改动，以及 fail-open 用例。

离线编译或检查失败时停止新版本发布，当前已校验版本保持不变。正常用户分析中的 Policy 失败则按应用契约丢弃增强状态并使用原始 Query；两类失败不可混为业务阻断。

## 新增规则模板

评审源中的动作必须显式提供稳定、语义化的 `action_id`。不得使用数组序号，因为动作重排会破坏幂等去重身份。

```json
{
  "rule_id": "example-business-rule",
  "name": "业务可读名称",
  "status": "active",
  "version": "1.0.0",
  "priority": 60,
  "routing": {"terms": ["只用于有界召回的词"]},
  "applicability": {
    "all": ["必须同时成立的业务条件"],
    "none": ["明确不适用条件"]
  },
  "actions": [
    {
      "action_id": "default_missing_business_scope",
      "op": "set_default",
      "when": "用户未明确业务范围",
      "field": "business_scope",
      "value": "经业务确认的默认值"
    }
  ],
  "relations": {"depends_on": ["user-explicit-priority"]},
  "user_explicit_protection": ["用户明确范围不得覆盖"],
  "boundaries": ["禁止外推的场景"],
  "source_ref": {
    "heading_path": "飞书章节/规则标题",
    "block_id": "飞书稳定 block id"
  }
}
```

编译器补入 `schema_version`、`policy_version` 和动作 `idempotency`，并检查 rule/action ID、依赖目标、依赖环、深度、允许的动作类型、manifest hash 及动作幂等声明。依赖只表示加载和重新判断，不表示无条件执行依赖动作。

## Review 清单

- 来源：revision 与 hash 是否对应本次评审原文；每条规则的 heading/block 是否可定位。
- 语义：applicability、none、用户显式保护、动作顺序和禁止外推边界是否与文档一致。
- 关系：依赖是否必要、目标是否存在、是否无环；依赖规则是否仍独立判断适用性。
- 幂等：每个动作 ID 是否稳定；同一 `(task_id, rule_id, action_id, target_scope_fingerprint)` 是否最多提交一次。
- 上下文：索引只做高召回，packet 是否保持在规则数、依赖深度、轮次、requirement 和字节预算内。
- 降级：资源损坏、依赖错误、重复动作、IR 校验失败等用例是否均返回 `fallback_raw`，且原始 Query 未改变。
- 边界：候选引擎、Provider、Prepare、编译器、runner 和归因引擎是否没有因业务规则维护而改动。

回滚通过恢复上一个已评审的完整 Policy 版本完成，索引、manifest 和全部规则必须作为一个整体回滚，不能混用不同版本文件。
