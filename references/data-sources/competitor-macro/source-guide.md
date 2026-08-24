# 竞品宏观数据源维护

本目录是数据源连接和 Sheet 角色的业务维护入口。常规变更只编辑 `source-config.json`，无需修改分析、编译或执行代码。

可维护字段：

- `provider_id`、`source_id`：稳定路由身份；修改会使旧 checkpoint 失效。
- `source_url`：默认数据源地址；临时诊断可用 `--source-url` 覆盖，覆盖值也进入 config hash。
- `sheet_roles.<role>.allowed_names`：每个逻辑角色允许的精确 Sheet 名。换名时可先同时加入新旧名称，确认源表只存在一个匹配项后再移除旧名。
- `allow_stale_by_default`：源不可达时是否允许使用已有结构快照。生产默认保持 `false`。
- `period_semantics.week`：周度表只提供周标签时，标签按 ISO 8601 解释，即周一开始、周日结束；不在索引中重复维护周起止日期。

不要在配置中维护 credentials、token、sheet ID、行列坐标、指标块位置、公式、匹配阈值、指标/维度元信息副本或 live revision。这些内容分别属于运行环境、物理索引或源表本身。

指标别名、单位、定义、聚合性、支持维度和枚举值仍由源表元信息高频维护；配置只描述如何找到各 Sheet 角色。修改配置后运行完整单元测试，并执行一次真实诊断以确认 `resolved-capabilities.json` 的 binding、availability、config hash 和 freshness。
