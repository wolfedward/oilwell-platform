# 同步波形厂家接口 OWP-WAVE/1.0

本文件描述已实现并在软件向量中核验的报文合同，供厂家开发采集与上传适配器。它不是设备接入、同步时钟、校准、现场精度或生产部署的验收证明。当前公开示例 `waveform-example.owpwave` 完全是 **synthetic**；不得改称现场实测。

## 两种输入与运行边界

在线：网关以 QoS 1、非 retained 向 `{prefix}/v2/{gateway_id}/{device_id}/waveform` 发送一个 `kind=manifest` 和全部 `kind=chunk` 原始 UTF-8 JSON 报文。完整 MQTT packet、会话、单网关额度及 ACL 还须在目标 Broker 核验。接收端独立回执主题为 `{prefix}/v2/{gateway_id}/{device_id}/waveform-ack`，schema 为 `OWP-WAVE-ACK/1.0`；旧 v1 遥测和 `OWP-INGEST-ACK/1.0` 继续按原合同运行。

离线：将同一组**原始报文字节**按一个 manifest、全部 chunk 各一份排列成 `.owpwave`；每行只用单个 LF 分隔，最后一行可有或没有 LF。禁止 BOM、CRLF、空行、ZIP 包和多行 JSON。文件上限 `33,554,432 + 513` 字节。导入走授权的 HTTP 保存/完成状态，没有由离线导入自动产生的 MQTT 回执；若原 capture 已离线完成，再上线重发同一 manifest，系统可登记此在线请求并通过既有回执链返回最终状态，原完成事实与字节摘要不变。混合传输只补齐精确缺片，不改变来源真实性或质量结论。

下载 `validate_waveform.py` 与相邻五个 `waveform_*.py` 后，可在独立 Python 3.13 环境运行：

```text
python -I validate_waveform.py --input waveform-example.owpwave
```

退出码 0 返回 `ok=true`、原文件 SHA-256、原 manifest SHA-256、原文件字节数、末尾 LF、行数和 capture UUID；退出码 2 返回 `ok=false` 与 `reason_code`。它只读取指定本地文件及相邻的五个公开纯模块，不连接项目数据库。文件名须以 `.owpwave` 结尾。`waveform.schema.json`、`waveform-ack.schema.json` 供结构编辑器提示；**最终有效性以这个同源原始字节校验器为准**。JSON Schema 无法单独保证重复键、数字原词法、UTF-8 字节、base64 规范写法、摘要、跨片布局和总量限制。

## 报文结构与上限

两个报文都必须且仅包含 `schema=OWP-WAVE/1.0`、`kind`、`gateway_id`、`device_id`、`capture_id` 与各自下表字段。`gateway_id`、`device_id`、流/通道/时钟标签均为 1—128 字符，首字符字母或数字，其余仅 ASCII 字母数字、`_ . : -`；UUID 与 SHA-256 的具体写法见 schema，中心校验器最终判定。原始 JSON 单报文中不得出现字面 CR/LF；字符串里的 JSON 转义仍须符合字段规则。最大嵌套深度 16；重复键、未知/缺失字段、非法 UTF-8、非有限数值及布尔冒充整数均拒绝。

| manifest 独有字段 | 类型和含义 |
| --- | --- |
| `boot_id` | UUID；须与流 `source.boot_id` 对应实际申报来源。 |
| `capture_kind` / `source_kind` | `cycle` 或 `event`；来源 `synthetic`、`bench_measurement`、`field_measurement` 或 `unknown`。自报来源不证明物理来源。 |
| `producer` | 仅 `manufacturer`、`recorder_version`，各 1—128 字符。 |
| `source_evidence` / `sync_evidence_id` | 中心证据 UUID 或 null；空缺与上传方式均不表示已经核验同步。 |
| `utc_start` / `utc_uncertainty_ns` | UTC 微秒 `YYYY-MM-DDTHH:MM:SS.ffffffZ` 或 null；非负 i64 十进制字符串或 null。声明时间与中心收件时间有别。 |
| `clock_domain_id` / `window_ns` | 时钟域标签；正的 i64 ns 十进制字符串。窗口跨度最多 60 s。 |
| `cycle` | `event` 可 null；`cycle` 必填对象：`cycle_id` UUID；`start_ns`、`turn_ns`、`end_ns` 为 i64 十进制字符串且严格递增；`direction` 为 `clockwise`、`counterclockwise`、`unknown`；`zero_reference` 为 1—128 字符；`reference_channel` 为通道标签；`reference_events` 恰有 start/turn/end 三条，各含 `stream_id`、`event` 与非负 i64 `sample_index` 字符串或 null。引用须落在实际流/通道及样本范围。 |
| `streams` | 1—8 个流；合计最多 32 通道、512 chunk、16 MiB 解码数据。流字段详见下表。 |

每个流必须且仅含以下字段：`stream_id` 标签；`source` 为精确三字段 `gateway_id`、`device_id`、`boot_id`；`clock_domain_id` 标签；`rate_num`、`rate_den` 为正整数且约分，分母最多 1,000,000，采样率 1—128,000 S/s；`first_sample_index` 为非负 i64 十进制字符串；`offset_ns` 为 i64 十进制字符串；`frame_count`、`frame_bytes`、`chunk_frames`、`chunk_count`、`decoded_bytes` 为正整数；`data_sha256` 为整流解码字节摘要；`channels` 为 1—32 个对象。流内通道标签不得重复。`frame_bytes=5×通道数`、`decoded_bytes=frame_count×frame_bytes`、`chunk_count=ceil(frame_count/chunk_frames)`，每片须为整帧。所有这些数值还须同时满足单 capture 原始报文字节最多 32 MiB、总采样时间/窗口及 i64 边界。

每个通道只含 `channel_id`、`quantity`、`unit`、`encoding`、`measurement_source`、`signal_kind`、`config_id`、`calibration_id`。quantity 最多 128 字符，unit 最多 32；编码 `i32le` 或 `f32le`。`measurement_source` 为 `sensor`、`derived`、`synthetic`、`unknown`，`signal_kind` 为 `instantaneous`、`rms`、`window_mean`、`derived`。`config_id` 是可 null 的厂家不透明标签，**不是**中心配置 UUID；`calibration_id` 是可 null 的中心 UUID。null 配置可以保留原文，不等于配置合格；非 null 型别错误会拒绝。中心配置版本、报表标签以及校准 ID、SHA-256、通道绑定由后续中心证据管理，不能从厂家标签推断。

每个样本按通道顺序写 4 字节数值 + 1 字节质量：`0=good`、`1=missing`、`2=clipped`、`3=bad`、`4=uncertain`。missing 的数值四字节必须全零且不能解释为观测值；f32 非有限值拒绝。片 `chunk` 必须且仅含 `stream_id`、`chunk_index`、`sample_start`、`sample_count`、`manifest_sha256`、`data_sha256`、`data_base64`，另加公共五字段。片的引用、索引、帧数、整帧字节、规范 base64 与摘要均须和 manifest/全流一致。manifest 精确报文字节不超过 65,536；chunk 精确报文字节不超过 262,144，解码数据不超过 131,072；隔离单报文硬上限同为 262,144。`chunk_index`、`sample_start` 和其他计数是 JSON **整数 token**，范围 0—2,147,483,647（需要正值处从 1 起）；JSON 整数 token `-0` 在允许零的计数字段与中心现行语法一致，但 `0.0`、`0e0` 不行。全部绝对样本序号与 ns 是规范 i64 **字符串**，不接受 `"-0"`。勿用浮点或重新序列化覆盖原始字节/摘要。

容量限制必须**同时**满足。例如 128,000 S/s、单组 32 通道时，16 MiB 只容 **104,857** 帧（16,777,120 字节），首末样本跨度 0.8191875 s；6 通道时为 **559,240** 帧（16,777,200 字节），跨度 4.3690546875 s。以上仅是分配算例，不是推荐现场采样率或抽油周期窗口；不得截短所需物理周期以通过校验。显示端每通道原始窗口最多 20,000 帧、摘要最多 2,000 点；悬停两位小数仅用于显示，原码、序号、时间不能截断。

## 四种确认与重试

独立回执报文最多 4 KiB，字段准确为 `schema, receipt_id, gateway_id, device_id, capture_id, scope, manifest_sha256, payload_sha256, stream_id, chunk_index, status, reason_code, recorded_at`；null 字段也要出现。`receipt_id` 和 `capture_id` 为 UUID，`recorded_at` 为 UTC 微秒。`scope=message` 的 `stored` 表示该原报文被持久保存，`quarantined` 表示原文被持久隔离，二者均绑定 **payload_sha256**；`scope=capture` 的 `complete` 表示收齐并封存，`invalid` 表示该 capture 被不可变地判为无效，二者绑定 **manifest_sha256**。`complete` 不表示质量合格、来源可信、校准有效或两个来源同步。`stored/complete` 的 reason_code 为 `accepted`；`quarantined/invalid` 使用合同限定的永久原因码。不能把 MQTT PUBACK 当应用确认，也不能把旧 ingest ACK 用于波形。

丢失 `stored` 确认时，保留同一主题、capture_id、manifest 原始字节及 SHA-256，重发同一报文；缺片按其原始字节与片摘要重试。收到 `quarantined` 或最终 `invalid` 时核查 reason_code，修正后的不同内容必须使用新的 capture_id，不能用同一身份覆盖原始事实。超时、中心暂不可用或事务结果不确定时先查询/等待并保留原文，不把未收到确认解释为可改写；重试可能积累在持久队列。超过 24 h 没有新增有效片的 collecting 只产生 `stalled` 派生告警，晚到合法原片仍可恢复，不自动删除。一个 manifest 预占 32 MiB，封存释放未用预占；单事实最低计费 1 KiB，网关额度及全局容量由中心部署配置承担。

原 capture、原文件、规范导出各有独立的 SHA-256 与来源语义；规范导出不能冒充原上传文件。研究/训练/证据导出意图与数据集 `raw_archive`、`software_pair_test`、`field_pair_input` 用途不同。原始留存、历史井号归属、当前门禁、质量评估、独立验证均为分开的证据。同步 ADC 必须由具体硬件时基与校准材料支持；普通 Modbus TCP/RTU 只读轮询不能变为“同步采集”。此协议不开放远程启停、写寄存器、参数下发、OTA 或保护输出。
