# 油井平台完整页面预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 制作包含完整演示业务测试数据的 55 个应用页面和 1 个厂家开放资料入口，供用户逐页审查；不在本阶段扩展真实业务功能。

**Architecture:** 新建独立预览入口和本机预览服务，复用已有页面的 `renderX(context)`、样式、图表与路由，通过注入的预览 API 读取脱敏业务快照和补充样例。草稿、流程演练及页面反馈写入单独状态库；不加载正常应用入口、真实采集服务、命令发送器或现有安装凭据。页面确认后再按稳定字段和接口逐页接入真实业务。

**Tech Stack:** Python 标准库 HTTP 与 SQLite，原生 JavaScript ES modules、HTML、CSS、SVG，现有 unittest、Node、Playwright 和 Edge 测试工具；不更换前端框架，不新增外部服务。

**Spec:** [完整页面与测试数据预览设计](../specs/2026-10-12-page-first-platform-design.md)，用户于 2026-10-12 确认。本计划待用户审阅和选择实施方式；尚未实现以下任务。

## Global Constraints

- “保留演示版的 24 个页面、开发版新增的 11 个页面及其详情和弹窗，另补充 20 个页面。”
- “原工作目录”不编辑源码，不读写其业务数据库或密钥；冻结演示目录、ZIP 和标签不修改、替换或删除。
- “全部页面经用户确认后，才进入真实功能开发和接入阶段。”
- “顶部统一显示‘页面预览 · 测试数据’”；详情展示来源、数据时点及功能接入状态，原演示版文案不变。
- “预览读取接口限定在 `/api/preview/`”，不把业务请求转发到 8765、8770 或其他生产服务。
- “不启动 PostgreSQL 写入客户端、MQTT 发送器、Modbus 轮询、终端模拟器或现场控制工作进程，不加载现有安装凭据。”
- “悬停或键盘选点时显示当前点和横纵坐标，数值固定两位小数并带单位；仅格式化显示，不修改保存的精度。”
- “主验收视口为 1366×768 和 1920×1080。”
- 新预览目录为 `C:/Users/hp/AppData/Local/OilWell/page-preview-20261012`，只绑定 `127.0.0.1:8772`。目录已存在不得覆盖，端口冲突不得结束陌生进程。
- 当前演示数据来源仅为 `C:/Users/hp/AppData/Local/OilWell/demo-20260912-run/platform.db`。取一致性快照，不停止演示、不重新播种，不复制源命令密钥。
- 当前固定审查安装 `C:/Users/hp/AppData/Local/OilWell/review-20261011-r2` 不改动、不启停；不自动推送、合并公开仓库。
- 新文件使用 `apply_patch`；新增演示数据及运行状态不提交仓库。每任务先失败测试，再实现和通过测试，只提交自己本任务的文件。

## Review Focus

1. 源库仍在追加数据或尚有 WAL：快照应内部一致、不遗漏提交记录、不向源库执行迁移或写 SQL。测试归任务 2。
2. 新旧页面切换、登出或切换岗位后迟到的请求：旧响应不得覆盖新页面或在退出后保存。测试归任务 4 和 5。
3. 旧页面直接调用上传、下载或控制路径：所有六类 API 方法均走预览边界，实际发送路径在服务器拒绝。测试归任务 3 和 4。
4. 同一井跨页使用不同日期或样例来源：卡片、曲线、明细及报表口径应一致，不修改源时间来假装实时。测试归任务 6、8、10 和 12。
5. 已确认页面修改、两人同时反馈或恢复旧预览状态：版本冲突不能覆盖意见，页面内容变更必须重新待确认。测试归任务 13。

## 文件边界与通用接口

这是一个独立的页面预览子系统，不包含多个真实后端子系统的重建。14 个制作任务共用以下契约；各业务页分组可以独立检查。

| 文件或目录 | 职责 |
| --- | --- |
| `preview.html`、`frontend/preview/entry.js`、`shell.js`、`preview.css` | 独立入口、演示风格外壳和仅预览使用的布局扩展 |
| `frontend/preview/catalog.json`、`catalog.js`、`renderers.js` | 55 个页面、旧链接兼容、年度任务关系和渲染器装配 |
| `frontend/preview/api.js`、`actions.js`、`context.js` | 请求边界、显式流程演练、生命周期及原页面上下文 |
| `frontend/preview/pages/`、`presenters/` | 新增页面及旧页面的预览专用组合，不重写正常应用入口 |
| `backend/preview/contracts.py`、`catalog.py`、`errors.py` | 目录验证、预览元数据、错误及统一边界 |
| `backend/preview/snapshot.py`、`snapshot_policy.py`、`dataset.py` | 在线快照、字段脱敏、只读数据集及完整性清单 |
| `backend/preview/server.py`、`auth.py`、`repository.py`、`state.py` | 预览 HTTP、独立会话、读取分派和演练状态 |
| `backend/preview/read_models/`、`fixtures/` | 分业务查询和可复现补充测试场景，无设备或算法执行 |
| `tools/page_preview.py`、`tools/verify_page_preview.py` | 独立安装启动、归属检查、停止、完整性及交付验证 |
| `tests/preview_support.py`、`tests/test_preview_*.py`、`tests/fixtures/preview_*.html`、`preview_runner.cjs` | 临时库、模块测试、浏览器场景和实际 HTTP 验证 |

现有 `app.js`、`index.html`、`styles.css`、`backend/http_server.py` 的正常 demo/review 行为保持不变。优先通过上下文注入复用已有模块；不得为了预览修改 review 白名单、接入现有库或全局替换 `fetch`。共享图表若需修复，必须另加对应回归测试，不能在业务页分组中顺便改动。

以下类型由任务 1 定义，后续保持字段一致：

- `PageSpec = {id, label, group, origin, hidden, annual_tasks, content_version, scenarios}`；`origin` 仅为 `demo`、`development`、`new`，`content_version` 为该页内容版本字符串。
- `PreviewMeta = {mode: 'preview', control_enabled: false, snapshot_id, snapshot_at, data_from, data_to, source_kind: 'software_fixture', page_catalog_version}`。源记录自己的 `source_type` 保留，不被这一环境标记改写。
- `PreviewActor = {id, display_name, role_code, permissions}`；身份与原业务 actor 的映射分开保存。
- `PreviewQuery = dict[str, str]`；统一支持 `scenario` 为 `normal`、`empty`、`error`、`denied`、`stale`、`unconnected`，异常业务场景另由目录声明。
- 成功 HTTP JSON 仍为 `{ok: true, data: ...}`；错误为 `{ok: false, error: {code, message, details}}`，不把异常返回成空列表。
- 旧页面的 `api` 六类方法为 `request(path, options)`、`get(path, options)`、`post(path, body, options)`、`patch(path, body, options)`、`raw(path, bytes, options)`、`download(path, filename, options)`；读请求只映射到同源 `/api/preview/legacy/`，新增视图读 `/api/preview/views/`。写请求只允许显式确认的 `/api/preview/rehearsals` 或页面审查接口，不能自动重放旧业务命令。
- `PreviewDataset.query_all(sql: str, parameters: tuple = ()) -> list[dict]`、`query_one(...) -> dict | None`、`scalar(...) -> object` 仅使用脱敏业务快照的只读连接，开启 `query_only` 并用 authorizer 拒绝写操作、ATTACH 和危险 PRAGMA；不复用会设置 WAL 的 `Database.connection()`。
- `PreviewRepository.read(path: str, query: PreviewQuery, actor: PreviewActor) -> object` 仅分派明确注册的读取模型。各读取模块导出 `register(repository: PreviewRepository) -> None`，仅在该模块任务完成后装配，不以通用假响应兜底。
- `renderPreviewPage(context) -> Promise<void | boolean>` 使用已有页面上下文字段：`api, components, format, escapeHtml, workspace, page, pageHeader, params, permissions, actorId, authSession, registerDispose, capabilities, navigate, isCurrent`。预览另提供 `meta`、`scenario`、`actions`，不让原组件访问真实全局服务。

验证命令在独立开发工作树执行；`$previewPython` 指向现有 Python 虚拟环境解释器，调用统一带 `-B`，禁止写回该环境的字节码。测试仅用临时路径，不能读取旧测试库配置：

```powershell
$previewPython = 'C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe'
```

## Task 1 页面目录与测试契约

**Files:** Create `frontend/preview/catalog.json`, `catalog.js`; `backend/preview/__init__.py`, `contracts.py`, `catalog.py`, `errors.py`; `tests/preview_support.py`, `tests/test_preview_catalog.py`, `tests/fixtures/preview_runner.cjs`.

**Interfaces:** Produces `load_catalog(path: Path) -> list[PageSpec]`、`validate_catalog(pages: list[dict]) -> None`；JS `loadCatalog(url, fetchImpl) -> Promise<PageSpec[]>`。测试支持 `make_source(root: Path) -> Path` 创建临时最小完整业务关联库；`read_source_rows(path: Path) -> dict[str, list[dict]]` 仅供小型临时源库对账，`find_secrets_in_served_snapshot(manifest: dict) -> list[str]` 检查服务副本和允许导出的字段，不扫描私有原快照；`run_browser(case: str, *, width: int = 1366, height: int = 768) -> dict` 以现有 Edge/Node 路径运行临时页面并收集脚本错误，不使用实际服务。

- [ ] **Step 1:** 编写目录失败测试，从已批准设计读取预期 ID，断言 55 个唯一页面、`demo=24/development=11/new=20`、12 个年度任务均有对应；另校验原 35 个 ID 与详情参数兼容。

```python
pages = load_catalog(CATALOG)
self.assertEqual(len(pages), 55)
self.assertEqual(Counter(p['origin'] for p in pages), {'demo': 24, 'development': 11, 'new': 20})
self.assertEqual(LEGACY_PAGE_IDS - {p['id'] for p in pages}, set())
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_catalog -v`；先因模块不存在失败。
- [ ] **Step 3:** 按设计表原样实现目录、类型和验证，不注册空渲染器；测试支持复用现有无头浏览器机制，但所有数据库在临时目录创建。
- [ ] **Step 4:** 重跑同一命令，全部通过；确认开放资料入口不被算作第 56 个应用页。
- [ ] **Step 5:** 只提交本任务文件，commit `feat: define preview page catalogue and test contracts`。

## Task 2 演示数据快照与脱敏导入

**Files:** Create `backend/preview/snapshot.py`, `snapshot_policy.py`, `dataset.py`; `tests/test_preview_snapshot.py`, `tests/test_preview_dataset.py`.

**Interfaces:** Consumes 任务 1 契约与 `make_source`。Produces `capture_snapshot(source: Path, private_root: Path) -> dict`、`build_business_snapshot(raw_snapshot: Path, target: Path) -> dict`、`verify_manifest(root: Path) -> dict`、`PreviewDataset(path: Path)`。原快照清单包含 `snapshot_id, captured_at, path, schema_version, file_sha256, tables, integrity, foreign_key_violations`；业务副本清单另有 `exclusions, actor_map, data_from, data_to`，其 `path` 指向服务副本。每表保存行数、主键和业务字段摘要。

- [ ] **Step 1:** 编写测试，包含尚未 checkpoint 的提交、并发追加、损坏外键、非空目标、包含认证信息和长历史的源；业务记录逐表对账，不只比较种子 manifest。

```python
snapshot = capture_snapshot(source, private_root)
result = build_business_snapshot(Path(snapshot['path']), target)
self.assertEqual(snapshot['integrity'], 'ok')
self.assertEqual(snapshot['foreign_key_violations'], [])
self.assertEqual(read_source_rows(source), source_rows_before)
self.assertEqual(find_secrets_in_served_snapshot(result), [])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_snapshot tests.test_preview_dataset -v`；先失败。
- [ ] **Step 3:** 在新私有目录先验证 Windows ACL，再用 URI `mode=ro` 和 SQLite backup 取得一致性快照。记录所有源表及字段处置：业务字段保留，认证表和敏感字段排除，人员引用映射保留；未知表或字段无处置策略则拒绝发布。私有原快照不服务；脱敏副本不含旧密码、会话和命令密钥。摘要流式计算，数据读取不调用迁移、种子、模拟器、真实服务构造器或恢复旧命令。字段排除不改变功图 JSON 原始点值、来源和业务关联。
- [ ] **Step 4:** 重跑两组测试；增加 authorizer 对 INSERT、UPDATE、DELETE、ATTACH、可写 PRAGMA 的拒绝断言，确认源数据库的业务内容未由导入器改变。此处只使用临时源，不操作实际演示库。
- [ ] **Step 5:** Commit `feat: preserve demo data in isolated preview snapshots`。

## Task 3 独立预览服务与会话边界

**Files:** Create `backend/preview/server.py`, `auth.py`, `repository.py`; `tests/test_preview_http.py`, `tests/test_preview_isolation.py`.

**Interfaces:** Consumes `PreviewDataset`、`PreviewMeta` 和目录。Produces `create_preview_server(root: Path, host: str = '127.0.0.1', port: int = 8772) -> ThreadingHTTPServer`、`PreviewRepository.register(method: str, pattern: str, reader: Callable) -> None`、独立会话 `PreviewAuth`。实际服务只允许回环地址；测试可传端口 0。健康仅报告预览自身状态，`control_enabled=false` 固定。

- [ ] **Step 1:** 编写回环 HTTP 失败测试：独立认证、过期退出、Origin 校验、未知接口、路径穿越、编码穿越、符号链接、超限 JSON、认证文件和原库禁止下载；控制及实际发送路径明确拒绝。

```python
self.assertEqual(client.get('/api/preview/health').json()['data']['mode'], 'preview')
self.assertEqual(client.post('/api/commands/x/retry', {}).status, 403)
self.assertEqual(client.get('/private/source.db').status, 404)
self.assertEqual(observed_external_connections, [])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_http tests.test_preview_isolation -v`；先失败。
- [ ] **Step 3:** 独立 HTTP handler 只服务明确列出的预览 HTML、前端模块、样式、公开资料与预览 API；不继承正常 handler 的控制派发。只用独立用户目录中的随机密码及 HttpOnly、SameSite=Strict 会话，写请求检查 Origin 和 CSRF；原演示 Cookie 不被识别。JSON 上限 `1 MiB`，原文预览导入上限 `8 MiB`，列表最多 `100` 条；静态下载无目录列表、无任意文件路径参数。
- [ ] **Step 4:** 重跑测试，并断言创建服务时没有实例化 `SimulatorRegistry`、PG 客户端、MQTT 或采集/算法 worker。缺快照、摘要错误或缺身份配置时不报健康。
- [ ] **Step 5:** Commit `feat: add isolated preview service and authentication`。

## Task 4 演示风格外壳与请求适配

**Files:** Create `preview.html`; `frontend/preview/entry.js`, `shell.js`, `preview.css`, `api.js`, `context.js`, `renderers.js`; `tests/test_preview_shell.py`, `tests/test_preview_transport.py`, `tests/fixtures/preview_shell.html`.

**Interfaces:** Consumes 任务 1 目录、任务 3 HTTP。Produces `createPreviewApi({fetchImpl, csrfToken, confirmRehearsal})` 六方法接口；`mountPreviewShell({root, catalog, api, meta}) -> {navigate, dispose}`；`createPreviewContext({page, params, session, workspace, generation})`。渲染器表仅引用实际实现，后续任务分别追加自己的装配项。

- [ ] **Step 1:** 编写浏览器和模块测试，核对 9 组导航、55 页目录、统一预览提示、旧参数链接、返回和刷新、角色权限、未知路由回到 `dashboard`；记录所有请求，包含上传和下载。

```python
r = run_browser('shell_navigation')
self.assertEqual(r['catalog_pages'], 55)
self.assertTrue(r['legacy_query_roundtrip'])
self.assertEqual(r['outside_preview_requests'], [])
self.assertFalse(r['late_response_replaced_current'])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_shell tests.test_preview_transport -v`；先失败。
- [ ] **Step 3:** 保留 `#app`、`#workspace` 和 toast/modal 容器，复用原样式和 router；不加载 `app.js`。通过 context 注入 API，不修改全局 fetch 或正常 api singleton。URL 必须同源且符合明确路径表，拒绝绝对 URL、协议相对 URL、二次解码穿越；`raw/download` 同样约束。进入新页面或登出时中止请求并 dispose 图表、监听、临时状态，独立 origin 防止与原系统缓存混用。
- [ ] **Step 4:** 重跑两组测试及 `tests.test_center_frontend`，确认正常应用的路径和权限行为未变。
- [ ] **Step 5:** Commit `feat: add full preview navigation and safe transport`。

## Task 5 独立流程演练与岗位视图

**Files:** Create `backend/preview/state.py`, `rehearsals.py`; `frontend/preview/actions.js`, `presenters/rehearsal.js`; `tests/test_preview_rehearsals.py`, `tests/fixtures/preview_rehearsals.html`.

**Interfaces:** Produces `PreviewState(path: Path)`、`save_rehearsal(actor: PreviewActor, request_id: str, action: str, object_id: str, expected_version: int, values: dict) -> dict`；JS `confirmRehearsal({action, objectId, values}) -> Promise<boolean>`。仅保存带 `mode='rehearsal'` 的新增记录；`request_id` 同人同内容幂等，不同内容冲突；后续读取模型只在预览 DTO 上叠加状态。

- [ ] **Step 1:** 写测试覆盖取消不写、同键重试、不一致重试、版本冲突、表单保留、切换岗位和登出后的迟到提交、自审禁止、未知动作及控制重放拒绝。

```python
self.assertEqual(repeat['id'], first['id'])
self.assertEqual(conflict.status, 409)
self.assertEqual(self_approval.status, 403)
self.assertEqual(snapshot_business_hash_after, snapshot_business_hash_before)
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_rehearsals -v`；先失败。
- [ ] **Step 3:** 为编辑、复核、审批、工单、规则、配置草稿、场景切换定义显式动作白名单；旧业务写请求必须先打开“流程演练”确认，再映射到预览状态。终端启停、补发和策略执行只能建立演练记录，不能直接接受旧命令 ID 重放。新参数的算法动作只保存输入或打开标明来源的预置样例，不回传伪造“计算成功”。重置需要用户确认且仅清理预览演练状态，不清理快照、登录身份或页面审查意见。
- [ ] **Step 4:** 重跑测试，验证角色权限和独立审批人关系，保证预览事件和历史业务审计可以区分。
- [ ] **Step 5:** Commit `feat: support isolated page workflow rehearsals`。

## Task 6 综合看板与采油监控九页

**Files:** Create `backend/preview/read_models/monitoring.py`, `fixtures/monitoring.py`; `frontend/preview/pages/monitoring.js`, `presenters/dashboard.js`; `tests/test_preview_monitoring.py`, `tests/fixtures/preview_monitoring.html`. Modify only 本任务在 `renderers.js` 的注册项。

**Interfaces:** Consumes `PreviewRepository`、只读快照、任务 5 演练。Produces `register(repository)` 与 `renderMonitoring(context)`；复用已有 dashboard、wells、well-detail 渲染器，新增六页通过新读取模型提供具单位数据。负责 `dashboard,wells,well-detail,production-monitor,well-realtime,well-history,well-status,fluid-level,haulage-monitor` 共 9 页。

- [ ] **Step 1:** 写 9 页数据与浏览器失败测试，逐页核对设计规定的主要区域、筛选和下钻；使用两口井、两个时段，验证跨页口径、空/错/陈旧状态及长名称。

```python
r = run_browser('monitoring')
self.assertEqual(r['pages_checked'], 9)
self.assertEqual(r['cross_page_totals_mismatch'], [])
self.assertEqual(r['rewritten_source_timestamps'], [])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_monitoring -v`；先失败。
- [ ] **Step 3:** 以只读 SQL 映射旧 API 响应结构；默认时间取快照内最后有效日期，按井组筛选后再聚合。新增液面和罐记录明确列为补充样例；产液与耗电波动来自一致数据集，原数据不重写。暂停实时页仅暂停画面或样例回放，不控制采集服务。
- [ ] **Step 4:** 重跑测试，在两个要求视口检查图表、明细和表单不溢出；逐页截图供最终人工检查。
- [ ] **Step 5:** Commit `feat: build monitoring and well preview pages`。

## Task 7 采集设备八页及完整子视图

**Files:** Create `backend/preview/read_models/acquisition.py`, `fixtures/acquisition.py`; `frontend/preview/pages/acquisition.js`, `presenters/acquisition.js`; `tests/test_preview_acquisition.py`, `tests/fixtures/preview_acquisition.html`. Modify 本组渲染器注册项。

**Interfaces:** Produces `register(repository)`、`renderAcquisitionPreview(context)`；负责 `acquisition,data-quality,devices,field-registry,ingest-reliability,waveforms,channel-mapping,uplink-monitor` 共 8 页。台账、可靠性和波形客户端仍使用原 API 注入接口，响应结构按现有页面契约。

- [ ] **Step 1:** 为 8 页及设备详情、点表版本/审核/绑定、原文与回执、录波导入、校准、配对和数据集子视图写失败测试；导入用临时有界文件，不打开用户任意路径。

```python
r = run_browser('acquisition')
self.assertEqual(r['pages_checked'], 8)
self.assertEqual(r['missing_subviews'], [])
self.assertEqual(r['device_or_broker_writes'], [])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_acquisition -v`；先失败。
- [ ] **Step 3:** 老采集记录读快照，生产目录等补充场景固定 `preview-` 身份及原井映射；显示时间、单位、序号和质量。字段编辑、导入和补发只演练；真实原文、样例原文与格式错误分别展示。点表只允许配置预览，不建立真实连接。
- [ ] **Step 4:** 重跑测试，并运行 `tests.test_registry_browser tests.test_ingest_browser tests.test_waveform_browser` 的现有页面契约回归；不调用其真实数据库 provision 工具。
- [ ] **Step 5:** Commit `feat: build acquisition and device preview workspaces`。

## Task 8 功图与生产反演十页

**Files:** Create `backend/preview/read_models/dynagraphs.py`, `fixtures/dynagraphs.py`; `frontend/preview/pages/dynagraphs.js`, `presenters/dynagraphs.js`; `tests/test_preview_dynagraphs.py`, `tests/fixtures/preview_dynagraphs.html`. Modify 本组注册项。

**Interfaces:** Produces `register(repository)`、`renderDynagraphPreview(context)`；负责 `dynagraphs,dynagraph-analysis,diagnosis-review,dynagraph-tracking,production-metering,card-alarm-rules,standard-cards,production-parameters,production-inversion,production-results` 共 10 页。导入的 `sensor_samples`、转换 JSON 和模型输入输出保持原值；补充图形记录使用新命名空间。

- [ ] **Step 1:** 写 10 页及双图、工况库、自定义工况、参数/结果版本、误差/拒算详情测试；悬停和键盘值必须两位小数，隐藏一条图不改变对比坐标范围。

```python
r = run_browser('dynagraphs')
self.assertEqual(r['pages_checked'], 10)
self.assertTrue(r['dual_toggle_independent'] and r['shared_axes_stable'])
self.assertRegex(r['hover_x'], r'^-?\d+\.\d{2}$')
self.assertEqual(r['changed_raw_points'], 0)
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_dynagraphs -v`；先失败。
- [ ] **Step 3:** 复用细线图表、原样例轮廓和状态/参数区；旧显示欠缺部分用预览专用组合补齐。显式区分电功自变量、反演图与传感器参考图；保留异常窄图、缺口和未知结论。人工复核只追加预览意见。机械参数新输入不调用真实反演或伪造新结果，提供带原始输入引用的样例查看入口。
- [ ] **Step 4:** 重跑测试与 `tests.test_dual_dynagraph_ui tests.test_inversion_browser`；截图检查正常、杆断脱、供液不足、阀漏、碰泵等不同轮廓，不能只检查单一正常图。
- [ ] **Step 5:** Commit `feat: complete dynagraph and inversion preview pages`。

## Task 9 保留五个工程研究页面

**Files:** Create `backend/preview/read_models/studies.py`, `fixtures/studies.py`; `frontend/preview/presenters/studies.js`; `tests/test_preview_studies.py`, `tests/fixtures/preview_studies.html`. Modify 本组注册项。

**Interfaces:** Produces `register(repository)`、`renderStudiesPreview(context)`；负责 `engineering-overview,condition-lab,recovery-lab,motor-lab,study-review` 共 5 页，使用原研究表单和图表的上下文接口。

- [ ] **Step 1:** 测试模型训练/推理、恢复、调度、效果、频谱、保护七类研究的输入结果、拒绝状态、依赖、复核和撤销；记录请求，不允许触发计算引擎。

```python
r = run_browser('studies')
self.assertEqual(r['pages_checked'], 5)
self.assertEqual(r['missing_study_kinds'], [])
self.assertEqual(r['real_engine_calls'], 0)
self.assertTrue(r['modified_input_marked_not_computed'])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_studies -v`；先失败。
- [ ] **Step 3:** 将现有软件样例按受支持结构整理为预置输入结果对，固定算法版本及样例来源；不用当前服务或新运行来填充。表单修改后显示未计算/待接入，不能沿用旧结果冒充新参数结果。保留分井分时、混淆矩阵、逐类评价、拒识、拟合残差、频谱限制和独立复核交互。
- [ ] **Step 4:** 重跑测试及 `tests.test_studies_browser`，确认正常研究页仍保留真实 API 行为，只有预览上下文进入样例演练。
- [ ] **Step 5:** Commit `feat: preserve engineering studies in page preview`。

## Task 10 节能业务六页

**Files:** Create `backend/preview/read_models/energy.py`, `fixtures/energy.py`; `frontend/preview/pages/energy.js`, `presenters/energy.js`; `tests/test_preview_energy.py`, `tests/fixtures/preview_energy.html`. Modify 本组注册项。

**Interfaces:** Produces `register(repository)`、`renderEnergyPreview(context)`；负责 `energy-overview,recovery-analysis,strategy-center,strategy-editor,multiwell-schedule,energy-effects` 共 6 页；`recovery-lab` 已归任务 9，保持相互链接。

- [ ] **Step 1:** 写恢复、策略版本/审批、调度时间轴和效果对照测试，包含无可行制度、覆盖不足、不同单位与跨时段、旧策略 ID 链接。

```python
r = run_browser('energy')
self.assertEqual(r['pages_checked'], 6)
self.assertTrue(r['strategy_version_links_complete'])
self.assertEqual(r['baseline_periods_overlapping_effect_periods'], 0)
self.assertFalse(r['test_savings_labeled_field_verified'])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_energy -v`；先失败。
- [ ] **Step 3:** 恢复和效果读取原始快照完整关联，不把历史追加数据丢成固定种子数量；明确液面深度与产液率的区别。新增调度样例为明确测试计划，调整约束只演练和显示待计算。所有策略执行按钮进入任务 5 的演练流程。
- [ ] **Step 4:** 重跑测试；人工检查开停时长、参数区、观测点与拟合曲线、审批历史及效果页都有对应内容，缺数据场景与正常场景独立。
- [ ] **Step 5:** Commit `feat: build energy optimization preview pages`。

## Task 11 电机业务五页

**Files:** Create `backend/preview/read_models/motor.py`, `fixtures/motor.py`; `frontend/preview/pages/motor.js`; `tests/test_preview_motor.py`, `tests/fixtures/preview_motor.html`. Modify 本组注册项。

**Interfaces:** Produces `register(repository)`、`renderMotorPreview(context)`；负责 `motor-overview,motor-faults,motor-spectrum,protection-settings,protection-events` 共 5 页；`motor-lab` 已归任务 9。

- [ ] **Step 1:** 写五页、保护时间线、表单及频谱检查；覆盖缺相、不平衡、温升、疑似边带、无转速、低分辨率、故障锁存与不可复位场景。

```python
r = run_browser('motor')
self.assertEqual(r['pages_checked'], 5)
self.assertFalse(r['missing_speed_claims_confirmed_broken_bar'])
self.assertEqual(r['physical_reset_requests'], 0)
self.assertTrue(r['thresholds_identified_as_test_settings'])
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_motor -v`；先失败。
- [ ] **Step 3:** 使用可复现测试波形与预置频谱，轴、单位、采样率、分辨率、证据和不确定性同时展示；不从图形相似推出实际断条结论。整定表保留依据、版本、延时、迟滞、锁存和复位条件，所有阈值只是测试样例，没有下发能力。
- [ ] **Step 4:** 重跑测试，检查窄频段悬停、长事件表和整定弹窗在两种视口都可用。
- [ ] **Step 5:** Commit `feat: build motor diagnostics and protection preview pages`。

## Task 12 协同报表与系统管理十一页

**Files:** Create `backend/preview/read_models/workflows.py`, `administration.py`; `frontend/preview/presenters/workflows.js`, `administration.js`; `tests/test_preview_workflows.py`, `tests/test_preview_administration.py`, `tests/fixtures/preview_workflows.html`, `preview_administration.html`. Modify 本组注册项。

**Interfaces:** 两个读取模块分别导出 `register(repository)`；前端导出 `renderWorkflowPreview(context)`、`renderAdministrationPreview(context)`。负责 `approvals,commands,device-command,alarms,work-orders,reports,users,roles,audit,system-health,acceptance-center` 共 11 页。

- [ ] **Step 1:** 测试详情、回执、报警转工单及处理闭环、六类报表、账号角色弹窗、审计和年度技术任务；业务快照与预览状态分开核对。

```python
r = run_browser('workflows_administration')
self.assertEqual(r['pages_checked'], 11)
self.assertEqual(set(r['report_types']), {'production','diagnosis','data-quality','strategy','energy','work-orders'})
self.assertEqual(r['replayed_historical_commands'], 0)
self.assertEqual(r['annual_task_count'], 12)
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_workflows tests.test_preview_administration -v`；先失败。
- [ ] **Step 3:** 历史指令只展示业务摘要、阶段和关联，不读取签名密钥；流程修改写演练库。报表与看板使用相同时间/井范围；CSV 处理公式开头的单元格及中文编码，导出无认证字段。管理员全页面可见仍禁止同身份复核；系统健康区分真正的预览服务状态与样例设备状态；年度入口不新增绩效申报。
- [ ] **Step 4:** 重跑测试，核查五岗位视图、旧 `id/type` 深链接和导出数据范围；不得用每个岗位都返回管理员数据作为快捷实现。
- [ ] **Step 5:** Commit `feat: preserve workflows reports and administration preview`。

## Task 13 页面审查与开放资料入口

**Files:** Create `backend/preview/page_reviews.py`; `frontend/preview/pages/review.js`; `tests/test_preview_page_reviews.py`, `tests/test_preview_open_interface.py`, `tests/fixtures/preview_reviews.html`. Modify `shell.js` 的资料链接与本页注册项。

**Interfaces:** Produces `save_page_review(actor: PreviewActor, page_id: str, content_version: str, expected_version: int, decision: str, comment: str) -> dict`；`decision` 为 `pending/changes_requested/approved`，只在用户明确操作时记录确认。页面 key 为 55 个 ID 或 `open-interface`；读取 `get_page_reviews(catalog_version: str) -> list[dict]`。前端 `renderPageReviews(context)` 负责第 55 页 `preview-review`。

- [ ] **Step 1:** 测试初始全部待确认、意见持久、版本冲突、页面内容版本变更后重新待确认、重启持久和不存在页面拒绝；验证公开入口及 `#hardware`，下载不得包含任何数据快照或密钥。

```python
self.assertEqual(len(initial_reviews), 56)
self.assertTrue(all(x['decision'] == 'pending' for x in initial_reviews))
self.assertEqual(review_after_content_change['decision'], 'pending')
self.assertEqual(concurrent_stale_update.status, 409)
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_page_reviews tests.test_preview_open_interface -v`；先失败。
- [ ] **Step 3:** 提供按模块和任务筛选、逐页入口、场景切换、修改意见和导出；助手的测试不自动替用户确认。公开资料使用现有受控静态资产及下载包，不生成虚假硬件认证。页面审查状态与流程演练重置分离。
- [ ] **Step 4:** 重跑测试，确认修改旧反馈需匹配版本并保留历史，页面审查通过不会解锁真实控制或生产写入。
- [ ] **Step 5:** Commit `feat: add page signoff and open-interface preview entry`。

## Task 14 集成检查与独立预览交付

**Files:** Create `tools/page_preview.py`, `tools/verify_page_preview.py`; `tests/test_preview_lifecycle.py`, `tests/test_preview_end_to_end.py`; `docs/页面预览使用说明.md`. Modify 目录中各页 `content_version` 的打包校验逻辑，不修改正常部署工具。

**Interfaces:** CLI `python -B -m tools.page_preview prepare --source-db <explicit-path> --target <new-target>`、`launch --target <target>`、`status --target <target>`、`stop --target <target>`；`verify_page_preview --target <target> --output <private-report>`。prepare 执行已测试的任务 2，不允许隐式选择源；launch 只开预览服务，不启动其他后台组件。

- [ ] **Step 1:** 生命周期测试覆盖目标已有、端口占用、错误快照摘要、错误身份、启动未就绪、异常退出和停止陌生进程拒绝；集成测试用临时完整快照遍历 55 页和开放资料，检查网络隔离及审查记录持久。

```python
self.assertEqual(result['application_pages'], 55)
self.assertEqual(result['open_interface_entries'], 1)
self.assertEqual(result['outside_preview_requests'], [])
self.assertEqual(result['page_errors'], [])
self.assertEqual(result['source_business_changes_by_preview'], 0)
```

- [ ] **Step 2:** Run `& $previewPython -B -m unittest tests.test_preview_lifecycle tests.test_preview_end_to_end -v`；先失败。
- [ ] **Step 3:** 实现显式安装与启动 CLI，后台进程隐藏运行并核对自身 PID、目录和实例标识；健康就绪等待上限 `120 秒`，每秒检查一次并提供初始化状态，不使用固定 15 秒窗口。超时仅停止本次启动且归属匹配的子进程并保留日志。私有账号、原快照和证据保持 ACL 保护；启停只针对所属预览进程，未运行时幂等停止。
- [ ] **Step 4:** Run `& $previewPython -B -m unittest discover -s tests -p 'test_preview_*.py' -v`；所有预览测试通过。
- [ ] **Step 5:** Run `& $previewPython -B -m unittest tests.test_center_frontend tests.test_dual_dynagraph_ui tests.test_registry_browser tests.test_ingest_browser tests.test_waveform_browser tests.test_inversion_browser tests.test_studies_browser -v`；正常页面及模式隔离契约回归通过。完成独立整体代码审查并修复问题，复跑受影响测试。
- [ ] **Step 6:** 提交固定代码来源，commit `feat: deliver isolated full-page preview`；从该提交建立独立预览安装，用实际演示运行副本的一致性快照逐表核对，并在两种视口逐页操作和保存截图。人工检查截图后记录页面完整度和剩余问题；样例测试通过不能代替此实际数据检查。安装中发现代码问题时回到工作树修复、测试并形成新固定版本，不临时改安装文件。
- [ ] **Step 7:** 保存实际验证及交付说明，文档提交不得改变已验证代码；报告 URL、账号取得方式、快照时间和对账结果、页面数及实际测试结果。提供逐页审查入口，等待用户页面确认；不自动扩展真实功能，也不把尚未确认的页面批量标成通过。

## 顺序与分组

任务 1→2→3→4→5 先建立共同契约、数据隔离及可演练外壳。任务 6—12 按业务分组推进，允许在公共接口冻结后分配独立目录进行并行制作；公共装配文件由主实施者统一整合，避免互相覆盖。任务 13 可在目录和状态接口稳定后制作。任务 14 在所有分组通过后执行。

每组只运行对应测试，涉及公共边界时跑关联回归；实际快照导入和全页浏览器检查集中在关键集成点，不为每页重复启动全套数据库回归。出现新权限需求、数据源不一致或必须改变已确认范围时停止相关组并报告，其他独立安全工作可以继续。

## 计划核对

- 页面归属为任务 6 的 9 页、任务 7 的 8 页、任务 8 的 10 页、任务 9 的 5 页、任务 10 的 6 页、任务 11 的 5 页、任务 12 的 11 页、任务 13 的 1 页，合计 55；无重复路由。
- 开放资料入口和登录界面不算新增应用页；24 个演示旧页和 11 个开发页完整保留。
- 数据完整性、认证隔离、图表格式、典型工况、子视图、全部状态、两种视口、逐页确认及冻结版本保留均有对应任务和验证。
- 本计划不包括新增真实采集、控制、算法、保护或生产部署。现有后端及历史验证资料保留，页面审查通过后再制定接入任务。
