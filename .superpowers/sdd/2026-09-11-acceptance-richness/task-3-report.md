# Task 3 实施报告

## RED

- 新增 `test_dashboard_exposes_live_three_layer_operations_overview`，覆盖四类趋势字段/标签、三类异常队列、实时数据时点、六个业务入口以及禁用 `Math.random` / `Date.now`。
- RED 命令：`python -m unittest tests.test_page_contracts.OperationalPageSourceTests.test_dashboard_exposes_live_three_layer_operations_overview`
- RED 结果：`FAILED (failures=11)`；缺失项包括 `power_kwh` 趋势、高危报警、节能成效、采集监控与数据质量入口等，失败原因与任务目标一致。

## 实现结果

- 首页形成三层作战总览：8 个可下钻运行指标；日产液/日耗电两张 14 日 SVG 趋势及日产油/泵效同源序列摘要；最新生产汇总；节能成效；三类异常待办；六个真实业务入口。
- 所有业务值来自 `/api/dashboard` 的 `data` 字段；生产日级值和服务端实时数据时点分别标注；未加入随机值、客户端时间业务值或自动轮询。
- GREEN：新增契约测试 `Ran 1 test ... OK`；`node --check frontend/pages/dashboard.js` 退出码 0；`git diff --check` 无空白错误。
- 目标套件：`python -m unittest tests.test_page_contracts tests.test_well_api tests.test_energy tests.test_production` 共 44 项，43 项通过；一次错误为未改动后端中的种子指纹重启校验。隔离复跑该用例后 `Ran 1 test ... OK`。

## 自审

- 未修改 backend、`frontend/charts.js`、功图页面、文档或 `scripts/`。
- 保留原有 `styles.css` 内容，仅追加首页作用域样式；未覆盖 Task 2 功图样式及断言。
- 八卡均带 `data-page` 下钻；六入口映射到现有页面；动态值经过格式化和 HTML 转义。
- 空节能值显示 `—`，不会把服务端 `null` 误显示为 0。

## 内容哈希（SHA-256）

- `frontend/pages/dashboard.js`: `70A0BBAFA44E76784F4B0C9D9209B5E2CD04316AF8D50009E723FD5C5196C730`
- `styles.css`: `0E6ACC1E8192CE323A03565F6F1156B75B35E0FD38F38C2CD2440CE18E55E215`
- `tests/test_page_contracts.py`: `13DA89B481BF9C4682A98ACD944F92B15818F07E9604F42CA7589EF78CA164E6`
- 基线提交：`4dbb1c384ffed4cbc66339ddbe94742c647037cc`

## Concern

- 完整目标套件中的种子指纹重启用例出现一次不可复现错误；隔离复跑通过。该路径与本次仅前端首页及契约测试的改动无关，按任务约束未修改后端。

## Fix Round 1：窄屏网格

- RED：新增 `test_dashboard_has_narrow_viewport_layout` 后，隔离运行得到 `FAILED (failures=1)`，证明 dashboard 专属 4/2/2 列规则未被通用窄屏规则覆盖。
- 修复：在 1260px 媒体查询中将 `.dashboard-metrics` 调整为 2 列，将趋势、结果、行动区调整为单列；同时为行动区直接子项设置 `min-width: 0`，消除 310px 首列在窄屏的溢出风险，桌面布局保持不变。
- GREEN：`python -m unittest tests.test_page_contracts` → `Ran 14 tests in 26.930s / OK`；`node --check frontend/pages/dashboard.js` 退出码 0；`git diff --check` 无空白错误。
- 更新哈希：`styles.css` SHA-256 `CF4359DC804D9FB191599EDCB89A8CAE68296510A4491C7FC02363981DD1CD5B`；`tests/test_page_contracts.py` SHA-256 `0BE370E8DFFAFB2B1369483173050059B8E72BE466F789EC3BDC59393344F063`。
