# 内嵌归因内核维护

本文件只用于升级内嵌归因内核或排查版本不一致。普通分析运行不读取。

## 所有权

`ecom-attribution-calculation-engine/attribution_core` 是算法、注册表和 Python API 的唯一源码。`scripts/_vendor/attribution_core` 是生成快照，不得人工修改。独立归因 Skill 的 CLI 和本 Skill 都调用同一 API。

## 同步步骤

1. 在 `ecom-attribution-calculation-engine/attribution_core` 修改算法、注册表或契约，同时更新单元测试。
2. 根据兼容性更新 `CORE_VERSION`、`REGISTRY_VERSION`、`CONTRACT_SCHEMA_VERSION` 或 `ENGINE_API_VERSION`。
3. 在归因项目目录运行全部测试和 CLI/API 等价性测试。
4. 从归因项目目录生成快照：

```bash
cd ../ecom-attribution-calculation-engine
python3 scripts/sync_embedded_core.py
python3 scripts/sync_embedded_core.py --check
```

5. 检查同步产生的 identity 和契约差异；`--check` 必须通过，再运行本 Skill 全部测试。
6. `patch` 只需归因维护者审核；新增算子等 `minor` 变更需要 scene 维护者检查 IR/binding；不兼容变更必须联合修改后再发布。

可选极性和 TopN 语义属于 core/contract 的向后兼容 minor 变更：先在独立引擎实现并测试，再由同步脚本生成 scene 快照。它们不新增外部取数、独立 Skill 加载或归因节点；scene 只透传配置并在最终结果阶段检查排序视图。

## 版本闸门

编译器把 `engine_api_version`、`contract_schema_version`、`registry_version`、`core_version` 和 `core_sha256` 写入计划。执行器同时计算内嵌包 identity 并读取 lock；三者不完全相等时阻断执行，不能静默升级或降级。

回滚时恢复上一版 `_vendor/attribution_core` 和对应 lock，二者必须作为同一个变更提交。
