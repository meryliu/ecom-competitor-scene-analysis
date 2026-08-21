# Changelog

## 1.1.0 - 2026-08-21

- Add a versioned, declarative business-intent policy with bounded semantic hypotheses.
- Resolve each hypothesis against metric object, grain, dimension, aggregation, fact-block and period capability in one pinned source snapshot.
- Auto-select one executable interpretation, return one confirmation case for distinct viable interpretations, and keep attribution/formula/composition semantics fail-closed.

## 1.0.3 - 2026-08-21

- Normalize task-level `eq/in` filters into every physical fact selector with conflict detection and plan validation.
- Reuse one internal runtime role per canonical aggregate child period across metrics.
- Make formula target fallback executable only with direct fact inputs and an explicit, runtime-verified unit scale conversion.

## 1.0.2 - 2026-08-21

- 将指标元信息的 `可支持时间粒度`、`可支持拆解维度`、`聚合方式` 接入源表索引和 Query 级事实能力判断。
- prepare 阶段生成 `fact_capability_plan` 与 canonical selectors；直接事实优先，安全聚合受元信息可聚合性约束。
- 目标事实缺失且用户公式因子齐全时，生成公式目标中间事实；不使用元信息中的指标公式和使用说明。

## 1.0.1 - 2026-08-20

- 修复维度为“无”的月度指标块取数行定位：当 `rows` 中只有一行数据时，将该行识别为整体数据行，避免误读 `header_row` 标题行。
- 增加无维度整体行、旧表空 rows 回退和多行歧义阻断的 Provider 回归测试。

## 1.0.0 - 2026-08-20

- 从 `ecom-competitor-scene-analysis` 复制生成 DAS 合规版 Skill。
- 补齐 `metadata.version`、`compatibility`、WHEN / DO NOT USE WHEN 描述。
- 补充 DAS 沙箱运行约束，要求运行目录与缓存位于 `/workspace/runtime`。
- 新增 `references/index.md`，为多篇 reference 提供索引。
- 保留原有 runner、Provider、派生与归因逻辑，不改变业务口径。
