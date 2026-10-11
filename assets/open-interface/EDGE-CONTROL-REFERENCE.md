# DSP + MCU 软件参考与厂家对照门

2026-10-11 · 可执行 Python 参考模型 / 非固件 / 输出始终禁用。

`edge-reference-state.svg` 是可编辑状态参考图；`edge_protection_reference.py`、`protection_replay.py`、`protection_contracts.py` 是同源、独立可导入的标准库参考代码。接入包不是已验证的保护继电器，不含 BSP、启动程序、ADC/DMA 驱动、最终原理图/PCB 或硬件认证。主平台中的同源函数为 `backend.gateway.protection_reference.EdgeProtectionReference` 与 `backend.studies.protection.ProtectionReplay`。

## 职责与信息流

| 环节 | 必须保留的证据 | 故障行为 |
| --- | --- | --- |
| DSP 共享时钟采样 | boot_id、单调序号、UTC 时间、瞬时波形、质量、削顶、标定和抗混叠依据 | 缺采样/未知质量不得补零或宣称正常 |
| DSP 统计窗口 | 三相 RMS 单位、窗口时间、温度/转速/频率、显式设置版本 | 必需通道缺失则无有效判据 |
| MCU 本地证据适配 | 整帧校验、保护锁定、私有检查点、写前不确定标记 | 看门狗、损坏、写入失败或重启保持 shadow_inhibit |
| MCU 上行缓存 | 原始字节、消息 ID、配额、匹配应用回执 | Broker ACK 不等于中心入库；网络恢复重发原字节 |
| 独立硬件保护链 | 厂家定值、接线联锁和动作回读 | 不依赖 MCU、网络、网页或本参考程序 |

参考状态/检查点 JSON 是**本地软件接口**，不是新发布的 MQTT 上行主题。厂家上传仍遵循 INTERFACE.md / WAVEFORM.md，不得将未知字段混入现有 schema。本轮无远程启停、定值下发、OTA 或 Modbus 写接口。

## 设置与逐帧合同

构造 `ProtectionReplay(settings)`：version、basis_reference、approved_by、effective_from/until（UTC）、maximum_frame_gap_ms、reset_safe_ms，以及 1—16 条 `{id,metric,direction,pickup,dropout,delay_ms}`。所有设置必须显式提供；basis/approved_by 是提交者声明，不替代现场保护配合审批。高阈值 pickup 大于 dropout，低阈值反之。

支持的 metric 为 current_max_A、voltage_min_V、voltage_max_V、current_deviation_ratio、voltage_deviation_ratio、temperature_C、speed_rpm、frequency_hz。三相偏差为最大绝对偏离均值除以均值，不是负序分量。无热模型、无厂家电机热容量推定。

`step(frame)` 必填 at（UTC）、sequence、boot_id、quality（valid/invalid/unknown）、configuration_valid、hardware_trip、reset_request（none/local）和 measurements。测量对象含 currents_A/voltages_V（三相数组或 null）、temperature_C、speed_rpm、frequency_hz；未使用的通道可 null，已配置判据必需的通道不可缺。

状态是 normal → pickup → would_trip_latched。只有连续有效实际时间计入延迟；坏质量、间断、重启、乱序、失效设置清除尚未成熟的计时但不解除锁定。复位需要本地请求、当前设置有效、没有硬件故障，且所有配置判据连续达到安全侧达到 reset_safe_ms。复核/确认不是复位。始终输出 hardware_execution=false、control_enabled=false。

## 持久适配器使用

1. 在**私有、本机、非网络盘**中准备父目录。Windows 使用部署账户独占 ACL；本参考不代替操作系统权限配置。显式调用 `EdgeProtectionReference.initialize(new_directory, settings, commissioning_reference='本地调试记录号')`，已有目录拒绝。
2. 用相同设置构造适配器；每个目录只允许一个持有操作系统锁的实例。启动始终保持禁用，检查点缺失/损坏/配置摘要不同/存在 pending.json 会拒绝启动，保留原文件供核对。
3. 本地 ADC 适配调用 `receive(frame)`；本地物理复位另需调用参数 `local_reset=True`，不能将网络消息直接映射成这一授权。
4. 当有新的有效安全帧后，`confirm_local_startup(actor, reason)` 仅解除**软件影子禁用**，不清除保护锁定，也不使能任何物理输出。`mark_unavailable('watchdog_timeout')` 清除连续时间和旧安全证据，进入禁用。
5. 每次接受帧先写并同步 pending 标记，再原子替换含摘要的检查点；成功后才删除本次标记。存储失败保留证据并禁用。调用 `close()` 释放独占锁。

SHA-256 是损坏检测，不是签名或防恶意修改。文件同步与原子替换不构成所有操作系统/控制器断电保证；厂家必须在目标 Flash/FRAM、电源保持和文件系统上验证。不确定恢复不得手工删除 pending 后宣称保护状态完整；需核对事件、独立硬件状态与原始检查点，保留故障包，重新执行经批准的本地调试流程。本 Python 模型没有硬实时/中断延迟保证。

## 可重复对照测试

解压完整接入包后，运行 `python -B check_edge_reference.py`，无需安装平台或第三方库。它在临时目录中使用固定日期的**软件测试定值**（不是当前现场定值）检查启动禁止、持续延时锁存、重启保留锁存、安全复位与看门狗、坏质量下的硬件输入优先级。成功返回 `status=passed`，失败非零退出；不连接设备或网络。代码内的 `settings()` / `frame()` 可作为厂家构造自己测试向量的格式参考，不可复制为保护定值。

厂家固件与同一输入参考输出对照：阈值相等、回差区、动作延迟、时间乱序/重复、boot 变化、缺相数据、设置过期/撤销、硬件输入在坏质量时锁定、不安全复位拒绝、重启保留锁定、存储满/中断写入保留 pending、双进程拒绝、看门狗后不得继续计时。参考实现的对应测试在平台 `tests/test_protection_replay.py` 与 `tests/test_edge_protection_reference.py`；它们验证软件行为，不是样机报告。

出厂还需 ADC/传感器相位与量值校准、跨通道延迟、抗混叠、温漂、绝缘/EMC、危险区适用性、实际保护动作与回读、断电寿命和现场试运行。参考器件与安全边界沿用 HARDWARE.md。方法参考 [ABB REM615 应用说明](https://library.e.abb.com/public/a491466747e31f17c1257b130056ca30/REM615ANSI_appl_259901_ENa.pdf)；本项目未复制定值或宣称达到其产品性能。
