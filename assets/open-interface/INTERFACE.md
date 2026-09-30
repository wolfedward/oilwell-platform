# OWP-EDGE/1.0 井场设备开放接入标准

版本：OWP-EDGE/1.0 + OWP-INGEST-ACK/1.0 + OWP-WAVE/1.0；更新：2026-10-01。v1 标量格式和原 ingest ACK 不变；同步波形有独立报文与回执。本文是本项目的接口约定，不是国家、行业或认证标准；软件实现不等于已部署或经过现场验证。示例是协议测试向量，不是现场记录。

## 1. 能力与适用边界

| 能力 | 当前状态 | 对接方式 |
| --- | --- | --- |
| Modbus TCP/RTU 读取标量 | 已实现 | 平台网关读取 FC03/FC04，点表可配置 |
| MQTT 5 标量上报 | 已实现 | 终端直接上报，或经平台网关转发 |
| PostgreSQL 幂等入库、历史查询 | 已实现 | 原始报文、测量/接收时间和来源留存 |
| 设备登记、时效井号绑定、点表版本 | 已实现软件管理 | 人工审核；不自动认领井号，不自动下发点表；Broker 身份另行配置 |
| 同步波形上行、独立回执与离线文件 | 软件接口已实现，默认关闭 | 见 WAVEFORM.md；不能向 v1 塞入数组或新字段；同步硬件和现场配对尚未验证 |
| 应用层逐报文持久保存回执、异常隔离 | 已实现，可选启用 | 显式迁移、独立身份、逐网关允许列表；见第 8 节 |
| 固件升级、远程启停、自动保护 | 未开放 | 当前接口不得写寄存器，不接执行线圈 |

本地软件虚拟终端使用的旧二进制接口不是本标准的厂家入口。不得把仿真身份自动登记为真实设备。`source` 为来源声明而非可信证明；准入需结合受控身份和现场核验。

## 2. 两种设备接入方式

1. **标准 Modbus 终端 + 平台网关（首选）**：厂家提供寄存器说明、读写属性、量程、单位、字节/字顺序及校准报告；网关负责轮询、换算、缓存和 MQTT。
2. **自主 MQTT 终端**：厂家实现本报文、TLS、唯一身份、持久缓存与重发；必须通过相同协议测试。设备不依赖特定 DSP/MCU 品牌。

两者共用中心数据模型。禁止“读到任何设备就自动上线”；设备台账、井号绑定与点表由授权人员登记，Broker 身份和 ACL 仍须独立配置。

## 3. MQTT 传输与身份

- 协议 MQTT 5.0；遥测 QoS=1；Retain=false。公网/跨主机连接必须使用 TLS 与服务端证书验证；部署建议端口 8883，端口不是身份凭据。
- 每网关独立 `client_id`、凭据/客户端证书；禁止共用万能密码。凭据不放在示例、点表、网页或 Git 中。
- 命名空间：`{prefix}/v1/{gateway_id}/{device_id}/telemetry`，默认 prefix=`oilfield`。prefix 最多 64 字符；其余标识最多 128 字符，允许 ASCII 字母、数字、下划线、点、冒号、连字符且首字符须字母或数字。
- 上报身份须与主题一致。中心并不能仅凭报文证明设备身份；Broker ACL 必须限制账号可发布的网关和终端路径。一个设备的账号不得发布其它井设备的数据。
- 当前参考 ACL 允许某网关下任意终端；生产部署应按登记台账枚举到具体终端，或保证该网关只连接受控终端。跨网关冒用必须被拒绝。
- 中心消费者保持稳定 client_id、Clean Start=false、默认会话保留 7 天。Broker 必须持久化并配置磁盘容量、保留期和告警；会话过期或 Broker 丢失持久状态仍会导致待确认数据丢失。
- 网关与 Broker 的 PUBACK 只代表该 MQTT 跳的交付。默认旧 broker 模式只记录 Broker 确认；显式 application 模式在收到匹配的持久回执前保留待确认队列。中心在事务提交后才确认消费消息。新模式支持重投去重，不宣称全链路 exactly-once。

依据：[OASIS MQTT 5.0](https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html)。本项目 JSON 不是 Sparkplug；未实现 Sparkplug birth/death、metric alias 或 protobuf，不能标记 Sparkplug 兼容。

## 4. 报文字段与一致性

UTF-8 JSON，总长度不超过 1,048,576 字节，每报文 1—5000 个标量样本。不允许重复 JSON 键、NaN、Infinity、未知字段。结构见 `telemetry.schema.json`，完整例子见 `telemetry-example.json`。

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| schema | 字符串 | 固定 `oilwell.telemetry.v1` |
| message_id | 标识符 | 全平台唯一，推荐 UUID；重发必须原样复用 |
| gateway_id / device_id | 标识符 | 与主题逐字一致；大小写敏感 |
| boot_id | 标识符 | 每次网关启动新建，重发历史消息保留旧 boot_id |
| created_at | UTC 时间 | 报文创建时间，RFC3339 秒/最多 6 位小数，Z 或 +00:00 |
| sequence | 对象 | first/last 与首尾样本序号相等 |
| samples | 数组 | 1—5000 行，序号非递减；同采集周期多测点可同序号 |

每行必须包含 `point, measured_at, sequence, value, quality, unit, source`，不得少字段。value 是换算后的有限工程值，不是原始寄存器；unit 是 1—32 个 Unicode 码点的单位字符串或 null，不允许 NUL (U+0000) 或孤立代理码点 (U+D800—U+DFFF)，以避免 PostgreSQL jsonb 无法入库；合法的代理对应解码成对应的 Unicode 码点。单位为空不代表无量纲，须在点表解释。quality 只允许 good/uncertain/bad。source 常用 modbus-tcp、modbus-rtu、device-adc；仿真应保留虚拟来源标识。约束依据：[PostgreSQL JSON 类型](https://www.postgresql.org/docs/current/datatype-json.html)。

sequence 范围 0—9223372036854775807，不接受布尔值；上报为 JSON 整数。JavaScript 厂家实现不得经 Number 损坏大整数；查询接口将序号返回十进制字符串。单个采集周期加 1；不同 boot_id 不连算丢包。

measured_at 必须表达实际采样/快照时刻。现有通用 Modbus 网关采用轮询开始时间，不代表 ADC 硬件时间戳，也不能证明多次读取同相位同步。需要同步功图时必须使用后续周期波形协议。缺测不得补造 good=0；保留本地通信错误，等待后续质量事件协议补齐故障上行。

重发必须使用**相同字节**。中心按 message_id 和原始字节 SHA-256 检查幂等；重新排键或改时间可能触发冲突。SHA-256 只用于完整性/冲突识别，不是设备数字签名。启用可靠接收后，可保全的冲突或畸形消息在受限隔离区提交原文/原因后才确认；数据库故障、配额耗尽或超过 2 MiB 硬上限不确认。旧模式保持原拒绝行为，不能无条件灌入生产主题。

工程值按 IEEE754 binary64 使用，允许正常舍入但禁止溢出为无穷或非零数下溢成零。报文原始数字还须能保存为 PostgreSQL jsonb 数值：十进制小数位最多 16383 位、整数位最多 131072 位，禁止使用极端指数拼写零来绕过范围检查；建议只发送传感器有效精度内的简洁数值。序号另按 64 位整数检查，不经过浮点转换。JSON Schema 不能检查解析前的数字拼写，必须同时运行离线校验器。依据：[PostgreSQL 数值类型](https://www.postgresql.org/docs/current/datatype-numeric.html)。

## 5. Modbus 基础映射

- 当前支持 TCP 和 RTU；只读功能码 03/04，每批最多 125 个 16 位寄存器。
- 地址用 **0 起算 PDU 地址**；不要把说明书的 30001/40001 直接当 address。Unit ID 为 1—247。
- 支持 uint16/int16/uint32/int32/float32/float64；byte_order 与 word_order 必须显式约定。float32 big/big 表示 IEEE754 的高字先、高字节先，例如 5.8 的编码须按实际工具打包，不能人工四舍五入寄存器字。
- 工程值 = 原始值 × scale + offset。超出配置工程范围为 uncertain；无响应不生成假的成功样本。
- 参考新终端采用 RTU 19200、8E1，现场可配置。当前软件 RTU 默认是 8N1，厂家接入必须显式填写 parity/stopbits，不能依赖默认值。
- Modbus 本身不提供当前使用路径的加密认证；仅在受控 OT 网段/隔离串口使用，不将 502 端口暴露到互联网。

下面是**供新设备选择的参考点表**，不是所有厂家必须修改固件采用的固定地址；实际地址通过配置适配。全部为 FC04、float32、big/big、scale=1、offset=0，单值占两个寄存器。是否具备某测点须按设备能力登记。

| PDU 地址 | point | unit | 含义 |
| ---: | --- | --- | --- |
| 0 | motor.active_power | kW | 三相总有功功率，发电回馈时允许负值 |
| 2 | motor.current | A | 三相 RMS 电流算术平均，明确不是单相瞬时电流 |
| 4 / 6 / 8 | motor.voltage_a / _b / _c | V | 各相对中性点 RMS；无中性点设备另用 line_voltage_ab 等点名 |
| 10 / 12 / 14 | motor.current_a / _b / _c | A | 各相 RMS 电流 |
| 16 | motor.frequency | Hz | 电气基频，不是抽油冲次 |
| 18 | motor.speed | rpm | 测得机械转速；估算值须另命名 speed_estimated |
| 20 | rod.load | kN | 光杆载荷，含测点标定方向 |
| 22 | rod.displacement | m | 光杆位移，以校准零位为基准 |
| 24 | pump.stroke_rate | min^-1 | 每分钟完整往复次数 |
| 26 | cabinet.temperature | degC | 指定位置柜内温度 |

依据：[Modbus 官方规范入口](https://www.modbus.org/modbus-specifications)。终端快照一致性、跨块冻结和设备时间寄存器仍需在下一版驱动中实现；仅分配地址不构成同步采样能力。

## 6. 厂家自测与入场验收

下载本目录文件，在无平台依赖的 Python 3.10+ 环境运行：

```text
python validate_telemetry.py --topic oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry --file telemetry-example.json
```

成功退出码 0，失败退出码 2；工具不联网、不读取平台凭据、不下发指令。JSON Schema 验证结构，工具补充主题一致性、字节上限、重复键和序号关系检查。2026-09-23 已将本 profile 的重复键、UTC 格式、64 位序号、单位 Unicode 和数字范围校验补齐到中心解析入口，并使用共同测试向量及真实 PostgreSQL 临时表验证。主题和 schema 不变；此前不合规但被宽松解析接受的报文现在会明确拒绝。工具通过仍不等于硬件合格、设备获准接入或所有安全边界均已完成。

| 编号 | 验收项 | 必须观察到的结果 |
| --- | --- | --- |
| P01 | 身份/主题错误 | 无权账号被 Broker 拒绝；报文/主题不符不进入遥测表 |
| P02 | 断网后恢复 | 同 message_id、同字节补发，可查原始测量时间 |
| P03 | 重复消息 | 不重复累计入库；冲突 ID 明确拒绝 |
| P04 | 非法类型/超限/异常值 | 不产生半条遥测；可靠模式在容量内持久隔离原文；不可关联输入无应用回执，超硬限/数据库失败不确认 |
| P05 | 字节序/缩放 | 标准源输入和结果一致，覆盖正负、零点、上下量程 |
| P06 | 时钟漂移/重启 | 标识异常；boot_id 更新，不虚构在线或连续序号 |
| P07 | Broker/数据库异常 | 保留未确认消息，恢复后查询核对；测试持久化重启场景 |
| P08 | 满磁盘/缓存满 | 明确告警、确定背压/丢弃策略，不静默丢数 |
| P09 | 成对功图 | 同周期、同时间基准与标定信息，不能以 v1 标量代替 |
| P10 | 电气/安全 | 厂家提供绝缘、EMC、环境、危险区适用性和保护验证材料 |

厂家交付设备说明、版本、点表、校准记录、协议测试报告、升级/回退方案及故障响应负责人。平台方另行批准生产准入；协议自测不自动颁发生态认证。

## 7. 开放范围

本接入包可离线复制；平台通过 `/assets/open-interface/index.html` 提供无账号的文档/示例下载，不公开配置、口令、测点数据或管理 API。当前 HTTP 服务仍只监听本机；本页存在不等于互联网已发布。部署对外文档门户需另配 HTTPS 与访问策略。

本目录原创材料的许可见 LICENSE.txt。第三方芯片原理图、商标、固件与标准文本没有复制进包，不属于本项目许可范围。

## 8. OWP-INGEST-ACK/1.0 应用回执

这是在 v1 遥测之上的**可选软件能力**。未部署可靠接收、未完成队列迁移或未列入中心允许列表的网关，不得切换 application 模式。旧报文仍使用原 v1 主题和字段。

回执主题为 `{prefix}/v2/{gateway_id}/{device_id}/ingest-ack`，MQTT 5、QoS=1、Retain=false；回执发布身份与遥测接收身份分离。网关仅可读自己的回执，不能写任何回执；发布器仅可写回执，不能写遥测；中心接收账号只读遥测。按网关配置的 `+` 终端权限须结合台账细化。

### 8.1 九个字符串字段

所有字段必填，禁止额外字段和重复键，总回执不超过 4096 UTF-8 字节。见 `ingest-ack.schema.json` 与 `ingest-ack-example.json`。

| 字段 | 约束 |
| --- | --- |
| schema | 固定 `OWP-INGEST-ACK/1.0` |
| receipt_id | 带连字符 UUID，重复发送保持原值 |
| gateway_id / device_id / message_id | 与本地原报文、主题逐字一致，标识规则同 v1 |
| payload_sha256 | 原始遥测完整字节的 64 位小写十六进制摘要；不能重新序列化 JSON 再计算 |
| status | `stored` 或 `quarantined` |
| reason_code | stored 只能为 `accepted`；quarantined 只能为下述固定原因 |
| recorded_at | 中心首次持久保存时刻，UTC，固定 6 位小数，Z 或 +00:00；重发不换时间 |

隔离原因：`topic_invalid`、`payload_too_large`、`utf8_invalid`、`json_invalid`、`json_duplicate_key`、`contract_invalid`、`identity_mismatch`、`idempotency_conflict`、`qos_not_supported`、`retained_publish`。原因词典完整不代表每种输入都有安全回执地址。

`stored` **只表示中心已持久保存合法报文和标量样本**，不证明设备已登记、井号已绑定、数据质量良好、测点已被认可或业务算法已使用。未登记、坏质量及点表不匹配的合法遥测仍保留原始记录，不能丢弃或改成 good。`quarantined` 表示只持久保全拒绝事实，不计作正常遥测样本；人工关单不把隔离报文变为有效数据，也不改变原回执。

### 8.2 关联、重试与无法回执的例外

只对允许列表网关、合法 v1 主题、整份无歧义 JSON 中可安全读取且匹配的 schema/三项身份产生回执。坏 UTF-8、重复键、坏 JSON、身份冲突或非法主题等无法安全关联时，即便中心已隔离，也**没有应用回执**；不能从半段文本猜回复地址。应用队列对此持续保留并告警，由运维核查。禁止因迟迟没收到回执就伪造成功。

网关先订阅自己的回执再发布；收到回执必须检查完整格式、主题/网关/设备/message_id 和原始字节摘要，再事务保存回执与终态。错误、未知或 retained 回执不完成本地队列。没有 ack-of-ack：中心的“已交 Broker”不代表“网关已确认”。中心补发保持同一原回执，网关重复收到不重复处理。人工操作须带原因、版本及幂等键，并独立留审计记录。

启用步骤：数据库备份及独立迁移 → 四类运行身份和 ACL → 中心逐网关允许列表 → 网关离线备份及 v2 队列迁移 → 验证真实断连/恢复 → 逐台开启 application。迁移保留历史 broker 确认含义，不能把旧已发送记录改标为 stored。回退时保全新队列与回执，旧程序不得直接打开 v2 队列。配额或磁盘不足明确背压，不静默清除待确认记录。

### 8.3 厂家离线核验与大小限制

```text
python validate_ingest_ack.py --topic oilfield/v2/GW-EXAMPLE/D-EXAMPLE/ingest-ack --file ingest-ack-example.json --telemetry-topic oilfield/v1/GW-EXAMPLE/D-EXAMPLE/telemetry --telemetry-file telemetry-example.json
```

Python 3.10+ 标准库、无需平台源码，成功退出 0，失败退出 2。输入必须是实际发送的原始文件；修改空白/换行会改变摘要。校验器只做离线合同与关联检查，不认证发送者、不连接网络。JSON Schema 不能代替重复键、精确字节、主题和真实日历时间校验。

v1 合法 payload 上限为 1,048,576 字节；中心原文隔离硬上限为 2,097,152 字节，回执上限 4096 字节。参考 Broker 模板设置 `max_packet_size 2097152`（完整 MQTT 包，含主题/头/属性）及 `message_size_limit 2097152`（仅 payload）；因此不是所有恰好 2 MiB 的 payload 都能通过 Broker。超过完整包上限会断开，不会进入中心。payload 限额拒绝也不能用 QoS 握手推断保存成功。依据：[Mosquitto 官方配置说明](https://mosquitto.org/man/mosquitto-conf-5.html)。

兼容性实测边界：Mosquitto 2.1.2 在人为调低 payload 限额后返回过 PUBACK 原因 0x95，Paho 2.1.0 拒绝此包/原因组合；MQTT 3.1.1 则可能成功握手但不转发。不要单独调低 payload 限额并假定客户端通用兼容。部署须测试锁定的 Broker/客户端版本；模板的完整包上限先于同值 payload 上限生效。此记录不是第三方兼容性认证。
