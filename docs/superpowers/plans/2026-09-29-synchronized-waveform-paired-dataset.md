# WP-04 Synchronized Waveform and Paired Dataset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将同步电参和载荷/位移录波通过统一在线/离线标准可靠保存，形成有质量依据、可独立复核及冻结导出的成对数据集。

**Architecture:** 新增独立 `backend/waveforms/` 与 PostgreSQL `waveforms` schema，精确原文、分片、回执、封存结果和审核记录分别建模；不改变 v1 遥测和 WP-03 回执。共同合同内核服务 MQTT、文件导入及厂家校验；前端接入现有登录、权限和导航，生产反演执行留给 WP-05。

**Tech Stack:** 现有 Python 3.13、PostgreSQL 18、psycopg[binary]==3.3.5、paho-mqtt==2.1.0、pymodbus==3.15.0（本包不新增 Modbus 读取/写入）、SQLite、标准库 HTTP、原生 JavaScript/CSS、unittest、Node/Playwright 与本机 Edge。无新服务端框架、CDN、对象存储或科学计算依赖。

**Spec:** [已确认书面设计](../specs/2026-09-29-synchronized-waveform-paired-dataset-design.md)。用户于 2026-09-29 确认设计。**本实施计划及执行方式待用户审阅选择，复选框不是已完成记录。** 实施者完整阅读设计及本计划；两者冲突先报告，不通过降低设计验收标准解决。

## Global Constraints

- 保持 Modbus TCP/RTU 只读与 MQTT 上行路线。普通 Modbus 轮询不升级为“同步 ADC”。
- 冻结演示版 `C:/Users/hp/Documents/外专双百-演示版-20260912`、源码 ZIP 和 `demo-v2.0-20260912` 标签不改动。不把演示库记录自动迁成生产测量。
- 不开放远程启停、Modbus 写寄存器、参数下发、OTA 或保护输出。不迁移业务库、不更新正在运行的服务。
- 目前没有现场设备与成对实测数据。软件向量保留 `synthetic` 来源，不改称现场测量；导入方式也不能证明实测真实性。
- 对应年度任务 Y1-01/02/03、Y2-02、Y3-04/05/06；绩效申报、专利、软著和奖项不属于研发验收。
- 入口 schema 固定为 `OWP-WAVE/1.0`，`kind` 为 `manifest` 或 `chunk`。在线主题为 `{prefix}/v2/{gateway_id}/{device_id}/waveform`，仅 QoS 1、非 retained。
- 独立回执 schema `OWP-WAVE-ACK/1.0`，主题 `{prefix}/v2/{gateway_id}/{device_id}/waveform-ack`；回执最多 4 KiB。不修改 `OWP-INGEST-ACK/1.0`。
- manifest 精确字节 64 KiB；单个 chunk 报文精确字节 256 KiB，解码数据最多 128 KiB，必须整帧；隔离单报文硬上限 256 KiB。
- 单 capture：16 MiB 解码数据、32 MiB 原始报文字节，最多 512 片；最多 8 组、合计 32 通道；窗口跨度最多 60 s；采样率为正有理数、1—128000 S/s。
- 每通道值为 4 字节数值 + 1 字节质量标记，仅 i32le/f32le。质量 0=good、1=missing、2=clipped、3=bad、4=uncertain；missing 四字节全零且不能当作观测值。
- JSON 最大深度 16；重复/未知字段、布尔冒充整数、非法 UTF-8、非有限数值均拒绝。绝对样本序号及所有 ns 使用十进制字符串，带符号 64 位界限；其余计数最大 2147483647，采样率分母最大 1000000 且分数约分。
- `.owpwave` 最多 `32 MiB + 513 字节`，仅 LF 分隔，末行 LF 可选；禁止 BOM、CRLF、空行、ZIP、多行 JSON；一个 manifest 和全部 chunk 各一份。
- 事务提交失败或结果不确定时不返回持久成功；台账暂不可用不阻止合法原文保存。complete 不等于质量合格。
- stalled 是 collecting 超过 24 小时无新增有效片的派生告警，迟到的原片仍可恢复，不自动删除。
- manifest 预占 32 MiB，封存后释放未用预占；单事实最低计费 1 KiB。每网关独立持久 MQTT 消费会话/额度，全局配置容纳各网关额度之和。
- 数据附件仅 PDF/PNG/JPEG，每件最多 4 MiB，每记录最多 8 件；初版不自动取外部 URL、不执行上传内容。
- 原始曲线窗口每通道最多 20000 帧；显示摘要每通道最多 2000 点。坐标悬停两位小数仅用于显示，精确序号/时间/原码不得截断。
- 数据集每版本最多 1000 个配对成员；禁止跨分区复用同一物理周期/派生变体。管理员有全部权限但不得自审、改原文或绕过硬质量失败。
- 启动只读检查依赖、结构和权限，不执行 DDL、不提升业务账号权限；密钥不进源码和公开包。所有新模块导入不得连接数据库或启动线程。

## Review Focus

1. 文件末行没有 LF、在线与离线行序不同：文件来源摘要和导出摘要要分别正确，绝不先重排/strip 再声称逐字节一致（Task 2、9、12）。
2. 全部样本恰好处于迁井/校准截止时刻，UTC 不确定度扩大窗口：采用保守边界，不因 PostgreSQL 微秒时间精度把 ns 边界四舍五入为合格（Task 10、11）。
3. 网关 A 的孤片/隔离区耗尽时网关 B 与 v1 仍有消息：独立会话、公平调度和按网关预留均实际生效（Task 4、6、17）。
4. 显示分桶极值位置与采样时间顺序冲突，超大原始序号超过 JS 精确整数：曲线不倒序、缺口不被连起、悬停指向原样本（Task 14、15）。
5. 导入/冻结/撤销已提交但响应丢失，用户换选项或权限被撤回：原请求幂等恢复、重新鉴权、撤销与冻结并发不会发布失效数据集（Task 9、12、13、15）。

---

## A. 工期、里程碑和保护范围

本工作包预计净开发、测试与审查 **160—224 小时，按每天 8 小时折算约 20—28 个工作日**。这是基于当前设计的工作量估算，不是后台持续运行、指定日期交付或已实现百分比的承诺；等待审阅/授权、现场实物、计量试验和正式部署不计入。完成首阶段后以真实测试结果重新校准估算。

| 阶段 | 任务 | 预计工时 | 阶段可验收结果 |
| --- | --- | --- | --- |
| A 协议与文件 | 1—2 | 16—24 | 同一内核识别线上报文和离线文件，厂家向量可自检 |
| B 中心可靠保存 | 3—6 | 40—56 | 真数据库分片留存、组装恢复、网关回压隔离 |
| C 端到端传输 | 7—9 | 24—32 | 应用回执、本地发送队列、断点文件导入 |
| D 可信成对样本 | 10—12 | 32—48 | 校准资料/历史井核查、配对复核、冻结数据集 |
| E 管理与页面 | 13—15 | 24—32 | 按角色可操作的四个工作视图及真实波形交互 |
| F 集成与交付 | 16—17 | 24—32 | 厂家包、故障/恢复/容量证据、整体回归与审查 |

主要风险：字节协议边界、事务/队列竞争、时间与校准证明、无硬件条件下的证据口径。阶段结果逐次交付，不等全部页面完成才展示进展。WP-05 模型标定、WP-06 渗流/影子策略、WP-07 电机诊断、WP-08 整机现场集成不包含在这 20—28 个工作日中。

工作区为 `C:/Users/hp/.codex/worktrees/production-device-registry/外专双百`，设计提交 `081f9a8`，执行时从计划批准后的实际 HEAD 开始。已有 linked worktree 不重复创建；当前 detached HEAD 不擅自强制切换或覆盖分支。原工作区的未跟踪 `scripts/` 和旧 `.superpowers/` 内容不处理。8765 页面、业务 PostgreSQL/现有 Broker 不重启，不注入测试数据。

本计划不授权合并/推送 PR、生产迁移或正式部署。用户已授权隔离测试环境；执行时先核对 `127.0.0.1:3822` 的专用集群身份，增加独立 `oilwell_waveforms_test`，不复用/清空 WP-02、WP-03 测试表。环境凭据通过既有私密文件和 Windows 凭据读取，命令与日志不得打印密码。

## B. 文件结构与任务接口

所有下列 Create 为拟建文件，不表示已经存在。新文件按职责拆分，不对旧业务整体重构。

| 路径 | 职责 / 任务 |
| --- | --- |
| Create `backend/waveforms/{__init__,errors,contracts,wire,samples}.py` | 常量/不可变类型、严格合同、样本解码（1） |
| Create `backend/waveforms/files.py`, `tools/validate_waveform.py`, `tests/waveform_vectors.py` | 精确文件封装和确定性测试向量（1—2） |
| Create `backend/waveforms/sql/{001_capture,002_evidence,003_datasets,grants}.sql`, `migrate.py`, `privileges.py` | 分阶段 schema、不可变约束及最小授权（3、10、12） |
| Create `tools/provision_waveform_test.py`, `tests/{waveform_support,waveform_stack,waveform_worker}.py` | 独立测试库与自有工作进程（3、6、17） |
| Create `backend/waveforms/{repository,capacity,assembly,outbox}.py` | 分片事务、配额、组装/回执任务租约（4—5、7） |
| Create `backend/waveforms/{config,consumer,publisher,__main__}.py`, `config/waveforms.example.toml` | 组件配置、逐网关消费和独立发布进程（6—7） |
| Create `backend/waveforms/{sender_spool,sender}.py`, `sql/sender_001.sql` | 独立 SQLite 发送队列（8） |
| Create `backend/waveforms/{commands,imports}.py` | 用户动作元数据、导入会话（9） |
| Create `backend/waveforms/{evidence,registry_context,timing,quality,pairing}.py` | 资料版本/撤销、井归属、同步与配对（10—11） |
| Create `backend/waveforms/{datasets,export}.py` | 冻结成员、划分检查、可重现导出（12） |
| Create `backend/waveforms/{api,routes,permissions,runtime,queries,windows}.py` | HTTP、RBAC、快照分页、精确窗口和图形摘要（13—14） |
| Modify `backend/{http_server,seed}.py`, `server.py` | 可选 runtime 和能力声明、一次性新权限引入（13） |
| Create `frontend/pages/waveforms.js`, `frontend/waveforms/{client,imports,detail,evidence,pairs,datasets,charts}.js` | 四工作视图、导入、资料和细线图交互（15） |
| Modify `app.js`, `styles.css` | 页面路由/导航/权限、局部样式（15） |
| Create `assets/open-interface/{WAVEFORM.md,waveform.schema.json,waveform-ack.schema.json,validate_waveform.py,waveform-example.owpwave,waveform_errors.py,waveform_contracts.py,waveform_wire.py,waveform_samples.py,waveform_files.py}` | 厂家公共波形协议资料与同源校验模块（16） |
| Modify `tools/build_open_interface_kit.py`, `assets/open-interface/{index.html,INTERFACE.md,ROADMAP.md}` | 显式公共白名单、能力状态更新（16） |
| Create `tools/verify_waveforms.py`, `docs/同步波形部署与回退.md`; Modify `docs/年度工作任务逐项响应.md` | 安全验证入口、运维交接和任务证据（16—17） |

各任务列出具体测试名。新增 `tests/test_waveform_*.py`、必要的 `tests/fixtures/waveform_*.html`/`.cjs`，保留既有测试；禁止用删测试或放宽质量条件消除失败。

### 公共类型与签名约定

以下类型均由列明任务定义，再被后续任务消费。DTO 是显式 dataclass 或 TypedDict，所有字段有验证；不依赖自由嵌套 dict 猜测跨任务接口。

| 定义者 | 固定接口 |
| --- | --- |
| Task 1 contracts | `CaptureKey(gateway_id: str, device_id: str, capture_id: UUID)`；`Observation(transport: str, topic: Optional[str], payload: bytes, received_at: datetime, source_ref: UUID, qos: Optional[int], retain: Optional[bool], dup: Optional[bool])` |
| Task 1 contracts | `ManifestFrame(key, raw_sha256, body: dict, streams: tuple[StreamSpec,...])`；`ChunkFrame(key, manifest_sha256, stream_id, chunk_index, sample_start, sample_count, data_sha256, data: bytes)`；`StreamSpec` 为 wire stream 中已验证的全部字段 |
| Task 1 errors/wire | `ContractError(code: str)` 仅指永久输入错误；`WaveformError(code: str, message: str, status: int=503)` 指系统/业务失败；`parse_frame(payload: bytes) -> Union[ManifestFrame,ChunkFrame]`；`classify(observation: Observation, *, prefix: str) -> Candidate(frame, key, reason_code, validator_version)` |
| Task 1 samples | `iter_samples(stream: StreamSpec, data: bytes, *, sample_start: int) -> Iterator[Sample]`；`Sample(index: int, channel_id: str, raw_value: Optional[Union[int,float]], quality: int)` |
| Task 1 contracts | `WaveReceipt.from_bytes(payload: bytes) -> WaveReceipt`、`to_bytes() -> bytes`、`matches(topic: str, *, expected: ReceiptExpectation) -> bool`；Expectation 含身份、scope、manifest/报文摘要、片索引，不含“只有 mid 就够”的匹配 |
| Task 2 files | `scan_file(source: BinaryIO) -> FileManifest(sha256, manifest_sha256, size_bytes, final_lf, lines, manifest)`；line 含 offset/length/digest；`iter_file_frames(source: BinaryIO) -> Iterator[bytes]` 不规范化原文，两者都先 seek(0)，结束停在EOF |
| Task 3 config | `WaveformConfig` 含角色连接配置、逐网关额度和 MQTT/TLS 会话、最大连接/任务数；机密字段 `repr=False`；`CapacityLimits` 含总/网关原文、孤片、隔离、附件、事实/元数据数额度 |
| Task 4 repository | `WaveformRepository(connect, *, limits: CapacityLimits, prefix: str)`；`ingest(observation: Observation) -> StoredMessage(fact_id: UUID, status: str, duplicate: bool, receipt_id: Optional[UUID])` 只在提交后返回；内部 `store_in_transaction(cursor, observation) -> PendingStoredMessage` 不对调用者冒充已提交 |
| Task 5 assembly | `Assembler(connect, *, clock)`；`claim(worker_id: UUID) -> Optional[AssemblyLease]`；`run(lease: AssemblyLease) -> Optional[Completion]`；lease 包含 capture key、generation、expiry，失效租约不能封存 |
| Task 9 commands | `Actor(domain, user_id, username, role_code)`；`Command(actor: Actor, key: UUID, expected_version: int, reason: str, request_hash: str)`；新建 expected_version=0，其余为已读版本，API 序列化为字符串 |
| Task 10 evidence | `EvidenceService(connect)`：`create(kind: str, body: dict, command: Command) -> EvidenceVersion`、`review(id: UUID, *, decision: str, command: Command) -> EvidenceVersion`、`revoke(id: UUID, command: Command) -> Restriction` |
| Task 11 quality | `evaluate(context: QualityContext, *, profile: ProfileVersion) -> QualityResult(purpose, status, reasons, evidence_ids, validator_version)`；`PairService.propose(body: dict, command: Command) -> PairVersion`、`review(id: UUID, *, decision: str, command: Command) -> PairReview` |
| Task 12 datasets | `DatasetService.create/update/submit/freeze(body: dict, command: Command) -> DatasetVersion`（update/submit/freeze 的 body 必含 target_id）；`DatasetExporter.describe(version_id: UUID, *, purpose: str, actor: Actor) -> ExportManifest`；`stream_capture(key: CaptureKey) -> Iterator[bytes]` |
| Task 13 runtime | `WaveformRuntime(status, queries, services, code, allowed_origins)`；`build_waveform_runtime(config_path) -> WaveformRuntime`；`WaveformApi(handler).get/post/unsupported(path) -> bool` |
| Task 14 queries | `WaveformQueries.fetch(resource: str, filters: dict) -> dict`；`WaveformWindows.read(key, stream_id, *, start: int, count: int, channel_ids: tuple[str,...], mode: str, budget: int) -> WindowResult` |
| Task 15 UI | `renderWaveforms(context) -> Promise<boolean>`；`createWaveformClient(api)`；各 mount 函数返回 `{dispose()}`，不能留下轮询/上传回调污染别的页面 |

`body` 的精确 wire 字段在 Task 1 表内固定；管理 DTO 在所属任务测试中固化后写入 JSON schema，不接受任意属性透传。服务间只传已验证值，不能以 HTTP DTO 直接拼 SQL。

### 执行与测试命令约定

PowerShell 工作目录固定为上述开发 worktree。执行前设置 `$wavePython = 'C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe'` 与 `$env:PYTHONUTF8='1'`。下文每个 `& $wavePython -m unittest ... -v` 要求 exit 0、0 failures、0 errors、0 skipped 才算通过；红灯阶段必须是所写断言/缺少该实现造成失败，不是私密配置缺失或业务服务被误连。

测试库使用 `OILWELL_WAVEFORM_TEST_CONFIG` 和 `OILWELL_WAVEFORM_PROVISION_IDENTITY` 指向专用私密配置/已核验集群身份；读配置但不打印。PG 测试若依赖未就绪必须失败并说明，不 skip。前端测试沿用现有 Node/Playwright/Edge fixture 模式，缺浏览器同样是环境失败。最终整体回归另保留既有 `OILWELL_INGEST_TEST_CONFIG`、`OILWELL_REGISTRY_TEST_CONFIG`、`OILWELL_POSTGRES_TEST_CONFIG` 配置，不把它们改向新的波形测试库。

每个任务完成先运行本任务全部测试、检查 diff，再只 stage 本任务文件并提交。提交前使用 verification-before-completion，记录红灯/绿灯命令、结果、未完成边界；不要 `git add .`。创建临时服务用隐藏进程，只清理有本轮 run_id/PID 归属的进程和已校验的临时目录。

## C. 分项实施

### Task 1: 严格波形合同、分片与回执类型

**Files:** Create B 节 Task 1 文件、`tests/waveform_vectors.py`、`tests/test_waveform_contracts.py`。

**Interfaces:** 消费现有稳定的身份规则作为参照，不修改原合同；产出 B 节 Task 1 的所有类型/函数及 `golden_capture() -> (manifest_bytes, tuple[chunk_bytes,...])` 测试工厂。

golden_capture 固定 UUID/UTC/种子，synthetic 来源、一个完整 10 s 周期、8 kS/s 三相组和 100 S/s 载荷/位移组，首样本 offset_ns="0"；包含周期两端支持样本，流编号 electrical/sensor。坏向量只在测试中按单个输入属性改变，重算哪些摘要必须明确，不能因摘要先错而漏测目标字段。纯合同模块只能依赖标准库及自身 errors/contracts/samples/wire，不导入数据库或旧 HTTP 服务，便于公共工具同源发布。

锁定 manifest 顶层字段：共同字段加 boot_id、capture_kind、source_kind、producer（manufacturer/recorder_version）、source_evidence（nullable UUID）、utc_start、utc_uncertainty_ns、clock_domain_id、sync_evidence_id、window_ns、cycle、streams。cycle 为 null 或 `{cycle_id,start_ns,turn_ns,end_ns,direction,zero_reference,reference_channel,reference_events}`；reference_events 为起/换向/终三个 `{stream_id,sample_index,event}` 引用，未知索引可为 null 并使周期门禁失败。

stream 字段：stream_id、source（gateway_id/device_id/boot_id）、clock_domain_id、rate_num/rate_den、first_sample_index、offset_ns、frame_count、frame_bytes、chunk_frames、chunk_count、decoded_bytes、data_sha256、channels。channel 字段：channel_id、quantity、unit、encoding、measurement_source、signal_kind、config_id、calibration_id。measurement_source 为 sensor/derived/synthetic/unknown，signal_kind 为 instantaneous/rms/window_mean/derived；unknown 物理量可留存但不具备算法资格。direction 为 clockwise/counterclockwise/unknown。所有未知值只在规范声明 nullable 的字段允许。

- [ ] **1. 写红灯测试。** `test_exact_manifest_digest`、`test_integer_and_layout_bounds`、`test_missing_not_zero`、`test_receipt_scopes`，至少包含：

```python
raw, chunks = golden_capture()
self.assertEqual(parse_frame(raw).raw_sha256, sha256(raw).hexdigest())
self.assertEqual(parse_frame(chunks[0]).sample_start, 0)
with self.assertRaises(ContractError): parse_frame(b'{"schema":true,"schema":false}')
```

- [ ] **2. 运行红灯：** `& $wavePython -m unittest tests.test_waveform_contracts -v`，确认未实现或对应断言失败。
- [ ] **3. 实现上述签名。** 固定错误优先级为大小→主题/运输→UTF-8/JSON→字段/数值→布局/摘要→身份；对应 reason_code 固定为 payload_too_large、topic_invalid、qos_not_supported、retained_publish、utf8_invalid、json_invalid、json_duplicate_key、contract_invalid、layout_invalid、digest_mismatch、identity_mismatch。仓储再处理 reference_conflict/idempotency_conflict。验证 SHA-256、小端解码、规范 Base64 重编码相等、64 位加法溢出；回执 scope/status 只接受设计四组合，UTC 严格六位小数。
- [ ] **4. 运行绿灯：** 同命令，增加上下界±1、空流、负 ns 不确定度、NaN/Inf、深度 17、重排 JSON 摘要差异及全部非有限 f32 位模式测试。
- [ ] **5. 检查并提交：** `feat: add strict waveform wire and receipt contracts`。

### Task 2: 单文件格式及公共校验内核

**Files:** Create `backend/waveforms/files.py`、`tools/validate_waveform.py`、`tests/test_waveform_files.py`。依赖 Task 1。

**Interfaces:** 消费 parse_frame；产出 scan_file、iter_file_frames。CLI `--input <path>` 输出安全 JSON 摘要，合格 exit 0、输入拒绝 exit 2，不连接网络/数据库。

- [ ] **1. 写 `test_file_lines_are_exact`、`test_final_lf_changes_file_hash_not_capture`、`test_file_bounds` 红灯。** 用 BytesIO 拼接同一 golden_capture 两种末尾形式：

```python
self.assertNotEqual(scan_file(with_lf).sha256, scan_file(without_lf).sha256)
self.assertEqual(scan_file(with_lf).manifest_sha256, scan_file(without_lf).manifest_sha256)
self.assertEqual(tuple(iter_file_frames(without_lf)), (manifest, *chunks))
```

- [ ] **2. 红灯命令：** `& $wavePython -m unittest tests.test_waveform_files -v`。
- [ ] **3. 实现有界逐行扫描。** 每次最多读行上限+1字节；计数最多 513 行，禁空行/BOM/CRLF/重复片/混合 capture，允许 chunk 乱序；检查缺片、整组摘要、原始序号和总额度。只保存 offsets，不把所有解码样本展开到内存；超大文件先拒绝但不只信文件扩展名。
- [ ] **4. 绿灯同命令。** 子进程测试 CLI；含无末尾换行、恶意超长行、换序及截断文件，验证文件指针在工具定义的重新 seek(0) 后可重复读。
- [ ] **5. 提交：** `feat: validate exact-byte waveform files`。

### Task 3: 独立数据库、事务骨架及最小权限

**Files:** Create `backend/waveforms/sql/001_capture.sql`、`sql/grants.sql`、`migrate.py`、`privileges.py`、config 中 CapacityLimits；`tools/provision_waveform_test.py`、`tests/waveform_support.py`、`tests/test_waveform_schema.py`、`tests/test_waveform_privileges.py`。依赖 Task 1。

**Interfaces:** `migrate(settings, *, expected_database: str, limits: CapacityLimits) -> dict`、`grant_roles(connection, *, roles: dict) -> None`、`probe_role(connect, role: str) -> None`；`WaveformDatabaseCase.connect(role)`、`count_facts_for(observation: Observation) -> int` 和带数据库身份门禁的 reset fixture。六角色固定名 migration_owner/receiver/assembler/receipt_publisher/reader/operator；测试账号名另加 waveform_test_ 前缀。

- [ ] **1. 写 schema/最小授权红灯。** `test_wrong_database_never_mutates`、`test_migration_idempotent`、`test_facts_immutable`、`test_reader_no_raw_table_access`、`test_runtime_cannot_ddl`；断言迁移两次不改已存原文，receiver UPDATE/DELETE facts 被拒绝。
- [ ] **2. 在已验证的专用集群建立仅本包测试库/六角色后运行：** `& $wavePython -m unittest tests.test_waveform_schema tests.test_waveform_privileges -v`。先确认错误数据库测试完全不执行 DDL，再测试正常路径；不得使用业务账号补救。
- [ ] **3. 实现迁移和 fixture。** 创建 facts/observations、captures/streams/chunks、completion、assembly_jobs、receipts/deliveries、imports/import_lines、capacity/worker_health、operation_receipts/audit/state_head。原文 SHA 与精确字节双核验；内容实体不可变、租约代际/配额受约束。advisory lock 固定 `499001211`；reset 只列举本库 waveforms 表。receiver 只可新增事实/导入关联并更新明确运行状态；assembler 可读取片、封存/调度回执；publisher 只读回执及更新领取/健康；operator 只写审核/业务/审计实体；reader 只读元数据和固定受控函数。各角色无DDL/事实修改权，receiver 的 imports/lines/audit 写权用于 Task9同事务。原文函数固定 search_path、撤销 PUBLIC 执行权，并将精确ID访问审计落库。后续002/003只增本任务所需的最小授权。
- [ ] **4. 运行绿灯同命令。** 用每个真实连接角色验证 SELECT/INSERT/UPDATE/DELETE/DDL、角色继承、PUBLIC 权限、表所有者/超管误配；缺配置应显式失败而不跳过。
- [ ] **5. 提交：** `feat: add isolated waveform persistence and privilege gates`。

### Task 4: 分片事务、幂等与配额隔离

**Files:** Create `backend/waveforms/{repository,capacity}.py`、`tests/test_waveform_repository.py`、`tests/test_waveform_capacity.py`。依赖 Task 1、3。

**Interfaces:** 产出 WaveformRepository.ingest；内部 `store_in_transaction(cursor, observation) -> PendingStoredMessage` 只能在外层提交后转成 StoredMessage，避免 Task 9 在尚未提交的导入事务中报成功。

- [ ] **1. 写重复/冲突/原子性红灯。** `test_duplicate_exact_bytes`、`test_same_key_reordered_json_conflicts`、`test_orphan_other_manifest_does_not_claim_slot`、`test_rollback_no_receipt`、`test_gateway_capacity_isolated`：

```python
self.assertTrue(repo.ingest(original).duplicate is False)
self.assertTrue(repo.ingest(original).duplicate)
self.assertEqual(self.count_facts_for(original), 1)
self.assertEqual(repo.ingest(same_key_different_bytes).status, "quarantined")
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_repository tests.test_waveform_capacity -v`。
- [ ] **3. 实现短事务保存。** 锁顺序固定网关容量→capture→片键；孤片键含 manifest_sha256，错误清单引用不抢有效槽位；收齐任务和 message 回执同事务。按 32 MiB 预占、每事实 max(1024,len(payload)) 计费；孤片/隔离独立子额度且同时计入该网关总额，重复不再扣账。同一次重投只更新饱和计数，不能无限长 observations。未知台账不参与阻断原文事务。
- [ ] **4. 绿灯同命令。** 真数据库并发重复/不同字节、孤片→清单转账、每类额度±1、提交异常和回滚；断言接收结果只在 commit 返回后可用，A 满不扣 B 额度。
- [ ] **5. 提交：** `feat: persist waveform chunks atomically with bounded quotas`。

### Task 5: 可恢复组装、封存与完整性结果

**Files:** Create `backend/waveforms/assembly.py`、`tests/test_waveform_assembly.py`。依赖 Task 2、4。

**Interfaces:** 产出 Assembler claim/run；`Completion(key, manifest_sha256, status, reason_code, completed_at)` 不含“quality approved”。

- [ ] **1. 写 `test_all_chunks_but_missing_samples_still_complete`、`test_full_hash_mismatch_invalid`、`test_stale_lease_cannot_seal`、`test_late_conflict_does_not_rewrite_completion` 红灯。** 断言缺一个 chunk 时 collecting，补片后 complete；质量 missing 计数保留而非改 good。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_assembly -v`。
- [ ] **3. 实现按 stream/chunk 顺序流读和封存。** 领取 30 s 租约并允许有界续租，generation 校验贯穿结果提交；封存结果、capture 回执、配额释放同事务。整段哈希/布局失败 invalid，临时存储错误可重试。stalled 只看新增有效片的时间，不被重复坏包“续活”；后到冲突追加 restriction，不修改 completion。
- [ ] **4. 绿灯同命令。** 杀组装进程后重新领取、两个组装器争抢、UTC 时钟回退告警及租约失效；最多持有单片解码数据，单 capture 最大值实测内存另交 Task 17。
- [ ] **5. 提交：** `feat: assemble and seal waveform captures recoverably`。

### Task 6: 逐网关 MQTT 接收与组件启动门禁

**Files:** Create `backend/waveforms/{config,consumer,__main__}.py`、`config/waveforms.example.toml`、`tests/{waveform_stack,waveform_worker}.py`、`tests/test_waveform_config.py`、`tests/test_waveform_consumer.py`。依赖 Task 4—5。

**Interfaces:** `load_waveform_config(path, *, component: str) -> WaveformConfig|None`，component限定receive/assemble/publish/management/sender并只读所需凭据；`WaveformConsumer(repository).handle_message(client, message)`；CLI `python -m backend.waveforms --config PATH receive|assemble|publish`，带 `--check` 时仅核验配置/权限、不启动循环。

- [ ] **1. 写启动和确认红灯。** `test_disabled_no_connections`、`test_nonloopback_requires_verified_tls`、`test_no_ack_on_unconfirmed_commit`、`test_gateway_a_backpressure_does_not_disconnect_b`：

```python
with self.assertRaises(WaveformError): consumer.handle_message(client, message)
self.assertEqual(client.ack_calls, [])
self.assertNotEqual(config.gateways[0].client_id, config.gateways[1].client_id)
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_config tests.test_waveform_consumer -v`。
- [ ] **3. 实现显式每网关配置及独立持久会话。** MQTT 5、手动确认、retain 原标志；每网关 receive_maximum=4、内存工作队列≤4，接收只管自己的主题。数据提交/隔离成功后 ack；硬超限/数据库故障只回压自己的会话，有限退避 1—60 s。配置拒绝重复 client_id、同角色账号、网关额度和超过全局、通配身份、非本机明文或关闭证书验证。共享组装池按网关轮转，每轮每网关最多领一项。
- [ ] **4. 绿灯同命令。** 自有 Broker/临时端口启动真接收器，正常接收、错误后合法消息、A 阻塞 B 连续保存；退出/失败关闭自己线程和连接，不触碰已有服务。
- [ ] **5. 提交：** `feat: isolate waveform MQTT consumers by gateway`。

### Task 7: 波形回执 outbox 与独立发布器

**Files:** Create `backend/waveforms/{outbox,publisher}.py`、`tests/test_waveform_outbox.py`、`tests/test_waveform_publisher.py`。依赖 Task 1、4—6。

**Interfaces:** `WaveOutbox.claim(worker_id: UUID, *, limit: int) -> tuple[DeliveryLease,...]`、`finish(lease, *, delivered: bool, error_code: str|None) -> bool`；`WavePublisher.run_once() -> int`。DeliveryLease 包含 receipt_id、generation、lease_token、完整主题/回执字节。

- [ ] **1. 写 `test_old_generation_cannot_finish_new_delivery`、`test_callback_before_publish_return`、`test_mid_reuse_after_reconnect` 红灯；断言过期 lease finish=False，新任务状态未变；PUBACK 丢失时重试同 receipt_id/字节。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_outbox tests.test_waveform_publisher -v`。
- [ ] **3. 实现独立发布账号、30 s 租约及指数退避上限 300 s。** 回调绑定连接代次+mid，与完整已持久回执匹配；人工/manifest 重投补发只增加 requested_generation，旧领取不能吞掉新请求。成功仅表示 Broker 接受，不写“网关已确认”。
- [ ] **4. 绿灯同命令。** 真 Broker 重启、丢确认、发送器崩溃、延迟回调与断线重连；验证 publisher 不能写 facts 或任意上行主题。
- [ ] **5. 提交：** `feat: publish durable waveform application receipts`。

### Task 8: 独立持久文件发送队列

**Files:** Create `backend/waveforms/{sender_spool,sender}.py`、`sql/sender_001.sql`、`tests/test_waveform_sender.py`。依赖 Task 1—2、7。

**Interfaces:** `WaveSenderSpool.initialize(path, *, max_bytes: int)`、`enqueue(source: BinaryIO) -> UUID`、`apply_receipt(topic: str, payload: bytes) -> bool`、`capture_state(key: CaptureKey) -> str`、`status() -> dict`（含 retained_capture_bytes）；CLI `python -m backend.waveforms.sender init|enqueue|run|status --config PATH`，enqueue 另带 `--input`。

- [ ] **1. 写 `test_broker_ack_keeps_capture`、`test_stored_chunk_does_not_release_file`、`test_capture_receipt_requires_manifest_digest`、`test_old_scalar_spool_untouched` 红灯：

```python
self.assertEqual(spool.status()["retained_capture_bytes"], original_bytes)
self.assertFalse(spool.apply_receipt(wrong_topic, valid_receipt))
self.assertEqual(spool.capture_state(key), "awaiting_capture_receipt")
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_sender -v`。
- [ ] **3. 实现新 SQLite schema 与队列锁。** 内容先持久再发，message/capture 状态分列；完整回执前保留副本，收到 complete 只转 archived，不自动删。重试已确认清单可取回整段结果；未知/冲突回执写有界事件。满盘/配额拒新文件、不覆盖旧文件；不打开或迁移既有 gateway-v2.db。
- [ ] **4. 绿灯同命令。** 进程重启、消息回执乱序、错误摘要、队列满、时钟回退；证实同源文件重入队幂等且本地损坏不再发送为合法录波。
- [ ] **5. 提交：** `feat: add persistent waveform file sender`。

### Task 9: 可恢复离线导入会话

**Files:** Create `backend/waveforms/{commands,imports}.py`、`tests/test_waveform_imports.py`。依赖 Task 2、4—5。

**Interfaces:** 产出 Actor/Command；`ImportService.create(metadata: dict, command: Command) -> ImportSession`、`append(id: UUID, line: int, payload: bytes, command: Command) -> ImportProgress`、`seal(id: UUID, command: Command) -> ImportResult`。metadata 固定 file_sha256/bytes/line_count/final_lf/gateway_id/device_id/capture_id。

- [ ] **1. 写 `test_reimport_reuses_facts_not_provenance`、`test_seal_exact_final_lf`、`test_line_commit_and_session_atomic`、`test_lost_response_retry_same_key` 红灯，断言离线 receipt_id=None，未 seal 来源为 pending，重复会话不复制 facts。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_imports -v`。
- [ ] **3. 实现服务事务。** 会话创建/每行关联与 Task 4 内部保存同一事务；API 只有 commit 后才能报 saved。按原文件行序+final_lf 核对 SHA，丢行可查询后补传。相同 key 改正文冲突，换会话不能绕过 capture 额度；限制每 actor 同时 4 个未封存会话、单 capture 8 个来源会话，总量计元数据额度。取消仅追加状态，不删原文。
- [ ] **4. 绿灯同命令。** 截断/BOM/CRLF、混合 capture、伪造文件摘要、断点/重复行、并发版本变化、权限撤回后的重试必须经 API 重验（Task 13）。
- [ ] **5. 提交：** `feat: resume audited offline waveform imports`。

### Task 10: 校准/采样/时钟/来源资料与历史井核查

**Files:** Create `backend/waveforms/{evidence,registry_context}.py`、`sql/002_evidence.sql`、`tests/test_waveform_evidence.py`、`tests/test_waveform_registry_context.py`；更新 migrate/privileges/grants。依赖 Task 3、9。

**Interfaces:** 产出 EvidenceService；`RegistryContext.resolve(sources: tuple[CaptureKey,...], *, utc_start, start_ns: int, end_ns: int, uncertainty_ns: int|None, revision: int|None) -> RegistrySnapshot`，snapshot 含 resolved/pending/ineligible、井/绑定/状态记录及完整窗口证据；采集源按 gateway/device 身份解析，capture UUID 只作引用，不用当前井名匹配。

资料 kind 仅 sampling_config、calibration、sync、origin、profile。每种 body 固定字段：公共 source_kind、device/channel 引用、valid_from/to、附件 IDs、说明；sampling_config 加量程/有效位/接线/滤波/时钟/窗口定义，calibration 加 scale/offset/unit/零点方向/测量不确定度/time_correction_ns/residual_ns，sync 加时钟域映射/适用窗口/偏移和频率误差界，origin 加 source_identity/核查凭据引用，profile 加设计 §8 的用途/必需物理量/采样率/同步/量程/证据门槛。采样配置内容 SHA 与 calibration/config ID 绑定，不以相同单位字符串代替配置一致。

- [ ] **1. 写 `test_review_not_creator_even_admin`、`test_revoke_appends_not_rewrites`、`test_ns_at_binding_end_is_not_inside`、`test_unknown_utc_does_not_use_received_time` 红灯：

```python
self.assertEqual(resolver.resolve(sources, utc_start=binding_end, start_ns=0, end_ns=1,
                                 uncertainty_ns=1, revision=None).status, "ineligible")
self.assertEqual(resolver.resolve(sources, utc_start=None, start_ns=0, end_ns=1,
                                 uncertainty_ns=None, revision=None).status, "pending")
self.assertEqual(bytes_before_revoke, bytes_after_revoke)
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_evidence tests.test_waveform_registry_context -v`。
- [ ] **3. 实现不可变内容+追加 review/revoke。** 002_evidence.sql 创建 evidence_versions/evidence_events/attachments/restrictions 和供下一任务使用的 quality_runs/pair_versions/pair_reviews；禁止日后改写已执行迁移。先上传受控 attachment 得 UUID/摘要，create 引用已上传附件（≤8），批准后不加附件改依据；附件安全响应为下载、不内嵌，验证类型签名字节、大小和配额。`RegistryContext` 用只读 REPEATABLE READ 快照核查所有源，边界计算保留 Fraction/ns；先外包到微秒查询候选，再用精确 ns 核对，禁止四舍五入放行。对 device_registry 仅授予必需历史视图读取权，不授予修改；资料有效期及台账全过程均覆盖，未知字段不可默认 0。测试只在新的波形隔离库调用 registry.migrate 创建测试台账并登记两井/迁井时间线，不继承会清空原WP02库的 RegistryDatabaseCase；正常业务部署仍消费已存在的WP02台账。
- [ ] **4. 绿灯同命令。** 覆盖跨迁井/状态/有效期边界、多个设备同井/不同井、共享 clock_domain 标签但无证据、资料附件伪 MIME/大文件/外部 URL、同键重试、并发批准/撤销；测试资料统一留在隔离库。
- [ ] **5. 提交：** `feat: govern waveform evidence and historical source context`。

### Task 11: 时间误差、用途质量门禁与周期配对

**Files:** Create `backend/waveforms/{timing,quality,pairing}.py`、`tests/test_waveform_timing.py`、`tests/test_waveform_quality.py`、`tests/test_waveform_pairing.py`。依赖 Task 5、10。

**Interfaces:** 产出 QualityContext/ProfileVersion/QualityResult、PairService；`sample_time_ns(stream, index: int, *, correction_ns: int) -> Fraction`、`sync_bound_ns(context, duration_ns: int) -> Fraction|None`、`align_pair(context, *, reference_times: tuple[Fraction,...]) -> AlignmentPreview`。quality_run 保存参数/资料/台账版本和理由；PairVersion 保存原始索引与方法版本。

QualityContext 固定字段 key、completion、manifest_sha256、source_kind、origin_evidence、registry_snapshot、streams、calibrations、sampling_configs、sync_evidence、cycle、sample_quality_counts、restrictions；缺依赖以显式None表示。ProfileVersion 固定 id/version/purpose/required_quantities/min_rates/max_sync_error_ns/ranges/required_evidence/review_state。软件测试profile可以有确定的测试阈值但purpose不可变成field；所有证据ID及内容摘要同时写入quality_run。AlignmentPreview包含原始参考时刻、两侧原始索引、插值标识、缺口及method_version，不只返回两条绘图数组。

- [ ] **1. 写同步/准入红灯。** `test_delay_sign_and_unknown_budget`、`test_same_cycle_label_not_sufficient`、`test_missing_blocks_field_pair`、`test_rms_not_instantaneous`、`test_interpolation_never_bridges_missing`：

```python
self.assertEqual(sample_time_ns(stream_100_hz, 1, correction_ns=-1_000_000), Fraction(9_000_000))
self.assertIsNone(sync_bound_ns(unknown_clock_context, 10_000_000_000))
self.assertIn("missing_samples", evaluate(missing_context, profile=field_profile).reasons)
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_timing tests.test_waveform_quality tests.test_waveform_pairing -v`。
- [ ] **3. 实现精确时间和固定用途门禁。** offset/rate 描述公共名义时基，线性时钟证据用于核对这份映射和残余误差，不再重复添加同一偏移；校准通道时序修正仅一次。误差按设计保守合成，未知为 None。raw_archive、software_pair_test、field_pair_input 分开结果；无现场 profile 默认不可现场准入。定量有效区间不能跨 missing/bad/clipped/uncertain，插值≤2个名义周期、不外推。三相瞬时组或有方法/窗口依据的总有功组二选一，不能把 I_rms 填 I_instantaneous；几何/反演算法不在本任务执行。
- [ ] **4. 同命令绿灯。** golden 向量覆盖不同采样率、已知延迟/漂移/负功率/反向位移/掉点/跨周期，按独立手算结果断言；测试 helper 构造的 field 规则上下文仅证明判定程序，不作为真实设备合格记录。机器硬失败复核被拒，换 profile/资料/方法使旧审批失效；自审在服务层拒绝。
- [ ] **5. 提交：** `feat: gate synchronized pairs with traceable timing and calibration`。

### Task 12: 数据集冻结、划分约束与导出

**Files:** Create `backend/waveforms/{datasets,export}.py`、`sql/003_datasets.sql`、`tests/test_waveform_datasets.py`、`tests/test_waveform_export.py`；更新 migrations/grants。依赖 Task 2、10—11。

**Interfaces:** 产出 DatasetService/Exporter。成员 `{kind: capture|pair, reference_id, quality_run_id, split}`；capture 成员仅 raw_archive 可用，其余用途必须配对成员；version DTO 含 content_sha256、purpose、restriction_state、成员和快照版本，列表不含原文。

- [ ] **1. 写 `test_freeze_immutable_manifest`、`test_same_cycle_variants_cannot_cross_splits`、`test_revoke_racing_freeze`、`test_export_lf_and_order_stable` 红灯：

```python
self.assertEqual(first_export_bytes, second_export_bytes)
self.assertEqual(sha256(first_export_bytes).hexdigest(), manifest["file_sha256"])
self.assertEqual(old_dataset_bytes, reread_frozen_dataset_bytes)
self.assertTrue(revoked_dataset["restricted"])
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_datasets tests.test_waveform_export -v`。
- [ ] **3. 实现事务冻结与使用限制。** 草稿管理、提交与不同账号 review/freeze 分开；冻结/资料撤销按统一依赖 guard 锁顺序，避免检查后撤销穿过发布。≤1000 成员；well holdout 同井不跨区，time holdout 区间/留出量显式、训练早于验证/测试，未分区只能收集。设备+boot+周期/重叠时间连通组及原文重复识别防止改 capture UUID 泄漏。原文留存不取得训练资格。固定清单 JSON 排序/UTF-8/无空白规则并保存其字节，导出引用当时版本，不查询“最新”替换旧成员。
- [ ] **4. 绿灯同命令。** 同内容改编号、跨通道派生变体、未证实独立性、边界重叠、混合 synthetic/field、1001成员、冻结响应丢失、后撤销仍可证据导出但不能新训练导出。下载逐片流出，不造大 ZIP；文件名由服务端固定生成，重试内容一致。
- [ ] **5. 提交：** `feat: freeze reproducible waveform datasets and audited exports`。

### Task 13: 权限、严格 HTTP 边界与可选服务集成

**Files:** Create `backend/waveforms/{api,routes,permissions,runtime,queries}.py`、`tests/test_waveform_api.py`、`tests/test_waveform_permissions.py`、`tests/test_waveform_startup.py`；Modify `backend/http_server.py` 的 do_* / create_server / _auth_me、`backend/seed.py` 的权限汇总与一次性 bootstrap、`server.py` 配置接缝。依赖 Task 6、9—12。

**Interfaces:** 产出 B 节 API/runtime/queries；`required_permissions(action: str) -> frozenset[str]`。HTTP 成功结构沿用 `{ok:true,data:...}`，失败沿用安全 code/message；capabilities.waveforms={enabled,status}，status=disabled/ready/unavailable。

固定路由如下。全部路径 UUID 严格校验，冒号项是路径变量；每个实体的详细字段对应前面已固化 DTO。

| Method / `/api/waveforms` 后缀 | 接口与权限 |
| --- | --- |
| GET `/health`, `/captures`, `/captures/:id` | read；id 为中心稳定资源 UUID，响应另含 CaptureKey |
| GET `/captures/:id/streams/:stream/window` | read；start/count/mode=raw或summary/budget；工程值窗口不是精确原文 |
| GET `/facts/:id/raw` | read+raw.read，审计下载单个精确报文 |
| POST `/imports`, `/imports/:id/lines/:line`, `/imports/:id/seal`, `/imports/:id/cancel`；GET `/imports/:id` | read+import；line 为十进制0—512，原文上传不用 JSON reserialize |
| POST `/attachments`；GET `/attachments/:id/raw` | 写：read+evidence.manage；读：read+raw.read；上传4MiB专用限额 |
| GET/POST `/evidence`；GET `/evidence/:id`；POST `/evidence/:id/review`, `/evidence/:id/revoke` | 读：read；建：evidence.manage；审/撤：review；kind过滤包含采样/校准/时钟/来源/profile |
| POST `/captures/:id/evaluations`；GET `/captures/:id/evaluations` | read+pair.manage 请求追加评估；read 查询；不得提交自定的 eligible 结果 |
| GET/POST `/pairs`；GET `/pairs/:id`, `/pairs/:id/preview`；POST `/pairs/:id/review` | read；建：pair.manage；审：review，禁止自审 |
| GET/POST `/datasets`；GET `/datasets/:id`；POST `/datasets/:id/update`, `/datasets/:id/submit`, `/datasets/:id/freeze` | 读：read；写/提交：dataset.manage；冻结：review，禁止自审 |
| POST `/dataset-versions/:id/exports`；GET `/exports/:id/manifest`, `/exports/:id/captures/:capture/file` | read+raw.read+export；purpose=research/training/evidence，逐下载重新鉴权/检查限制 |
| GET `/audit` | read+audit.read |

相同 idempotency_key 绑定 actor、路由及精确请求摘要；普通管理 JSON≤64KiB。原文行上传 Content-Type=application/octet-stream，Idempotency-Key、If-Match（字符串版本）在 header；附件同样专用 raw body，固定类型/摘要 header。服务端拒绝重复身份/版本 header、不支持的 Transfer-Encoding、冲突 Content-Length 和超限 body，未消费的非法 body 关闭该连接，不能串到下一请求。版本不足报409；无权限403、未登录401、输入422、超限413、依赖不可用503。列表 page_size≤200、page≤100001、最大offset100000；翻页必须保持字符串 snapshot_revision 与 received_before，复杂 time过滤最大31天。state_head在用户可见状态变化时递增；首屏单事务读取，后续页修订变化返回409要求刷新，不假称保有跨请求数据库快照。列表过滤字段白名单，未知/重复 query 均拒绝。

- [ ] **1. 写角色/边界红灯。** `test_role_matrix_and_no_self_review`、`test_admin_union_preserves_revoked_other_roles`、`test_raw_upload_does_not_use_json_body`、`test_failed_commit_not_http_success`、`test_denied_replay_never_leaks_previous_result`；断言所有未授权 route 403，disabled 与 unavailable 不返回空集合伪成功。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_api tests.test_waveform_permissions tests.test_waveform_startup -v`。
- [ ] **3. 实现轻量 API 适配。** 沿用既有会话校验、Origin规范化和安全错误响应，不调用旧 AnalysisService.run。Web management 显式加载 reader/operator 与仅供 ImportService 的 receiver 连接：导入在 receiver 事务内保存 imports/lines/facts/audit，operator 不因方便而取得事实修改权。server.py 增加 `--waveform-config`/`PLATFORM_WAVEFORM_CONFIG` 并在隐藏子进程启动参数中透传，create_server增加 `waveform_config_path=None`；缺省disabled，不复用center配置误启用。AUTH_BOOTSTRAP_VERSION 从当前9进到10，只给新权限做一次引入，保持既有人工撤权。schema/runtime探测只读。
- [ ] **4. 绿灯同命令，另跑：** `& $wavePython -m unittest tests.test_registry_permissions tests.test_ingest_api -v`。真实 HTTP 验证 CSRF/Origin、过期/撤权、未知方法/编码路径、精确256KiB行、并发版本、raw权限与全量角色页面能力一致。
- [ ] **5. 提交：** `feat: expose secured optional waveform management APIs`。

### Task 14: 原始窗口、有界摘要与传感器图数据

**Files:** Create `backend/waveforms/windows.py`、`tests/test_waveform_windows.py`；完成 queries 的窗口/快照读取。依赖 Task 11、13。

**Interfaces:** 产出 WaveformWindows.read 和 `WindowResult(points, gaps, mode, total_frames, calibration_id, time_basis)`；`WindowPoint(index: str, relative_ns: str, value: Optional[float], quality: int, raw_index: str)`；XY 点另含校准位移/载荷与实际参考时间，不按位移重排。

- [ ] **1. 写 `test_summary_points_are_original_ordered_samples`、`test_missing_not_connected`、`test_budget_including_segment_boundaries`、`test_raw_window_precision` 红灯：

```python
self.assertLessEqual(len(result.points), 2000)
self.assertEqual([p.index for p in result.points], sorted(set(p.index for p in result.points), key=int))
self.assertIn("9007199254740993", [p.index for p in result.points])
self.assertTrue(result.gaps)
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_windows -v`。
- [ ] **3. 实现保持顺序的分段首末/极值摘要。** 先在 good 连续区段分桶，每桶首、末、最小、最大去重后按原始索引排序，再合并；预算含区段端点，若缺口数量超可显示预算则返回 gap_overflow 与更窄窗口建议，不擅自连线。raw最多20000、summary最多2000；保留负值，工程转换结果非有限时返回质量原因不传播NaN。XY预览使用Task11已声明对齐，闭环连线不冒充新增实测闭合点。
- [ ] **4. 绿灯同命令。** f32极值、scale溢出、单点区段、全missing、20001帧请求、重复极值、来回位移、超大序号、撤销校准后的预览限制均固定断言；摘要点值必须能回到原始分片索引。
- [ ] **5. 提交：** `feat: query precise waveform windows and gap-safe previews`。

### Task 15: 四工作视图、恢复导入与精确图表交互

**Files:** Create B节前端各文件、`tests/test_waveform_browser.py`、`tests/fixtures/waveform_workspace_check.html`、`waveform_actions_check.html`、`waveform_import_check.html`、`waveform_chart_check.html`、`waveform_fixture_runner.cjs`；Modify `app.js`、`styles.css`，仅必要时修改 `frontend/api.js` 增加通用原始字节请求而不改变旧 request 语义。依赖 Task 13—14。

**Interfaces:** 产出 renderWaveforms/createWaveformClient；`mountWaveformChart(root, result, {onWindowRequest}) -> {dispose()}`；`scanWaveformFile(file: File) -> Promise<FileDescriptor>` 与 server contracts共用发布的schema/向量一致性测试，服务端始终复验。

- [ ] **1. 写 DOM/浏览器红灯。** `test_four_states_and_direct_route`、`test_two_decimal_hover_retains_exact_index`、`test_gaps_not_joined`、`test_resume_import_original_bytes`、`test_stale_selection_and_revoked_retry`：fixture 断言 hover横纵数值两位、index文本为9007199254740993、两缺失区段之间无path连接，管理员看全部入口但自审返回拒绝。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_browser -v`，以现有 fixture runner 调用 Node+Playwright+Edge；浏览器退出码与 JSON `ok:true` 均核验，测试不能仅检查页面源字符串。
- [ ] **3. 实现页面与专用客户端。** /#/waveforms 四视图 captures/pairs/datasets/imports，详情内带资料面板；navigation需waveform.read及capability。使用 TextDecoder fatal/逐行 bytes、Web Crypto文件摘要、有界逐片上传，payload不经过 JSON.stringify；浏览器预检深度/重复键/数值词法由专门受限解析器识别，不只依赖 JSON.parse。上传身份/版本/幂等键在重试前固定；离开页面仅停网络，不删除已保存会话。图表相对窗口坐标才转 Number，绝对index/ns留字符串/BigInt；缺口多路径、细线、hover才出点。来源/完整性/用途资格分别显示，不伪造现场反演。
- [ ] **4. 绿灯同命令。** 1440与1024宽度、所有角色、direct URL、未配置/配置错/无记录、失去会话、晚到成功/失败、工作区切换和dispose，保存局部截图核对；浏览器与Task2同一套文件向量一致。另跑 `tests.test_ingest_browser tests.test_dual_dynagraph_ui tests.test_page_contracts` 防旧行为回退。
- [ ] **5. 提交：** `feat: add waveform and paired dataset workspace`。

### Task 16: 厂家接口包与安全部署资料

**Files:** Create/Modify B节公开资料、构建工具与 `docs/同步波形部署与回退.md`；Create `tests/test_waveform_interface.py`、`tests/test_waveform_packaging.py`。依赖 Task 1—15。

**Interfaces:** `tools/build_open_interface_kit.py:PUBLIC_FILES` 仅增加明确审阅的新协议文件与校验器依赖白名单；schema使用 additionalProperties=false，描述字段/类型/长度；重复键、数值词法、二进制摘要和跨片约束由同源规范校验器执行，不宣称单靠JSON Schema即可完成全部合同验证。公共工具不依赖项目数据库；接口包保持已有 v1 和ACK文件可用。

- [ ] **1. 写公共/中心合同交叉红灯。** `test_public_validator_matches_center`、`test_public_zip_exact_whitelist`、`test_reproducible_kit_no_private_inputs`，相同黄金/坏向量得相同code，ZIP连续构建摘要相同且不含 .local.toml/DB/logs/secret。
- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_interface tests.test_waveform_packaging -v`，在临时公开根测试，不先改运行站点文件。
- [ ] **3. 发布同源公共校验代码。** 构建时将纯内核 errors/contracts/wire/samples/files 显式映射为白名单中的 waveform_*.py；仅机械改写这些已知内部 import 为平铺模块名，保留其他字节与来源摘要。任何新内部依赖必须显式扩白名单并测试，不自动递归发现。CLI使用同样的 files/wire代码；独立临时目录用隔离Python运行，无仓库sys.path也能验证。不得人工维护另一套宽严不同的核心。WAVEFORM.md含两种输入、精确字段/上限、四种确认、缺样/来源/校准边界和重试例。公共金样本不超过32KiB且标synthetic，完整高采样率向量运行时生成、不提交大文件；不公开现场文件。部署资料列独立账号/主题ACL/TLS、迁移备份、权限探测、磁盘/配额、停收及回退保留队列，不声称实物已接入。
- [ ] **4. 绿灯同命令，另跑：** `& $wavePython -m unittest tests.test_open_interface tests.test_ingest_interface -v`；用更新的白名单工具生成开发工作区公开包，再核对每个成员字节。原冻结演示包不重建。
- [ ] **5. 提交：** `docs: publish verified waveform integration kit and operations guide`。

### Task 17: 故障集成、容量/恢复证据与整体验证

**Files:** Create `tests/test_waveform_end_to_end.py`、`tests/test_waveform_failure_recovery.py`、`tests/test_waveform_verification_tool.py`、`tools/verify_waveforms.py`；完成 `tests/waveform_stack.py`、`waveform_worker.py`；Modify `docs/年度工作任务逐项响应.md`，生成忽略目录中的报告而不提交私密日志。依赖全部任务。

**Interfaces:** CLI `python tools/verify_waveforms.py --config PATH --evidence-dir PATH --duration-seconds 1800 --include-existing-regression`；`qualify(report: dict) -> tuple[str,...]` 输出不能签发通过的具体原因。工具只编排本run_id拥有的子进程，不调用当前8765或业务库。

- [ ] **1. 写验收器红灯。** `test_gate_rejects_skip_partial_run_or_short_soak`、`test_gate_requires_both_ingress_paths_and_frozen_protection`：

```python
self.assertIn("tests.skipped", qualify(report_with_one_skip))
self.assertIn("duration.too_short", qualify(report_with_1799_seconds))
self.assertEqual(qualify(complete_software_report), ())
```

- [ ] **2. 红灯：** `& $wavePython -m unittest tests.test_waveform_verification_tool tests.test_waveform_end_to_end tests.test_waveform_failure_recovery -v`。
- [ ] **3. 实现自有协议实验栈与报告。** 两网关在线+离线同源数据、非默认临时Broker/HTTP端口、至少10轮接收/组装/发布/发送端重启或链路故障、一个网关容量耗尽、真数据库事务回滚/提交不确定、错回执和丢回执。至少30分钟恢复实验不冒充72小时现场稳定性；量测最大capture内存、正常/异常延迟、逻辑原文/物理数据库增长，记录机器配置/速率。备份仅新库，用另一明确命名的隔离恢复库核对facts/versions/导出字节；恢复库同样按身份和所有者限定，保留失败证据不自动广域清理。
- [ ] **4. 绿灯及最终门禁。** 先跑本任务同命令，再 `& $wavePython -m unittest discover -s tests -p 'test_waveform_*.py' -v`，最后验证工具带 `--include-existing-regression` 串行运行 `unittest discover -s tests -p 'test_*.py'`。回归分母由实际发现数决定，0 failures/errors/skipped；核对现有v1/WP02/WP03测试、冻结目录/ZIP/tag及原工作区状态。测试启动前后记录环境状态，仅恢复自己启动的隔离服务状态。
- [ ] **5. 独立审查、修复再验证后提交：** `test: verify waveform recovery integrity and acceptance boundaries`。更新年度任务表时区分设计、软件实现、未部署及待现场验证；最终交付文件/提交/命令/报告摘要，不宣称完成硬件或现场算法效果。不自动合并或推送 PR。

## D. 依赖、验证覆盖与实施交接

主依赖顺序：1→2→3→4→5→6→7→8；9依赖2/4/5，10依赖3/9，11→12→13→14→15→16→17。允许在合同及schema接口锁定后并行只读审查，或安排互不改共享文件的发送端与资料业务实现；数据库真实测试使用全局隔离库锁串行，不能为“并行”绕过fixture保护。API/前端在服务返回结构稳定后开始，避免多个人各自发明字段。

| 设计验收项 | 负责实现/证据任务 |
| --- | --- |
| W04-01 统一内核 | 1、2、9、16、17 |
| W04-02 严格合同 | 1、2、13、16 |
| W04-03 分片与完整性 | 4、5、17 |
| W04-04 提交/回执竞争 | 4、5、7、9、17 |
| W04-05 重启恢复 | 5—8、17 |
| W04-06 容量与隔离 | 3、4、6、9、10、17 |
| W04-07 时间/缺样 | 1、10、11、14 |
| W04-08 历史归属/资料 | 10、11、12 |
| W04-09 来源与物理量 | 10、11、12、15 |
| W04-10 权限/复核 | 3、10—13、15 |
| W04-11 冻结与独立性 | 11、12、17 |
| W04-12 离线及安全 | 2、9、13、15 |
| W04-13 浏览器行为 | 14、15、17 |
| W04-14 迁移/备份/最小授权 | 3、10、12、13、17 |
| W04-15 回归与冻结保护 | 13、15—17 |

### 计划自查清单

- [x] 主代理逐节核对设计，确认每项都有实现任务/证据；并行代理仅提供既有接缝/测试限制的只读核查。
- [x] 检查跨任务类型、文件和函数名一致；修正事务返回值、文件末行字节、迁移归属与公开工具同源发布；新增/现有文件明确区分。
- [x] 五项 Review Focus 均有具体测试归属；尤其原文字节、ns边界、独立回压、摘要缺口与不确定写入。
- [x] 检查容量/编码/数据用途/现场未验证边界不弱于设计；没有引入控制/训练或生产部署。静态计划检查不等于这些测试已经通过。
- [ ] 仅在计划及执行方式获得确认后开始 Task 1。计划阶段不运行带迁移/TRUNCATE 的旧测试fixture或重建公开ZIP。

### 用户审阅与执行方式

建议 **逐任务开发并独立审查**：每项由独立上下文的实施者完成，再由独立审查者核对，最后进行整包审查。理由是本包包含原文不可变、时间误差和数据集准入等高影响边界，阶段审查能减少错误累积；代价是更多审查上下文和时间。

另一方式为 **本会话直接实施，最后整体独立审查**：我逐项实现、测试并提交，最终另请独立审查者检查。其审查开销较小，但跨任务问题可能到最后才暴露。

用户确认计划并选择执行方式后，分别进入 subagent-driven-development 或 executing-plans。这里只询问这一轮计划与方式，不重复征求已确认的架构方案，不把本次“确认书面设计”扩张成对尚未写出的计划或部署的批准。
