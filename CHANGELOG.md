# Changelog

## 1.0.1 - 2026-08-20

- 修复维度为“无”的月度指标块取数行定位：当 `rows` 中只有一行数据时，将该行识别为整体数据行，避免误读 `header_row` 标题行。
- 增加无维度整体行、旧表空 rows 回退和多行歧义阻断的 Provider 回归测试。

## 1.0.0 - 2026-08-20

- 从 `ecom-competitor-scene-analysis` 复制生成 DAS 合规版 Skill。
- 补齐 `metadata.version`、`compatibility`、WHEN / DO NOT USE WHEN 描述。
- 补充 DAS 沙箱运行约束，要求运行目录与缓存位于 `/workspace/runtime`。
- 新增 `references/index.md`，为多篇 reference 提供索引。
- 保留原有 runner、Provider、派生与归因逻辑，不改变业务口径。
