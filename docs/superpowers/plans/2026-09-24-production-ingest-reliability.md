# WP-03 Production Ingest Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现异常报文可靠留存、中心事务性回执、网关应用确认及真实可操作的异常处置工作区，并通过隔离环境的断网、重启、并发和权限验证。

**Architecture:** 保持 v1 遥测契约，在现有中心 PostgreSQL 内增加 `ingest_reliability`，将接收事实、原文、测点及待发回执放进同一事务；独立发送进程负责回执。网关持久队列按每条记录的确认模式处理，界面复用现有登录与导航，不以 Broker 确认代替数据库入库。

**Tech Stack:** 项目现有 Python 3.13、PostgreSQL 18 / psycopg 3、SQLite、Paho MQTT 5、Mosquitto、标准库 HTTP、原生 JavaScript/CSS、unittest 与本机 Edge；不增加 Web 框架、CDN 或消息中间件。

**Spec:** [已确认书面设计](../specs/2026-09-24-production-ingest-reliability-design.md)。实施者先完整阅读设计和本计划。用户已确认设计；本计划与执行方式待确认。这里的代码是测试和实现约束，不表示文件已实现、测试已通过或服务已部署。

## Global Constraints

- 保持现有遥测格式，不重新设计整套设备协议。
- 对应任务：Y1-02 可靠采集与处理、Y1-03 稳定安全传输与共享；处置界面支撑 Y3-05 集成平台。不包含绩效目标、专利、软著或奖项。
- v1 有效 payload 上限仍为 1 MiB。隔离单条原文上限设为 2 MiB。
- 新增回执主题：`{prefix}/v2/{gateway_id}/{device_id}/ingest-ack`，QoS 1、retain=false。
- 回执为严格 UTF-8 JSON 对象，上限 4 KiB；字段全为字符串，不接受数组或嵌套对象，拒绝重复/未知字段及错误类型。
- 首版拒绝代码限定为 `topic_invalid`、`payload_too_large`、`utf8_invalid`、`json_invalid`、`json_duplicate_key`、`contract_invalid`、`identity_mismatch`、`idempotency_conflict`、`qos_not_supported`、`retained_publish`。
- SHA-256 是内容匹配凭据，不是身份签名。
- 台账不可用不应阻止合法遥测的原始入库。
- 首版用中心配置中的明确网关允许列表启用应用回执，默认列表为空；不从发现消息或设备登记状态自动启用。
- 首次成功提交的结果固定；不自动重判原文、不自动重放隔离数据。
- 确认模式显式配置为 broker 或 application，并在每条队列记录入队时固定。
- 本轮不增加“回执的回执”；中心不得显示未经证明的“网关已确认”。
- 不在启动时建库、执行 DDL、授予权限或使用管理员账号补救；不直接给现有入库账号提权。
- 首版不自动清除原文、幂等事实或回执；没有现场设备时不编造厂家实物接入证明。
- 冻结演示版、既有业务库和运行服务不因本设计或隔离环境研发被改动。
- 不开放 Modbus 写、远程启停、配置下发、波形、功图反演或电机保护功能；这些属于后续工作包。

## Review Focus

1. 语义相同但空白/字段顺序不同的 JSON：摘要按精确字节，不可被“规范化”后误去重；同编号异字节必须冲突（Task 1、3）。
2. 同一 message_id 被另一个网关复用：既有消息不变，拒绝回执不能泄露旧身份或完成另一条本地记录（Task 3、8）。
3. 发布回调早于 publish 返回，或重连后 MQTT mid 被复用：按连接代次隔离回调，不能确认错误租约（Task 6、9）。
4. 重启后本地时钟回退且记录仍在等待：不会无限延期、错误过期或伪造成功，明确报时钟异常并有界重试（Task 5、8）。
5. 人工操作已提交但 HTTP 返回 5xx，随后切换选择或撤权：原键重试、重新鉴权、旧响应不覆盖新详情（Task 10—12）。

---

## A. 工期、执行边界与文件结构

### 排期估算

仅估算 WP-03，按每天 8 小时折算，净开发与验证约 **80—112 小时，即 10—14 个工作日**。这是工作量区间，不是承诺后台连续运行或某日自动交付；以每个任务的实际测试结果更新，不用完成文件数计算百分比。

| 阶段 | 任务 | 预计工作量 | 可核对交付 |
| --- | --- | --- | --- |
| 中心可靠接收与回执 | 1—6 | 28—40 小时 | 原文留存、隔离、事务回执、发送恢复 |
| 网关确认与升级 | 7—9 | 20—28 小时 | 不提前完成、乱序/重启恢复、旧队列保全 |
| 运维业务与页面 | 10—12 | 16—24 小时 | 查询、授权原文访问、审计处置、真实页面 |
| 厂家资料与集成验证 | 13—14 | 16—20 小时 | 自检包、故障证据、回归与交付记录 |

主要风险是数据库并发、回执乱序、旧队列升级和最终审查修复。出现新增接口需求或环境授权缺失时单独报告影响；不把现场联调、生产部署、24—72 小时耐久实验和其余 WP-04—08 算进这个区间。

### 工作区和保护对象

当前独立工作区：`C:/Users/hp/.codex/worktrees/production-device-registry/外专双百`，设计提交 `32a1354`。计划批准后以当时实际 HEAD 为起点，不重置历史、不覆盖原目录的未跟踪 `scripts/`，不重复创建已存在的隔离 worktree。

冻结目录 `C:/Users/hp/Documents/外专双百-演示版-20260912`、其 source ZIP 及 `demo-v2.0-20260912` 标签只读。既有 8765 网页、5432 数据库与现有 Broker 不重启、不改配置、不注入测试数据。此计划不授权正式部署、合并、推送或创建 PR。

### 文件图（Create 指新建，不代表已经存在）

| 文件 | 职责 / 任务 |
| --- | --- |
| Create `backend/ingest_reliability/{__init__,errors,contracts,wire}.py` | 稳定类型、严格回执、输入分类（1） |
| Create `backend/ingest_reliability/sql/{001_reliability,grants}.sql`, `migrate.py`, `privileges.py` | 完整 DDL、显式角色授权、结构/实际权限核验（2） |
| Create `tests/ingest_support.py`, `tools/provision_ingest_test.py` | 新测试库、四类运行身份及独立迁移身份与安全重置（2） |
| Create `backend/ingest_reliability/{repository,facts}.py`; Modify `backend/center/postgres.py` | 一次提交接收事实、原文、测点和回执（3） |
| Create `backend/ingest_reliability/{config,consumer}.py`; Modify `backend/center/__main__.py` | 可选接收器、失败分类和启动门禁（4） |
| Create `backend/ingest_reliability/outbox.py` | 租约、请求代次、补发、容量/时间边界（5） |
| Create `backend/ingest_reliability/{publisher,__main__}.py` | 独立回执进程、MQTT 发送和健康（6） |
| Create `backend/gateway/{spool_migrate,reliable_spool}.py`, `backend/gateway/sql/002_reliable_spool.sql`; Modify `backend/gateway/spool.py` | 显式队列升级、防旧写入、精确入队与容量（7） |
| Create `backend/gateway/receipt_state.py`; Modify `backend/gateway/reliable_spool.py` | 应用回执持久状态机及异常回执事件（8） |
| Create `backend/gateway/reliable_uplink.py`; Modify `backend/gateway/{config,runtime,__main__}.py` | 异步收发、确认模式、CLI 状态和退出（9） |
| Create `backend/ingest_reliability/{queries,operations,management_contracts}.py` | 元数据查询、原文授权访问、人工处置事务（10） |
| Create `backend/ingest_reliability/{api,permissions,runtime}.py`; Modify `backend/{http_server,seed}.py` | HTTP/RBAC、能力声明、只一次权限引入（11） |
| Create `frontend/pages/ingest-reliability.js`, `frontend/ingest/{client,detail,actions}.js`; Modify `app.js`, `styles.css` | 接收异常工作区与请求竞争保护（12） |
| Modify `config/{center.example.toml,gateway.example.toml,mosquitto.acl.example,mosquitto.example.conf}`, `assets/open-interface/` | 开关、角色、厂商回执契约及可下载包（4、6、9、13） |
| Create `tools/verify_ingest_reliability.py`, `docs/生产接收可靠性部署与回退.md`; Modify `docs/年度工作任务逐项响应.md` | 分层最终验证与真实状态交付（14） |

测试随任务创建 `tests/test_ingest_*.py`、`tests/test_gateway_receipt_*.py` 和 `tests/fixtures/ingest_*.html`。不新增第二套 Web 框架，不重构无关演示业务。

## B. 固定接口与验证约定

所有新模块导入无数据库连接或后台线程副作用。以下类型/接口是任务之间的约定，必须一起维护；实现允许增加私有函数，但不能静默改名或让后续任务猜返回结构。

| 定义 | 字段 / 签名 |
| --- | --- |
| `ReliabilityError`（errors.py） | `code: str, message: str, status: int = 503`；系统错误，不直接用来判坏报文 |
| `Observation`（contracts.py） | frozen dataclass：`topic: str, payload: bytes, qos: int, retain: bool, dup: bool, received_at: datetime` |
| `Correlation`（contracts.py） | frozen dataclass：`gateway_id: str, device_id: str, message_id: str` |
| `Candidate`（contracts.py） | frozen dataclass：`batch: UplinkBatch | None, correlation: Correlation | None, reason_code: str | None, validator_version: str` |
| `Receipt`（contracts.py） | frozen dataclass：设计 §5 的全部字符串字段；`from_bytes(payload: bytes) -> Receipt`, `to_bytes() -> bytes`, `topic(prefix: str) -> str`, `matches(topic: str, observation: Observation) -> bool` |
| `CapacityLimits`（contracts.py） | frozen dataclass：`total_bytes: int, quarantine_bytes: int`；均正数，后者不大于前者 |
| `CommittedResult`（contracts.py） | frozen dataclass：`fact_id: UUID, status: str, receipt_id: UUID | None, duplicate: bool`；只在事务提交后返回 |
| `classify`（wire.py） | `classify(observation: Observation, *, prefix: str) -> Candidate`；不执行 I/O；内部时钟错误/超过 2 MiB 抛系统错误 |
| `ReliableRepository`（repository.py） | `ReliableRepository(connect, *, prefix: str, receipt_gateways: frozenset[str], limits: CapacityLimits)`；`ingest(observation: Observation) -> CommittedResult` |
| `schedule_receipt`（facts.py） | `schedule_receipt(cur, receipt_id: UUID, instant: datetime) -> None`；在调用者事务内登记请求；任务 3 定义，任务 5/10 复用 |
| `DeliveryLease`（outbox.py） | frozen dataclass：`receipt_id: UUID, token: UUID, generation: int, topic: str, payload: bytes, attempts: int` |
| `OutboxRepository`（outbox.py） | `OutboxRepository(connect)`；`claim(*, limit=20, lease_seconds=30, now=None) -> list[DeliveryLease]`；`finish(lease, *, success: bool, error_code=None, now=None) -> bool`；生产 now 使用数据库 UTC，显式 now 仅作测试注入 |
| `ReceiptPublisher`（publisher.py） | `ReceiptPublisher(repository, mqtt_config, *, client_factory=None)`；`run_once() -> dict[str,int]`, `close() -> None` |
| `SpoolLimits`（reliable_spool.py） | frozen dataclass：`max_messages: int, max_payload_bytes: int, min_free_bytes: int` |
| `SpoolLease`（reliable_spool.py） | frozen dataclass：`message_id: str, token: str, topic: str, payload: bytes, qos: int, confirmation_mode: str, attempts: int` |
| `ReliableSpool`（reliable_spool.py） | `ReliableSpool(path, *, limits: SpoolLimits, clock=None)`；`enqueue(message_id, topic, payload, *, confirmation_mode, qos=1, now=None) -> bool`；`claim(*, now=None, limit=1, lease_seconds=60) -> list[SpoolLease]`；`get(message_id) -> dict | None`, `counts() -> dict`, `close()` |
| 本地状态方法（任务 8） | `broker_ack(lease, *, now=None) -> bool`；`fail(lease, error_code, *, now=None) -> bool`；`apply_receipt(topic, payload, *, retain=False, now=None) -> str`，结果仅 applied/duplicate/ignored |
| `ReliableUplink`（reliable_uplink.py） | `ReliableUplink(mqtt_config, spool, *, gateway_id, prefix, client_factory=None)`；`start()`, `wake()`, `snapshot() -> dict`, `close()`；不在采集循环等待中心 |
| `Actor`（management_contracts.py） | frozen dataclass：`domain, user_id, username, display_name, role_code`，全为 str，由服务端认证构造 |
| `ManagementCommand`（management_contracts.py） | `action: str, target: UUID, actor: Actor, key: UUID, expected_version: int, reason: str, request_hash: str` |
| `ReliabilityOperations`（operations.py） | `ReliabilityOperations(connect)`；`execute(command: ManagementCommand) -> dict`；`read_raw(fact_id: UUID, actor: Actor, *, preview: bool) -> dict` |
| `ReliabilityQueries`（queries.py） | `ReliabilityQueries(connect)`；`read(resource: str, filters: dict) -> dict`，resource 仅 cases/case/receipts/receipt/health/audit |

`Receipt.matches` 同时核对回执主题、三个标识、原文 SHA-256；不从坏文本恢复编号。JSON 数字型数据库计数/行版本/代次以十进制字符串进入 Web JSON；UUID 为字符串，UTC 显示为北京时间并带时区。分页 page/page_size 可用小整数，默认 20、上限 100，最大偏移 100000；列表时间默认近 24 小时、最大 31 天，单条 UUID 查询不受时间窗口影响。

### 测试环境与命令

从本工作区根运行：

```powershell
$taskPython = 'C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe'
$env:PYTHONUTF8 = '1'
$env:OILWELL_INGEST_TEST_CONFIG = 'C:/Users/hp/Documents/外专双百/data/ingest-reliability-test/ingest-test.local.toml'
```

新测试数据库固定 `oilwell_ingest_reliability_test`，只使用已获准的本机隔离实例 `127.0.0.1:3822`；不修改 `oilwell_registry_test`。初始化前核对 `C:/Users/hp/Documents/外专双百/data/registry-test/identity.json` 与服务报告的数据目录，必须属于其 `data` 子目录。管理员凭据通过既有系统凭据引用读取，不输出秘密，不修改现有账号。

Task 2 的显式初始化工具只创建固定测试库及 `ingest_test_` 前缀的独立迁移/接收/发布/运维读/运维写账号，随机密码存 Windows 凭据管理器，配置只存引用。若身份不能核实或管理员凭据不可用，停止该初始化并报告；不尝试 5432 或业务入库身份。需要联测台账时，另建测试用只读观察身份并在该新库安装台账 DDL，不复用原测试库的清理函数。

`IngestDatabaseCase(unittest.TestCase)` 在 `tests/ingest_support.py` 提供 `connect(role='ingestor')`、`scalar(sql, params=(), role='reader')`、`repository(receipt_gateways=frozenset({'GW'}))`、`outbox()`、`operations()`。连接角色仅 migrator/ingestor/publisher/reader/operator。每次 reset 在实际执行连接确认数据库名、持有新库专属 advisory lock 后，按固定顺序清理本模块依赖表及本库遥测；不使用 CASCADE，不删除数据库，不影响旧测试锁。测试套件串行，并发用例在一个独占 fixture 内开子连接。

Task 1 在 `tests/ingest_vectors.py` 提供 `observation(message_id='M-1', *, value=41.0, gateway='GW', device='D', quality='good') -> Observation`，固定 UTC 时间 `2026-09-24T00:00:00+00:00`，以现有 `TelemetrySample`、`UplinkBatch.create` 构造真实 v1 正文；`receipt_for(obs, *, status='stored', reason_code='accepted') -> Receipt` 只生成测试输入，不代表实测来源。测试类与用例代码按下列各任务放入指定文件，不能用静态字符串搜文件替代业务断言。

```python
from datetime import datetime, timezone
from hashlib import sha256
import json
from backend.gateway.contracts import TelemetrySample, UplinkBatch
from backend.ingest_reliability.contracts import Observation, Receipt

FIXED_TIME = datetime(2026, 9, 24, tzinfo=timezone.utc)

def observation(message_id='M-1', *, value=41.0, gateway='GW', device='D', quality='good'):
    sample = TelemetrySample(gateway_id=gateway, device_id=device, point_code='load',
                             measured_at=FIXED_TIME, sequence=1, value=value,
                             quality=quality, unit='kN', source='modbus_tcp')
    batch = UplinkBatch.create(gateway, device, 'BOOT-1', [sample],
                               message_id=message_id, created_at=FIXED_TIME)
    return Observation(batch.topic('oilfield'), batch.to_json().encode('utf-8'),
                       1, False, False, FIXED_TIME)

def receipt_for(obs, *, status='stored', reason_code='accepted'):
    body = json.loads(obs.payload)
    return Receipt(schema='OWP-INGEST-ACK/1.0',
                   receipt_id='00000000-0000-4000-8000-000000000001',
                   gateway_id=body['gateway_id'], device_id=body['device_id'],
                   message_id=body['message_id'], payload_sha256=sha256(obs.payload).hexdigest(),
                   status=status, reason_code=reason_code,
                   recorded_at=FIXED_TIME.isoformat(timespec='microseconds'))
```

下列无类名包围的 `test_` 方法片段均须放入 unittest.TestCase 或明确指定的 fixture 子类，不能作为模块级函数导致 unittest 发现零用例。运行结果必须列出实际执行数。

本计划中每组“增加测试 → 跑 RED → 最小实现 → 跑 GREEN → 提交”是独立小循环；单个测试/函数修改控制在 2—5 分钟粒度，较大场景逐个循环，不一次提交整包未验证改动。每个任务只暂存自己列出的文件，不 `git add .`。日志保存 `logs/ingest-wp03/task-N-red.log`、`task-N-green.log`；日志不是测试断言，最终仍检查退出码和失败/跳过数。

---

### Task 1: 严格回执契约与输入分类

**Files:** Create `backend/ingest_reliability/__init__.py`, `backend/ingest_reliability/errors.py`, `backend/ingest_reliability/contracts.py`, `backend/ingest_reliability/wire.py`; Create `tests/ingest_vectors.py`, `tests/test_ingest_contracts.py`, `tests/test_ingest_classification.py`。

**Interfaces:** 消费既有 `UplinkBatch.from_json`；产出 B 节 Observation/Correlation/Candidate/Receipt/CapacityLimits/CommittedResult 与 `classify`。`Receipt` 构造及解析都执行同一字段验证。

- [ ] **1. 写匹配与错误分类测试，先锁定字节而非 JSON 等价。**

```python
from dataclasses import replace
from hashlib import sha256
from tests.ingest_vectors import observation, receipt_for

def test_receipt_binds_exact_bytes(self):
    obs = observation()
    receipt = receipt_for(obs)
    self.assertTrue(receipt.matches(receipt.topic('oilfield'), obs))
    spaced = replace(obs, payload=obs.payload + b' ')
    self.assertNotEqual(sha256(obs.payload).digest(), sha256(spaced.payload).digest())
    self.assertFalse(receipt.matches(receipt.topic('oilfield'), spaced))
```

逐个补参数化负例：超 4 KiB、未知/缺字段、重复键、嵌套/数组、布尔代替字符串、无效 UUID/UTC/hash、错误主题与身份；输入分类覆盖 10 个代码、重复键不提取关联、无时区中心时间抛系统错误。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_contracts tests.test_ingest_classification -v`；新功能不存在应失败；导入缺失只证明尚未实现，随后要看到实质负例断言。
- [ ] **3. 实现严格匹配核心，并分离分类与系统故障。**

```python
def matches(self, topic, observation):
    candidate = classify(observation, prefix=topic.split('/')[0])
    identity = candidate.correlation
    return (identity is not None and topic == self.topic(topic.split('/')[0])
            and (self.gateway_id, self.device_id, self.message_id)
                == (identity.gateway_id, identity.device_id, identity.message_id)
            and self.payload_sha256 == hashlib.sha256(observation.payload).hexdigest())
```

接收分类先检查 2 MiB 硬限和服务器时间，再按设计顺序分类；JSON 严格解析保留数字词法限制、拒绝重复键。完整契约失败不等于安全关联失败，只有全 JSON 安全解析且 schema/三个标识正确时提取 Correlation。保留能解析的 batch，供仓储优先识别旧已接受事实；不改旧 v1 校验结果。

- [ ] **4. 跑 GREEN 和旧入口回归。** `& $taskPython -m unittest tests.test_ingest_contracts tests.test_ingest_classification tests.test_wire_validation tests.test_gateway_contracts -v`。
- [ ] **5. 只提交本任务文件。** `git commit -m "feat: define strict ingest receipts and input classification"`；交付独立契约单元测试，不连接业务服务。

### Task 2: 完整数据库迁移、真实角色与新测试库

**Files:** Create `backend/ingest_reliability/sql/001_reliability.sql`, `backend/ingest_reliability/sql/grants.sql`, `backend/ingest_reliability/migrate.py`, `backend/ingest_reliability/privileges.py`; Create `tools/provision_ingest_test.py`, `tests/ingest_support.py`, `tests/test_ingest_schema.py`, `tests/test_ingest_privileges.py`。

**Interfaces:** `migrate(settings: dict, *, expected_database: str, limits: CapacityLimits) -> dict`；`grant_roles(connection, *, roles: dict[str,str]) -> None`；`probe_role(connect, role: str) -> None`。schema 版本/文件摘要核验，不自动迁移；fixtures 接口见 B 节。

- [ ] **1. 写真实事务/角色测试及隔离身份反例。**

```python
from tests.ingest_support import IngestDatabaseCase

class SchemaTests(IngestDatabaseCase):
    def test_reader_cannot_obtain_original_bytes(self):
        import psycopg
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.connect('reader') as conn:
                conn.execute('SELECT raw_payload FROM ingest_reliability.facts')
```

另外固定用例：迁移重跑不改版本、不预置设备；reader 不能写；publisher 不能改正文/原文；operator 不能改遥测；ingestor 不能删除/改原文/建表；运行身份不能是 owner/superuser 或通过角色继承获得上述权限；SQL 标识由 `psycopg.sql.Identifier` 处理。

- [ ] **2. 显式准备隔离身份并跑 RED。** 新工具校验 identity、端口/目录和精确数据库名后才 CREATE DATABASE/ROLE；已有异名对象拒绝接管。命令 `& $taskPython tools/provision_ingest_test.py --identity 'C:/Users/hp/Documents/外专双百/data/registry-test/identity.json' --output 'C:/Users/hp/Documents/外专双百/data/ingest-reliability-test'`；随后 `& $taskPython -m unittest tests.test_ingest_schema tests.test_ingest_privileges -v`。此步骤仅在计划批准执行时进行。
- [ ] **3. 建立事实、案件、回执、投递、审计、操作幂等、健康、配额和版本表。** 核心约束至少包含：

```sql
UNIQUE (topic, payload_sha256),
CHECK (octet_length(raw_payload) = payload_bytes),
CHECK (payload_bytes BETWEEN 0 AND 2097152),
CHECK ((status = 'stored' AND accepted_message_id IS NOT NULL AND reason_code = 'accepted')
    OR (status = 'quarantined' AND accepted_message_id IS NULL AND reason_code <> 'accepted'))
```

固定表名：`schema_migrations/facts/cases/receipts/deliveries/audit_events/operation_receipts/worker_health/capacity`。facts 的 accepted_message_id 外键到 public.ingest_messages；cases、receipts 外键 facts；deliveries 外键 receipts。facts/receipts/audit_events/operation_receipts 禁止 UPDATE/DELETE；cases 只改状态/版本/统计，deliveries 只改租约/发送进度。receipts 必须与 facts 身份/摘要/结果一致，由不可变约束触发器校验。capacity 单行存总/隔离字节上限及已用量，计数更新与写入同事务。视图 `case_metadata/receipt_metadata/health_metadata` 不暴露 BYTEA；所有 PK/FK/计数约束与保护触发器由预检核验。若采用 SECURITY DEFINER 函数，须固定 search_path、禁止 PUBLIC 执行并审计精确可调用权限，不得提供任意 SQL 通道。每条 DDL 单独补 rollback/越权负例。

- [ ] **4. 跑 GREEN，核对固定库名和测试证据不带密码。** `& $taskPython -m unittest tests.test_ingest_schema tests.test_ingest_privileges -v`；测试不能因缺库被悄悄 skip 后宣称完成。
- [ ] **5. 提交迁移、工具及测试，私密 TOML/凭据不提交。** `git commit -m "feat: add isolated ingest reliability schema and least-privilege roles"`。

### Task 3: 接受/隔离/回执同事务与稳定去重

**Files:** Create `backend/ingest_reliability/repository.py`, `backend/ingest_reliability/facts.py`, `tests/test_ingest_repository.py`, `tests/test_ingest_concurrency.py`; Modify `backend/center/postgres.py`。

**Interfaces:** B 节 `ReliableRepository.ingest`、`schedule_receipt`；在旧仓储提取 `insert_batch(cursor, batch, *, topic, payload_sha256, raw_payload, received_at) -> bool`，不自行 commit；旧 `store` 调用它并保持原返回/错误语义。

- [ ] **1. 写幂等、摘要变化及跨网关编号冲突测试。**

```python
def test_duplicate_reuses_result_but_different_bytes_are_quarantined(self):
    from dataclasses import replace
    from tests.ingest_vectors import observation
    repo, obs = self.repository(), observation()
    first = repo.ingest(obs)
    second = repo.ingest(obs)
    changed = repo.ingest(replace(obs, payload=obs.payload + b' '))
    self.assertEqual((first.status, second.fact_id), ('stored', first.fact_id))
    self.assertEqual(changed.status, 'quarantined')
    self.assertEqual(self.scalar('SELECT count(*) FROM public.telemetry_samples', role='migrator'), 1)
```

逐个增加：损坏 JSON 不发猜测回执；可关联契约错误发 quarantined；全部写阶段失败回滚；提交后连接断开再重试；旧 JSONB 仅在匹配重投后补原文；已隔离事实跨验证器不重判；合法未知/quality bad 保留；两个真实连接同编号同/不同正文；配额并发、空 payload、计数饱和。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_repository tests.test_ingest_concurrency -v`。
- [ ] **3. 实现单事务顺序与提交后返回。**

```python
with self.connect() as conn:
    with conn.cursor() as cur:
        cur.execute('SET LOCAL lock_timeout = \'1s\'')
        cur.execute('SET LOCAL statement_timeout = \'3s\'')
        cur.execute('SET LOCAL search_path = pg_catalog, public, pg_temp')
        cur.execute('SELECT * FROM ingest_reliability.capacity WHERE singleton=1 FOR UPDATE')
        capacity = cur.fetchone()
        if capacity is None:
            raise ReliabilityError('ingest.capacity_missing', '接收容量配置缺失')
        result = self._store_locked(cur, observation, candidate, capacity)
return result
```

`_store_locked(cur, observation, candidate, capacity) -> CommittedResult` 顺序固定：查 topic/hash 事实并比较完整字节 → 查同编号旧已接受消息 → 按 Candidate 接受或隔离 → 原子检查/累计配额 → 创建案件/回执 → schedule。有事实复用原结果；不同字节摘要冲突不得覆盖。旧已接受消息匹配优先于本次传输标志错误。只有业务 `IdempotencyConflict` 转隔离；数据库约束/超时不是坏报文。隔离重现关闭案件时同事务 reopen+系统审计。重复回执请求不重复正文；事务外不先更新缓存。

- [ ] **4. 跑 GREEN 和旧仓储回归。** `& $taskPython -m unittest tests.test_ingest_repository tests.test_ingest_concurrency tests.test_center_ingest tests.test_wire_postgres tests.test_center_postgres_schema -v`。
- [ ] **5. 提交。** `git commit -m "feat: atomically persist telemetry quarantine and receipt facts"`。

### Task 4: 接收器门禁、持久隔离后确认与开关

**Files:** Create `backend/ingest_reliability/config.py`, `backend/ingest_reliability/consumer.py`, `tests/test_ingest_consumer.py`, `tests/test_ingest_config.py`; Modify `backend/center/__main__.py`, `config/center.example.toml`。

**Interfaces:** `ReliabilityConfig` 包含 enabled、receipt_gateways、limits、postgres_ingest/publisher/reader/operator、mqtt_publisher、allowed_origins、重试/健康阈值；秘密字段 repr=False，各组件不使用的连接字段允许 None。`load_reliability_config(path, *, component: str, env=None, credential_reader=None) -> ReliabilityConfig | None`；component 仅 ingestor/publisher/management，分别只要求自身连接和参数。没有 reliability 段或 enabled=false 返回 None，不能以异常返回 None。`ReliableConsumer(repository, *, clock=None).handle_message(client, message) -> CommittedResult`，兼容既有 `create_paho_client` 的 consumer 接口。

- [ ] **1. 写 ACK 顺序和系统故障反例。**

```python
def test_repository_failure_does_not_ack(self):
    from unittest.mock import Mock
    from backend.ingest_reliability.consumer import ReliableConsumer
    from tests.ingest_vectors import observation
    obs, client, repo = observation(), Mock(), Mock()
    repo.ingest.side_effect = OSError('injected storage failure')
    message = Mock(topic=obs.topic, payload=obs.payload, qos=1, retain=False, dup=False, mid=7)
    with self.assertRaises(OSError):
        ReliableConsumer(repo).handle_message(client, message)
    client.ack.assert_not_called()
```

还要验证：quarantined 提交后正常 ack 并继续处理下一合法消息；服务时钟错误不调用隔离仓储；QoS 0 不尝试 ack；retained 标志可观察；新模式缺结构/权限退出，旧配置仍用旧消费者；查询配置坏不拖垮接收进程。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_consumer tests.test_ingest_config -v`。
- [ ] **3. 接线而不重写旧消费者。**

```python
result = self.repository.ingest(observation)
if message.qos == 1:
    ack_code = client.ack(message.mid, message.qos)
    if ack_code not in (0, None):
        raise ReliabilityError('ingest.ack_failed', '接收确认尚未完成')
return result
```

新接收模式订阅使用 Paho `SubscribeOptions(qos=1, retainAsPublished=True)`，必须核对 SUBACK；仅运行所需角色预检，不让中心依赖 Web Origin/运维连接可用性。旧 `[postgres]` 身份保持不变，新配置 `[postgres_reliable_ingest]` 等独立段启用；所有 PG 段指向同一 host/port/database 且运行用户不同，远程连接沿用 verify-full 与证书要求。可配限额必须与已迁移 capacity 上限一致，不能启动时改表。

- [ ] **4. 跑 GREEN。** `& $taskPython -m unittest tests.test_ingest_consumer tests.test_ingest_config tests.test_center_recovery tests.test_center_config tests.test_center_mqtt_consumer -v`。
- [ ] **5. 提交。** `git commit -m "feat: enable durable quarantine before MQTT acknowledgement"`。

### Task 5: 可恢复待发队列与租约竞争

**Files:** Create `backend/ingest_reliability/outbox.py`, `tests/test_ingest_outbox.py`; Modify `backend/ingest_reliability/facts.py`。

**Interfaces:** B 节 DeliveryLease、OutboxRepository、schedule_receipt。deliveries 固定字段 `receipt_id/requested_generation/sent_generation/lease_token/lease_generation/lease_until/next_attempt_at/attempts/last_error_code/broker_acked_at/version`；时间全 UTC，版本正 BIGINT。

- [ ] **1. 写“发送途中再次请求”与旧租约测试。**

```python
def test_old_lease_cannot_swallow_new_request(self):
    from tests.ingest_vectors import observation
    result = self.repository().ingest(observation())
    outbox = self.outbox()
    lease = outbox.claim(limit=1)[0]
    self.repository().ingest(observation())
    self.assertTrue(outbox.finish(lease, success=True))
    remaining = outbox.claim(limit=1)
    self.assertEqual(remaining[0].receipt_id, result.receipt_id)
    self.assertGreater(remaining[0].generation, lease.generation)
```

补两个 publisher 会话争抢、租约过期后旧 finish 返回 False、失败退避上限、不因次数终止、clock 回退和代次上限。回执无人订阅仍待发；请求代次溢出拒绝操作并报警，不回绕为零。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_outbox -v`。
- [ ] **3. 短事务领取，网络在事务外。**

```sql
SELECT receipt_id FROM ingest_reliability.deliveries
WHERE requested_generation > sent_generation
  AND next_attempt_at <= %s AND (lease_until IS NULL OR lease_until <= %s)
ORDER BY next_attempt_at, receipt_id
FOR UPDATE SKIP LOCKED LIMIT %s;
```

领取时生成 token，lease_generation=requested_generation。finish 的 WHERE 同时匹配 receipt_id/token/lease_generation；成功只推进 sent_generation 到这次领取代次，不能覆盖期间增加的 requested_generation；失败清 lease 并退避。已 pending 且未领取时可合并重投请求，在途则递增代次。租约/退避最大值有界，时钟回退时记录异常并将等待限制在配置上限内，不宣称成功。

- [ ] **4. 跑 GREEN 并复跑接收事务。** `& $taskPython -m unittest tests.test_ingest_outbox tests.test_ingest_concurrency -v`。
- [ ] **5. 提交。** `git commit -m "feat: fence receipt outbox leases and recover retries"`。

### Task 6: 独立 MQTT 回执发送器

**Files:** Create `backend/ingest_reliability/publisher.py`, `backend/ingest_reliability/__main__.py`, `tests/test_ingest_publisher.py`; Modify `config/center.example.toml`。

**Interfaces:** B 节 ReceiptPublisher；`python -m backend.ingest_reliability --config <path> --check|--once` 或常驻模式。`run_once` 返回 claimed/published/failed 整数，published 只表示交给 Broker。

- [ ] **1. 写发布崩溃窗口和回调竞态测试。**

```python
def test_publish_without_successful_puback_stays_pending(self):
    from unittest.mock import Mock
    from backend.ingest_reliability.publisher import ReceiptPublisher
    lease = self.outbox().claim(limit=1)[0]
    repo = Mock()
    repo.claim.return_value = [lease]
    worker = ReceiptPublisher(repo, self.mqtt_config, client_factory=self.no_subscriber_client)
    self.addCleanup(worker.close)
    self.assertEqual(worker.run_once()['published'], 0)
    self.assertFalse(repo.finish.call_args.kwargs['success'])
```

本测试类 setUp 通过隔离库创建一条待发回执；`self.mqtt_config` 为回环测试 MqttConfig，`self.no_subscriber_client(config)` 是测试内 Paho 接口替身，publish 触发 reason_code=0x10。分别增加 callback 在 publish 返回前触发、跨 epoch 同 mid、PUBACK 成功后 finish 失败、TLS/ACL/初连失败、退出不丢 lease 的测试。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_publisher -v`。
- [ ] **3. 建立独立客户端和发送循环。**

```python
for lease in self.repository.claim():
    try:
        self._publish_and_wait(lease)
    except ReliabilityError as error:
        self.repository.finish(lease, success=False, error_code=error.code)
    else:
        self.repository.finish(lease, success=True)
```

`_publish_and_wait(lease) -> None` 使用单调时钟、有界超时、QoS 1、retain=False；通过锁保护的 `(connection_epoch, mid)` 结果表处理早到回调，不复用过期结果，及时清理有界状态。MQTT 身份仅发布回执，不能读取遥测；网络等待时不持有 PG 事务。每个循环记录有期限 heartbeat，失败只记有限错误码。

- [ ] **4. 跑 GREEN 与 outbox 回归。** `& $taskPython -m unittest tests.test_ingest_publisher tests.test_ingest_outbox -v`；本任务替身验证不冒充 Task 14 的真实 Broker 验证。
- [ ] **5. 提交。** `git commit -m "feat: publish durable application receipts with recovery"`。

### Task 7: SQLite 显式升级、旧程序写入隔离与精确入队

**Files:** Create `backend/gateway/spool_migrate.py`, `backend/gateway/reliable_spool.py`, `backend/gateway/sql/002_reliable_spool.sql`, `tests/test_gateway_receipt_migration.py`; Modify `backend/gateway/spool.py`。

**Interfaces:** B 节 SpoolLimits/ReliableSpool 的构造、enqueue/get/counts/close；`migrate_spool(path, *, backup_path, limits: SpoolLimits) -> dict`、`read_spool_version(path) -> int` 在 spool_migrate.py 定义。CLI `python -m backend.gateway.spool_migrate --database <path> --backup <new-path>`，空队列初始化用 `--initialize`，不在运行构造器中迁移。

- [ ] **1. 写旧状态/原文保全和不能重复覆盖测试。**

```python
def test_migration_keeps_broker_confirmation_meaning(self):
    from backend.gateway.spool import DurableSpool
    from backend.gateway.spool_migrate import migrate_spool
    from backend.gateway.reliable_spool import ReliableSpool, SpoolLimits
    from tests.ingest_vectors import observation
    obs = observation()
    limits = SpoolLimits(100, 1024 * 1024, 1024 * 1024)
    old = DurableSpool(self.path)
    old.enqueue('M-1', obs.topic, obs.payload)
    old.claim(limit=1)
    old.ack('M-1')
    old.close()
    migrate_spool(self.path, backup_path=self.backup, limits=limits)
    new = ReliableSpool(self.path, limits=limits)
    self.addCleanup(new.close)
    row = new.get('M-1')
    self.assertEqual((row['confirmation_mode'], row['state']), ('broker', 'legacy_broker_confirmed'))
    self.assertEqual(row['payload'], obs.payload)
```

本测试类 setUp 使用 TemporaryDirectory 建 path/backup；增加 pending/inflight/dead、未 checkpoint WAL、失败回滚、已有备份拒绝覆盖、两个进程打开冲突、同编号异原文/主题、字节/数量/磁盘预算竞争测试。无权覆写历史的旧程序也必须实际尝试打开升级队列并失败。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_gateway_receipt_migration -v`。
- [ ] **3. 实现事务迁移和防旧写入结构。**

```python
with sqlite3.connect(path) as source, sqlite3.connect(backup_path) as backup:
    source.backup(backup)
    check = backup.execute('PRAGMA integrity_check').fetchone()[0]
    if check != 'ok':
        raise ReliabilityError('ingest.spool_backup_failed', '队列备份校验失败')
```

先获取进程级独占锁并确认旧进程停止，检查目标绝对路径、备份不存在及空间足够；不能用仅复制主文件代替 SQLite 备份。迁移 BEGIN IMMEDIATE 下将记录完整复制到 `uplink_queue_v2`，校验计数、每条原文摘要/状态，删除旧表仅发生于已验证事务内，历史由新表及一致备份保留。旧名 `uplink_spool` 改为只读兼容视图，删除旧索引名并以不可写视图挡住旧初始化/写入；实际用旧 DurableSpool 构造验证不能悄悄另建空队列。user_version=2、新 receipts/security_events/capacity 表一并提交，失败整笔回滚。新 DurableSpool 增加版本拒绝检查；可靠队列运行时不建/改表。

SQLite 使用 WAL/FULL；enqueue 的编号存在时逐字节比较并校验确认模式，完全相同返回 False，不同抛冲突。预算与插入放在同一 SQLite 写事务。迁移保存旧 state/attempts/next_attempt_at，旧 inflight 仅恢复为待传输，不转换为应用确认。涉及 SQLite 结构变化遵循[官方重建表步骤](https://www.sqlite.org/lang_altertable.html)；备份 API 依据 [Python SQLite backup](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)。

- [ ] **4. 跑 GREEN 和旧队列回归。** `& $taskPython -m unittest tests.test_gateway_receipt_migration tests.test_gateway_spool -v`。
- [ ] **5. 提交。** `git commit -m "feat: migrate edge spool without upgrading legacy confirmation claims"`。

### Task 8: 应用确认状态机、非法回执留痕与时钟边界

**Files:** Create `backend/gateway/receipt_state.py`, `tests/test_gateway_receipt_state.py`; Modify `backend/gateway/reliable_spool.py`。

**Interfaces:** B 节 claim/broker_ack/fail/apply_receipt。`receipt_state.transition(current: str, mode: str, event: str) -> str` 只负责合法转移；持久比较与 token 校验由 ReliableSpool 事务完成。`get` 返回 message_id/topic/payload/confirmation_mode/state/attempts/receipt_id/last_error_code；payload 为 bytes。

- [ ] **1. 写早到回执不能被旧失败逆转的测试。**

```python
def test_early_receipt_wins_over_late_transport_failure(self):
    from tests.ingest_vectors import observation, receipt_for
    obs = observation()
    self.spool.enqueue('M-1', obs.topic, obs.payload, confirmation_mode='application', now=100)
    lease = self.spool.claim(now=100)[0]
    receipt = receipt_for(obs)
    self.assertEqual(self.spool.apply_receipt(receipt.topic('oilfield'), receipt.to_bytes(), now=101), 'applied')
    self.assertFalse(self.spool.fail(lease, 'transport.timeout', now=102))
    self.assertEqual(self.spool.get('M-1')['state'], 'stored')
```

测试 setUp 显式初始化临时 v2 队列；逐个补：quarantined 不算 stored、broker ack 只待应用确认、超过 12 次仍能 claim、进程重开后迟到回执、旧 token、相同/矛盾回执、错误网关/摘要/编号、4 KiB 边界、非法回执安全事件合并/饱和、SQLite commit 失败、时钟从 1000 回退到 100 的有界等待。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_gateway_receipt_state -v`。
- [ ] **3. 按持久模式推进状态，不使用 MQTT mid 当应用身份。**

```sql
UPDATE uplink_queue_v2
SET state='awaiting_receipt', lease_token=NULL, claimed_at=NULL, next_attempt_at=?
WHERE message_id=? AND lease_token=? AND confirmation_mode='application' AND state='inflight';
```

有效回执先校验 Receipt.matches，再在同事务插入本地 receipt 和更新 pending/inflight/awaiting_receipt 到最终状态；相同结果幂等。legacy broker 记录只能通过其 broker 状态机结束，应用回执不“升级”旧语义。失败/超时更新须匹配 token，final 行永不回退。安全事件按 topic/hash/reason 合并，最多 1024 个键；满额后只累计持久 overflow_count 并报警，不增加无限原文；事件或计数不能保存时抛错，网络层不确认。回退时钟使期限重新锚定到当前时间加有限退避并留痕，同时更新持久 last_clock_seen；不能每次轮询都向后滑动到期时间。增加第二次轮询仍能按重锚期限领取的断言；中心 outbox 使用相同一次性重锚规则。

- [ ] **4. 跑 GREEN。** `& $taskPython -m unittest tests.test_gateway_receipt_state tests.test_gateway_receipt_migration -v`。
- [ ] **5. 提交。** `git commit -m "feat: persist gateway application receipt state and reject stale callbacks"`。

### Task 9: 网关异步上行、确认订阅与可控退出

**Files:** Create `backend/gateway/reliable_uplink.py`, `tests/test_gateway_receipt_runtime.py`; Modify `backend/gateway/config.py`, `backend/gateway/runtime.py`, `backend/gateway/__main__.py`, `config/gateway.example.toml`。

**Interfaces:** B 节 ReliableUplink；GatewayConfig 增加 `confirmation_mode: str = 'broker'`、spool_limits、receipt_timeout_seconds 默认 60、重试 2—300 秒；新增字段排在现有必填字段后，保持旧测试构造兼容。GatewayRuntime 在 v1+broker 时用原逻辑；v2 队列使用可靠 worker，新增记录使用当前配置模式，旧 application 行不降级。

- [ ] **1. 写“不等回执仍继续采集”及 SUBACK 失败测试。**

```python
def test_network_delay_does_not_serialize_poll_cycles(self):
    runtime = self.make_runtime(block_receipts=True)
    self.addCleanup(runtime.close)
    one, two = runtime.run_cycle(), runtime.run_cycle()
    self.assertEqual((one.messages_enqueued, two.messages_enqueued), (1, 1))
    self.assertEqual(runtime.spool.counts()['stored'], 0)
    self.assertEqual(self.poller_calls, 2)
```

`make_runtime` 是该测试类方法：临时 v2 SQLite、固定采样的 poller_factory、带阻塞 Event 的 client_factory；网络阻塞只在 worker，不在 run_cycle。另以真实单调耗时上限做小幅容差断言，避免只统计调用次数。补拒绝/延迟 SUBACK 不 publish、成功后按自身网关订阅、同 mid 跨 epoch、非法回执留痕后 ack、存储失败不 ack、关闭/重启 pending、`--once` 不谎报全部送达。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_gateway_receipt_runtime -v`。
- [ ] **3. 运行时只负责入队及唤醒。**

```python
if self.reliable_mode:
    self.uplink.wake()
    snapshot = self.uplink.snapshot()
    messages_published = snapshot['broker_published_since_last_snapshot']
else:
    messages_published = self.uplink.publish_pending()['published']
```

worker 生命周期 start/connect/subscribe/ready/drain/reconnect/close；只订阅 `{prefix}/v2/{gateway}/+/ingest-ack`，manual_ack=True、Retain As Published，SUBACK=1 后发送。采用有界 pending mid 表，连接 epoch 防旧回调污染，PUBACK 仅调用 broker_ack。入站回执 apply_receipt 提交后才 ack；异常存储失败断开恢复。close 先停/等待 worker 在发布超时内退出，再关 SQLite；超时退出报错，不关闭仍被线程使用的连接。`--once` 最多做一次有界 drain 并输出未完成计数，不能把退出码 0 解释成中心全部保存。

- [ ] **4. 跑 GREEN 和旧采集回归。** `& $taskPython -m unittest tests.test_gateway_receipt_runtime tests.test_gateway_runtime tests.test_gateway_config tests.test_gateway_mqtt tests.test_gateway_poller -v`。
- [ ] **5. 提交。** `git commit -m "feat: decouple polling from application receipt delivery"`。

### Task 10: 元数据查询、人工处置与不可覆盖审计

**Files:** Create `backend/ingest_reliability/queries.py`, `backend/ingest_reliability/operations.py`, `backend/ingest_reliability/management_contracts.py`, `tests/test_ingest_operations.py`, `tests/test_ingest_queries.py`。

**Interfaces:** B 节 Actor/ManagementCommand/Operations/Queries。`validate_management(action: str, target: UUID, actor: Actor, body: dict, *, route: str) -> ManagementCommand`；action 仅 case.ack/case.close/case.reopen/case.note/receipt.resend。成功返回 `{'entity': {'id': str, 'version': str, 'state': str}, 'replayed': bool}`。

- [ ] **1. 写审计失败整体回滚与同键重放测试。**

```python
def test_same_operation_key_does_not_double_audit(self):
    command = self.case_command('case.close', reason='已核查设备数据格式')
    first = self.operations().execute(command)
    second = self.operations().execute(command)
    self.assertEqual(first['entity'], second['entity'])
    self.assertTrue(second['replayed'])
    self.assertEqual(self.scalar('SELECT count(*) FROM ingest_reliability.operation_receipts', role='migrator'), 1)
```

`case_command(action, reason) -> ManagementCommand` 是本测试类 helper：通过真实 repository 先隔离可关联坏报文，读取当前 cases.version，构造固定真实测试 Actor 与 uuid4 key，不直接改案件状态。补审计触发器拒绝回滚、同键变正文409、并发关单版本冲突、已关单重投重开、resend 审计、reader 看不到原文、raw 访问审计失败不返回、预览精确截断与全量下载摘要。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_operations tests.test_ingest_queries -v`。
- [ ] **3. 验证并固定事务锁序。**

```python
canonical = json.dumps({'action': action, 'route': route, 'body': body},
                       sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
request_hash = hashlib.sha256(canonical).hexdigest()
```

请求含 reason 2—500 字、idempotency_key UUID、expected_version 十进制字符串；禁未知字段。操作事务：按 actor/key 加事务锁 → 查既有操作回执 → 锁 case 或 delivery → 比版本 → 变更 → 写审计/幂等结果 → 提交后返回。case.ack 仅 open→acknowledged，close 从 open/acknowledged→closed，reopen 仅 closed→open，note 保持状态但升版本；非法转移409。receipt.resend 只调用 schedule_receipt，不修改持久结果。case 重现的系统审计不会伪装成人工操作者。

read_raw 在 operator 事务记录 raw.preview/raw.download 授权审计后返回 `{fact_id, payload_sha256, payload_bytes, text, raw_bytes, truncated}`；preview 的 raw_bytes=None 且 text 最多 4096 原文字节解码，full 的 text=None/raw_bytes 为精确 bytes。元数据 SELECT 走独立 reader，固定参数化过滤/分页/时间/排序，不拼接任意表或排序 SQL。health 将缺心跳/连接失败/配额不足显示非健康，不输出秘密。

- [ ] **4. 跑 GREEN。** `& $taskPython -m unittest tests.test_ingest_operations tests.test_ingest_queries tests.test_ingest_privileges -v`。
- [ ] **5. 提交。** `git commit -m "feat: add audited ingest incident management and restricted raw access"`。

### Task 11: HTTP、岗位权限、能力门禁与跨层真实请求

**Files:** Create `backend/ingest_reliability/api.py`, `backend/ingest_reliability/permissions.py`, `backend/ingest_reliability/runtime.py`, `tests/test_ingest_api.py`, `tests/test_ingest_startup.py`; Modify `backend/http_server.py`, `backend/seed.py`。

**Interfaces:** `IngestReliabilityApi(handler).get/post/unsupported(path) -> bool`；`ReliabilityRuntime(status, queries=None, operations=None, code=None)`；`build_reliability_runtime(config_path) -> ReliabilityRuntime`。create_server 增加独立可注入 `ingest_runtime=None`；`/api/auth/me` 追加 `capabilities.ingest_reliability`，不覆盖 registry。

- [ ] **1. 写真实回环 HTTP 鉴权、撤权重试和原文泄漏测试。**

```python
def test_revoked_permission_cannot_replay_committed_operation(self):
    status, result = self.http('POST', self.close_route, self.close_body)
    self.assertEqual(status, 200)
    self.revoke_permission('ingest.manage')
    status, result = self.http('POST', self.close_route, self.close_body)
    self.assertEqual(status, 403)
```

本测试类 `http(method, path, body=None) -> tuple[int,dict]` 用 http.client 连接临时 create_server 与临时 SQLite 认证库；`revoke_permission(code)` 只改该测试账号授权，后续请求走真实 AuthService；close_route/body 在 setUp 通过本库真实案件生成。继续覆盖匿名401、普通用户无 raw 权限403、超长/重复参数/Content-Length/Origin/Host 欺骗、默认 80/443 Origin、审计失败不返回原文、响应下载头、多个权限岗位、后台假空数据禁止。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_api tests.test_ingest_startup -v`。
- [ ] **3. 按明确路由表接线，不走任意服务反射。**

```python
READ_ROUTES = {'/cases': 'cases', '/receipts': 'receipts', '/health': 'health'}
ACTION_PERMISSIONS = {'case.ack': 'ingest.manage', 'case.close': 'ingest.manage',
                     'case.reopen': 'ingest.manage', 'case.note': 'ingest.manage',
                     'receipt.resend': 'ingest.manage'}
```

完整路由前缀 `/api/ingest-reliability`；GET cases、cases/{uuid}、receipts、receipts/{uuid}、health、audit、facts/{uuid}/preview、facts/{uuid}/raw；POST cases/{uuid}/ack|close|reopen|note、receipts/{uuid}/resend。所有入口要求 ingest.read，raw 再要求 ingest.raw.read，audit 再要求 audit.read，POST 再要求 ingest.manage；鉴权先于重放查找。HTTP 请求体最多64 KiB，错误 401/403/404/409/422/503，超时/5xx 不输出连接信息。下载 application/octet-stream、attachment 固定 UUID 名和 nosniff。

seed 按现有新增权限一次性引入机制加三项权限和设计岗位矩阵；不在每次启动把非管理员已撤销权限授回。管理员沿用既有全部权限保障。新 runtime disabled 隐藏导航，坏配置 unavailable；分别加载 Web 所需连接，不把 Web/发送器的缺配置当接收实例必需项。

- [ ] **4. 跑 GREEN 与旧认证/台账启动回归。** `& $taskPython -m unittest tests.test_ingest_api tests.test_ingest_startup tests.test_registry_api tests.test_registry_startup -v`。
- [ ] **5. 提交。** `git commit -m "feat: expose authorized ingest operations through platform API"`。

### Task 12: 接收异常工作区与可重试真实操作

**Files:** Create `frontend/pages/ingest-reliability.js`, `frontend/ingest/client.js`, `frontend/ingest/detail.js`, `frontend/ingest/actions.js`, `tests/test_ingest_browser.py`, `tests/fixtures/ingest_workspace_check.html`, `tests/fixtures/ingest_actions_check.html`; Modify `app.js`, `styles.css`。

**Interfaces:** `renderIngestReliability(context) -> Promise<boolean>`；`createIngestClient(api, {isCurrent})` 返回 `read(path)`, `prepareChange(path, fields)`, `dispose()`；`prepareChange` 返回 `{key, run}`，run 不换键；`mountIngestDetail({root, api, permissions, fact, isCurrent})`、`mountIngestActions({root, client, permissions, onChanged, isCurrent})` 均返回 dispose。

- [ ] **1. 写真实 Edge DOM 用例，锁住不确定请求重试。**

```javascript
const bodies = [];
const fakeApi = {post: async (_path, body) => {
  bodies.push(structuredClone(body));
  if (bodies.length === 1) throw Object.assign(new Error('uncertain'), {status: 503});
  return {entity: {id: 'case-a', version: '2', state: 'closed'}, replayed: true};
}};
const client = createIngestClient(fakeApi, {isCurrent: () => true});
const op = client.prepareChange('/api/ingest-reliability/cases/case-a/close',
  {expected_version: '1', reason: '核对后关闭'});
await op.run().catch(() => {});
await op.run();
if (JSON.stringify(bodies[0]) !== JSON.stringify(bodies[1])) throw new Error('retry changed request');
```

fixtures 导入真实模块，覆盖 1440/1024 宽度、允许/拒绝岗位、禁用/故障/真空数据、恶意 HTML 原文、4 KiB 截断、raw 下载按钮、处置表单、错误提示、同屏新旧状态含义；控制 Promise 先后验证选择 B 后 A 的迟到请求无效，以及写成功触发的旧刷新不能覆盖新选择。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_browser -v`，仿照现有 Edge headless fixture 驱动，使用新临时 profile，不操纵用户的现有标签。
- [ ] **3. 实现小页面模块及不可变操作快照。**

```javascript
function prepareChange(path, fields) {
  const key = crypto.randomUUID();
  const body = Object.freeze({...structuredClone(fields), idempotency_key: key});
  return {key, run: () => api.post(path, body)};
}
```

查询/选择各有 generation，dispose 后停止更新。原文使用 textContent/pre，不拼 innerHTML；事实、人工处理、回执发送分别显示，中心不出现“网关已确认”。列表计数/版本不经 Number 丢失精度。403 不继续自动重试，409 刷新并保留原因；超时/5xx 留原操作供重试，不默认宣称未提交。补充类 `.ingest-reliability` 局部样式，不改全局图表或演示页布局。

- [ ] **4. 跑 GREEN 与已有页面回归。** `& $taskPython -m unittest tests.test_ingest_browser tests.test_registry_browser tests.test_center_browser -v`。静态 DOM 之外，Task 14 还要做真实 HTTP+PG 页面闭环。
- [ ] **5. 提交。** `git commit -m "feat: add ingest incidents and receipt operations workspace"`。

### Task 13: 厂家回执规范、自检资料与 Broker 边界

**Files:** Create `assets/open-interface/ingest-ack.schema.json`, `assets/open-interface/validate_ingest_ack.py`, `assets/open-interface/ingest-ack-example.json`, `tools/build_open_interface_kit.py`, `tests/test_ingest_interface.py`; Modify `assets/open-interface/INTERFACE.md`, `assets/open-interface/ROADMAP.md`, `assets/open-interface/index.html`, `assets/open-interface/open-interface-r1.zip`, `config/mosquitto.acl.example`, `config/mosquitto.example.conf`。

**Interfaces:** 自检脚本 `validate(topic: str, payload: bytes, *, telemetry_topic: str, telemetry_payload: bytes) -> dict`，成功返回 schema/status/message_id/payload_sha256；失败抛 ValueError。独立标准库脚本不依赖服务器源码。打包工具只打固定公开白名单，不扫描全目录。

- [ ] **1. 写中心/网关/厂家验证器一致性测试。**

```python
def test_public_validator_checks_the_same_exact_payload(self):
    import runpy
    from tests.ingest_vectors import observation, receipt_for
    validate = runpy.run_path('assets/open-interface/validate_ingest_ack.py')['validate']
    obs, receipt = observation(), receipt_for(observation())
    result = validate(receipt.topic('oilfield'), receipt.to_bytes(),
                      telemetry_topic=obs.topic, telemetry_payload=obs.payload)
    self.assertEqual(result['status'], 'stored')
    with self.assertRaises(ValueError):
        validate(receipt.topic('oilfield'), receipt.to_bytes(),
                 telemetry_topic=obs.topic, telemetry_payload=obs.payload + b' ')
```

所有契约边界向量同时喂三个实现；保留 v1 样例回归。真实临时 Broker 验证 gateway-A 不得读 B 回执/写任何回执、publisher 不得写遥测、消息整包与 payload 限额差别、retain 标志、合法 1 MiB 边界。

- [ ] **2. 跑 RED。** `& $taskPython -m unittest tests.test_ingest_interface -v`。
- [ ] **3. 只发布实现过的回执能力。** schema additionalProperties=false、九个字符串字段全部 required；JSON Schema 不能替代重复键和精确字节验证。资料写明 stored 只表示保存、quarantined 不算测点、无关联回执例外、允许列表/旧模式、无 ack-of-ack。修正 ROADMAP 的未登记数据隔离建议，其他控制/波形仍标未开放。

```text
user gateway-GW-001
topic write oilfield/v1/GW-001/+/telemetry
topic read oilfield/v2/GW-001/+/ingest-ack

user platform-receipt-publisher
topic write oilfield/v2/+/+/ingest-ack
```

保留现有中心 read-only ACL；模板显式 `max_packet_size 2097152`，payload 限额不小于合法 v1，并与隔离上限匹配；测试超限不以协议握手完成算中心保存。ZIP 固定包含已有 11 个公开源资料以及本任务三个公开回执文件，不包含 zip 自身、凭据、local 配置或日志；生成后重新读取清单/摘要校验。

- [ ] **4. 跑 GREEN 与原厂商包回归。** `& $taskPython -m unittest tests.test_ingest_interface tests.test_open_interface -v`；仅在独立工作区构建 ZIP，不更新现有运行服务的公开包。
- [ ] **5. 提交。** `git commit -m "docs: publish tested application receipt contract and manufacturer vectors"`。

### Task 14: 真实故障闭环、完整回归和交付证据

**Files:** Create `tests/test_ingest_end_to_end.py`, `tests/test_ingest_failure_recovery.py`, `tools/verify_ingest_reliability.py`, `docs/生产接收可靠性部署与回退.md`; Modify `docs/年度工作任务逐项响应.md`；新增本工作包的 `docs/superpowers/reviews/2026-09-24-production-ingest-reliability.md` 在实际审查后填写结论。

**Interfaces:** `verify_ingest_reliability.py --config <isolated-config> --soak-seconds 1800 --report <path>`；输出 JSON 包含 code_revision、database_identity、test_counts、failure_cases、soak_duration、protected_artifacts、deployed=false。测试类自建随机端口 Broker/HTTP 子进程；只停止自己持有 PID 的测试进程。

- [ ] **1. 补集成 RED 用例，逐项制造真实故障。**

```python
def test_commit_then_lost_receipt_recovers_once(self):
    with self.stack() as stack:
        stack.start(receipt_delivery=False)
        stack.enqueue_one()
        stack.await_counts(messages=1, samples=1)
        stack.restart_gateway()
        stack.enable_receipt_delivery()
        stack.await_gateway_state('stored')
        self.assertEqual(stack.database_counts()['samples'], 1)
        self.assertEqual(stack.gateway_record()['confirmation_mode'], 'application')
```

`self.stack()` 是本测试模块上下文 helper：创建临时 MQTT 配置/认证/ACL、临时 v2 队列、隔离 DB factory 和临时 HTTP 认证库。接口 start(receipt_delivery: bool)、enqueue_one()、await_counts(messages:int,samples:int)、restart_gateway()、enable_receipt_delivery()、await_gateway_state(state:str)、database_counts()->dict、gateway_record()->dict 都操作真实进程/数据库；所有 await 用单调时钟最多 30 秒，超时输出安全诊断并失败，不无限等成功。

追加故障：DB 连接拒绝、事务中断、入站 ACK 丢失、publisher 发布前/后崩溃、Broker 丢会话、队列空间不足、权限撤销、旧队列回退保护；每个故障必须看到设计对应状态和恢复，而不是只看到进程存活。通过 HTTP 注册测试井/台账和映射，仅写 WP-03 测试库，再验证未知/质量坏/点表不符原始数据保留。真实浏览器临时平台完成查案件→预览→关单→审计→请求补发，不使用全程假 API。

- [ ] **2. 跑专项 RED/GREEN，每次只修对应失败。** `& $taskPython -m unittest tests.test_ingest_end_to_end tests.test_ingest_failure_recovery -v`；真实 Broker 和完整 DDL 必须启用，缺依赖就是未完成，不以 skip 算成功。这里的 RED 可揭露集成遗漏；若已通过，记录首次通过，不人为制造伪 RED。
- [ ] **3. 运行 30 分钟有界持续实验和至少 10 次测试进程重启，留真实报告。**

```powershell
& $taskPython tools/verify_ingest_reliability.py --config $env:OILWELL_INGEST_TEST_CONFIG --soak-seconds 1800 --report 'logs/ingest-wp03/integration.json'
```

实验报告列出实际报文数、字节/速率、峰值积压、恢复耗时、重复入库数和故障次数；容量不足/无数据时不能生成“通过”。测得性能仅代表本机软件实验，不推导现场吞吐。工具持续写短进度，执行期间保持用户更新，不长时间阻塞工具等待；不把 30 分钟实验称作生产耐久验证。

- [ ] **4. 串行全项目回归，严格检查失败、错误和跳过。**

```powershell
$env:OILWELL_REGISTRY_TEST_CONFIG = 'C:/Users/hp/Documents/外专双百/data/registry-test/registry-test.local.toml'
$env:OILWELL_POSTGRES_TEST_CONFIG = 'C:/Users/hp/Documents/外专双百/data/registry-test/legacy-test.local.toml'
$env:OILWELL_TEST_UNUSED_MQTT = 'not-connected-test-value'
& $taskPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Full regression failed' }
```

在工具创建的临时工作目录设置 PLATFORM_DATABASE_PATH/PLATFORM_LOG_PATH，NO_BROWSER=1，不能让默认测试启动操作本机现有业务 SQLite。完整最终报告单独统计 testsRun/failures/errors/skipped；需要真实集成的测试零跳过，若其他明确不适用项跳过须逐项列明且不计通过。最终修复后重跑相关故障和全量；不得复用旧 WP-02 的通过数字。

- [ ] **5. 根据用户选择的执行方式完成独立审查及修复，再核验保护对象。** 审查针对本工作包完整 diff 与上述 Review Focus；用实际结果填写审查文件，未审不预写通过。最终冻结目录/ZIP/tag 与已记录基线比较，原开发目录 dirty 状态保持；停止测试进程只使用自己保存的 PID 和身份，不按端口批量杀现有服务。保留只属于本工作包的日志和测试库，不删除 WP-02 留存证据。
- [ ] **6. 更新年度响应、部署与回退说明并提交。**

```powershell
git diff --check
git status --short
```

部署说明列明四类运行身份与独立迁移身份、数据库备份、逐网关允许列表/ACL、application 启用检查、积压监测和保全新队列的回退。年度清单仅把 WP-03 已验证子能力更新为“独立工作区软件验证”，明确 deployed=false、无实物、无现场效果。提交信息 `test: verify end-to-end ingest reliability and record deployment limits`；不自动部署或合并。

## C. 设计覆盖、自查与交付门槛

| 设计章节 / 关键边界 | 实现及测试任务 |
| --- | --- |
| §1—3 目标、旧链路兼容、错误分类、不丢未知合法数据 | 1、3、4、14 |
| §4 原文、事务、并发、稳定结果、旧 JSONB 不冒充原文 | 2、3、5、14 |
| §5 回执字段、精确字节、不可关联例外、允许列表 | 1、3、4、8、13 |
| §6 独立发布、租约、补发、真实“已交 Broker” | 5、6、9、14 |
| §7 队列迁移、旧确认保留、异步采集、晚到/非法回执 | 7、8、9、14 |
| §8 原文权限、人工案件、审计、请求竞争 | 10、11、12、14 |
| §9 2 MiB/4 KiB、配额、时钟、ACL、健康与容量告警 | 1—9、11、13、14 |
| §10 权限、显式迁移、启用、回退、不提权旧账号 | 2、4、7、11、14 |
| §11 模块/资料和年度任务逐项响应 | 全部任务、13—14 |
| §12 故障矩阵与分层证据 | 各任务测试及14 |
| §13 不做项、硬件/现场/投产边界 | 全局约束、14、下述门槛 |

计划自查时逐项确认：上表无漏项；所有跨任务函数名与 B 节一致；五项 Review Focus 都有归属测试；每个任务可独立验证；代码片段中测试 helper 必须按所属任务定义，不能跳过 helper 只改断言让测试变绿。

完成 WP-03 的门槛是：软件功能、真实故障验证、权限验证、页面操作、最终回归和审查证据均完成。不能只做到中心隔离就宣布应用确认完成，不能只用模拟 publisher 证明 Broker 恢复，不能只用临时表替代新库完整 DDL。

**待用户确认：** 请审阅本计划，并选择执行方式：① 本会话逐项实现，最后做一次独立整体验证/审查（本计划推荐，接口串联较多，避免反复交接）；② 每任务由独立实现者和审查者把关，再做最终整体审查（更细、上下文与时间开销更高）。此前 WP-02 的方式选择不自动当作本计划已获批准。确认前不实施产品改动。
