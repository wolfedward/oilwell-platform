# Production Monitoring Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认的 55 页产品基线中的首批 8 页接入真实台账、接收事实、历史查询和质量处置，交付独立、可保存与追溯的软件工作台。

**Architecture:** 保持 `runtime_mode='review'`，新增独立工作台入口、身份库和 Cookie。新增只读 `/api/production-monitoring` 查询层；写入继续使用 registry 和 ingest-reliability，保留现有事务、权限、版本与幂等契约。其余 47 页保留目录及完整预览入口，不混入业务查询。

**Tech Stack:** 现有 Python HTTP 服务、psycopg/PostgreSQL、SQLite 身份库、原生 ES modules、现有图表组件、unittest、Edge/Playwright；复用已锁定依赖，不增加前端框架。

**Spec:** `docs/superpowers/specs/2026-10-13-production-monitoring-workbench-design.md`

状态：2026-10-13 用户确认后，任务 1—9 已实施；独立审查发现的三个重要问题已补充失败回归并修复，最终分层检查 129/129 通过。本文件以下清单保留原计划步骤，实际交付、固定版本与证据见 `docs/production-monitoring-workbench.md`。本批仅接入 8 页，其余 47 页与现场验收未完成。

## Global Constraints

- 工作树：`C:/Users/hp/.codex/worktrees/production-device-registry/外专双百`；实施前从已确认设计提交 `2d4bd43` 的后续计划提交建立 `codex/production-monitoring-workbench` 分支，不移动安装的固定版本。
- 原工作目录、冻结演示目录及 ZIP、Git 标签不修改；8765 演示、8770 审查、8772 完整预览的代码、数据与启动配置不覆盖。8772 的 659,408 条业务记录不缩减、不改时间、不迁作真实采集事实。
- 新安装目标 `C:/Users/hp/AppData/Local/OilWell/monitoring-workbench-20261013`，HTTP `8774`，仅 `127.0.0.1`；目标存在或端口占用就拒绝，不停止未知进程。数据库、身份库、配置、凭据全部独立。
- Cookie 为 `oilwell_monitoring_session`；不得接受或覆盖 `platform_session`、`oilwell_page_preview`。禁止预览角色切换、`confirmRehearsal`、旧模拟业务回退。
- 页面路由为 `wells`、`well-detail`、`field-registry`、`channel-mapping`、`acquisition`、`data-quality`、`well-realtime`、`well-history`；全部 55 个名称和目录入口保留，不批量批准页面审查。
- 单项目岗位授权；管理员权限合集仍受独立复核、状态和版本限制。区域筛选不是多租户隔离。井 UUID 不接受 `W-001` 的模糊映射。
- 历史窗口最长 `31 天`；每页最多 `200` 条、偏移最多 `100,000`；趋势最后 `600` 原始点；CSV 最多 `10,000` 行或 `10 MiB`；实时每页最多 `32` 测点。
- 查询上下文保留 `registry_revision`、`received_before`、`query_total`，台账修订变化为 `409`；过滤先于分页；时刻必须带时区，UTC 查询、北京时间展示。UUID、版本、序列、大计数以字符串传输。
- 刷新默认 `10 秒`，仅允许 `5、10、30、60 秒`；退出、切页、权限变化取消旧请求。缺测为 null；bad 和单位不符不连有效趋势；uncertain 单独标识。已知间隔超过 `1.5 倍` 断线，否则中位间隔仅作显示依据。
- 图表细线，悬停或键盘选点显示两位小数与单位；存储、明细和导出保留原精度。日报、功图、产量、节能没有来源则保留待接入，不造数。
- 点表 `1 至 256` 测点，同版代码唯一，Modbus 仅功能码 `3/4`、零起始地址、`writable=false`；服务端规范化和摘要；不同身份审核具体版本后才能指派；边缘连接参数不假保存。
- 原因 `2—500` 字符，UUID 幂等键，变更携带打开表单时的 `expected_version`；不确定结果只按原键原文重试。新请求重新鉴权，不承诺跨库在途事务与撤权的原子性。
- 禁止回执重发、设备重试、配置下发、任意 MQTT 发布、Modbus 写、补零、修原始质量、隐式激活。查询账号不能接触原始敏感报文或写权限。
- 测试资料通过独立软件测试链路进入新测试库，保留来源和时刻；没有页面按钮改为现场实测。无设备时不承诺计量精度、节能率、现场稳定性或保护安全。

## Review Focus

1. 同井两个设备都有相同测点代码：实时和历史不能串线；用 `(registry_device_id, point_code)` 定位序列。任务 3、7 固化测试。
2. 最新点异常、相同时刻多点、迟到补传：保留真实最新值和稳定排序，不回退旧好值；翻页、趋势、CSV 对账。任务 3 固化测试。
3. 单包跨迁井边界、接收事实没有井归属：按样本时刻解析，未解析事实仍可在设备范围查见；不把整包划给当前井。任务 4 固化测试。
4. 网络中断重试期间撤权、过期或另一个账号登录：保留原请求内容但重新鉴权，不让旧表单或 Cookie 继续写。任务 1、6、9 固化测试。
5. 新数据、台账修订或案件状态在翻页和导出间变化：冻结事实截止与总数，拒绝混用台账修订；案件当前处置状态另给查询时刻，不能伪称跨请求历史快照。任务 3、4、9 固化测试。

## 文件边界与执行约定

`backend/monitoring/` 管查询契约、SQL、导出和 HTTP；`frontend/workbench/` 管独立壳、上下文与本批页面；不将业务能力写进 preview。`tests/monitoring_support.py` 管新建测试集群；现有 `tests/registry_support.py` 和 `tests/ingest_support.py` 的旧库清空逻辑不得复用。部署扩展复用已有目标占用检查、固定提交导出、进程归属及备份工具，默认 review 行为保持不变。

以下命令均在本工作树执行。先设置 `$MonitoringPython='C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe'`、`$MonitoringGit='C:/Users/hp/AppData/Local/hermes/git/cmd/git.exe'`。每项保存 RED、GREEN、提交号的实际记录；写完测试先验证因目标行为缺失而失败，依赖或环境故障不算 RED。每项只暂存列明文件，禁止 `git add .`。文中的测试片段是必须覆盖的断言，不是完整测试实现。

### Task 1: 独立会话与运行边界

**Files:** Create `backend/session_cookies.py`, `tests/test_monitoring_session.py`; Modify `backend/http_server.py`, `backend/studies/api.py`, `server.py`。

**Interfaces:** 增加 `create_server(..., ui_mode: str='standard', monitoring_runtime=None)`，只允许 `standard/monitoring`，monitoring 必须配合 review。`cookie_name(server)->str` 与 `read_session_cookie(headers, name: str)->str|None` 放在 session_cookies；重复同名 Cookie 拒绝。CLI `--ui-mode` 在启动和隐藏子进程重建时完整传递。`/api/auth/me` 增加 `ui_mode` 与 `capabilities.monitoring`（初期 disabled，任务 3 接通）。

- [ ] 写失败测试：`test_monitoring_cookie_isolated` 检查 login/logout 只改专用 Cookie；`test_monitoring_gate` 检查 demo+monitoring 拒绝、模拟器为空、旧控制及 receipt resend 不可用；`test_reauthentication_on_retry` 检查过期和撤权后重新提交、操作回执查询及重试被拒绝。
  ```python
  self.assertIn('oilwell_monitoring_session=', login_cookie)
  self.assertNotIn('platform_session=', login_cookie)
  self.assertEqual(old_cookie_request.status, 401)
  self.assertEqual(revoked_retry.status, 403)
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_session -v`，预期新参数或隔离断言失败。
- [ ] 实现会话名称统一读取：主 handler 和 StudyApi 都使用新 helper，StudyApi 保持只接受 Cookie 的既有语义；Bearer 保持独立身份库验证，失败不回退 Cookie。monitoring 登录、退出及 Cookie 写操作要求精确本源，既有 registry/ingest Origin 校验保留；新工作台 HTTP 白名单只放认证、本批 registry、monitoring 读接口、案件四类操作与独立原文读取，不开放重发。standard 行为不改。
- [ ] GREEN：重跑本项命令，并跑 `& $MonitoringPython -B -m unittest tests.test_review_runtime -v`，旧模式运行边界必须保持通过。
- [ ] 提交：`& $MonitoringGit add -- backend/session_cookies.py backend/http_server.py backend/studies/api.py server.py tests/test_monitoring_session.py`，然后 `& $MonitoringGit commit -m 'feat: isolate monitoring workbench sessions'`。

### Task 2: 最小权限查询运行时和全新数据库夹具

**Files:** Create `backend/monitoring/__init__.py`, `backend/monitoring/config.py`, `backend/monitoring/privileges.py`, `backend/monitoring/contracts.py`, `backend/monitoring/errors.py`, `backend/monitoring/sql/grants.sql`, `tests/monitoring_support.py`, `tests/test_monitoring_runtime.py`, `tests/test_monitoring_privileges_pg.py`。

**Interfaces:** `load_monitoring_config(path)->MonitoringConfig|None` 读取 `[monitoring] enabled/environment/source_ref` 与 `[postgres_monitoring]` 连接配置；config 定义为 frozen dataclass，字段 `postgres:dict`（repr 隐藏）、`environment:str`、`source_ref:str`，无内嵌密码，沿用已有 secret store/环境变量加载方式。environment 仅 `test/production`，本次安装固定 test，无网页改标。`probe_reader(connect)->None` 验证实际账号权限，启动不迁移不授予。`MonitoringError(code,message,status=422)`、`CsvExport(filename:str,body:bytes)`；`parse_filters(resource:str,query_string:str)->dict[str,str]` 拒绝重复/未知参数，`validate_filters(resource:str,supplied:dict[str,str])->dict` 拒绝无时区与越界输入。

- [ ] 写失败测试：`test_disabled_never_falls_back`、`test_query_limits`、`test_reader_rejects_excess_privileges`、`test_fixture_refuses_existing_cluster`。检查 31 天可接受、超过拒绝，201 行/100001 偏移/33 测点拒绝，无时区、重复参数拒绝；测试库连接身份不符时不执行任何 DDL。
  ```python
  self.assertIsNone(load_monitoring_config(disabled_path))
  self.assertEqual(disabled_connect_calls, [])
  self.assertFalse(reader_can_write_or_read_raw)
  self.assertEqual(existing_target_ddl_calls, [])
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_runtime -v`。
- [ ] 实现独立 reader 的逐表或逐列授权：registry 需要的 wells/devices/head/有效期表/点表；public.telemetry_samples；public.ingest_messages 仅非 raw_payload 列；ingest 的 case_metadata/receipt_metadata 等所需安全视图。验证无 DML/DDL、危险函数执行、raw 读取、角色继承或提权能力；不沿用 writer/owner/宽泛 ALL TABLES 作为查询角色。SQL 使用完整模式名；授权分别由表所属 owner 执行。
- [ ] 实现 `MonitoringDatabase` 上下文夹具：用新临时目录、空闲 loopback 端口和独立随机凭据创建自有集群，复用 review_deploy_pg 的初始化和 schema 顺序，再添加专用 reader。每次连接核对数据库和角色，通过集群拥有者的独立探针核对 system identifier，不给 reader 额外管理权限；每个测试使用独立实体，不清空旧库。结束只停止已证明归属的进程，保留失败日志，不自动递归删除。供后续任务使用 `db.connect(role:str)->psycopg.Connection` 和 `db.seed(scenario:str)->dict`；seed 仅在自有测试集群用既有台账服务、接收入口建立场景，返回创建的井/设备/消息/测点 ID 字典。任务 3、4、9 在此夹具追加所需场景，不直接修改运行中业务库。
- [ ] GREEN：本项 unit 命令及 `& $MonitoringPython -B -m unittest tests.test_monitoring_privileges_pg -v`；必须实际运行 PG，不把跳过当通过。通过猴子补丁/新建专用测试角色验证越权拒绝，不提升已有业务账号。
- [ ] 提交仅本项文件：`& $MonitoringGit add -- backend/monitoring tests/monitoring_support.py tests/test_monitoring_runtime.py tests/test_monitoring_privileges_pg.py`，然后提交 `feat: add least-privilege monitoring reader configuration`。

### Task 3: 井详情、最新观测、历史及一致导出

**Files:** Create `backend/monitoring/queries.py`, `backend/monitoring/well_queries.py`, `backend/monitoring/telemetry_queries.py`, `backend/monitoring/exports.py`, `backend/monitoring/api.py`, `backend/monitoring/runtime.py`, `tests/test_monitoring_wells_pg.py`, `tests/test_monitoring_history_pg.py`, `tests/test_monitoring_api.py`; Modify `backend/monitoring/contracts.py`, `backend/http_server.py`, `tests/monitoring_support.py`, `tests/test_monitoring_runtime.py`。

**Interfaces:** 消费任务 2 config/probe/filters。`build_monitoring_runtime(path)->MonitoringRuntime`，字段 `status/query/code/environment/source_ref`，disabled/unavailable 时 query=None；ready 时 `MonitoringQueries(connect, *, environment:str,source_ref:str,online_seconds:int,delayed_seconds:int,clock_tolerance_seconds:int)`，阈值从同文件 registry 配置读取，不另造设备周期。`read(resource:str,filters:dict[str,str],permissions:frozenset[str])->dict`、`export(resource:str,filters:dict[str,str],permissions:frozenset[str])->CsvExport`；`MonitoringApi(handler).get(path:str)->bool`。resource 为 `well/latest/history/wells_csv/history_csv`，下一项增补事实/质量。公共响应含 `context={registry_revision,received_before,query_total,queried_at,source_ref,environment}`；列表加 `items,total,page,page_size`，total 为字符串。输入沿用 registry 的 `revision`，由响应的 registry_revision 回填。

- [ ] 写失败测试：详情 404、当前/历史绑定分页、无权限摘要不查询；迁井有效期左闭右开；同井同名测点分设备；最新异常值不回退；稳定次序 `(measured_at,message_id,sample_index)`，跨设备序列不混排；filter 在 SQL 分页前执行；600 点截取不影响明细/导出；截止后到达记录不混入。
  ```python
  self.assertEqual(len(latest_same_code['items']), 2)
  self.assertEqual(latest_bad['items'][0]['observation']['quality'], 'bad')
  self.assertEqual(history['trend']['point_count'], 600)
  self.assertTrue(history['trend']['truncated'])
  self.assertEqual(csv_sample_ids, table_sample_ids)
  self.assertEqual(changed_revision.status, 409)
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_wells_pg tests.test_monitoring_history_pg tests.test_monitoring_api -v`。
- [ ] 实现每次同一 REPEATABLE READ READ ONLY 事务，statement 3s、lock 1s、idle 10s、UTC；复用历史有效期与 `resolution.assess` 语义，禁止 N+1 或遍历所有页找井。well 响应为 `well,bindings,availability`；bindings 用 `binding_scope=current/history` 及页参数，明确起止时刻。well/wells_csv 要求 registry.read；latest/history/history_csv 还要求 telemetry.read。可选遥测/链路/案件区分别追加 telemetry.read/acquisition.read/ingest.read，局部查询使用 savepoint，失败为 unavailable，不回零；主依赖不可用 503。禁用配置和运行时故障绝不查询旧 SQLite 数据。
- [ ] 实现 latest 的设备+测点分页、预期测点空槽 `observation=null`；history 必须明确 `well_id,registry_device_id,point_code,from_time,to_time`，quality 可选。返回采集/接收时间、原单位、source、消息引用、解析原因；`application_status=unverified`、`eligible_for_automation=false`。未知点在质量页查，不冒作已配置测点。设备当前绑定或状态不重分配历史样本。
- [ ] 实现 GET spec 中 well/latest/history/history.csv，以及台账导出 `GET /api/production-monitoring/wells.csv`（与 `/api/registry/wells` 同筛选和 revision/cutoff，客户端传列表 total 为 query_total）。翻页/导出要求首屏上下文并核对总数；trend 元数据含 `from_time,to_time,point_count,truncated,interval_basis`，最后 600 点按时刻正序绘制。CSV 先完整有界生成，10,001 行或字节超过 10×1024×1024 时返回 422，不发送部分文件；文本公式前缀 `= + - @` 及控制字符安全处理，数值字段保留数值语义和精度。
- [ ] GREEN：重跑本项命令；另加 `test_export_boundary_10000_and_10mib`、`test_csv_formula_and_unicode`、`test_timezone_and_large_integer`，核对大整数原样、31 天边界、中文和换行，不在浏览器 Number 化 ID。
- [ ] 提交：暂存本项列明文件后 `& $MonitoringGit commit -m 'feat: query well telemetry with consistent exports'`。

### Task 4: 接收事实和三类质量查询

**Files:** Create `backend/monitoring/acquisition_queries.py`, `backend/monitoring/quality_queries.py`, `tests/test_monitoring_acquisition_pg.py`, `tests/test_monitoring_quality_pg.py`; Modify `backend/monitoring/queries.py`, `backend/monitoring/api.py`, `backend/monitoring/contracts.py`, `backend/monitoring/sql/grants.sql`, `tests/monitoring_support.py`。

**Interfaces:** Task 3 的 read 增加 `messages/message/quality`；对应 `/acquisition/messages[/{id}]`、`/quality?view=observations|resolution|cases`。message id 使用已入库的字符串 message_id；关联 fact/case/receipt id 仍为 UUID。message 详情用 page/page_size 分页 well_ids，另返 well_count/unassigned_count；summary 的样本数不随井集合分页改变。公共 context 不变；可变处置信息额外返回 `status_queried_at`、实体 version，不能声称是 received_before 时的历史状态。

- [ ] 写失败测试：正常报文单次计数、重复报文不重复累加遥测、入库状态与 broker_confirmed 分栏；单包在迁井前后同时归属两个井；未注册/未绑定数据在设备或未解析视图可见；三个口径不求和；ingest.read 缺失时不泄漏案件和回执；普通读取不返回原文。
  ```python
  self.assertEqual(message['sample_count'], '2')
  self.assertEqual(set(message['well_ids']), {old_well, new_well})
  self.assertNotIn('raw_payload', message)
  self.assertFalse(resolution['items'][0]['eligible_for_automation'])
  self.assertEqual(cases['scope'], 'current_case_state')
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_acquisition_pg tests.test_monitoring_quality_pg -v`。
- [ ] 实现 messages 基于 public 入库事实并安全关联 metadata，不从多张 receipt/case join 重复计数；隔离但未入库的报文留在案件视图，不冒充成功入库。acquisition.read 为基础，receipt/case 内容另需 ingest.read，井归属另需 registry.read；无权限不查询也不返回该分区计数。摘要列样本时间范围、样本数、来源集合；归属集合最多按 200 条分页，沿用详情接口，不无界展开。
- [ ] 实现 observations 按 quality、resolution 按历史解析原因、cases 按既有案件 state 筛选。observations 要求 data_quality.read+telemetry.read，resolution 另加 registry.read，cases 要求 ingest.read；任何带井筛选的分支再加 registry.read。前两类按样本 received_before，cases 按案件事实接收 cutoff，当前状态筛选/排序加状态查询时刻；若变更导致首屏 total 不一致返回 409。全局或设备范围保留未解析项；选井时明确只是该井范围，并提供未解析入口。
- [ ] GREEN：本项命令及 Task 3 API 测试；增加状态变更与分页交错、超长消息 ID、未知参数、503 不暴露数据库异常或凭据的断言。
- [ ] 提交：暂存本项列明文件后 `& $MonitoringGit commit -m 'feat: expose acquisition facts and quality views'`。

### Task 5: 55 页目录的独立真实工作台

**Files:** Create `workbench.html`, `frontend/workbench/app.js`, `frontend/workbench/context.js`, `frontend/workbench/api.js`, `frontend/workbench/routes.js`, `frontend/workbench/styles.css`, `tests/monitoring_browser_support.py`, `tests/fixtures/monitoring_shell.html`, `tests/test_monitoring_shell_browser.py`; Modify `backend/http_server.py`。

**Interfaces:** `createWorkbenchApi(baseApi,{signal,isCurrent})->Api` 包装 `frontend/api.js` 的真实 api，提供 `get(path,options={})/post(path,body,options={})/download(path,options={})`，路径白名单、AbortController 和迟到结果代际检查。`createWorkbenchContext({auth,page,params,root,navigate})->WorkbenchContext` 提供 api/components/format/workspace=root/page/pageHeader/params/permissions:Set/actorId/authSession/capabilities/registerDispose/isCurrent/navigate/dispose；沿用既有 renderer context 字段，不引入 preview/context 或 preview/api。`renderWorkbenchRoute(context)->Promise<void>` 根据 catalog 与八页 renderer 映射分发。浏览器夹具提供 `run_browser(case:str,*,width=1366,height=768,data=None)->dict`，新 runner 复用现有 Node/Edge 路径，替身仅在边界，不调用 preview。

- [ ] 写失败浏览器测试：55 名称/路由完全匹配 catalog，只有指定 8 页为接入入口；其余页面展示待接入和明确的 `http://127.0.0.1:8772/` 完整预览链接；权限不足不是空数据。旧井链接 W-001 有环境不匹配说明；无预览换角色控件；所有 fetch 无 `/api/preview` 和旧模拟路径。
  ```python
  self.assertEqual(result['catalog_count'], 55)
  self.assertEqual(set(result['connected_ids']), CONNECTED_EIGHT)
  self.assertEqual(result['forbidden_requests'], [])
  self.assertFalse(result['late_response_replaced_new_route'])
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_shell_browser -v`；测试使用新 fixture/Edge runner，不启动旧版本或调用 preview 的造数适配器。
- [ ] 实现新入口：monitoring 模式 `/` 服务 workbench.html，标准模式仍 index.html；静态路径安全规则保留。只读载入 `frontend/preview/catalog.json` 的目录元数据，不调用预览服务；公共视觉组件可复用。登录后读取真实 auth/me，切页与会话变更 dispose；页面明确功能测试环境、来源与截止时刻。API 白名单按具体操作，不允许整个 ingest 前缀任意写。
- [ ] 实现通用状态：loading/empty/forbidden/unconfigured/unavailable/stale/conflict 分开；401 回登录、403 不重试、404 保留返回入口、409 用户确认后重查、422 定位字段、503 保留表单。保护用户字符串用 textContent，download 错误按 JSON 展示而非下载成 CSV。
- [ ] GREEN：本项浏览器命令在 1366 和 1920 两宽均通过；保持键盘导航，检查长中文、长 ID、无横向整页溢出。此阶段未完成的八页 renderer 显示“功能接入中”，不暂填数据冒充完成。
- [ ] 提交：暂存本项列明文件后 `& $MonitoringGit commit -m 'feat: add independent 55-page monitoring shell'`。

### Task 6: 台账、单井与点表真实保存

**Files:** Create `frontend/workbench/pages/wells.js`, `frontend/workbench/pages/well-detail.js`, `frontend/workbench/pages/field-registry.js`, `frontend/workbench/pages/channel-mapping.js`, `tests/fixtures/monitoring_registry.html`, `tests/test_monitoring_registry_browser.py`; Modify `frontend/workbench/routes.js`；仅在复用受阻时最小扩展 `frontend/registry/forms.js`, `frontend/registry/history.js`，不得改变旧调用默认行为。

**Interfaces:** 四文件分别导出 `renderWells/renderWellDetail/renderFieldRegistry/renderChannelMapping(context:WorkbenchContext)->Promise<void>`。复用 `createRegistryClient(api,{isCurrent})` 的 beginMutation/submitMutation/dispose 与 `mountRegistryForms({root,api,client,components,permissions,onChanged,isCurrent})`；设备历史复用 `mountRegistryHistory`。井详情消费 Task 3，台账列表与所有写入用现有 registry 路由。

- [ ] 写失败测试：建井/改名/绑定井不可归档；设备各状态与链路分栏；变更后刷新版本和审计；双击只一写；不确定网络结果同键原文重试；旧版本 409 不覆盖；另一账号登录使旧表单失效；独立复核与 Modbus/MQTT 表单分别验证。
  ```python
  self.assertEqual(first_request['body'], retried_request['body'])
  self.assertEqual(result['self_review_successes'], 0)
  self.assertEqual(result['submitted_edge_config_fields'], [])
  self.assertEqual(result['assignment_state'], 'suspended')
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_registry_browser -v`。
- [ ] 实现井建档 area_code/well_code/name、改名/归档及相同筛选导出；列表分页不靠一次拉全。详情分页展示当前/历史绑定、设备链路和可用数据入口，缺权限不显示可推断的汇总；产液/功图/节能/单位井组保留待接入。设备建档、编辑、启停退役、绑定解绑沿用现有字段与状态约束，不新增硬件投运声明。
- [ ] 实现点表创建、具体版本/摘要复核、指派和历史，1/256 边界、257/重复代码拒绝；Modbus 类型沿用 uint16/int16/uint32/int32/float32/float64 及原有字节/字序验证；writable 固定 false。服务器生成摘要，界面展示读回结果；指派前提示可能 suspended，完成后重查。边缘 IP/串口/波特率/周期显示未接入，不创建本地假配置。原因长度、表单捕获版本、权限失效和草稿保留使用现有错误语义。
- [ ] GREEN：本项与 `& $MonitoringPython -B -m unittest tests.test_registry_browser -v`；另核权限合集并不绕过自审、来源或状态限制。
- [ ] 提交：暂存本项实际改变的列明文件后 `& $MonitoringGit commit -m 'feat: connect well registry and point map workflows'`。

### Task 7: 实时、历史和可核对的趋势

**Files:** Create `frontend/workbench/pages/well-realtime.js`, `frontend/workbench/pages/well-history.js`, `frontend/workbench/telemetry-chart.js`, `tests/fixtures/monitoring_telemetry.html`, `tests/test_monitoring_telemetry_browser.py`; Modify `frontend/workbench/routes.js`。

**Interfaces:** `renderWellRealtime/renderWellHistory(context:WorkbenchContext)->Promise<void>`；`mountTelemetryChart(root:Element,{items,unit,intervalSeconds,intervalBasis})->{dispose():void}`，items 使用 Task 3 原始观测，不能修改数据精度。实时 request 使用页参数选择最多 32 个设备+测点组合；历史的所有翻页和 CSV 复用首屏 context。

- [ ] 写失败测试：默认 10 秒和四种刷新选项，暂停显示停留时刻、切页注销取消请求；两个同码设备独立显示；缺测 null 不补零；最新 bad 可见但不连有效线；uncertain 标记、间隔缺口断线；hover 与键盘两位小数，导出原精度；600 点截取提示与表格总数不同。
  ```python
  self.assertEqual(result['refresh_choices'], [5, 10, 30, 60])
  self.assertEqual(result['hover_value'], '12.35 kN')
  self.assertEqual(result['stored_value'], 12.34567)
  self.assertEqual(result['missing_value'], None)
  self.assertEqual(result['requests_after_dispose'], 0)
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_telemetry_browser -v`。
- [ ] 实现实时展示测点、设备、单位、质量/解析原因、采集/接收时刻、年龄和消息引用；不依据在线判定数据新鲜。历史必须选定设备+测点，时间和 quality 在服务端过滤，显示实际绘图范围/点数/截取状态，日报保留明确空态。筛选变化清空旧 context，409 不自动混页；CSV 按同上下文请求，不从屏幕 600 点拼文件。
- [ ] 实现细线图、正常点隐藏而鼠标/键盘选中才显点和两位坐标；bad、unit_mismatch 断开有效趋势，uncertain 样式区分；已知周期或正间隔中位数决定显示缺口，重复时刻不产生零周期，数据不足显示更新周期未配置。大计数以字符串显示不转 Number。
- [ ] GREEN：本项浏览器命令，两宽并行核对；确认 UI 刷新只发 GET，不改采样配置；重新跑 Task 3 的 history PG 测试验证表格、CSV 与图的一致范围。
- [ ] 提交：暂存本项列明文件后 `& $MonitoringGit commit -m 'feat: connect realtime and historical telemetry views'`。

### Task 8: 采集与质量处置页面

**Files:** Create `frontend/workbench/pages/acquisition.js`, `frontend/workbench/pages/data-quality.js`, `tests/fixtures/monitoring_quality.html`, `tests/test_monitoring_quality_browser.py`; Modify `frontend/workbench/routes.js`。

**Interfaces:** `renderAcquisition/renderDataQuality(context:WorkbenchContext)->Promise<void>`。读取 Task 4；质量案件写入复用 `createIngestClient(api,{isCurrent}).prepareChange(path,{reason,expected_version}).run()`；仅 `/cases/{id}/ack|note|close|reopen`，不整体挂载会包含 resend 的旧 actions。原文仍 `/api/ingest-reliability/facts/{id}/preview|raw` 并要求原文权限。

- [ ] 写失败测试：三视图分别计数；空态、未解析、无权限、不可用区分；接收/入库/业务回执/broker 发送分栏；长报文引用可读；四类实际操作带版本原因幂等键；关闭不改变观测值；普通权限不请求原文、页面和网络无重发控件。
  ```python
  self.assertEqual(result['quality_view_count'], 3)
  self.assertEqual(result['forbidden_write_requests'], [])
  self.assertEqual(result['telemetry_before_close'], result['telemetry_after_close'])
  self.assertEqual(result['raw_requests_without_permission'], 0)
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_quality_browser -v`。
- [ ] 实现按网关/设备/接收窗口查询的采集摘要与详情；跨井包只展示集合/混合归属说明。质量页提供 observations/resolution/cases 三 tab 与未解析入口；不可用汇总不显示 0。状态更改明确只改变处置流程，展示审计引用和当前状态查询时刻；409 核对新版本，503 保留原操作，403 停止重试。原文弹层使用现有有界授权接口，不把原文嵌入列表缓存。
- [ ] GREEN：本项浏览器命令与 `& $MonitoringPython -B -m unittest tests.test_ingest_browser -v`；两宽、键盘、长内容检查；原文请求分别测试允许和拒绝。
- [ ] 提交：暂存本项列明文件后 `& $MonitoringGit commit -m 'feat: connect acquisition and quality case handling'`。

### Task 9: 隔离端到端验证与固定版本部署

**Files:** Create `tools/monitoring_check.py`, `tests/test_monitoring_e2e.py`, `tests/test_monitoring_deploy.py`, `docs/production-monitoring-workbench.md`; Modify `tools/review_deploy.py`, `tools/review_deploy_config.py`, `tools/review_deploy_pg.py`, `tools/review_deploy_child.py`, `tools/review_deploy_process.py`, `tools/review_deploy_ready.py`, `tools/review_deploy_backup.py`, `tests/test_review_deploy.py`, `tests/monitoring_support.py`。

**Interfaces:** 为 `tools.review_deploy init` 新增 `--profile monitoring`，默认仍 review；profile 写入 sealed 配置及 marker，派生 `--ui-mode=monitoring`，角色库存增加专用 monitoring_reader，但仅 monitoring profile 使用新配置。保留 `launch/stop/status/backup/restore`，恢复继承 profile 与新端口并验证新 Cookie。`tools.monitoring_check --layer unit|browser|integration|all --report <path>` 只调用本批列明测试和相关安全回归；integration 创建新自有集群，不读取旧测试库环境变量；失败或必测跳过返回非零。

- [ ] 写失败测试：新配置库存/角色授权/密封哈希/进程凭据完整、固定代码导出；已有 target、占用端口、错误集群身份拒绝；默认 review 不变；备份/恢复保留身份、数据来源和实际行数。HTTP 五岗位矩阵、自审拒绝、同键不同内容拒绝、旧版本冲突、撤权后重试/回执读取拒绝均通过真正服务检查。
  ```python
  self.assertEqual(restored['profile'], 'monitoring')
  self.assertEqual(restored['row_counts'], backup['row_counts'])
  self.assertEqual(protected_install_hashes_before, protected_install_hashes_after)
  self.assertEqual(report['forbidden_requests'], [])
  self.assertEqual(report['required_skips'], 0)
  ```
- [ ] RED：`& $MonitoringPython -B -m unittest tests.test_monitoring_deploy tests.test_monitoring_e2e -v`。
- [ ] 实现 profile 贯穿初始化、凭据库存、schema/grant 顺序、配置校验、reader probe、HTTP 启动、readiness 和备份恢复；无默认 profile 的旧 marker 按 review 解释，不能强行补写旧安装。新安装计划 PostgreSQL `55436`、MQTT `1888`；恢复另用 `8775/55437/1889`，全部先检查占用，被占用就报告不擅自改端口。运行时不得有 owner/bootstrap 密码，日志不输出凭据。
- [ ] 实现端到端场景：实际 HTTP 建井和设备→另一身份审核点表→指派、显式启用与绑定→独立 MQTT 测试发送→中心入库→页面/CSV/案件处置→停启后重新读取；软件测试来源不可变成 field。涵盖正常、重复、延迟、bad/uncertain、时钟偏移、未知点、单位错误、迁井前后、同时间观测、31 天范围和报文跨归属。保存 payload 哈希、事实引用、审计、分页/导出对账及权限拒绝证据，不存公开密码。
- [ ] GREEN 分层：先运行每项针对性命令，再 `& $MonitoringPython -B -m tools.monitoring_check --layer all --report private/monitoring-check.json`；报告分开统计 unit/browser/PG/HTTP/MQTT，不把浏览器网络替身算真实联调。8 页×2 宽检查列表、表单和异常态，55 路由不丢失。最终独立审查修复发现后重跑受影响层，不每次重复旧 40 分钟全量套件。
- [ ] 提交经验证的本项文件：`& $MonitoringGit commit -m 'feat: deploy isolated monitoring workbench'`。必须先暂存本项明确文件；不要提交 private 日志、账户、库文件。然后从该固定提交执行下列初始化；调用前记录目标、端口、创建新角色所需权限及保护版本只读指纹，任何所有权不明立即停止。
  ```powershell
  & $MonitoringPython -B -m tools.review_deploy init --profile monitoring --source 'C:/Users/hp/.codex/worktrees/production-device-registry/外专双百' --target 'C:/Users/hp/AppData/Local/OilWell/monitoring-workbench-20261013' --http-port 8774 --postgres-port 55436 --mqtt-port 1888
  & $MonitoringPython -B -m tools.review_deploy launch --target 'C:/Users/hp/AppData/Local/OilWell/monitoring-workbench-20261013'
  ```
- [ ] 交付验收：在新安装完成实际登录/保存/查询/审计/重启后，使用既有 backup 命令生成可校验备份（该命令会停止新实例），只恢复到全新 `C:/Users/hp/AppData/Local/OilWell/monitoring-workbench-restore-20261013` 目录（不覆盖源）；核对行数、来源和服务状态。原新实例保持停止，完成恢复演练后只停止自有恢复实例，再启动 8774，避免两个复制身份库同时提供会话。保护指纹覆盖旧版本代码、配置及冻结快照；活跃服务自行产生的日志/运行数据变化不能误算为本轮改写，另通过进程、网络、数据库目标白名单证明本轮未向旧实例写入。将实际提交号、入口、私人账号文件位置、测试报告和现场验证缺口写入交付文档，提交文档更新；不宣称整个平台或现场控制已验收。

## 设计覆盖与审阅结果

版本/会话隔离对应任务 1、2、5、9；井设备点表对应任务 3、6；采集质量对应任务 4、8；历史实时图表与导出对应任务 3、7；全部验收证据在任务 9 汇总。五条 Review Focus 均有所属测试；读取、写入和预览的数据通路分开。实施按任务 1—9 顺序，任务 3/4 后复查一次后端接口，任务 8 后进行组合验证，不将后续算法、节能、电机功能提前算入本批完成。
