# 可复现测试数据与隔离联调 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付能重新生成、能通过现有软件真实运行、能追溯输入和结果的仿真测试包与独立阅读入口。

**Architecture:** 离线生成器只负责输入与预先固定的期望，不调用被测内核制造答案。隔离运行器创建两套分别使用 review、monitoring 配置的新实例，通过真实 Modbus TCP、MQTT、数据库及 HTTP 验证现有能力；运行绑定数据与确定性原始包分别保存。阅读入口只展示已生成数据和实际运行结果，不实施控制。

**Tech Stack:** Python 3.13.2、unittest、标准库 JSON/CSV/struct/Decimal；复用 requirements-production.txt 的 paho-mqtt==2.1.0、pymodbus==3.15.0、psycopg[binary]==3.3.5，以及现有 PostgreSQL、Mosquitto、原生 JavaScript/SVG 和浏览器测试框架；不新增第三方依赖。

**Spec:** [已确认的交付方案](../specs/2026-10-19-simulation-and-hardware-delivery-design.md)，实施 A 包；B 硬件工程图纸和 C 生产页面接入另有计划，不计入本包完成度。核对基线为 `34be0d3`。

**Status:** 2026-10-19，实施计划待审阅；本文件中的测试、数据和服务均为后续实施项，不是已完成报告。Files列表中省略目录的文件沿用同列最近一个完整路径的父目录。

## Global Constraints

- 原 8765 演示版独立保留；不改动其源文件、账号或业务库。
- 现有 8770 工程审查版、8772 完整页面预览和 8774 监控工作台不灌入本批测试数据，不用其服务做故障注入。
- 生成数据保持仿真来源。虚拟井号不称为脱敏现场井号，模拟校准参数不称为检定证书，软件记录不称为硬件试验报告。
- 不发布远程启停、Modbus 写寄存器、保护定值远程下发或执行线圈接口。保护回放继续保持物理输出禁用。
- 数据包外置清单版本为 `owp.testpack/1`，与上行报文的 schema 分开。
- 包总量上限默认 256 MiB，不含实际运行日志与系统备份。
- 波形使用 `source_kind=synthetic`；合成通道使用 `measurement_source=synthetic`。配对用途保持 `software_pair_test`，不得借测试资料进入 `field_pair_input`。
- 小型周期默认 8 秒、128 Hz、含端点 1025 帧，四通道 i32le 加质量码共 20,500 字节解码数据；每片最多 256 帧。
- 小型电流案例为 16,384 点、1,024 Hz，16 秒，不进行未声明的补零或截短。
- 坐标提示保留两位小数，原始文件不因此降低精度。
- 所有故障注入限定本轮实例。未实际执行的检查保持 `not_run`；预期拒绝只有观察到约定拒绝才算通过。
- 每任务先跑新增失败测试，再实现、针对性验证、提交；关键集成点再跑关联回归。禁止将历史通过记录作为本次源码的通过证据。
- 实施前读取仓库指令并按 using-git-worktrees 检查隔离位置；复用合适的现有 linked worktree，不动主目录的无关 `scripts/`。只提交本任务文件，不自动推送 GitHub。

## Review Focus

1. Windows 中文路径、目录联接、符号链接、硬链接及已存在目标：不得覆盖或清理演示版、未知目录和实例。由任务 1、7 的路径与进程身份测试覆盖。
2. 修改后的数据包、重复 JSON 键、路径穿越与恶意说明文本：加载前拒绝摘要不符或越界路径，阅读页面只以文本显示。由任务 1、6、11 覆盖。
3. 固定历史时间早于本轮设备登记、跨日汇总及加速回放：原始仿真时间不变，网络采集使用实际时间并另存映射，不能产生未登记的历史归属或假长周期试验。由任务 4、8 覆盖。
4. 死点、非有限数、质量缺口、削顶、时移和容量超限：不补造有效零值、不连接缺口、不调宽阈值追求通过。由任务 2、3、6、9 覆盖。
5. 证据 UUID、复核版本或来源发生变化：不得重写已归档原文、借用旧复核状态或把软件来源升级为现场来源。由任务 9、10 覆盖。

## 交付结构与运行顺序

| 目录或文件 | 职责 |
| --- | --- |
| `tools/testpack/contracts.py`、`storage.py`、`config.py` | 类型、包清单、确定性序列化、路径与容量边界 |
| `tools/testpack/mechanics.py`、`cycles.py`、`currents.py` | 独立正向模型、周期及电流信号；不导入生产反演计算内核 |
| `tools/testpack/waveforms.py`、`history.py`、`study_inputs.py` | 现有合同编码、历史积分、研究输入和依赖模板 |
| `tools/testpack/build.py`、`validate.py`、`__main__.py` | 生成及离线核对命令，不启动服务 |
| `tools/testpack/lab.py`、`modbus_device.py`、`gateway_adapter.py` | 新实例所有权、只读软件设备、真实采集后来源标记 |
| `tools/testpack/telemetry_run.py`、`waveform_run.py`、`study_run.py` | 三条实际运行路径与运行绑定映射 |
| `tools/testpack/report.py`、`viewer.py`、`frontend/testpack/` | 分层报告、脱除秘密后的导出、只读阅读入口 |
| `config/testpack/smoke.json`、`extended.json` | 小型基准与扩展配置；不含真实账号、连接串或证书 |
| `tests/test_testpack_*.py`、`tests/fixtures/testpack_viewer.html` | 各模块的针对性测试及浏览器断言 |
| `docs/测试数据包与隔离联调使用说明.md` | 生成、校验、隔离运行、查看报告和失败排查说明 |

任务 1 → 2/3/4/5 → 6 形成第一份可复现离线包；任务 7 → 8/9/10 完成真实软件运行；任务 11 汇总交付。允许独立文件并行开发，但共享类型和公开接口先定版。小型基准通过后再执行扩展配置。

### 共享类型与文件约定

统一在 `contracts.py` 定义以下 frozen dataclass，参数名即跨任务接口，不另造同义类型：

- `PackConfig(profile: str, seed: int, start_utc: str, timezone: str, max_bytes: int)`；profile 为 `smoke|extended`，seed为0..2³²−1的整数且不能是bool，默认种子 `20261019`，起点 `2026-09-30T16:00:00.000000Z`，业务时区 `Asia/Shanghai`，`max_bytes=268435456`。
- `Artifact(path: str, media_type: str, content: bytes)`；路径为唯一的 POSIX 相对路径，拒绝绝对路径、反斜杠、冒号、空段、`.`、`..` 和 Windows 设备名。
- `Case(case_id: str, category: str, artifacts: tuple[Artifact, ...], facts: dict, expected: tuple[dict, ...])`；category 为 `mechanistic|signal_fault|shape_only|protocol_fault`。expected 元素固定为 `{check_id, layer, assertion, target, expected}`；layer 为 `offline|numeric|software_network|http|browser`，assertion 为 `equals|within|rejects|unchanged`，不接受可执行表达式。
- `Channel(role: str, unit: str, scale: str, offset: str, raw: tuple[int, ...], quality: tuple[int, ...])`；工程值为 `raw*scale+offset`，无效样本的 raw 按合同编码但不得用于积分。
- `Cycle(case_id: str, sample_rate_hz: int, time_ns: tuple[int, ...], channels: tuple[Channel, ...], turn_index: int, model: dict, diagnostics: dict)`；时间是相对仿真周期的精确整数纳秒，四通道顺序为功率、曲柄角、载荷、位移。
- `EvidenceBinding(sampling: dict, calibration: dict, origin_id: str | None, sync_id: str | None)`；前两个字典按通道 role 索引实际 API 返回的 `{id, content_sha256, review_version}`。离线编码传 `None`，不得伪造此对象。
- `PackIndex(root: Path, manifest: dict)`；只能由完成摘要和路径验证的加载器产生。
- `CheckResult(check_id: str, layer: str, status: str, expected: dict, actual: dict, artifacts: tuple[str, ...])`；status 仅 `passed|failed|not_run|blocked`，actual 仅含公开字段，不含原始认证响应。
- `LabHandle(root: Path, lab_id: str, monitoring_root: Path, review_root: Path, ports: dict)`；从所有权验证加载，不能仅用一个 URL 构造。

`owp.testpack/1` 清单固定字段为 `schema, pack_id, profile, generator, configuration, source, cases, artifacts`。generator 包含 `version, source_commit, source_sha256, python_version, rng`，version初值 `owp.testpack.generator/1`，source_sha256由排序后的tools/testpack与config/testpack生成源文件路径及内容摘要计算；source 固定 `{kind:"synthetic", description:"可复现软件测试输入，非现场测量"}`；每个 artifacts 项为 `path, media_type, size_bytes, sha256`；cases 项为 `case_id, category, facts, expected, artifact_paths`。禁止未知字段和重复键。

配置 JSON 字段为共享 PackConfig 的全部字段，另加 `schema:"owp.testpack.config/1"`。JSON 使用 UTF-8、固定键排序、紧凑分隔、禁止 NaN；文本文件统一 LF。`manifest.sha256` 是独立摘要文件，不加入 manifest.artifacts，避免循环摘要。包标识为规范化 `{configuration, generator}`（包含source_commit及运行版本）的SHA-256前24位加 `tp-`；清单保留完整指纹。包内输入只能引用包ID、配置摘要及其他先生成的输入摘要，不引用包含自身文件摘要的最终manifest SHA；最终manifest SHA只进入外置运行文件，避免另一类循环摘要。

固定随机算法 `sha256-counter/1`：对规范化的 `[seed,case_id,channel,counter]` 求摘要，前 8 字节按大端无符号整数映射到 `[0,1)`；量化采用 Decimal 的 ROUND_HALF_EVEN。UUID 使用 UUIDv5(NAMESPACE_URL, pack_id/case_id/purpose) 生成录波、周期及预生成报文身份，不用于伪造服务器证据 ID。锁定运行环境下比较确定性文件摘要，不要求不同 libm/平台生成的浮点模型字节相同。

输出分开保存为 `pack/`、`runs/<run_id>/private/` 和 `runs/<run_id>/public/`。生成配置不包含运行时间；run 清单包含实际 UTC 起止时间、软件提交号、输入摘要、实例标识、时间映射、逐检查结果及实际接收数量。无记录的检查为 `not_run`，不得用“进程正常退出”推导所有检查通过。

---

### Task 1: 确定性包合同与安全文件写入

**Files:** Create `tools/testpack/__init__.py`, `contracts.py`, `storage.py`, `config.py`；Create `config/testpack/smoke.json`, `extended.json`；Test `tests/test_testpack_contracts.py`。

**Interfaces:** Produces `parse_config(value: dict) -> PackConfig`, `canonical_bytes(value: object) -> bytes`, `derive_uniform(seed: int, case_id: str, channel: str, counter: int) -> float`, `write_pack(target: Path, config: PackConfig, cases: tuple[Case, ...], generator: dict) -> PackIndex`, `load_pack(root: Path) -> PackIndex`。write_pack 仅接收不存在且父目录存在的绝对目标；不提供覆盖和自动删除选项。

- [ ] **Step 1: 写失败测试。** `test_same_inputs_same_manifest_and_files` 断言两个新临时目录输出全部文件摘要相同；`test_changed_seed_changes_pack_identity` 断言 seed+1 改变包 ID；`test_target_ownership_and_member_paths` 对已有目录、联接、越界成员、硬链接和重复成员断言拒绝且原文件摘要不变；`test_nonfinite_duplicate_keys_and_total_budget` 对 NaN、重复 JSON 键、总量 268435457 字节的预估声明断言写入前拒绝，不分配同等大内存。

  关键测试体断言（first/second来自相同输入的write_pack结果，changed来自seed+1）：
  ```python
  self.assertEqual(first.manifest, second.manifest)
  self.assertNotEqual(first.manifest['pack_id'], changed.manifest['pack_id'])
  with self.assertRaises(ValueError):
      canonical_bytes({'bad': float('nan')})
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_contracts -v`；新增接口缺失应使新增测试失败，记录原因，不能把旧环境依赖错误算作红灯。
- [ ] **Step 3: 实现接口。** 先验证所有成员名、独占目标及大小，再以独占文件创建写入；部分失败保留当前本轮目录并标为 incomplete，不覆盖和清扫。`load_pack` 流式验证摘要、数量和路径；保留完整生成器指纹以及 Python 版本。
- [ ] **Step 4: 跑绿灯。** 同一命令须全部通过；在中文带空格的临时路径再执行该测试类。
- [ ] **Step 5: 提交。** 只添加本任务文件，提交 `feat: add deterministic testpack contracts and safe storage`。

### Task 2: 独立机械正向模型与八个周期

**Files:** Create `tools/testpack/mechanics.py`, `cycles.py`；Test `tests/test_testpack_mechanics.py`, `test_testpack_cycles.py`；Reference `tests/inversion_geometry_oracle.py`（只参考独立圆交点方法，不在生成器导入 tests）。

**Interfaces:** Consumes PackConfig/Channel/Cycle；Produces `position(model: dict, theta_rad: float) -> dict`, `forward_state(model: dict, time_s: float) -> dict`, `generate_cycles(config: PackConfig) -> tuple[Cycle, ...]`, `generate_shape_cases(config: PackConfig) -> tuple[Case, ...]`。position 返回 `displacement_m, beta_rad, J_m`；forward_state 返回 `theta_rad, omega_rad_s, alpha_rad_s2, displacement_m, load_N, power_W`。smoke返回8个Cycle及空形状集；extended返回48个Cycle和12个仅展示轮廓Case，共60个功图样本。

- [ ] **Step 1: 写失败测试。** `test_circle_closure_and_independent_energy` 检查两圆半径残差≤1e-9 m、用独立差分对 J 的检查≤1e-5 m/rad；`test_window_mean_is_integrated` 断言 1 ms 功率窗口的 16/32 面板 Simpson 均值差≤0.02 W；`test_eight_cases_native_counts_and_turn_event` 断言八个 1025 帧周期、相邻时间差 7812500 ns、事件索引取独立位移极大值而非硬编码512；`test_oracle_does_not_import_production_kernels` 静态检查不导入 backend.inversion 的 geometry/mechanics/solver/motion 或 tests。

  ```python
  cycles = generate_cycles(config)  # smoke配置
  self.assertEqual(len(cycles), 8)
  self.assertTrue(all(len(c.time_ns) == 1025 for c in cycles))
  self.assertEqual(cycles[0].time_ns[1] - cycles[0].time_ns[0], 7812500)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_mechanics tests.test_testpack_cycles -v`。
- [ ] **Step 3: 实现机械计算。** 使用笛卡尔两圆交点、连续角差与独立能量差分。模型基准：B=(3,2)m，曲柄0.5m、后臂2m、连杆3m、驴头半径6m，branch/rotation/displacement sign 均+1；曲柄惯量200 kg·m²、x一阶质量矩1800 kg·m，其余下游质量项0，上游折算惯量30 kg·m²，传动比100，g=9.80665，库仑摩擦300 N·m、粘性100 N·m·s、恒损耗1200 W。零质量项代表简化模型，不对应实物BOM。独立搜索位移最小角作为周期起点；θ(t)=θ₀+2πt/T+εsin(2πt/T)，T=8s，基准ε=0。F=46000+14000·tanh(v/0.05) N；电功率按 `(U′+(M+Iup)α+0.5M′ω²+摩擦+FJ)ω+损耗` 生成，不能调用被测反演器。记录差分步长减半、积分细分后的差异；超限直接失败。
- [ ] **Step 4: 实现场景和量化。** 八个 ID 固定为 `cycle-base/balance/friction/speed/noise/missing/clipped/shifted`。前三个变化分别将一阶质量矩乘0.8、库仑摩擦乘1.5、ε设0.1；噪声在量化前施加功率±10W、参考载荷±50N、角度±1e-6rad、位移±1e-5m的独立均匀扰动。缺样为功率索引400..419并用协议缺样质量码；削顶为原功率绝对值超过20000W处保留限幅值并标无效；时移为功率窗口中心+2个样本间隔，不循环卷绕，边界标缺样。原始 scale 为功率0.01W、角度1e-6rad、载荷0.001kN、位移1e-6m，offset为0。turn_index 是离散位移最大值，另记连续根时间与离散事件误差≤半个采样间隔。测试缺口不参与积分、扰动不反向修改参考真值。
- [ ] **Step 5: 跑绿灯并提交。** 同上两模块全部通过；提交 `feat: generate independent synthetic pumping cycles`。不修改生产精度阈值换取通过；若数值预算无法满足，保留失败并先修生成模型。

### Task 3: 电流信号、波形编码与协议负例

**Files:** Create `tools/testpack/currents.py`, `waveforms.py`；Test `tests/test_testpack_waveforms.py`；Reuse `assets/open-interface/waveform_files.py`, `waveform_contracts.py` 和 `backend/waveforms/contracts.py` 的公开校验入口。

**Interfaces:** Consumes Cycle/EvidenceBinding；Produces `generate_currents(config: PackConfig) -> tuple[Case, ...]`, `encode_cycle(cycle: Cycle, config: PackConfig, binding: EvidenceBinding | None = None) -> Case`, `waveform_negative_cases(valid: Case) -> tuple[Case, ...]`。所有包内录波先不绑定证据；运行阶段传实际 binding 产生另一份录波，记录映射，不覆盖原包。

- [ ] **Step 1: 写失败测试。** `test_four_channels_20500_bytes_five_chunks` 断言周期解码20500字节、5片、每片≤256帧；`test_motor_count_and_frequency_not_resampled` 断言 smoke四例各16384点/1024Hz；`test_raw_archive_nullable_evidence` 断言 config/calibration/source/sync引用为空且原始录波可归档，未因此获得配对资格；`test_expected_protocol_failures` 固定缺片、错摘要、非法质量码和截断行四类拒绝码。

  ```python
  manifest = json.loads(encoded_wave.splitlines()[0])
  self.assertEqual(manifest['streams'][0]['decoded_bytes'], 20500)
  self.assertEqual(manifest['streams'][0]['chunk_count'], 5)
  self.assertTrue(all(c['calibration_id'] is None for c in manifest['streams'][0]['channels']))
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_waveforms -v`。
- [ ] **Step 3: 实现编码及电流输入。** 四个电流基准为50Hz正弦10A峰值、加150Hz的0.5A谐波、加48/52Hz各0.5A边带、已知缺样段；质量码取现有协议定义，不发明新值。3相需要时采用±2π/3基相位并记录调制假设；谱验证使用未抽稀原始相电流。电流量化1e-4A。复用合同构建 manifest/chunk，重新计算帧摘要和录波摘要；周期内role active_power映射到quantity total_active_power、signal_kind window_mean，其余quantity与role同名且instantaneous。扩展8例另加幅值变化、相不平衡、削顶、供频偏移（均为信号案例，非故障确诊）。受限负例用独立目录和 expected 列表，不要求其通过正例校验器。
- [ ] **Step 4: 跑绿灯。** 上述测试及 `python -m unittest tests.test_waveform_contracts tests.test_waveform_files -v`；正例 `scan_file` 成功，负例只能以预定拒绝计入成功。
- [ ] **Step 5: 提交。** `feat: package synthetic waveforms and current signal cases`。

### Task 4: 产液用电历史与积分口径

**Files:** Create `tools/testpack/history.py`；Test `tests/test_testpack_history.py`。

**Interfaces:** Produces `generate_history(config: PackConfig) -> tuple[Case, ...]`, `telemetry_point_map(unit_id: int=1) -> tuple[PointDefinition, ...]`。PointDefinition 复用 backend.gateway.contracts；每井每15分钟一帧，每帧12点；协议中的unit_id与虚拟井一一对应，不让两井共享同一寄存器组。

- [ ] **Step 1: 写失败测试。** `test_extended_history_exact_counts` 断言8井×14天×96=10752帧、129024值、112日汇总；`test_energy_and_volume_are_integrals` 断言1kW持续1小时=1kWh、2m³/h持续半小时=1m³，净电量=输入−回馈；`test_daily_timezone_and_missing_coverage` 断言UTC转换后的北京时间日界正确、缺测降低coverage且不补零；`test_same_condition_fluctuates_without_forcing_every_case_correlation` 检查基准产液/耗电非恒定且总体正相关，低效场景无需正相关。

  对生成文件解码后的记录执行：
  ```python
  self.assertEqual(len(frames), 10752)
  self.assertEqual(sum(len(frame['samples']) for frame in frames), 129024)
  self.assertEqual(len(daily_rows), 112)
  self.assertAlmostEqual(net_kwh, imported_kwh - exported_kwh, places=9)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_history -v`。
- [ ] **Step 3: 实现模型与点表。** 12点固定为 `flow_rate_m3_h, active_power_kw, voltage_v, current_a, power_factor, strokes_per_min, stroke_m, load_min_kn, load_max_kn, liquid_level_m, motor_temperature_c, pump_running`；单位依次 m3/h、kW、V、A、1、1/min、m、kN、kN、m、degC、1。每设备Modbus共24个input寄存器，地址0/2/…/22，float32、大端、FC04，smoke两井分别unit1和unit2；FC03只读镜像，不含写点。首版泵流量按柱塞面积×冲程×冲次×充满度×运行占比换算，平均有功按同一运行参数和已声明损耗产生，电流按3相平衡假设 P/(√3·U·PF)核算；瞬时正弦电流包与15分钟统计明确分开。日汇总使用相邻边界和固定区间均值积分，缺测区域不外推。smoke为2井各8帧；扩展按固定种子生成参数波动，异常另列。
- [ ] **Step 4: 跑绿灯并提交。** 同一测试模块全部通过；提交 `feat: generate consistent production and energy histories`。

### Task 5: 七类研究输入与独立划分

**Files:** Create `tools/testpack/study_inputs.py`；Test `tests/test_testpack_study_inputs.py`；Reference `backend/studies/contracts.py`、`tests/test_review_release.py` 的字段结构，不导入其固定样本生成函数。

**Interfaces:** Consumes config、周期、历史及电流 Case；Produces `generate_studies(config: PackConfig, cycles: tuple[Cycle, ...], history: tuple[Case, ...], currents: tuple[Case, ...]) -> tuple[Case, ...]`, `bind_study(template: dict, record_ids: dict[str, str], pack_sha256: str) -> dict`。依赖模型/恢复记录的模板保存在外置 `templates/`，不是假装可提交的 envelope；bind_study 以实际返回ID生成严格 envelope，request_id在本轮固定重用用于重试。

- [ ] **Step 1: 写失败测试。** `test_seven_study_kinds_and_fixture_source` 检查七类覆盖且source.kind=software_fixture；`test_train_validation_test_are_disjoint` 检查90条各30条且well/cycle/sample/time不跨分区，改测试标签不改变训练artifact；`test_dependency_templates_require_actual_ids` 缺依赖ID拒绝提交，不写“replace”或伪UUID；`test_recovery_and_effects_have_known_bounds` 固定恢复τ=120min、平衡深度100m及初始增量40m，检查拟合τ误差≤2min且效果只能descriptive。

  ```python
  self.assertEqual(Counter(row['partition'] for row in training['rows']),
                   {'train': 30, 'validation': 30, 'test': 30})
  self.assertFalse(train_wells & validation_wells or train_wells & test_wells or validation_wells & test_wells)
  self.assertLessEqual(abs(fit['parameters']['tau_minutes'] - 120), 2)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_study_inputs -v`。
- [ ] **Step 3: 实现输入。** 工况标签固定“合成基准/合成高摩擦/合成失衡”，特征来自对应模型参数的周期统计，参数取样与划分在生成前固定；BP的虚拟井分区独立于8口历史井，不谎称一套井号自然分割。恢复观测t=[0,30,60,90,120,180,240,300]min；匹配单井120min开/60min关×8的有限候选及站点约束，同时生成无可行候选、短恢复记录、低覆盖效果、谱信息缺失、保护定值非法案例。保护帧只给软件状态机输入；阈值来源说明为测试参数。包内引用包ID和配置摘要，实际提交时另引用最终清单SHA，源字段不得混入未知属性。扩展多井候选必须逐井绑定对应恢复记录，不能统一塞入同一个恢复ID；每次请求至多4井、1..7个完整上海自然日，不把8井14日历史直接塞进一份调度请求。
- [ ] **Step 4: 跑绿灯。** 上述模块和 `python -m unittest tests.test_condition_studies tests.test_recovery_studies tests.test_energy_schedule tests.test_motor_spectrum tests.test_protection_replay -v`；结果数量和状态由实际执行记录，不预写通过。
- [ ] **Step 5: 提交。** `feat: generate traceable engineering study inputs`。

### Task 6: 离线生成命令与全包验证

**Files:** Create `tools/testpack/build.py`, `validate.py`, `__main__.py`；Test `tests/test_testpack_cli.py`, `test_testpack_validation.py`。

**Interfaces:** Consumes任务1—5；Produces `build(config: PackConfig, target: Path) -> PackIndex`, `validate_pack(pack: PackIndex) -> tuple[CheckResult, ...]`, `main(argv: list[str] | None = None) -> int`。CLI为 `python -m tools.testpack generate --config FILE --output NEW_ABSOLUTE_DIR` 和 `validate --pack DIR --report NEW_FILE`；退出码0=该层全部必需检查通过，1=失败/不完整，2=参数或所有权错误。

- [ ] **Step 1: 写失败测试。** `test_smoke_cli_repeatable_and_no_services` 断言8周期、4电流例且没有socket/process启动；`test_manifest_tamper_refused_before_parsing` 修改一字节即拒绝；`test_negative_case_success_requires_expected_code` 错误拒绝码仍失败；`test_extended_budget_and_case_inventory` 断言60周期/8电流/112汇总/90BP记录，且总量≤256MiB、每类用途登记完整。

  ```python
  checks = validate_pack(pack)
  self.assertTrue(all(c.status == 'passed' for c in checks if c.layer == 'offline'))
  self.assertLessEqual(sum(a['size_bytes'] for a in pack.manifest['artifacts']), 268435456)
  self.assertTrue(all(c.status != 'passed' for c in checks if c.layer == 'software_network'))
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_cli tests.test_testpack_validation -v`。
- [ ] **Step 3: 实现组装与数值验证。** 60周期分为前8个小型案例、40个预先固定参数变体、12个纯轮廓样本；纯轮廓只保存显示坐标，不进入反演精度及BP评估。读取合同、质量码和单位后才积分；每项报告记录目标文件摘要、观察值与容差，未产生反演输出时其检查为not_run。软件包的模型自洽不显示为现场精度。
- [ ] **Step 4: 跑绿灯和小型基准。** 上述模块全通过；在两个新临时目录生成smoke并逐文件比较摘要。扩展生成只在smoke全部离线检查通过后执行；命令只运行新工具，不使用原demo seed脚本。
- [ ] **Step 5: 提交。** `feat: add reproducible testpack generator and offline verifier`。

### Task 7: 新建隔离实例与只读 Modbus 软件设备

**Files:** Create `tools/testpack/lab.py`, `modbus_device.py`, `gateway_adapter.py`, `lab_http.py`；Modify `tools/review_deploy.py`, `review_deploy_config.py`, `review_deploy_child.py`, `review_deploy_source.py` 的初始化、凭据库存和导出清单；Test `tests/test_testpack_lab.py`, `test_testpack_modbus.py`, `test_review_deploy.py`, `test_monitoring_deploy.py`。

**Interfaces:** Produces `initialize_lab(target: Path, *, pack: PackIndex, source: Path, ports: dict) -> LabHandle`, `load_lab(root: Path) -> LabHandle`, `restart_owned_component(lab: LabHandle, profile: str, component: str) -> None`, `stop_lab(lab: LabHandle) -> None`；组件重启仅允许http/center，停止须校验每个进程身份。`ReadOnlyModbusDevice(maps: dict[int, tuple[PointDefinition, ...]], *, host: str, port: int)` 提供 `start()`, `set_frame(unit_id: int, values: dict[str, float])`, `stop()`；`SoftwareFixtureModbusPoller(gateway_id, device).poll(*, sequence) -> PollResult` 保持既有返回类型。`LabHttp(lab: LabHandle, profile: str).request(method: str, path: str, body: object=None, *, actor: str, expected: int=200, raw: bool=False, headers: dict | None=None) -> object` 从本机私有库存读取凭据，不接受外部URL和账号参数；bytes请求体用于原始分片，额外headers仅允许Content-Type/Idempotency-Key/If-Match/X-Waveform-Reason，不能覆盖Host/Origin/Cookie/会话头。

- [ ] **Step 1: 写失败测试。** `test_owned_instances_do_not_touch_protected_paths_or_ports` 拒绝8765/8770/8772/8774、重复端口、任意外部DSN、已有或链接目录以及既有安装内部的新子目录；`test_fixture_config_sealed_at_initialization` 检查新fixture精确主题及旧默认配置不变；`test_unowned_pid_never_stopped` 校验PID复用、命令行/创建时间不符时拒绝停止；`test_real_tcp_device_rejects_writes` 使用实际loopback连接读FC03/04，并对06/16返回异常01且寄存器不变；`test_provenance_changes_only_after_actual_read` 检查wrapper保留数值/时间/错误，仅改变source，默认生产poller仍为modbus-tcp。

  ```python
  self.assertEqual(write_response.exception_code, 1)
  self.assertEqual(registers_after, registers_before)
  self.assertEqual(wrapped.samples[0].value, polled.samples[0].value)
  self.assertEqual(wrapped.samples[0].source, 'software-fixture-modbus-tcp')
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_lab tests.test_testpack_modbus -v`。Modbus测试可用临时loopback监听，不创建数据库，不接现场地址。
- [ ] **Step 3: 实现受限初始化。** 在既有 `initialize(..., profile='review')` 增加可选 `fixture: dict | None=None`，只对新目录生效。fixture固定 `{schema:"owp.testpack.fixture/1",lab_id,pack_id,pack_sha256,gateway_id,device_ids,modbus_port}`；网关沿用隔离安装的REVIEW-GW，设备固定TESTPACK-PUMP-001/002，其他值拒绝，两个新Broker的实例身份分别登记。`runtime_secrets/mqtt_accounts/write_configs/broker_configuration/config_hashes/verify_config_hashes` 同步支持该可选维度；旧六文件封存规则不变，新实例另封存fixture与gateway配置，新增最小权限mqtt_testpack账号，精确限定本包两设备的telemetry写主题及对应receipt读主题，不给用户输入主题通配符。凭据库存校验读取已封存fixture决定字段，不接受env开关绕过。保持review/monitoring分别运行，不增加混合profile。fixture实例首版明确拒绝backup/restore命令，直到单独实现兼容，不套用旧备份库存。
- [ ] **Step 4: 实现实例与软件设备。** lab根包含独立monitoring和review子安装、私有队列及实际拥有的进程记录；所有监听固定127.0.0.1。默认建议端口为monitoring HTTP18774/PG56432/MQTT19884，review HTTP18770/PG56433/MQTT19885，Modbus15020；执行前检查全部空闲，不结束占用者。软件设备采用标准库有界TCP处理MBAP/PDU，协议ID0、unit1/2各自独立寄存器、读数量1..125、仅已登记地址，其他功能码拒绝；各unit每帧原子切换，记录实际请求与响应摘要。测试必须验证两unit不同数据不会串井。网关用既有 GatewayRuntime(poller_factory=SoftwareFixtureModbusPoller)，内部真实默认ModbusPoller读取，不注入假client。应用确认使用新建v2队列及原ReliableUplink，端口/实例标识不能由命令行任意替换成现有服务。
- [ ] **Step 5: 跑绿灯并提交。** 新模块及 `python -m unittest tests.test_review_deploy tests.test_monitoring_deploy tests.test_gateway_modbus tests.test_gateway_poller tests.test_gateway_runtime -v` 全通过；提交 `feat: add owned testpack lab and read-only modbus device`。本任务单元测试用替身覆盖部署系统调用；真正的PG/MQTT初始化在任务8执行。

### Task 8: 实际采集上传入库与故障恢复

**Files:** Create `tools/testpack/telemetry_run.py`；Modify `tools/testpack/__main__.py` 增加lab命令；Test `tests/test_testpack_telemetry_run.py`, `test_testpack_live.py`；Reuse `backend/gateway/runtime.py`, `backend/ingest_reliability/repository.py`，不修改其协议和确认语义。

**Interfaces:** Consumes LabHandle/PackIndex/LabHttp；Produces `run_telemetry(lab: LabHandle, pack: PackIndex, run_root: Path) -> tuple[CheckResult, ...]`。CLI为 `lab-init --pack DIR --root NEW_DIR --source COMMITTED_TREE`、`lab-run --pack DIR --root OWNED_DIR --run NEW_DIR`、`lab-stop --root OWNED_DIR`。不提供任意DSN参数；输出只包含lab_id/run_id/状态/公开报告路径，不打印密码。

- [ ] **Step 1: 写失败测试。** `test_registry_then_actual_poll_then_history` 用可记录的适配器验证顺序；`test_simulated_clock_is_not_registration_clock` 检查真实measured_at不早于绑定生效，run映射保留历史time；`test_puback_is_not_database_success` 在只有broker确认时结果仍非passed；`test_failure_keeps_prior_receipts_and_not_run_steps` 在中途超时后保留已完成证据，后续未执行。

  ```python
  self.assertEqual(baseline_message_count, 16)
  self.assertEqual(baseline_sample_count, 192)
  self.assertEqual(rows_after_duplicate, rows_before_duplicate)
  self.assertGreaterEqual(measured_at, binding_effective_at)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_telemetry_run -v`。真实测试放独立test_testpack_live，不在普通discover时偷偷启动旧库；缺少本轮OWNED根时明确skip，交付gate将该必需skip判失败而非通过。
- [ ] **Step 3: 实现有界链路。** 通过registry实际API创建2口虚拟井/2设备（transport=modbus_tcp、usage=commissioning）、点表提交→第二账号复核→绑定→点表指派→启用，再开始真实采集。monitoring请求携带登录后auth/me返回的X-Monitoring-Session。每井读取8帧×12点，基准共16个上行批次/192点，报告另列异常流量。实际样本使用墙钟时间，另保存 `(scenario,index,simulated_at,measured_at,message_id,payload_sha256)` 映射。实际字节从本轮队列提取，不用重序列化替代原样重发。
- [ ] **Step 4: 实现故障步骤。** 真实等待中心入库回执和HTTP消息/井历史查询，不只看PUBACK；原样重发须无新增数据、重复计数增加；同ID异字节须quarantine/idempotency_conflict且不改原入库值。未知身份、权限不足分别测试broker或HTTP拒绝，注明实际拒绝层。关闭本轮Modbus服务验证read_failed；中心暂不可用时保留队列并重试；定向重启本轮HTTP/center及关闭重开网关队列后核对先前摘要。Broker始终保持运行，不宣称persistence false的持久化恢复。每次等待有截止时间及实际超时记录，禁止无界sleep。
- [ ] **Step 5: 运行真实验证。** 先提交待部署源码，使用lab-init创建全新实例后运行 `python -m unittest tests.test_testpack_live -v`，该模块仅从 `OILWELL_TESTPACK_LIVE_ROOT` 和 `OILWELL_TESTPACK_LIVE_PACK` 加载任务7所有权验证通过的目录。基准16批次192点及去重、拒绝、恢复检查全部实际完成才通过；失败保留实例和日志。验证既有安装源文件/配置摘要未变，不在原平台执行测试。
- [ ] **Step 6: 提交测试和适配器修正。** `feat: verify real modbus mqtt storage and recovery path`；按实际部署提交号重新运行受影响检查，源码改变后不能沿用旧部署报告。

### Task 9: 波形证据绑定、双图计算与不可升级来源

**Files:** Create `tools/testpack/waveform_run.py`, `inversion_binding.py`, `evidence_report.py`, `evidence_report.html`, `render_evidence.cjs`；Test `tests/test_testpack_waveform_run.py`, `test_testpack_inversion_binding.py`；Extend `tests/test_testpack_live.py`。

**Interfaces:** Consumes LabHandle/PackIndex/LabHttp/EvidenceBinding；Produces `run_waveforms(lab: LabHandle, pack: PackIndex, run_root: Path) -> tuple[CheckResult, ...]`, `bind_inversion_parameters(model: dict, pins: dict) -> bytes`。后者只生成现有parameter-set合同及实际证据引用，不计算预测，也不发明approved状态。

- [ ] **Step 1: 写失败测试。** `test_sampling_calibration_before_bound_manifest` 检查创建并复核sampling→calibration→编码顺序；`test_original_pack_never_rewritten` 断言前后包摘要一致且run-bound文件具有独立摘要；`test_source_and_calibration_cannot_be_promoted` 检查calibration_id=None原归档不能通过selection另挂证据升级、synthetic不能进入field_pair_input；`test_reference_not_given_to_predict` 断言预测输入只含功率与角度；`test_withdrawn_evidence_and_version_rejected` 检查撤销/旧版本导致下游拒绝。

  ```python
  self.assertEqual(pack_hash_after, pack_hash_before)
  self.assertEqual(bound_channel['calibration_id'], actual_calibration['id'])
  self.assertNotEqual(bound_file_sha256, original_file_sha256)
  self.assertLessEqual(baseline_rmse_N, 500)
  self.assertGreaterEqual(baseline_coverage, 0.80)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_waveform_run tests.test_testpack_inversion_binding -v`。
- [ ] **Step 3: 实现真实证据顺序。** 从实际API获得sampling_config与calibration ID/SHA/复核版本，再编码新manifest/chunks；校准绑定实际sampling证据，channel.calibration_id必须等于实际校准ID。通过imports创建、逐行octet-stream上传、seal及装配；使用返回的实际observation/fact/outcome/import ID生成origin，再创建sync和software_pair_test profile、质量评估与pair复核。sampling/校准说明是软件比例和量化定义，不称检定。附件接口仅接受PDF/PNG/JPEG，因此 `render_evidence(summary: dict, target: Path) -> Path` 使用已有Node/浏览器运行时把本次数值/时序说明页渲染成真实PNG，保存原始summary JSON、PNG与摘要；内容明确软件模型，不复用伪PDF字节。请求按各服务合同传idempotency/version/reason，波形decision为approve/reject，不与研究approved/rejected混用。
- [ ] **Step 4: 绑定反演参数并计算。** 通过现有production-inversion模型/参数/选用/运行接口创建本包独立软件参数，不直接INSERT批准记录。模型值取任务2，域固定power±250000W、speed0.1..2rad/s、acceleration±2rad/s²、load0..100000N、temperature250..350K、voltage300..500V、frequency45..55Hz、window1..10000000ns、bandwidth1..1000Hz；损耗网格power±250000W、motor speed1..250rad/s、loss1200W、loss error0.02W。软件基准uncertainty固定torque_bound=100Nm、factor_bound=0.0001m、speed_bound=0.00001rad/s、numerical_budget=0.01Nm、numerical_factor_budget=0.0000001m；policy固定numerical_margin=0.001、motion_max_time_span=250000000ns、angle_span=0.5rad、residual=0.0001rad、condition=10000、max_load_error=1000N、min_coverage=0.8、reference_max_gap=20000000ns、fill_max_time=50000000ns、fill_max_angle=0.2rad、relative_error_min_denominator=1N。实现时先独立验证量化、时间、窗口与差分误差被该软件预算覆盖，再封存参数；这些数值不是已验证的实物误差界。预算验证失败则停止该案例、报告失败，不为提高通过率调宽阈值。不使用生产build_geometry构建参考；证据不足记录blocked。运行至少base/balance/friction/speed四例；主比较只用共同有效支撑，预注册基准RMSE≤500N、覆盖率≥0.80，死点保留不可观测；噪声/缺样/削顶/时移分别报告覆盖和拒绝，不以该阈值宣称现场精度。
- [ ] **Step 5: 跑绿灯与真实层。** 新单元测试及 `python -m unittest tests.test_inversion_solver tests.test_inversion_comparison tests.test_waveform_quality -v`；仅在本轮owned review实例执行真实HTTP导入/证据/反演，检查原包不变、run文件映射、拒绝案例与结果导出摘要。任何任务尚未实际运行则保留not_run，不以纯计算替代HTTP通过。
- [ ] **Step 6: 提交。** `feat: run synthetic waveform evidence and inversion workflows`。

### Task 10: 七类研究实际计算、复核和撤销

**Files:** Create `tools/testpack/study_run.py`；Modify `tools/testpack/__main__.py` 连接研究步骤；Test `tests/test_testpack_study_run.py`；Extend `tests/test_testpack_live.py`。Reference `tools/verify_review_release.py:ReviewClient/verify_workflows` 的顺序，保留旧runner默认行为。

**Interfaces:** Consumes LabHandle/PackIndex/LabHttp/bind_study；Produces `run_studies(lab: LabHandle, pack: PackIndex, run_root: Path) -> tuple[CheckResult, ...]`。每个研究记录映射scenario、input_sha、实际record_id/result_sha、review_version，不把运行UUID放回确定性包。

- [ ] **Step 1: 写失败测试。** `test_actual_engine_seven_kinds` 使用真实study_engines而非lambda固定返回；`test_each_well_gets_its_own_recovery_id` 验证多井依赖；`test_source_pins_and_runtime_ids` 验证包ID/SHA与实际record绑定；`test_review_export_revoke_and_retry` 覆盖自审403、旧版本409、同request_id改body409、匿名401、原样重试不重复、撤销后不可选用及预测；`test_run_failure_does_not_become_success_json` 覆盖中途失败仍输出剩余not_run。

  ```python
  self.assertEqual(len(records_by_kind), 7)
  self.assertEqual(exported['source']['kind'], 'software_fixture')
  self.assertFalse(exported['field_validation'] or exported['control_eligible'])
  self.assertFalse(stale_schedule['dependency_current'] or stale_schedule['eligible'])
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_study_run -v`。
- [ ] **Step 3: 实现真实流程。** 单井smoke沿用真实研究顺序：training计算→独立复核→选用→prediction；recovery拟合→复核→schedule→复核/选用；effects、spectrum、protection分别实际计算。POST根路径与GET requests回执核对，GET export核对输入/结果摘要；撤销训练/恢复验证依赖失效后，若保留可查看示例必须重新复核并重新计算新的下游记录，不能改历史引用。扩展候选逐井读取自己的恢复ID；每种合法但不适用结果保留如no_feasible_candidate，不能预填通过或强制审批。
- [ ] **Step 4: 跑绿灯与重启检查。** `python -m unittest tests.test_testpack_study_run tests.test_review_release -v`；再由本轮lab-run实际调用七引擎，重启所属review HTTP后重新读取记录摘要、复核版本与来源。确认 `field_validation/control_eligible/dispatch_allowed/hardware_execution/fault_confirmation/causal_effect_proven` 对相应结果仍为false，shadow_only对影子能力为true。
- [ ] **Step 5: 提交。** `feat: verify traceable study workflows and review dependencies`。

### Task 11: 只读查看、真实报告与交付核对

**Files:** Create `tools/testpack/report.py`, `viewer.py`, `frontend/testpack/index.html`, `viewer.js`, `styles.css`, `docs/测试数据包与隔离联调使用说明.md`；Modify `frontend/inversion/charts.js` 仅增加可选曲线名称参数、保留默认行为；Modify `tools/testpack/__main__.py` 连接report/view；Test `tests/test_testpack_report.py`, `test_testpack_viewer.py`, `tests/fixtures/testpack_viewer.html`。

**Interfaces:** Produces `write_report(pack: PackIndex, checks: tuple[CheckResult, ...], run_root: Path, *, run_metadata: dict) -> Path`, `export_public_report(run_root: Path, target: Path) -> Path`, `serve_viewer(pack: PackIndex, public_report: Path | None, *, port: int=0) -> object`。CLI `report --run OWNED_RUN --output NEW_DIR`、`view --pack DIR [--report PUBLIC_JSON] [--port N]`；仅GET/HEAD，固定127.0.0.1和静态允许清单，不转发登录、控制或任意文件请求。

- [ ] **Step 1: 写失败测试。** `test_not_run_is_not_passed_and_failed_expected_reject` 验证总体状态从必需检查逐项推导；`test_public_export_has_no_secret_or_private_paths` 用哨兵口令/私钥/绝对个人路径验证输出排除，未知字段拒绝；`test_read_only_viewer_rejects_path_traversal` 验证POST、目录遍历及非允许路径拒绝；`test_compare_labels_toggles_hover_and_gaps` 断言两线独立勾选、两位小数、小悬停点、缺口断线、未知指标不补0、形状样本不得显示反演精度。恶意title以textContent显示，不能执行脚本。

  ```python
  self.assertNotIn(secret_sentinel, public_bytes.decode('utf-8'))
  self.assertNotEqual(incomplete_report['status'], 'passed')
  self.assertEqual(two_decimals_hover_text, '1.23 m / 32.46 kN')
  self.assertEqual(permanent_point_markers, 0)
  ```

- [ ] **Step 2: 跑红灯。** `python -m unittest tests.test_testpack_report tests.test_testpack_viewer -v`；复用现有Node浏览器fixture runner，不增加浏览器依赖。
- [ ] **Step 3: 实现阅读页和报告。** 入口包括场景目录、同周期双图、产液用电、研究结果、逐层验证与已知限制。复用mountComparison；可选名称固定“电功率反演结果”“仿真载荷位移参考”，无预测时不绘造第二条线。图周参数来自文件/实际结果，坐标显示不改原始值。报告按offline/numeric/software_network/http/browser分层，硬件单独not_run；区分生成10752帧与实际入库16批次，记录时间映射、源提交、输入输出摘要、失败及实际运行时长。公开导出采用字段允许清单，不简单复制private日志；页面说明只提供可核对的运行结果，不声称设备联调完成。
- [ ] **Step 4: 分层交付验证。** 使用本计划全部新增单元模块，加既有gateway、review_deploy、monitoring_deploy、waveform合同/质量、inversion solver/comparison/browser和七类研究测试。live模块必须在本轮实例执行且不能skip；浏览器实际查看新包及真实运行报告，不以fixture截图代替运行结果。扩展包须先完成离线验证，网络仍使用已登记的有限子集；在线波形分片/ACK若未执行标not_run，不纳入已完成链路。截图及报告保存本轮目录，执行前后核对既有安装代码/配置摘要和工作树范围。
- [ ] **Step 5: 独立代码审查和交付。** 按用户选定的执行方式完成独立审查，修复后重跑受影响层。说明文档写明命令、依赖、数据规模、运行入口、当前失败和实物待验证项；提交 `feat: deliver testpack viewer and evidence reports`。提交后如源码进入部署导出清单发生变化，重新封存新实例并核对报告commit，禁止手改旧实例。只报告实际通过的层级，不宣布整个项目或硬件完成。

## 验证命令与里程碑

执行命令时从本工作树运行；本机解释器可使用 `C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe`。文中 `python` 代表该已核对的3.13.2解释器；新隔离实例使用其专属环境与已固定依赖。全量旧测试库回归不在本计划默认命令内，不能自行清空历史专用库。

| 里程碑 | 完成任务 | 给用户的实际交付 | 不计算为已完成 |
| --- | --- | --- | --- |
| A1 离线基准 | 1—6 | 可重建数据包、来源/参数、离线检查结果 | 网络接入、硬件实测 |
| A2 真实采集 | 7—8 | 新实例Modbus→MQTT→入库→HTTP结果和异常恢复证据 | RTU、电气接口、Broker断电持久性 |
| A3 算法与证据 | 9—10 | 波形配对/反演、七类研究和复核撤销实际结果 | 现场准确率和节能效果 |
| A4 可审阅交付 | 11 | 只读入口、数据下载、报告、使用说明 | B硬件出图与C全页面接入 |

## 设计覆盖核对

| 已确认设计内容 | 实施位置 |
| --- | --- |
| 四种场景、可复现来源、固定种子、版本与容量 | 任务1、2、3、5、6 |
| 8周期/4电流最小基准及8井14日/60功图/8电流/90BP扩展 | 任务2—6 |
| 独立正向、功率窗口积分、生成/预测隔离、双图交互 | 任务2、9、11 |
| 产液用电波动、输入/回馈/净能量与覆盖率 | 任务4 |
| 严格合同、实际证据ID、确定性包与运行绑定分开 | 任务1、3、5、9、10 |
| 真实网络、独立review/monitoring、所有权与最小权限 | 任务7、8 |
| 重发、冲突、未知身份、权限、撤销、断连、所属服务重启 | 任务3、8、9、10 |
| 真实分层记录、未执行不冒充通过、秘密与公开包隔离 | 任务6、8、11 |
| 原demo和既有服务保护、针对性验证、独立审查 | 全局约束、任务7、11 |
| 硬件工程设计、统一55页真实接入 | 留在B/C后续计划，不以A替代 |

## 执行交接

本计划只批准范围内的本地开发与新建隔离联调，不授权现场设备写入、修改既有安装、对外发布数据或推送仓库。实施前请用户审阅计划并选择执行方式：由主代理逐项实现并在末尾独立审查，或由子代理分任务实现并逐项审查。用户先前选择的分层验证策略在两种方式下均保留。
