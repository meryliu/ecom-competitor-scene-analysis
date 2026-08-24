# 竞品取数执行契约

所有分析档位使用 `scripts/run_analysis.py`；`run_fetch.py` 只用于 Provider 诊断：

```bash
python3 scripts/run_analysis.py \
  --input <analysis-ir-or-bundle.json> \
  --work-dir <run-directory> \
  --resume auto
```

`run-state.json` 记录 input/request hash、集合注册表 hash、阶段 checkpoint、artifact hash、revision、schema hash、恢复决策和 append-only `fetch_attempts`。每个 attempt 包含唯一 `attempt_id`、`request_id`、状态、起止时间、耗时、facts 字节数和 source 元数据。

编译器生成 provider-neutral 的逻辑 `fact_slots`，runner 在合并时注入当前 `source_binding`，再形成 `fact_demands`；兼容选择器先求安全并集，原选择器和 `source_dimension_domains` 保留在 `consumer_bindings` 中。Gateway 对每个 binding 独立解析显式选择域、物理维度全域或命名集合后再读取并集，每个物理事实只输出一次，执行前才按 task/slot 投影。投影行保留 `physical_fact_id`、唯一 `binding_id`，并生成唯一逻辑 `fact_id`。不同 source binding（含 Provider 版本）、scope、filter、metric、period、grain、物理全域意图或 component 不合并。

bundle 输入使用 `analysis_bundle/1.0`，所有 task 先编译再合并请求，共享一次 Gateway 执行和同一 revision。仅当 run 处于失败态且 facts 阶段成功时，checkpoint 才在 input hash、含 config/revision/schema 的 request hash、集合注册表 hash、artifact hash 和 source revision 元数据齐全时复用；派生注册表变化只触发重新编译和计算，事实需求未变时仍可复用标准 facts。已成功完成的 run 再次执行会检查实时 revision，`--fresh` 可显式绕过任意失败态 checkpoint。

成功结果直接是 `scene_facts/2.0` 标准 facts 与 bindings，不存在原始自然语言响应或二次 JSON 适配阶段。事实槽位无法唯一绑定时返回 `needs_clarification` 或结构化错误，禁止按行顺序猜测。
