# WP-02 Production Device Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立不依赖演示设备的生产台账，让设备身份、井号归属、点表版本及每次管理变更能够按时间追溯，并在现有平台中实际操作。

**Architecture:** 在中心 PostgreSQL 的独立 `device_registry` schema 内，用同一事务保存业务、历史、审计、幂等回执和修订号。复用 SQLite 登录身份但不复用虚拟设备目录；以独立查询身份关联原始遥测，只观察核查，不改变 MQTT v1 接收及现场控制行为。新增领域模块和页面，旧中心查询与冻结演示版保持不变。

**Tech Stack:** 现有 Python 3.13、psycopg 3、PostgreSQL 18、标准库 HTTP 服务、原生 JavaScript/HTML/CSS、unittest 与本机 Edge；不增加框架或在线 CDN 依赖。

**Spec:** [已确认详细设计](../specs/2026-09-23-production-device-registry-design.md)。实施者必须同时读设计和本计划。用户已选择方式 1（当前会话逐项实施，最终独立审查）；本文描述实施目标，不能当作已经实现或通过测试的证明。

## Global Constraints

- 本阶段为观察核查模式。MQTT v1 原有接收路径及主题保持不变。
- 不做：自动扫描或注册陌生设备、模拟台账批量转正、凭据签发/撤销、配置远程下发、固件升级、波形协议、历史关系纠错重写、远程启停，以及新的诊断/节能算法。
- 迁移不会预置生产井或设备。无实物时生产目录可以为空，测试实体只在隔离测试库创建。
- 所有实体 UUID 由服务端生成。线上网关/终端/测点沿用 OWP-EDGE/1.0 ASCII 标识规则，大小写敏感。
- 行版本、点表版本及全局修订号均为正 BIGINT，API 使用十进制字符串传输，`expected_version` 原样回传字符串。
- 名称/型号/厂家最多 120 个 Unicode 码点，序列号最多 128，操作原因 2—500，人工核查依据最多 1000。
- 统一保存带时区的 UTC 时间，精度到微秒；界面显示北京时间和明确时区。区间采用左闭右开 `[valid_from, valid_to)`，未结束的上界为空。
- 每版 1—256 个唯一测点，整次 JSON 请求不超过 64 KiB。
- v1 路径的 `reported_map_version` / `application_status` 固定为 null / `unverified`；`eligible_for_automation` 固定 false。
- 不复用 MQTT 入库账号执行台账写操作。迁移身份拥有 DDL；台账应用身份只获得此 schema 必要读写权限及只追加审计权限；查询身份仅 SELECT。
- 列表和历史统一分页，默认每页 20、最多 200、偏移量不超过 100,000；审计查询默认近 24 小时、最长 31 天。
- 连接超时 3 秒、锁等待 1 秒、语句超时 3 秒；这些是各阶段上限，不宣称是端到端请求总超时。
- 迁移以显式工具执行，检查 schema 版本和摘要；应用启动不执行 DDL、不自动建账号、不批量激活旧设备。
- 回退关闭新模块配置和导航，继续旧采集路径；保留新 schema 数据供调查和重启恢复，不以 DROP TABLE 回退。
- 只响应年度工作任务 Y1-02 / Y1-03 / Y3-05，不添加专利、奖项或绩效申报交付。
- 保护用户未跟踪的 `scripts/`。冻结目录 `C:/Users/hp/Documents/外专双百-演示版-20260912`、源 ZIP 及标签 `demo-v2.0-20260912` 不写入、不重建、不移动。

## Review Focus

1. 已提交请求重试前操作者权限被撤销：仍须返回 403，不能从幂等回执绕过鉴权（Task 9 的撤权重试测试）。
2. 设备迁井并退役后补传迁井前的测量：历史井号与采样时状态保持原值，当前状态另示退役（Task 7 的微秒边界和迟到测试）。
3. 合法中文名称含 HTML/闭合标签：页面只显示文字，不执行脚本，也不破坏点表编辑器（Task 10、11 的真实 DOM 注入测试）。
4. 切换设备时旧请求晚到、提交时网络超时：旧响应不得覆盖新设备，重试保留原幂等键和正文；新操作另用新键（Task 10、11 的请求竞争测试）。
5. 并发归档目标井和绑定设备：只能一方成功，不出现绑定到已归档井；测试使用两个真实 PG 会话（Task 6 的并发测试）。

---

## 实施基线、文件边界与共用约定

基线：分支 `codex/production-hardening`，设计提交 `0bb60f9`。实施前按 worktree 技能核验隔离工作区及工作树状态，不复制冻结演示版；不得遗失当前开发分支上的生产改动。本文提交后以实际 HEAD 为实施起点，不硬重置到旧提交。

### 文件图

| 文件 | 职责 / 所属任务 |
| --- | --- |
| `backend/registry/__init__.py`, `errors.py`, `contracts.py` | 无副作用包入口、稳定错误、请求/版本/文字规范（1） |
| `backend/registry/point_maps.py` | 点表验证、规范化字节、摘要（1） |
| `backend/registry/sql/001_registry.sql`, `sql/grants.sql`, `migrate.py` | 显式迁移、不可变约束、限定权限（2） |
| `backend/registry/transactions.py` | 写事务、锁序、时钟、幂等及审计（3） |
| `backend/registry/directory.py` | 井目录、设备创建与元数据变更（4） |
| `backend/registry/maps.py` | 点表快照、新版、审核（5） |
| `backend/registry/lifecycle.py` | 状态、绑定、解绑、期望点表关联（6） |
| `backend/registry/service.py` | 仅显式 action → 领域函数分派（4—6） |
| `backend/registry/queries.py`, `resolution.py` | 只读台账/历史、按采样时间核查（7） |
| `backend/registry/config.py`, `runtime.py` | 配置、最小权限探测、模块可用性（8） |
| `backend/registry/api.py`, `permissions.py` | HTTP 严格入口、路径和权限矩阵（9） |
| `backend/seed.py`, `backend/http_server.py` | 新权限一次性引入及 HTTP/启动接线（9、12） |
| `frontend/pages/field-registry.js`, `frontend/registry/client.js` | 台账页面与请求/选择状态（10） |
| `frontend/registry/forms.js`, `frontend/registry/history.js` | 表单、点表/审核/历史/核查（11） |
| `app.js`, `styles.css`, `frontend/pages/central-telemetry.js` | 新导航、局部样式、原链路页进入核查的入口（10—11） |
| `config/center.example.toml`, `docs/生产设备台账部署与回退.md` | 无密钥配置示例及执行/回退操作（8、12） |
| `tests/registry_support.py`, `tests/test_registry_*.py`, `tests/fixtures/registry_*.html` | 隔离数据库、单元/权限/HTTP/真实 DOM 验证（各任务） |

不修改 `backend/center/schema.sql`、网关协议或旧业务数据来迁就新目录。不在 `backend/api.py`、`backend/center/postgres.py` 中堆叠新增业务。

### 对外字段与内部接口固定表

- 接入方式字段统一 `transport`，取值 `modbus_tcp` / `modbus_rtu` / `mqtt_direct`；用途字段 `usage`，取值 `commissioning` / `production`。审核状态与设备状态分开。
- `Actor`（冻结 dataclass）：`domain: str, user_id: str, username: str, display_name: str, role_code: str`。domain 固定 `platform-local`，由认证用户构造。
- `Command`（冻结 dataclass）：`action: str, target: UUID | None, actor: Actor, key: UUID, request_hash: str, body: dict[str, object]`；body 是严格验证且默认值展开的正文，不含 actor，保留 expected_version 字符串；hash 包含 HTTP 方法、规范路由和该正文，key 纳入一致比较。
- `Change`（冻结 dataclass）：`object_type: str, object_id: UUID, before: dict[str, object], after: dict[str, object], entity: dict[str, object]`。领域函数不自行 commit；对象变化事件统一由事务器写入。
- `RegistryError(code: str, message: str, status: int)`；不将 psycopg 原始错误/连接信息发送给用户。状态码仅使用设计中的 401/403/404/409/422/503；超大 JSON 在新模块统一 422 `registry.body_too_large`。
- 成功写结果示例：`{"entity": {"id":"00000000-0000-0000-0000-000000000001","version":"1"}, "registry_revision": "2", "committed_at": "2026-09-23T00:00:00.000001+00:00", "replayed": false}`；entity 包含 `id`、`version`；创建点表的 entity 同时包含 `device_version`、`map_version`、`sha256`。
- 读取接口返回 `{"items": [], "total": 0, "page": 1, "page_size": 20, "registry_revision": "1"}`；详情对象单独含其 `version`。HTTP 外壳沿用 `{"ok": true, "data": {"items":[]}}` 结构，data实际放入上述查询或变更结果。
- 所有写正文含 `reason`、`idempotency_key`；已有实体的写操作含 `expected_version`（井操作使用井版本，其余用所属设备版本）。创建点表含 `parent_version: str | null`，只能指向本设备最新版本，无版本时为 null。首次版本为 `"1"`。
- action 固定：`well.create`, `well.change`, `device.create`, `device.change`, `map.create`, `map.review`, `device.bind`, `device.unbind`, `map.assign`, `device.state`。不存在通用任意字段 UPDATE。
- `RegistryService.execute(command: Command) -> dict[str, object]`；`RegistryQueries.read(resource: str, filters: dict[str, object]) -> dict[str, object]`。resource 只接受 `wells/devices/device/state_history/bindings/point_maps/map_assignments/resolved_telemetry/link_status/audit`。
- 同一个写事务先生成 audit event UUID，领域函数写入的区间引用它；相关 FK `DEFERRABLE INITIALLY DEFERRED`，事务结尾插入事件，提交时必须存在。

### 测试执行规则

所有命令从开发工作区根运行，使用各任务列明的 `.venv/Scripts/python.exe -m unittest` 命令。优先复用现有 venv；新 worktree 可显式使用原开发根的 venv，不向演示目录安装。

PG 测试必须显式设置 `OILWELL_REGISTRY_TEST_CONFIG` 指向本地私密 TOML，目标名称只能为 `oilwell_registry_test`。该配置仅存凭据引用，必须位于忽略范围或工作区外。未配置允许日常单测跳过 PG，但最终交付不能把跳过记成通过；需要明确报告“数据库验证未执行”。测试库不存在或无迁移权限时停止对应验证，报告所需权限，不尝试入库账号提权。

测试库执行完整中心 DDL + 新台账迁移，专用 writer/reader 使用真实授权。测试重置只能由测试迁移身份，在新连接 `SELECT current_database()` 确认为精确测试库名后执行固定表集合TRUNCATE，列名为 `device_registry.operation_receipts, device_registry.point_map_assignments, device_registry.point_map_versions, device_registry.well_bindings, device_registry.device_state_periods, device_registry.audit_events, device_registry.devices, device_registry.wells, public.telemetry_samples, public.ingest_messages`；依赖表已全部列明，不加CASCADE扩大清理。单独UPDATE head=1，保留schema_migrations。不 DROP 数据库/schema、不匹配通配数据库名、不清理业务库。PG 测试使用会话 advisory lock 串行拥有该测试库；并发用例在这个独占范围内开启两个子连接。

共用 `tests/registry_support.py` 提供：

| RegistryDatabaseCase 方法签名 | 行为 |
| --- | --- |
| `setUp() -> None` | 校验目标库、取独占advisory lock、检查实际迁移/授权、固定表重置、head=1 |
| `tearDown() -> None` | 关闭本用例全部连接，释放独占锁；不删除数据库 |
| `connect(role: str = "writer") -> psycopg.Connection` | 从测试配置的migrator/writer/reader三份连接参数中显式选择，其他role拒绝 |
| `command(action: str, target: str | None = None, *, key: UUID | None = None, actor: Actor | None = None, **body) -> dict` | 通过validate_command和RegistryService执行，补测试actor/key/原因，不猜expected_version |
| `read(resource: str, **filters) -> dict` | 用reader构造RegistryQueries，调用read |
| `scalar(sql: str, params: tuple = (), *, role: str = "reader") -> object` | 执行固定测试SQL，返回首行首列，不暴露给HTTP |
| `fixture(*, active: bool = False) -> dict` | Task6开始使用，经公共服务建立井、设备、批准并关联点表，返回well_id/device_id/version/map_version/binding_time；active=True再启用 |

command默认测试actor为`Actor("platform-local","101","operator","测试操作者","administrator")`，reason为“隔离库验证”，路径从Task9固定路由表生成。Task2—5不调用尚未完成的fixture；setUp不依赖RegistryService存在，防止测试脚手架循环导入。临时TOML和HTML在测试代码里通过标准库生成是测试fixture，不是实施者绕过本工作区apply_patch约束的方式。

## Task 1: 严格请求契约与可重复点表摘要

**Files:** Create `backend/registry/__init__.py`, `errors.py`, `contracts.py`, `point_maps.py`; Test `tests/test_registry_contracts.py`。

**Interfaces:** 产出上述 Actor/Command/Change/RegistryError；`parse_json(data: bytes) -> dict`，`version(value: object) -> int`，`validate_command(action: str, target: UUID | None, actor: Actor, body: dict, *, method: str, route: str) -> Command`，`command_digest(method: str, route: str, body: dict) -> str`；`canonical_point_map(transport: str, points: list[dict]) -> tuple[dict, bytes, str]`。消费现有 `backend.gateway.contracts.PointDefinition`（先严格类型校验再构造，不改变其行为）。

- [ ] 写 `RegistryContractsTests`，导入新模块；先锁定重复键、版本精度和 canonical 默认值。

```python
def test_duplicate_json_and_version_forms_are_rejected(self):
    with self.assertRaises(RegistryError):
        parse_json(b'{"reason":"ok","reason":"no"}')
    for item in (True, 1, 1.0, "01", "0", "9223372036854775808"):
        with self.subTest(item=item), self.assertRaises(RegistryError):
            version(item)
    self.assertEqual(version("9223372036854775807"), 9223372036854775807)

def test_canonical_map_expands_defaults(self):
    point = {"point_code": "load", "unit_id": 1, "function_code": 3,
             "address": 0, "data_type": "float32", "unit": "kN"}
    explicit = point | {"byte_order": "big", "word_order": "big",
                        "scale": 1.0, "offset": 0.0, "writable": False,
                        "poll_group": "normal", "engineering_min": None,
                        "engineering_max": None}
    a = canonical_point_map("modbus_tcp", [point])
    b = canonical_point_map("modbus_tcp", [explicit])
    self.assertEqual(a, b)
    self.assertEqual(hashlib.sha256(a[1]).hexdigest(), a[2])
    for invalid in (point | {"function_code": 3.0}, point | {"writable": 0}):
        with self.assertRaises(RegistryError):
            canonical_point_map("modbus_tcp", [invalid])
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_contracts -v`；预期新模块不存在或断言失败，保存红灯结果。
- [ ] 实现严格 JSON：先校验 bytes 长度 ≤65536，UTF-8 strict 解码，object_pairs_hook 检查所有层级重复键，parse_constant 拒绝 NaN/Infinity，捕获深度/溢出错误统一 422；顶层只允许对象。所有未知字段拒绝（包括嵌套点表），标识正则 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`。文字逐字符拒绝 C0/C1、NUL、代理码点；不按字节误截中文。浮点输入拒绝 bool、无穷以及非零数字转成 0 的下溢；正文数字解析保留 Decimal 到验证后转换。

```python
def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise RegistryError("registry.invalid_version", "版本必须为正整数字符串", 422)
    result = int(value)
    if result > 9223372036854775807:
        raise RegistryError("registry.invalid_version", "版本超出范围", 422)
    return result

def encode_canonical(document):
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return document, raw, hashlib.sha256(raw).hexdigest()
```

`encode_canonical(document: dict) -> tuple[dict, bytes, str]` 是 point_maps.py 内部函数。canonical 文档为 `{"rule":"registry.point-map.canonical.v1","transport":transport,"points":sorted_points}`；MQTT 仅保留 point_code/unit/engineering_min/max，拒绝 Modbus 字段。Modbus 默认值采用测试列出的值；整数须 `type(x) is int`，宽度末地址不得超过65536，writable 必须 `is False`；scale/offset/range 转有限 float，单位复用 OWP-EDGE 长度规则且不做隐式大小写或空格转换。

- [ ] 按固定 action 字段白名单实现 validate_command：井创建 area_code/well_code/name；井变更 name/status（仅 active→archived）；设备创建 gateway_id/device_id/name/manufacturer/model/serial_number/transport/usage；设备变更 name/manufacturer/model/serial_number/check_note/usage；map.create points/parent_version；map.review map_version/sha256/decision；bind well_id；unbind 无附加字段；assign map_version；state status/check_note。所有公共字段遵循前述 reason/key/version 规则；必填/选填显式声明，不允许调用者新增内部字段。

```python
def command_digest(method, route, body):
    document = {"method": method, "route": route, "body": body}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
```

摘要只接收validate_command完成类型转换后的正文，Decimal已按字段转换为int/float，UUID为规范字符串；点表默认值按canonical展开，未指定可选文字统一为空字符串，不能包含datetime/任意对象。
- [ ] 加参数化边界用例并运行同一命令：65536/65537字节、非法UTF-8、嵌套重复键、unknown/actor/effective_at、JSON非对象、字符串长度边界、NaN/上下溢、256/257点、重复点、FC写操作、地址末端、错误单位、MQTT含寄存器、按点代码重排摘要相同/改一值摘要不同。断言全部通过。
- [ ] 审查公开类型一致，提交明确路径：`git add backend/registry/__init__.py backend/registry/errors.py backend/registry/contracts.py backend/registry/point_maps.py tests/test_registry_contracts.py`；`git commit -m "feat: define strict production registry contracts"`。

## Task 2: 完整数据库迁移与不可变历史约束

**Files:** Create `backend/registry/sql/001_registry.sql`, `sql/grants.sql`, `migrate.py`, `tests/registry_support.py`, `tests/test_registry_schema.py`。

**Interfaces:** `migrate(connect_kwargs: dict, *, expected_database: str) -> dict`，`grant_roles(connection: psycopg.Connection, *, writer: str, reader: str) -> None`；CLI `python -m backend.registry.migrate --config PATH --expected-database NAME --writer-role NAME --reader-role NAME [--check]`。配置使用 `[postgres_migrator]` 凭据引用，复用现有 `_postgres_settings`；不会自动新建数据库、登录角色或密码。`--check` 仅检查，不执行DDL。

- [ ] 创建隔离测试基类的 connect/scalar 与目标检查，再写完整 DDL 约束测试（以下 UUID 固定仅用于隔离库）。

```python
def test_binding_ranges_cannot_overlap(self):
    with self.connect("migrator") as conn:
        conn.execute("INSERT INTO device_registry.wells(id,area_code,well_code,name) "
                     "VALUES ('00000000-0000-0000-0000-000000000001','A','W','测试井')")
        conn.execute("INSERT INTO device_registry.devices(id,gateway_id,device_id,name,"
                     "transport,usage) VALUES ('00000000-0000-0000-0000-000000000002',"
                     "'GW','D','测试终端','mqtt_direct','commissioning')")
        conn.execute("INSERT INTO device_registry.audit_events(id,actor,action,object_type,"
                     "object_id,reason,before_data,after_data,request_id) VALUES "
                     "('00000000-0000-0000-0000-000000000003','{}','test','device',"
                     "'00000000-0000-0000-0000-000000000002','隔离库验证','{}','{}',"
                     "'00000000-0000-0000-0000-000000000004')")
        sql = """INSERT INTO device_registry.well_bindings
                 (device_id,well_id,valid_from,valid_to,audit_event_id)
                 VALUES ('00000000-0000-0000-0000-000000000002',
                         '00000000-0000-0000-0000-000000000001',%s,%s,
                         '00000000-0000-0000-0000-000000000003')"""
        conn.execute(sql, ("2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z"))
        with self.assertRaises(psycopg.errors.ExclusionViolation), conn.transaction():
            conn.execute(sql, ("2026-09-23T00:30:00Z", None))
        conn.execute(sql, ("2026-09-23T01:00:00Z", None))
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_schema -v`；无新 schema 时应失败；配置缺失须明确跳过而非伪造红灯。
- [ ] 实现下列完整表布局；所有表限定 schema，时间 timestamptz、ID uuid、内容 jsonb、字节 bytea；元数据默认空字符串；版本 bigint default1/check>0。UUID由服务端 `uuid4()` 产生；测试直接SQL省略绑定id时可用数据库默认 `gen_random_uuid()`。

| 表 | 除固定表名外的必备列与约束 |
| --- | --- |
| schema_migrations | version bigint PK、sha256 char(64)、applied_at；迁移内容摘要不符即拒绝 |
| wells | id PK、area_code/well_code/name、status default active、version、created_at/updated_at；unique(area_code,well_code) |
| devices | id PK、gateway_id/device_id/name、manufacturer/model/serial_number/check_note、transport、usage、usage_locked bool default false、status default pending、version、created_at/updated_at；unique(gateway_id,device_id) |
| audit_events | id PK、actor jsonb、action、object_type、object_id uuid、reason、before_data/after_data jsonb、event_at default clock_timestamp()、request_id uuid |
| device_state_periods | id PK、device_id FK、status、valid_from/valid_to、audit_event_id deferred FK、非空正长区间、同设备排除重叠 |
| well_bindings | id PK、device_id FK、well_id FK、valid_from/valid_to、audit_event_id deferred FK、正长区间、同设备排除重叠 |
| point_map_versions | device_id FK + version复合PK、parent_version本设备复合FK、transport、canonical_rule、canonical_bytes、content jsonb、sha256、status default draft、created_at、created_event_id deferred FK、reviewed_at/review_event_id deferred FK；review字段与status一致 |
| point_map_assignments | id PK、device_id + map_version复合FK、valid_from/valid_to、audit_event_id deferred FK、正长区间、同设备排除重叠 |
| operation_receipts | actor_domain/actor_id/key复合PK、request_hash、result jsonb、committed_at；只追加 |
| registry_head | singleton smallint PK check=1、revision bigint check>0；唯一初始行(1,1) |

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE SCHEMA IF NOT EXISTS device_registry;
-- Apply this constraint to each of the three temporal tables.
ALTER TABLE device_registry.well_bindings ADD CONSTRAINT binding_no_overlap
  EXCLUDE USING gist (device_id WITH =,
    tstzrange(valid_from, valid_to, '[)') WITH &&);

CREATE FUNCTION device_registry.close_period_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable history'; END IF;
  IF OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL
     OR NEW.valid_to <= OLD.valid_from
     OR (to_jsonb(NEW) - 'valid_to') IS DISTINCT FROM (to_jsonb(OLD) - 'valid_to')
  THEN RAISE EXCEPTION 'only closing an open period is allowed'; END IF;
  RETURN NEW;
END $$;
```

`close_period_only` 挂到三张区间表 UPDATE/DELETE；单独的 immutable trigger 禁止 audit/receipt UPDATE/DELETE；点表触发器只允许 draft→approved/rejected 且一次性写入 review字段，其他列逐一保持不变。设备身份/transport/created_at 不可改；usage_locked 只能 false→true，锁定后用途不可改；retired 行不可再更新。井代码不可改。索引覆盖各组合唯一键、对象审计(event_at,id)、receipt作用域、时间查询。

- [ ] 迁移器在连接后先核验 expected_database、取迁移 advisory lock、比对文件SHA和schema版本再执行事务DDL；未授权安装扩展直接报告失败，不回退成无约束表。grants.sql 只通过 psycopg.sql.Identifier 注入明确writer/reader角色名，不能字符串拼接用户SQL。writer：schema USAGE、业务SELECT/INSERT、仅允许更新列的UPDATE、audit/receipt SELECT/INSERT、head SELECT/UPDATE(revision)；无DELETE/TRUNCATE/DDL。reader：USAGE/SELECT，无写权限。public原始表仅给reader SELECT；writer不授予遥测写。不给运行角色schema/表/扩展所有权。
- [ ] 增加并运行实际SQL反例：跨设备点表FK、缺审计延迟FK提交失败、三类排除约束、已封闭区间二次关闭/删改、快照内容覆盖、重复最终审核、writer改删审计失败、reader写失败、运行身份不是owner/superuser、重复迁移无变化/摘要不同拒绝。断言两张基础目录初始0条。重复运行同一测试命令全通过。
- [ ] 提交上述6个明确文件：`git commit -m "feat: add constrained registry schema and explicit migration"`。不提交本机测试配置、角色凭据或测试库数据。

## Task 3: 事务内审计、全局锁与可安全重试的命令

**Files:** Create `backend/registry/transactions.py`, `tests/test_registry_transactions.py`; Modify `tests/registry_support.py` 的连接工厂。

**Interfaces:** `RegistryTransactions(connect: Callable[[], psycopg.Connection])`；`run(command: Command, apply: Callable[[psycopg.Cursor, Command, datetime, UUID], Change], *, lock: Callable[[psycopg.Cursor, Command], None] | None = None) -> dict`。lock先取得实体及引用对象锁，apply接收随后取得的单一数据库时间和已生成事件ID，不提交。辅助 `bump(value: int) -> int`、`ensure_later(instant: datetime, starts: list[datetime]) -> None`；消费Task1的command_digest结果。

- [ ] 写 `RegistryTransactionTests`：回调插入一口固定测试井并返回 Change；同key执行两遍只调用一次，改变正文/route冲突；audit插入故障必须整笔回滚。

```python
def test_receipt_replay_calls_business_once(self):
    actor = Actor("platform-local", "101", "operator", "值班员", "maintenance_engineer")
    body = {"area_code": "A", "well_code": "W1", "name": "一号井",
            "reason": "隔离库验证", "idempotency_key": str(uuid4())}
    command = validate_command("well.create", None, actor, body,
                               method="POST", route="/api/registry/wells")
    calls = []
    def apply(cur, command, instant, event_id):
        entity_id = uuid4()
        cur.execute("INSERT INTO device_registry.wells(id,area_code,well_code,name) "
                    "VALUES (%s,'A','W1','一号井')", (entity_id,))
        calls.append(entity_id)
        entity = {"id": str(entity_id), "version": "1"}
        return Change("well", entity_id, {}, entity, entity)
    runner = RegistryTransactions(lambda: self.connect("writer"))
    first = runner.run(command, apply)
    again = runner.run(command, apply)
    self.assertEqual(len(calls), 1)
    self.assertEqual(first | {"replayed": True}, again)
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_transactions -v`，预期新事务器缺失失败。
- [ ] 实现以下锁与提交顺序；每个数据库错误退出with后才转换成API错误，不能在aborted事务继续查回执。使用字典行工厂并 JSONB wrapper；所有值参数化。

```python
with self.connect() as conn:
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET LOCAL lock_timeout = '1s'")
        cur.execute("SET LOCAL statement_timeout = '3s'")
        cur.execute("SELECT revision FROM device_registry.registry_head "
                    "WHERE singleton=1 FOR UPDATE")
        revision = cur.fetchone()["revision"]
        cur.execute("SELECT request_hash,result FROM device_registry.operation_receipts "
                    "WHERE actor_domain=%s AND actor_id=%s AND key=%s",
                    (command.actor.domain, command.actor.user_id, command.key))
        receipt = cur.fetchone()
        if receipt:
            if receipt["request_hash"] != command.request_hash:
                raise RegistryError("registry.idempotency_conflict", "重试内容已改变", 409)
            return receipt["result"] | {"replayed": True}
        if lock is not None:
            lock(cur, command)
        cur.execute("SELECT clock_timestamp() AS instant")
        instant = cur.fetchone()["instant"]
        event_id = uuid4()
        change = apply(cur, command, instant, event_id)
        next_revision = bump(revision)
        result = {"entity": change.entity, "registry_revision": str(next_revision),
                  "committed_at": instant.isoformat(timespec="microseconds"), "replayed": False}
        cur.execute("INSERT INTO device_registry.audit_events "
                    "(id,actor,action,object_type,object_id,reason,before_data,after_data,event_at,request_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (event_id, Jsonb(asdict(command.actor)), command.action, change.object_type,
                     change.object_id, command.body["reason"], Jsonb(change.before),
                     Jsonb(change.after), instant, command.key))
        cur.execute("INSERT INTO device_registry.operation_receipts "
                    "(actor_domain,actor_id,key,request_hash,result,committed_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (command.actor.domain, command.actor.user_id, command.key,
                     command.request_hash, Jsonb(result), instant))
        cur.execute("UPDATE device_registry.registry_head SET revision=%s WHERE singleton=1", (next_revision,))
    return result
```

- [ ] 在同事务插入所有审计字段（actor用asdict，reason来自校验body）、`Jsonb(result)`回执、head新值。`bump` 在9223372036854775807返回409；`ensure_later` 对任一起点 ≥instant拒绝409 `registry.clock_conflict`，不得加微秒伪造未来。权限不放到回执后（Task9先鉴权）。唯一/排除/序列化冲突→409，连接/锁等待/语句超时→503可重试；消息不得含SQL/DSN。
- [ ] 新增真实DB故障测试：用测试身份安装仅测试事务的拒绝audit INSERT触发器，断言井/事件/receipt/head均无变化并恢复触发器；模拟“底层commit已完成后抛连接错误”的包装连接，第一次客户端收到503，原key重试恢复原结果；不能只在commit之前抛错冒充提交不确定。并发同key两会话只一笔；samekey不同actor各自隔离；head溢出/时钟异常回滚。运行上述测试及Task1—2全部通过。
- [ ] 明确stage本任务文件和contracts摘要变更，提交 `feat: make registry changes atomic audited and idempotent`。

## Task 4: 空白生产目录、登记与受控元数据变更

**Files:** Create `backend/registry/directory.py`, `service.py`, `tests/test_registry_directory.py`; Modify `tests/registry_support.py` 的 command。

**Interfaces:** `apply_well(cur: psycopg.Cursor, command: Command, instant: datetime, event_id: UUID) -> Change`；`apply_device` 同签名；`RegistryService(transactions: RegistryTransactions).execute(command: Command) -> dict`。directory 内提供 `lock_device(cur, device_id: UUID, expected_version: str) -> dict`，按ID FOR UPDATE、404/409、退役只读、BIGINT上限检查；所有领域模块共用。

- [ ] 先写跨网关身份和元数据限制测试。

```python
def test_identity_is_gateway_and_device_and_never_recycled(self):
    body = dict(gateway_id="GW1", device_id="D1", name="井场终端",
                manufacturer="", model="", serial_number="same-serial",
                transport="mqtt_direct", usage="commissioning")
    first = self.command("device.create", **body)["entity"]
    second = self.command("device.create", **(body | {"gateway_id": "GW2"}))["entity"]
    self.assertNotEqual(first["id"], second["id"])
    with self.assertRaises(RegistryError) as conflict:
        self.command("device.create", **body)
    self.assertEqual(conflict.exception.status, 409)
    self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.devices"), 2)

def test_metadata_conflict_keeps_first_edit(self):
    device = self.command("device.create", gateway_id="GW", device_id="D",
                          name="原名", transport="mqtt_direct", usage="production")["entity"]
    self.command("device.change", device["id"], expected_version="1", name="新名")
    with self.assertRaises(RegistryError):
        self.command("device.change", device["id"], expected_version="1", name="迟到修改")
    self.assertEqual(self.scalar("SELECT name FROM device_registry.devices WHERE id=%s",
                                 (device["id"],)), "新名")
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_directory -v`，预期未分派action失败。
- [ ] 新建实体采用服务端UUID、同一instant、version1；设备status=pending，插入对应状态区间引用event_id。创建不查询MQTT、不读取SQLite设备，不按serial合并。业务SQL显式列名，生产井初始active。

```python
def lock_device(cur, device_id, expected_version):
    cur.execute("SELECT * FROM device_registry.devices WHERE id=%s FOR UPDATE", (device_id,))
    row = cur.fetchone()
    if row is None:
        raise RegistryError("registry.not_found", "生产设备不存在", 404)
    if row["status"] == "retired":
        raise RegistryError("registry.retired", "设备已退役，只能查看历史", 409)
    if row["version"] != version(expected_version):
        raise RegistryError("registry.version_conflict", "数据已更新，请刷新后重试", 409)
    bump(row["version"])
    return row
```

- [ ] 元数据更新只操作白名单列，转换API UUID/版本/时间为字符串；before/after不带凭据。usage仅pending且usage_locked=false可改；首次启用后锁定。相同正文不构成实际变更时409，不生成空审计。井归档先锁井，再检查任何开放绑定，存在则409；不支持恢复或改代码。设备/井更新推进一次version和updated_at。

```python
HANDLERS = {"well.create": apply_well, "well.change": apply_well,
            "device.create": apply_device, "device.change": apply_device}

class RegistryService:
    def __init__(self, transactions):
        self.transactions = transactions
    def execute(self, command):
        handler = HANDLERS.get(command.action)
        if handler is None:
            raise RegistryError("registry.invalid_action", "不支持的管理操作", 422)
        return self.transactions.run(command, handler)
```

- [ ] 扩充并运行测试：不自动复制48个虚拟设备；空目录；重复area+wellcode；跨area同井号；中文长度；冻结身份；非pending改用途；未知UUID；同key重放；并发两线程登记同身份（一个201等价成功、一个409、总1条）。原始消息表前后行数/摘要相同。Task1—4通过后提交本任务3个新文件及测试支持变更，消息 `feat: register production wells and device identities`。

## Task 5: 不可覆盖的点表版本和审核

**Files:** Create `backend/registry/maps.py`, `tests/test_registry_maps.py`; Modify `service.py` 分派。

**Interfaces:** `apply_map(cur, command, instant, event_id) -> Change`；消费canonical_point_map/lock_device/bump。service加入`map.create/map.review`。map.create结果entity含设备id、version（设备新版本）、device_version、map_version、sha256、status；map.review同结构。

- [ ] 写审核摘要和不可覆盖测试。

```python
def test_review_requires_exact_snapshot_hash(self):
    device = self.command("device.create", gateway_id="GW", device_id="D",
                          name="终端", transport="mqtt_direct", usage="commissioning")["entity"]
    item = self.command("map.create", device["id"], expected_version="1", parent_version=None,
                        points=[{"point_code": "load", "unit": "kN"}])["entity"]
    with self.assertRaises(RegistryError) as failure:
        self.command("map.review", device["id"], expected_version=item["device_version"],
                     map_version=item["map_version"], sha256="0" * 64, decision="approved")
    self.assertEqual(failure.exception.code, "registry.snapshot_conflict")
    approved = self.command("map.review", device["id"], expected_version=item["device_version"],
                            map_version=item["map_version"], sha256=item["sha256"],
                            decision="approved")["entity"]
    self.assertEqual(approved["status"], "approved")
    with self.assertRaises(RegistryError):
        self.command("map.review", device["id"], expected_version=approved["device_version"],
                     map_version=item["map_version"], sha256=item["sha256"], decision="rejected")
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_maps -v`，确认map action未实现失败。
- [ ] 实现建版：锁设备、查询本设备MAX(version)，parent必须匹配当前最新版或首次null，版本max+1（检查BIGINT）；按设备transport canonical；存原始canonical bytes、Jsonb结构、SHA和rule，status=draft。不从JSONB重新算hash、不更新旧行。

```python
cur.execute("SELECT version FROM device_registry.point_map_versions "
            "WHERE device_id=%s ORDER BY version DESC LIMIT 1", (command.target,))
latest = cur.fetchone()
expected_parent = str(latest["version"]) if latest else None
if command.body["parent_version"] != expected_parent:
    raise RegistryError("registry.parent_conflict", "请基于最新点表创建新版本", 409)
new_version = bump(latest["version"]) if latest else 1
document, raw, digest = canonical_point_map(device["transport"], command.body["points"])
cur.execute("INSERT INTO device_registry.point_map_versions "
            "(device_id,version,parent_version,transport,canonical_rule,canonical_bytes,"
            "content,sha256,created_at,created_event_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (command.target, new_version, latest["version"] if latest else None,
             device["transport"], document["rule"], raw, Jsonb(document), digest, instant, event_id))
```

- [ ] 审核：FOR UPDATE读device_id+version；缺失404、非draft409、SHA不同409；仅更新status/reviewed_at/review_event_id；同时设备version+1。不添加应用回执、不创建关联、不改配置。审计after包含版本/规则/摘要/决定，内容可由不可变快照查回。
- [ ] 新增参数化测试：非法父版、跨设备版号、不允许换transport、同内容另存仍新版本、改draft必须另存新版本、并发审核一个成功一个409、retired拒绝。用writer直接改canonical_bytes/content/parent失败、最后审核后review字段不可覆盖。运行Task1—5全部通过，提交 `feat: version and review immutable production point maps`。

## Task 6: 时效绑定、生命周期与期望点表关联

**Files:** Create `backend/registry/lifecycle.py`, `tests/test_registry_lifecycle.py`, `tests/test_registry_concurrency.py`; Modify `service.py`, `tests/registry_support.py`。

**Interfaces:** `apply_lifecycle(cur, command, instant, event_id) -> Change`；服务分派bind/unbind/assign/state。内部 `close_periods(cur, table: str, device_id: UUID, instant: datetime) -> None` 只接受三张预定义表，用Identifier；`change_state(cur, device: dict, status: str, instant: datetime, event_id: UUID) -> None` 不自行推进version（每个命令最终仅推进一次）。

- [ ] 先写迁井导致停用且清除期望关联的测试、归档/绑定竞争测试。

```python
def test_move_suspends_and_clears_assignment(self):
    original = self.fixture(active=True)
    well = self.command("well.create", area_code="A", well_code="W2", name="二号井")["entity"]
    result = self.command("device.bind", original["device_id"],
                          expected_version=original["version"], well_id=well["id"])
    self.assertEqual(result["entity"]["status"], "suspended")
    self.assertEqual(self.scalar("SELECT count(*) FROM device_registry.point_map_assignments "
                                 "WHERE device_id=%s AND valid_to IS NULL",
                                 (original["device_id"],)), 0)
    times = self.scalar("SELECT array_agg(valid_to) FILTER (WHERE valid_to IS NOT NULL) "
                        "FROM device_registry.well_bindings WHERE device_id=%s",
                        (original["device_id"],))
    self.assertEqual(times, [datetime.fromisoformat(result["committed_at"])])

def test_archive_and_bind_cannot_both_succeed(self):
    well = self.command("well.create", area_code="A", well_code="W", name="一号井")["entity"]
    device = self.command("device.create", gateway_id="GW", device_id="D", name="终端",
                          transport="mqtt_direct", usage="commissioning")["entity"]
    barrier = threading.Barrier(2)
    def submit(action, target, **body):
        barrier.wait(timeout=5)
        try:
            self.command(action, target, **body)
            return "ok"
        except RegistryError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(submit, "well.change", well["id"], expected_version="1", status="archived")
        b = pool.submit(submit, "device.bind", device["id"], expected_version="1", well_id=well["id"])
        self.assertCountEqual([a.result(), b.result()], ["ok", 409])
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_lifecycle tests.test_registry_concurrency -v`；预期缺对应分派失败。
- [ ] 实现时效操作顺序：所有命令已持head锁，随后device锁，再目标井/点表锁；时间T在这些实体锁取得后采样（Task3需允许领域函数先锁定引用对象，再事务器取T，见下段接口补充）。关闭前遍历要关闭的开放区间，ensure_later一次验证，不能部分更新后补救。绑定检查active井、非同井；关旧binding和assignment、开newbinding、active变suspended。解绑要求已有binding，关闭binding+assignment并停用。关联要求当前binding+本设备approved点表+transport相符，重复相同版本409；关旧assignment，开新assignment，active变suspended。退役关闭binding+assignment、关旧状态并开retired期；保留历史。

**事务时间接口接线（本任务落地并补 Task3 回归）：** 使用Task3已定义的lock参数。取head后先查回执（重放不取新T），无回执调用lock取得全部实体/引用锁，再取clock_timestamp，随后apply。`RegistryService` 为所有写action传入 `lock_references(cur, command) -> None`（service.py定义）：井变更锁井；设备相关先设备，bind再井，map.review/assign再本设备map；创建无实体锁。apply内部可重复读已锁对象，不再等待新的引用锁。全局head序列化已有写事务但仍显式保持锁/时间定义。

```python
def close_periods(cur, table, device_id, instant):
    allowed = {"device_state_periods", "well_bindings", "point_map_assignments"}
    if table not in allowed:
        raise ValueError("unsupported period table")
    query = sql.SQL("SELECT valid_from FROM device_registry.{} "
                    "WHERE device_id=%s AND valid_to IS NULL FOR UPDATE").format(sql.Identifier(table))
    cur.execute(query, (device_id,))
    ensure_later(instant, [row["valid_from"] for row in cur.fetchall()])
    cur.execute(sql.SQL("UPDATE device_registry.{} SET valid_to=%s "
                       "WHERE device_id=%s AND valid_to IS NULL").format(sql.Identifier(table)),
                (instant, device_id))
```

- [ ] 实现状态表：pending/suspended→active须binding+approved assignment+非空check_note，首次置usage_locked=true；active→suspended；非retired→retired。不能active→pending、suspended→pending、pending→suspended或相同状态；启用不依赖收过消息。before/after包含闭合/新建区间ID、版本、状态、用途锁和人工依据，写一笔审计。`check_note`先检验2—1000字符再写，不接受空白说明。
- [ ] 增加测试：全部4×4状态组合；先缺binding、再缺approved map、再缺check_note的启用拒绝；retired全部写接口拒绝；停用保留binding/map；首次启用后usage不能改；多设备同井；跨设备关联拒绝；数据库时间落在起点及之前拒绝且全回滚；两个不同key同version迁井仅一成功；audit故障时关闭/新建/状态均回滚；head锁超时返回503。重复运行Task1—6全通过，提交 `feat: preserve temporal well bindings and reviewed device lifecycle`。

## Task 7: 只读查询与按采样时间的来源核查

**Files:** Create `backend/registry/queries.py`, `resolution.py`, `tests/test_registry_queries.py`, `tests/test_registry_resolution.py`; Modify `tests/registry_support.py` read。

**Interfaces:** `RegistryQueries(connect: Callable[[], psycopg.Connection], *, online_seconds: int, delayed_seconds: int, clock_tolerance_seconds: int)`，`read(resource: str, filters: dict) -> dict`；`assess(sample: dict, context: dict) -> dict` 纯函数，不改输入。时间阈值为界面链路口径及未来时钟容差，显式配置；默认online120秒/delayed900秒/clock容差300秒，不宣称现场采样规范。

- [ ] 先写工程值不二次缩放与历史核查测试；补传数据通过现有 `backend.center.postgres.PostgresTelemetryRepository` 的真实写入路径或明确的原始表fixture插入，不改其生产代码。

```python
def test_engineering_value_is_not_scaled_again(self):
    sample = {"value": 32.4, "unit": "kN", "quality": "good", "point_code": "load"}
    context = {"registered": True, "bound": True, "state": "active",
               "clock_valid": True, "map_version": "3",
               "point": {"unit": "kN", "scale": 100, "offset": 20,
                         "engineering_min": 0.0, "engineering_max": 80.0}}
    result = assess(sample, context)
    self.assertEqual(result["value"], 32.4)
    self.assertFalse(result["eligible_for_automation"])
    self.assertEqual(result["application_status"], "unverified")
    self.assertIsNone(result["reported_map_version"])

def test_late_samples_keep_historical_state(self):
    t = datetime(2026, 9, 23, 4, tzinfo=timezone.utc)
    # Insert explicit temporal fixture rows through migrator only, in the isolated DB.
    rows = [(t - timedelta(microseconds=1), "A", "active"),
            (t, "B", "suspended"), (t + timedelta(microseconds=1), "B", "suspended")]
    self.insert_temporal_samples(rows, received_at=t + timedelta(hours=1), current_status="retired")
    result = self.read("resolved_telemetry", gateway_id="GW", device_id="D",
                       from_time=t - timedelta(seconds=1), to_time=t + timedelta(seconds=1))
    actual = [(r["well_code"], r["sample_state"], r["current_state"]) for r in result["items"]]
    self.assertCountEqual(actual, [(well, state, "retired") for _, well, state in rows])
```

`insert_temporal_samples(rows: list[tuple[datetime,str,str]], *, received_at: datetime, current_status: str)` 定义在本测试类：明确插入A、B两井；设备创建时间T−1小时；binding A=[T−1h,T)、B=[T,T+1min)，state active同A、suspended同B、retired=[T+1min,∞)；map assignment仅A时期。原始消息+样本含固定独立message_id/index，并使用中心实际分区DDL。测试只覆盖按区间解释，不将直接fixture插入当作生命周期服务验证（Task6另测）。

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_queries tests.test_registry_resolution -v`，预期模块缺失失败。
- [ ] 实现每次read在 `BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY` 中取head和clock_timestamp，再查数据；下一页带revision/received_before，head不匹配409 `registry.query_changed`。首次接收截止以DB时间生成，后续沿用且不能未来；31天窗口、page/page_size/offset边界由统一解析器校验。排序固定 measured_at DESC, message_id DESC, sample_index DESC；SQL值全部参数化。

```sql
SELECT s.*, d.id AS registry_device_id, current_d.status AS current_state,
       st.status AS sample_state, b.id AS binding_id, w.id AS well_id,
       w.well_code, a.map_version AS expected_map_version, m.content
FROM public.telemetry_samples s
LEFT JOIN device_registry.devices current_d ON current_d.gateway_id=s.gateway_id
 AND current_d.device_id=s.device_id
LEFT JOIN device_registry.devices d ON d.gateway_id=s.gateway_id
 AND d.device_id=s.device_id AND d.created_at<=s.measured_at
LEFT JOIN device_registry.device_state_periods st ON st.device_id=d.id
 AND tstzrange(st.valid_from,st.valid_to,'[)') @> s.measured_at
LEFT JOIN device_registry.well_bindings b ON b.device_id=d.id
 AND tstzrange(b.valid_from,b.valid_to,'[)') @> s.measured_at
LEFT JOIN device_registry.wells w ON w.id=b.well_id
LEFT JOIN device_registry.point_map_assignments a ON a.device_id=d.id
 AND tstzrange(a.valid_from,a.valid_to,'[)') @> s.measured_at
LEFT JOIN device_registry.point_map_versions m
 ON m.device_id=a.device_id AND m.version=a.map_version
WHERE s.gateway_id=%s AND s.device_id=%s AND s.measured_at>=%s
 AND s.measured_at<%s AND s.received_at<=%s
ORDER BY s.measured_at DESC,s.message_id DESC,s.sample_index DESC
LIMIT %s OFFSET %s;
```

按well_id查询使用第二个固定SQL变体条件 `w.id=%s`；不以字符串插入任意filter。current_d不带created_at过滤，只用于显示当前状态；d历史登记join判断当时是否已注册，不能让登记之前的样本被当前状态误认证。历史未覆盖时registry_device_id仍为null并给原因。

- [ ] assess返回原sample副本，加reasons数组（not_registered/unbound/no_expected_map/unknown_point/unit_mismatch/out_of_range/clock_anomaly/quality_bad/quality_uncertain/state_not_active）、匹配状态、固定未确认和禁止自动化字段。clock_anomaly包括未来测量超过配置容差（相对received_at）及无法覆盖的异常时序，不将正常迟到数据当时钟错误。单位逐字等，范围用已有工程值；source和quality保留，不把注册用途覆盖原source。
- [ ] 目录列表必须从devices起查，链路独立API聚合public.ingest_messages该网关+终端最新received_at，无消息→never_reported。链路延迟按received_at（不是created_at或measured_at），返回阈值口径；age<0显示clock_anomaly。metadata普通read不读取public遥测，无acquisition.read不调用link_status。历史和audit分页固定二级排序ID；audit默认24小时/最多31天。所有版本字符串化，datetime明确UTC。
- [ ] 增补测试：未登记完整pair可查原始样本；注册前样本仍未登记；停用/退役不改变旧归属；from=T边界；unknownpoint/单位不符/上下限/bad/uncertain各独立原因；没有消息但active；同名跨网关链路分开；新接收晚于received_before排除；翻页修订变化409；同事务查询中另一连接提交不撕裂快照；输入只给gateway或同时well+pair拒绝；未配置/连接失败不返回虚拟记录。运行Task1—7通过，提交 `feat: resolve telemetry against historical registry context`。

## Task 8: 独立连接身份、配置和故障隔离

**Files:** Create `backend/registry/config.py`, `runtime.py`, `tests/test_registry_config.py`, `tests/test_registry_privileges.py`; Modify `config/center.example.toml`。

**Interfaces:** `load_registry_config(path, *, env=None, credential_reader=None) -> RegistryConfig | None`；RegistryConfig含writer/reader（repr=False）、allowed_origins tuple、online_seconds120/delayed_seconds900/clock_tolerance_seconds300。`build_registry_runtime(path) -> RegistryRuntime`；RegistryRuntime为dataclass，字段`status: str, service: RegistryService | None, query: RegistryQueries | None, code: str | None, allowed_origins: tuple[str, ...]`。内部异常仅脱敏日志。`probe_privileges(writer_connect, reader_connect) -> None`，失败不降级复用入库身份。

- [ ] 写纯配置测试：缺writer/query、不同数据库、同账号、远程未TLS、超时/阈值非法、内联密码均失败。

```python
def test_no_query_fallback_when_registry_is_enabled(self):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "registry.local.toml"
        text = ('[registry]\nenabled=true\nallowed_origins=["http://127.0.0.1:8765"]\n'
                '[postgres]\nhost="127.0.0.1"\ndatabase="oilwell_registry_test"\nuser="ingest"\n'
                '[postgres_registry]\nhost="127.0.0.1"\ndatabase="oilwell_registry_test"\n'
                'user="registry_writer"\nsslmode="disable"\npassword_env="REG_PW"\n')
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(RegistryError) as missing:
            load_registry_config(path, env={"REG_PW": "test-only"})
        self.assertEqual(missing.exception.code, "registry.query_identity_required")
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_config tests.test_registry_privileges -v`；配置测试先红，PG权限测试按实际测试库运行。
- [ ] 新`[registry] enabled=false`默认关闭；enabled必须bool，enabled=true要求explicitwriter/query两节、三个数据库host/port/name完全一致、三个user不相同（不解析DNS将不同host假定同机），port严格int。验证后复用 `_postgres_settings` 读取writer/query凭据，不解析不需要的ingest密码。允许origin只接受http(s)scheme+host+明确port，无userinfo/path/query/fragment；本轮服务器仍loopback。

```python
def same_database(section):
    return (section["host"], section.get("port", 5432), section["database"])

if same_database(raw["postgres_registry"]) != same_database(raw["postgres"]) or \
   same_database(raw["postgres_query"]) != same_database(raw["postgres"]):
    raise RegistryError("registry.database_mismatch", "台账与中心须连接同一数据库", 503)
writer["connect_timeout"] = 3
reader["connect_timeout"] = 3
```

- [ ] 连接探测只SELECT目录信息：version/checksum、btree_gist、必要表约束/trigger存在且启用；session/current_user非superuser、不拥有schema/extension/table、无BYPASSRLS/CREATEROLE/CREATEDB/REPLICATION、无继承或可SET ROLE到owner/ingest/migrator的路径；拒绝writer的raw INSERT/UPDATE/DELETE/TRUNCATE、audit/receipt UPDATE/DELETE/TRUNCATE和reader任意写权限，检查PUBLIC继承授权和column grants，不能仅看表级has_table_privilege。writer还需业务必要列权限，reader具raw+registrySELECT。应用不自动REVOKE/GRANT修复。
- [ ] runtime构造捕获本模块配置/连接/schema/权限异常，返回unavailable且不给service；旧平台不随registry失败。运行实际writer/reader探测测试：正常pass、reader多授一列UPDATE拒绝、writer成为schema owner拒绝、角色继承owner拒绝、缺约束拒绝、缺DDL拒绝、远程TLS参数透传。权限改变仅在隔离库测试后恢复。
- [ ] 示例加入注释配置和凭据引用，不改`*.local.toml`。验证Task1—8全通过，提交 `feat: isolate production registry credentials and availability`。

## Task 9: 受权 HTTP 操作与权限一次性迁移

**Files:** Create `backend/registry/api.py`, `permissions.py`, `tests/test_registry_api.py`, `tests/test_registry_permissions.py`; Modify `backend/seed.py`, `backend/http_server.py`。

**Interfaces:** `RegistryApi(handler).get(path: str) -> bool` / `.post(path: str) -> bool`，非本模块前缀返回False，本模块所有情况自行响应并True。`required_permissions(action: str, body: dict) -> tuple[tuple[str, ...], ...]`：外层AND、内层OR；例如停用返回`(("registry.read",),("registry.devices.manage","registry.review"))`。`create_server` 新增可选 `registry_runtime=None` 测试注入；默认由center_config_path构造模块runtime（Task12验证启动）。

- [ ] 新建轻量handler测试夹具：记录_require调用、headers、rfile、_send_json输出；auth.require按角色表返回具有完整属性的User或抛PermissionDenied。fixture暴露 `RegistryApiCase.request(method, path, body=None, *, actor="maintenance_engineer", headers=None) -> tuple[int, dict]`，使用实际 RegistryApi，不直接调用service冒充HTTP；`self.runtime.service/query` 为调用计数的Mock；`self.allowed`可动态撤权。正式HTTP会话测试另外使用create_server+独立临时SQLite路径。

```python
def test_revoked_permission_blocks_cached_receipt(self):
    path = "/api/registry/devices"
    body = {"gateway_id": "GW", "device_id": "D", "name": "终端",
            "transport": "mqtt_direct", "usage": "commissioning",
            "reason": "登记核验", "idempotency_key": str(uuid4())}
    self.runtime.service.execute.return_value = {"entity": {"id": str(uuid4()), "version": "1"},
        "registry_revision": "2", "committed_at": "2026-09-23T00:00:00+00:00", "replayed": False}
    first, _ = self.request("POST", path, body)
    self.assertEqual(first, 201)
    self.allowed.discard("registry.devices.manage")
    second, _ = self.request("POST", path, body)
    self.assertEqual(second, 403)
    self.assertEqual(self.runtime.service.execute.call_count, 1)
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_api tests.test_registry_permissions -v`，预期缺API失败。
- [ ] 将6种新权限定义在permissions.py供seed及API共同引用；角色默认值严格依设计表。`AUTH_BOOTSTRAP_VERSION`由7升8；在既有bootstrap中仅新增权限定义和首次新增默认grant，不能为重启恢复已撤销非admin授权，不重置密码；admin得到全部6项（既有管理员全权限规则不变）。对“旧库已有同名权限但尚未升级marker”的情况，显式区分手动grant和系统迁移，不删除/覆盖手动授权。SQLite迁移与PG业务事务互不宣称原子。
- [ ] 实现完整路由矩阵（全部先registry.read）：

| method/path（前缀 /api/registry） | action/resource | 额外权限 |
| --- | --- | --- |
| GET /wells, /devices, /devices/{id} | wells/devices/device | 无 |
| POST /wells, /wells/{id}/changes | well.create/well.change | registry.wells.manage |
| POST /devices, /devices/{id}/changes | device.create/device.change | registry.devices.manage |
| POST /devices/{id}/state | device.state | active/retired: registry.review；suspended: devices.manage或review |
| GET /devices/{id}/state-history, /bindings, /point-maps, /point-map-assignments | state_history/bindings/point_maps/map_assignments | 无 |
| POST /devices/{id}/bindings, /unbind | device.bind/device.unbind | registry.bindings.manage |
| POST /devices/{id}/point-maps, /point-map-assignment | map.create/map.assign | registry.maps.manage |
| POST /devices/{id}/point-maps/{version}/review | map.review | registry.review |
| GET /resolved-telemetry | resolved_telemetry | telemetry.read |
| GET /devices/{id}/link-status | link_status | acquisition.read |
| GET /audit | audit | audit.read |

路径UUID/版本严格解析，review的map_version只来自path，不接受HTTP正文同名覆盖；HTTP白名单检查后由API附加path版号传给validate_command，内部Command携带该规范字段。GET query拒绝重复/未知参数；POST路径允许字段之外一律422；重复Content-Length、Transfer-Encoding、负数/非整数/超限长度拒绝并关闭连接，防止未读正文污染下次请求。body读取设有限socket超时并恢复；auth→受控body解析/动作权限→origin→availability→数据库。没有权限时不调用包括read receipt在内的任何数据库操作。

```python
origin = handler.headers.get("Origin")
authorization = handler.headers.get("Authorization", "")
if origin is not None:
    if origin not in runtime.allowed_origins:
        raise RegistryError("registry.origin_denied", "不允许此站点修改生产台账", 403)
elif not authorization.startswith("Bearer ") or not authorization[7:].strip():
    raise RegistryError("registry.origin_required", "无来源头的客户端须使用 Bearer 认证", 403)
```

- [ ] actor从真实认证User构造，domain固定；不信任name/role自报。新模块只用parse_json，不改变旧_json_body。201仅创建well/device/map且非重放，其余200。异常响应稳定中文，禁止SQL/DSN；GET未配置/故障503，客户端只据code区分，不拿空成功替代。方法不支持返回受控404，OPTIONS不开放CORS。
- [ ] 全矩阵测试5角色×所有路由、未登录401、缺底层telemetry/acquisition/audit权、管理员仍受状态/version限制；跨源、Origin:null、伪造Host、无Origin Cookie、有效Bearer、duplicateJSON、超大body、伪造actor、真实HTTP重试和session撤销。权限迁移测试前后password_hash相同、手动授权不变、重启不恢复已撤销的新非admin权限。Task1—9通过后提交 `feat: expose authenticated production registry API`。

## Task 10: 台账页面、独立状态和可靠请求状态管理

**Files:** Create `frontend/pages/field-registry.js`, `frontend/registry/client.js`, `tests/test_registry_browser.py`, `tests/fixtures/registry_list_check.html`; Modify `app.js`, `styles.css`。

**Interfaces:** `renderFieldRegistry(context) -> Promise<boolean>` 接收现有api/components/format/escapeHtml/workspace/page/pageHeader/params/permissions/navigate/isCurrent；无registry.read直接显示无权且不发请求。`createRegistryClient(api, {isCurrent})` 返回 `{selectDevice(id): Promise<object|null>, beginMutation(path, body): object, submitMutation(operation): Promise<object>, dispose(): void}`。operation含固定path/body（structuredClone后深冻结）、key、phase(new/sending/uncertain/succeeded/rejected)；不使用Number转换版本。

- [ ] 浏览器fixture导入实际页面/client/components/format，fake api按路径返回记录；使用Edge真实DOM执行选择/分页/错误/转场，输出`<pre id="result">` JSON。测试runner沿用test_center_browser.py模式，但新增fixture不能替换实际被测模块。先写晚到请求和名称注入测试。

```javascript
const pending = new Map();
const fakeApi = { get: path => new Promise(resolve => pending.set(path, resolve)) };
const client = createRegistryClient(fakeApi, {isCurrent: () => true});
const oldRequest = client.selectDevice("A");
const newRequest = client.selectDevice("B");
pending.get("/api/registry/devices/B")({id: "B", name: "后选设备", version: "9007199254740993"});
const latest = await newRequest;
pending.get("/api/registry/devices/A")({id: "A", name: "旧请求", version: "1"});
if ((await oldRequest) !== null || latest.id !== "B") throw new Error("stale selection won");
const cell = document.createElement("td");
cell.textContent = '<img src=x onerror="window.registryInjected=true">井';
document.body.append(cell);
if (cell.querySelector("img") || window.registryInjected) throw new Error("unsafe rendering");
```

在同一fixture执行实际列表注入测试，不能只证明浏览器textContent安全：

```javascript
const attack = '<img src=x onerror="window.registryInjected=true">井';
const workspace = document.createElement("main");
document.body.append(workspace);
const listApi = {get: async path => {
  if (path.startsWith("/api/registry/wells")) return {items: [], total: 0, page: 1, page_size: 20};
  if (path.startsWith("/api/registry/devices")) return {
    items: [{id: "00000000-0000-0000-0000-000000000001", name: attack,
      gateway_id: "GW", device_id: "D", status: "pending", version: "9007199254740993",
      transport: "mqtt_direct", usage: "commissioning"}], total: 1, page: 1, page_size: 20,
      registry_revision: "1"};
  throw new Error(`unexpected request: ${path}`);
}};
await renderFieldRegistry({workspace, api: listApi, components, format, escapeHtml,
  page: {label: "生产设备台账"}, pageHeader: () => "<h1>生产设备台账</h1>",
  params: {}, permissions: new Set(["registry.read"]), navigate: () => {}, isCurrent: () => true});
if (!workspace.textContent.includes(attack) || workspace.querySelector("img") || window.registryInjected)
  throw new Error("registry list executed or lost the name");
```

版本断言还须检查实际编辑表单提交正文仍为`"9007199254740993"`（Task11），不能只测client转换。

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_browser -v`；缺模块必须以fixture未完成/错误失败，不以空HTML通过。
- [ ] 实现client选择代数：每次select递增局部generation，await后同时检查generation、isCurrent、disposed；销毁/转场后旧响应返回null。提交operation保持immutable body+key，重复点击sending不重复发；timeout/network_error/cancelled/503结果归uncertain，可重试同body/key，不自动重试；409/422显示明确错误，修正后是新操作；401/403停止并要求重新认证/检查权限。成功后重新GET实体，不能用旧replayed回执覆盖最新详情。

```javascript
let selection = 0;
let disposed = false;
async function selectDevice(id) {
  const generation = ++selection;
  const result = await api.get(`/api/registry/devices/${encodeURIComponent(id)}`);
  return !disposed && isCurrent() && generation === selection ? result : null;
}
function beginMutation(path, body) {
  const key = crypto.randomUUID();
  return {path, key, phase: "new", body: structuredClone({...body, idempotency_key: key})};
}
```

深冻结body时递归处理对象/数组，version一律字符串；冻结不包括phase对象本身。request错误只更新发起时的表单，不污染已切换详情。未决操作保留在该页面的内存operation中；离开页面若未决，提示可能已提交，不承诺跨浏览器重启恢复。

- [ ] 页面包含井目录、设备列表、详情栏及“登记状态/接收链路/平台期望点表/终端实际版本”四项；局部状态文字map不要复用旧components.active=运行中，改为“台账已启用”，停用写“台账停用待复核”，退役明确不是物理停机。无消息显示“从未上报”，无权限不请求link-status，禁止虚拟补齐。空目录给登记入口；503未配置给配置提示；故障给重试且不显示陈旧绿灯。
- [ ] app.js import/renderer/nav接线，仅新页使用permission属性和navigation过滤，保持原页规则；只改`.field-registry`局部样式，1440/1024宽表格可滚动、按钮可键盘操作，400%数据量分页不一次全读。低权限不展示修改按钮，后台依然独立鉴权。
- [ ] 扩充真实DOM用例：空/未配置/不可用三者不同；5角色按钮矩阵；不查无权链路/审计；选择竞争/离页竞争；高BIGINT保持字符串；refresh/pagination错误不丢筛选；HTML名称/厂家转义。运行Task10及既有center_browser，提交 `feat: add production registry workspace and request guards`。

## Task 11: 实际表单、点表审核、来源核查与历史

**Files:** Create `frontend/registry/forms.js`, `history.js`, `tests/fixtures/registry_forms_check.html`; Modify `field-registry.js`, `client.js`, `styles.css`, `frontend/pages/central-telemetry.js`, `tests/test_registry_browser.py`。

**Interfaces:** `mountRegistryForms({root, api, client, components, permissions, onChanged, isCurrent}) -> {dispose()}`；root为详情/目录容器。`mountRegistryHistory({root, api, permissions, device, params, isCurrent}) -> {dispose()}`。`onChanged({deviceId: string|null, wellId: string|null})`重新读取相应目录/详情。以DOM data-action固定枚举绑定操作，不从响应执行函数/HTML。

- [ ] 先写真实表单fixture测试：点选登记→绑定→新点表→审核→关联→启用→迁井→查历史，用fakeApi保存调用和每次新version；另外使用Task12的实际HTTP数据库闭环验证业务。

```javascript
const sent = [];
const retryApi = {post: async (path, body) => {
  sent.push({path, body: structuredClone(body)});
  if (sent.length === 1) throw Object.assign(new Error("连接中断"), {code: "network_error", status: 0});
  return {entity: {id: "D", version: "9007199254740994"}, replayed: true};
}};
const client = createRegistryClient(retryApi, {isCurrent: () => true});
const operation = client.beginMutation("/api/registry/devices/D/changes",
  {expected_version: "9007199254740993", name: "新名称", reason: "核对铭牌"});
try {await client.submitMutation(operation);} catch (error) {
  if (error.code !== "network_error") throw error;
}
await client.submitMutation(operation);
if (sent[0].body.idempotency_key !== sent[1].body.idempotency_key) throw new Error("retry changed key");
if (sent[1].body.expected_version !== "9007199254740993") throw new Error("version rounded");
```

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_browser -v`；新表单动作应先失败。
- [ ] 构建独立表单：井创建/改名/归档；设备登记/元数据；绑定选active井；解绑；状态启用（核查依据必填）、停用/退役（危险语义确认，仅改变台账）。每次操作展示对象组合身份和expected_version，reason2—500字符；点表界面1—256行可增删，Modbus显示只读FC03/04、零基地址、dtype/word/byte/scale/offset/范围/poll_group；MQTT不显示寄存器字段且提交剔除，不发送隐藏垃圾字段。保存新版本永不PATCH旧快照。

表单稳定选择器约定：编辑设备入口`data-action="edit-device"`，提交`data-action="submit-registry"`，表单字段使用API字段同名name；point_code/寄存器字段使用各行fieldset内同名name。fixture先通过实际页面选中version为`9007199254740993`的设备再执行：

```javascript
workspace.querySelector('[data-action="edit-device"]').click();
const dialog = document.querySelector('[role="dialog"]');
dialog.querySelector('[name="name"]').value = "修改后的终端";
dialog.querySelector('[name="reason"]').value = "核对设备铭牌";
dialog.querySelector('[data-action="submit-registry"]').click();
await new Promise(resolve => setTimeout(resolve, 0));
if (sent.at(-1).body.expected_version !== "9007199254740993") throw new Error("form rounded version");
```

上述sent由此fixture的api.post记录，等待一次事件队列后提交必须出现；页面依赖异步读取完成时在fixture先await选中结果，不用任意长延时掩盖竞争。

```javascript
function textInput(label, value, name) {
  const wrapper = document.createElement("label");
  const caption = document.createElement("span");
  caption.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.value = String(value ?? "");
  wrapper.append(caption, input);
  return wrapper;
}
function snapshotViewer(content) {
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(content, null, 2);
  return pre;
}
```

- [ ] 点表审核显示设备身份、版号、SHA、父版和完整快照，再提交所显示摘要；误摘要409提示重新读取。关联按钮与审核按钮分开，确认框注明“设置平台期望定义，不下发到设备”。应用状态固定“终端实际版本未确认”。被退役设备隐藏编辑且API仍拒绝。
- [ ] 历史区显示起止时间（UTC微秒原值保留，界面北京时间+08:00）、状态/井号/版本/actor/reason；缺audit.read不显示也不请求完整审计。来源核查支持完整pair或well二选一、时间窗、页码，沿用第一次revision/cutoff翻页；409提示“台账已变更，请刷新核查结果”，不静默拼页。quality/reasons/原source与新匹配结果分列；不出现“可自动控制”的按钮。
- [ ] 原中心链路页加“核查归属”入口，仅有registry.read和telemetry.read时展示，以navigate("field-registry", {gateway_id,device_id,view:"resolution"})携带完整pair；未知终端不要求先登记。后台原接口字段与调用参数不变。
- [ ] 扩充DOM测试：点击所有实际按钮产生正确path/body；无权限不发请求；确认取消不写；空reason和257点前端拦截但不替代后端；JSON快照含`</textarea><script>`仅显示文本；点表不同transport表单字段；切换设备旧表单提交回调不能重绘新设备；uncertain保留原文/键、成功后读取当前实体；401/403/409/422/503文案。所有Task10—11浏览器测试及既有chart/center浏览器测试通过，提交 `feat: complete registry forms reviews and provenance views`。

## Task 12: 真实跨层闭环、部署检查与任务证据更新

**Files:** Create `tests/test_registry_end_to_end.py`, `tests/test_registry_startup.py`, `docs/生产设备台账部署与回退.md`; Modify `backend/http_server.py`, `docs/年度工作任务逐项响应.md`；如启动参数现有center-config不能满足接线，限改 `server.py` 的既有传递点并补现有启动测试，不新增平行配置通道。

**Interfaces:** 复用 `--center-config` / `PLATFORM_CENTER_CONFIG`，不另启服务器端口。`create_server(host, port, database_path=None, *, central_repository=None, center_config_path=None, registry_runtime=None)` 的registry_runtime只用于测试注入；实际启动构造独立runtime，无配置则not_configured。中心旧查询配置错误已有行为不在本任务扩大重构；新增registry部分错误不能影响原本有效的central_repository。运行日志只记录模块状态/错误码/请求ID。

- [ ] 先写启动故障隔离及完整HTTP测试；临时SQLite认证库+隔离PG，HTTP绑定127.0.0.1随机端口，Cookie登录真实账号（密码只用测试专用配置，不向日志回显），写请求有匹配该测试端口Origin。测试助手 `RegistryEndToEndTests.http(method, path, body=None) -> tuple[int,dict]` 用http.client真实网络，不跳过HTTP层。

```python
def test_unavailable_registry_keeps_old_health_available(self):
    runtime = RegistryRuntime(status="unavailable", service=None, query=None,
                              code="registry.schema_missing", allowed_origins=())
    with self.platform(registry_runtime=runtime) as http:
        status, payload = http("GET", "/api/health")
        self.assertEqual(status, 200)
        status, payload = http("GET", "/api/registry/devices")
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "registry.schema_missing")
```

`platform(*, registry_runtime)` 为本测试类contextmanager：创建临时SQLite、调用create_server注入、起线程、登录测试用户、yield http helper；finally shutdown/server_close/join，不能启动全机服务或清理当前运行实例。

- [ ] 运行 `.venv/Scripts/python.exe -m unittest tests.test_registry_end_to_end tests.test_registry_startup -v`，确认新增接线缺口产生预期失败。
- [ ] 完成最小启动接线，在认证成功后暴露页面模块能力（只包含registry状态/启用布尔，不含数据库信息）；禁用模块时导航隐藏，直接路由显示未配置。若无需新增能力接口，扩展现有登录/me响应的`capabilities.registry`，保持原字段并补auth测试；此字段只决定展示，不代替API权限。运行角色探测失败没有service对象，无法写入。

```python
from backend.registry.runtime import build_registry_runtime
from backend.registry.api import RegistryApi

# In create_server, after the existing authentication service is constructed:
server.registry = registry_runtime if registry_runtime is not None else build_registry_runtime(center_config_path)
# At the start of do_GET/do_POST respectively:
if RegistryApi(self).get(self.path):
    return
if RegistryApi(self).post(self.path):
    return
```

GET/POST片段分别插入对应函数，不能在一个函数内连续调用两个分派。`build_registry_runtime(None)`必须返回not_configured，不能尝试读取默认私密路径。

- [ ] 真HTTP闭环按顺序验证：空台账→创建井/设备→未上报仍可查→绑定→保存两版点表→审核最新→关联→人工依据启用→原始数据核查→迁井自动停用且清空期望→重新关联/审核启用→退役→旧数据历史仍可查。每一步记录返回id/version/revision，版本不可数值化；插入样本只在隔离PG，由测试迁移身份/中心写入器完成。同时检验只读账号、audit失败回滚和commit后断线重试；未知pair原始入库依然成功，registry不会影响MQTT接收。
- [ ] 部署文档写明：当前软件边界、三身份矩阵、连接/TLS引用、Origin、阈值口径、显式迁移/check命令、schema摘要检查、空目录检查、模块故障恢复、禁用回退不删数据。示例命令：

```powershell
.venv/Scripts/python.exe -m backend.registry.migrate --config config/registry-migrator.local.toml --expected-database oilwell_platform --writer-role oilwell_registry_writer --reader-role oilwell_query --check
.venv/Scripts/python.exe -m backend.registry.migrate --config config/registry-migrator.local.toml --expected-database oilwell_platform --writer-role oilwell_registry_writer --reader-role oilwell_query
```

执行前先确认实际连接目标/已有角色/迁移权限，命令中的正式库名不能用于测试重置。只在已核验本机开发生产服务范围内实施新schema和授权；不存在所需管理员凭据时交付迁移包并明确未部署，不从私人文件搜索凭据或提升MQTT入库账号权限。`--check`在未迁移时预期明确schema_missing，不把它当已部署。
- [ ] 先测试库完整迁移和双连接/最小权限测试，再评估是否具备本机部署前提；正式库新schema初始wells/devices均0，public表不作测试插入。受控更新本机开发服务前先检查实例归属和当前连接，记录启动/恢复方法；只重启需要加载新代码的应用服务，不重启Broker/PG，不影响冻结演示目录。健康检查失败按禁用模块回退，不清空schema。
- [ ] 运行最终验证并读取退出码/完整汇总，报告准确pass/fail/skip及耗时，不复用旧418测试结果：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p "test_registry_*.py" -v
.venv/Scripts/python.exe -m unittest discover -s tests -v
git diff --check
git rev-parse "demo-v2.0-20260912^{commit}"
git status --short
```

需要后台长测试时使用非阻塞会话并持续向用户通报，每次等待不超过60秒。保存新日志在忽略的logs目录；检查独立demo标签仍为`d392c831e45e9273790d1c250950853966f5b709`，冻结目录和ZIP以实施前后文件清单/摘要对照而不是靠“没记得改”作证。用户scripts保持原状态。
- [ ] 按选定执行方式进行独立代码审查，再对审查修复执行对应红/绿测试和最终必要回归。没有审查结论、测试缺失或部署受阻均如实列出，不标整个平台生产可用。更新年度响应只写WP02实际完成项和证据，不把配置版本未确认、无实物等边界删除。提交明确产品接线/测试/文档路径，消息 `feat: verify and document production registry integration`；不push、不新增PR。

## 验证覆盖与交付门槛

| 设计验收项 | 责任任务 | 必须保留的证据 |
| --- | --- | --- |
| R01 空台账/未配置/故障 | 4、8、10、12 | 空库查询、runtime错误隔离、DOM空/故障区分 |
| R02 组合身份唯一 | 2、4、6 | 完整unique约束、并发登记、跨网关同名 |
| R03 登记与链路独立 | 6、7、10 | active且从未上报、晚到/离线各状态 |
| R04 状态及启用条件 | 5、6、9、11 | 4×4状态表、无依据拒绝、retired只读 |
| R05 微秒边界与补传 | 6、7、12 | T−1µs/T/T+1µs实际PG归属 |
| R06 真实并发约束 | 2、5、6 | 排除/FK、同version冲突、archive/bind两会话 |
| R07 原子生命周期 | 3、6、12 | audit故障不遗留半条关系 |
| R08 快照与未确认 | 1、2、5、7、11 | canonical摘要、immutable、错误摘要、不二次scale |
| R09 权限与安全 | 8、9、10、11 | 5角色矩阵、真实HTTP、Origin/actor/越权拒绝 |
| R10 幂等与不确定提交 | 3、9、10、11、12 | commit之后断线、同key原结果、撤权403 |
| R11 审计/原始数据保护 | 2、3、8 | writer不能改删审计/遥测、reader不能写 |
| R12 历史/时钟/分页 | 3、6、7、11 | 时间倒退拒绝、未覆盖不补套、revision冲突 |
| R13 完整真实DB验证 | 2—9、12 | 非LIKE完整DDL、独立数据库、角色及并发 |
| R14 实际页面操作 | 10—12 | Edge DOM表单、真实HTTP闭环、失败状态 |
| R15 旧平台与冻结版保护 | 9、10、12 | 全量新日志、标签/目录摘要、旧API兼容 |

## 排期口径与执行方式

12项是可独立验收的开发单元，不等于12个小改动。预计净开发/验证工作量为 **6—9个工作日**：契约/迁移/事务1.5—2天，目录/点表/生命周期1.5—2天，查询/安全接口1—1.5天，页面闭环1—1.5天，集成/审查/部署1—2天。时间以工作量估算，不承诺自动持续运行到某一日；数据库身份或本机部署权限缺失单列等待，不吞进“完成率”。此估计只覆盖WP02，不包含后续波形、算法、硬件或现场试验。

建议 **Native：当前代理按任务逐项实现，阶段测试通过后继续，最后独立审查**。这些任务共享时效/版本/权限契约，保持一个实施上下文更便于衔接。也可选 **Subagent-driven：每任务独立实施者与审查者**，每步复核更密集但消耗更多资源。用户审阅本计划并选定方式后才进入代码实施。

## 计划自查记录（不属于产品测试结果）

- 已将设计14节和R01—R15映射到上述文件/任务；未增加硬件完成、现场在线或终端版本确认声明。
- 五项Review Focus分别有对应失败测试及所属实现任务；真实数据库与真实DOM测试分开，不相互冒充。
- 所有版本在API/页面为字符串；device UUID与线上device_id在SQL命名中明确区分；所有写操作先鉴权再访问回执。
- 模块关闭、数据库不可用、没有设备均诚实显示；没有从演示库导入生产目录的步骤。
- 本文件只定义未来要运行的测试和提交点。当前计划编写不执行这些迁移、测试数据插入或服务重启。
