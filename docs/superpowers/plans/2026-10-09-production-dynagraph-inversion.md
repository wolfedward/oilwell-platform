# WP-05A Production Dynagraph Inversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将有来源的机械参数和冻结同步波形转为可解释、可独立对照、可复核追溯的地面示功图，保持演示版独立。

**Architecture:** 纯计算模块隔离参考载荷和位移；生产服务固定输入、参数、模型和证据版本。新增 PostgreSQL `inversion` schema 与有界后台工作进程，复用 WP-04 的受审计原文读取和历史依赖锁，通过独立页面交付参数、双图与复核流程。

**Tech Stack:** 现有 Python 3.13、标准库数值计算、PostgreSQL 18、psycopg[binary]==3.3.5、SQLite 身份服务、标准库 HTTP、原生 JavaScript/CSS、unittest、现有 Node/Edge 浏览器测试。首版不新增科学计算库、Web 框架、Redis/Celery 或云服务。

**Spec:** [已确认设计](../specs/2026-10-09-production-dynagraph-inversion-design.md)，批准日期 2026-10-09，设计提交 `ee8d4c4bb87d704040f1b13906cddc932d496851`。用户已批准逐项实施与独立审查；Tasks1—13 已实现并审查，Task14 提供最终集成与交付验证。实际验收状态以本次源码绑定报告和协调任务审查记录为准；整体分支审查、环境恢复与生产启用分别处理，不表示年度平台整体完成。

## Global Constraints

- 仅支持常规曲柄平衡、刚性四连杆、圆弧驴头和工频异步电机端总有功；输入保留负功率。VFD 前端、直流母线、六路电压电流合成功率不在首版范围。
- 四个必需角色为 `active_power`、`crank_angle`、`load`、`displacement`；功率为 `window_mean`、中心时间戳，其他为瞬时量。原始时间保留整数/有理数精度，校正仅应用一次。
- 预测不接收参考载荷/位移，不调用旧 `backend/algorithms/dynagraph.py` 或 `backend/services/analysis.py`；不补默认参数、不生成现场来源、不做自动控制或 BP 训练。
- 参数采用 `inversion.parameter-set/1`，算法标识 `beam_energy_balance/1`，输入包 `inversion.input/1`；参数、物理有效期、审批和选用分别记录。
- 默认运行限制为一个执行槽、最多 16 个等待请求；每请求只处理一个 pair、最多两个 capture。每 capture 的 16 MiB 解码和 32 MiB 原文上限不变。
- 四个必需通道各最多 32768 个 selection 样本；输入/结果暂存上限分别为 64 MiB 和 16 MiB；请求政策 JSON 上限 256 KiB。
- 总截止时间默认 180 秒，单次数据库操作最长 3 秒、锁等待 1 秒。默认 disabled；生产启用另行授权，不在启动时自动 DDL。
- 损耗网格每轴 2—64 个严格递增节点，最多 4096 个值；七点三次局部运动拟合，不跨缺口或选定周期外推。
- 业务锁顺序：waveform dependency guard → registry history 共享锁 → inversion dependency guard → waveform payer/capacity → inversion capacity → 稳定排序依赖行。持锁时禁止另开跨服务写事务。
- 管理员拥有全部页面和能力但不能自我复核；写入鉴权先于幂等回执查询，当前限制与历史批准分开。
- 图表默认细线、有效区段断开、可单独开关双图，悬停/键盘坐标两位小数；显示缩放不修改计算值。补绘不进入主误差或现场通过判定。
- 工作区固定为 `C:/Users/hp/.codex/worktrees/production-device-registry/外专双百`；不编辑原工作区、冻结演示目录、归档和标签，不操作正在使用的 8765 服务，不自动推送或更新 WP-04 合并请求。
- 分层验证：各任务针对性红绿测试，关键集成点独立审查，最终必要的全量回归一次；文档变更不重复长时间产品测试。

## Review Focus

1. 绝对时间超过 JavaScript 安全整数或均值窗口跨周期边缘时，仍保留精确索引和时间，并拒用不完整支持窗口。测试归 Task 1、3、8、13。
2. 冻结数据集有大量无关成员或当前成员缺少角度时，只读取本成员的有界证据和文件，不从预览或其他周期补角度。测试归 Task 8。
3. 页面登出、角色撤权或 worker 重启后，旧请求不能借缓存权限、迟到子进程或旧幂等键发布结果。测试归 Task 9、11、13。
4. 当天换设备或点表后，过去结果只按观测期间的事实重新评估；不因无关全局修订号上升而失效，也不能漏掉真实的历史适用性变化。测试归 Task 7、8、10。
5. 参考曲线恒定、仅部分有效或用户开启补绘时，绝对误差仍按定义报告，未定义指标为 null，主指标和来源标签不被展示设置改变。测试归 Task 5、13。

## 交付顺序与代码边界

本计划只落实 WP-05A，不代表整个平台或现场精度验收完成。任务书响应项为 Y1-01、Y1-04、Y3-06；其余研发工作继续在后续工作包推进。

| 阶段 | 任务 | 可检查的交付物 |
| --- | --- | --- |
| 参数和计算基础 | 1—5；6 可在 1 后并行 | 严格参数合同、独立机构/能量基准、分段误差，无数据库的纯计算测试 |
| 生产输入与档案 | 6—8 | 隔离迁移、参数独立审批、完整冻结成员输入与历史点表校验 |
| 后台和复核流程 | 9—11 | 有界计算、恢复、撤销/回退、鉴权 API，关键集成审查 |
| 页面和交付验证 | 12—14 | 参数中心、双图工作台、复核页、隔离端到端及部署回退说明 |

Task 2—5 依次依赖；Task 6 只依赖 1，可与纯计算工作并行但数据库测试串行。Task 7 依赖 2、6；8 依赖 7；9 依赖 4、5、8；10 依赖 9；11 依赖 10；12、13 依赖 11，共享导航修改由一个实施者集成；14 依赖全部。不让多个实施者同时修改同一文件或重置同一测试库。

| 路径 | 职责和归属 |
| --- | --- |
| `backend/inversion/{errors,contracts,parameter_contracts,policy,series}.py` | 严格合同、SI/有理数、不可变数据类型，Task 1 |
| `backend/inversion/{geometry,mechanics}.py` | 四连杆、导数、重力和惯量，Task 2 |
| `backend/inversion/{motion,small_linalg}.py` | 七点三次拟合、运动和数值误差，Task 3 |
| `backend/inversion/{losses,solver}.py` | 有功到转矩/载荷及不可观测门禁，Task 4 |
| `backend/inversion/{comparison,display_fill}.py` | 独立时间比较和隔离补绘，Task 5 |
| `backend/inversion/{migrate,privileges,transactions,capacity}.py`、`sql/001_inversion.sql`、`sql/grants.sql` | 独立 schema、角色、幂等、容量和锁，Task 6 |
| `backend/inversion/{parameters,models,selections}.py` | 配置、参数/模型审批和选用，Task 7 |
| `backend/inversion/{input_acquisition,input_contracts,history}.py` | 输入固定、源证据、运行条件、点表历史，Task 8 |
| `backend/waveforms/{member_exports,dataset_context,registry_context,registry_dependency}.py`、`sql/006_member_research.sql` | 狭窄内部成员读取、同游标检查、只读投影；不改旧导出合同，Task 8 |
| `backend/inversion/{runs,worker,compute_child,result_codec}.py` | 持久请求、所有权、子进程预算、不可变结果，Task 9 |
| `backend/inversion/{results,exports}.py` | 结果当前限制、独立复核和审计导出，Task 10 |
| `backend/inversion/{api,routes,permissions,queries,config,runtime,__main__}.py` | HTTP、配置、健康和工作进程入口，Task 11 |
| `frontend/inversion/` 及三个 `frontend/pages/production-*.js` | 新生产页面，不扩展演示算法页面，Task 12—13 |
| `tests/test_inversion_*.py`、`tests/inversion_*.py`、`tests/fixtures/inversion_*` | 数值向量、隔离数据库和真实浏览器交互，各任务随实现加入 |
| `tools/{provision_inversion_test,verify_inversion}.py`、`docs/功图反演部署与回退.md` | 隔离环境与交付验证，Task 6、14 |

既有接缝已核对：`backend/waveforms/dataset_context.py` 的 `certificate/current/recheck_dataset`；`export.py` 的 actor 绑定导出；`quality_context.py` 的受审计 `_raw` 和校准通道组装；`timing.ChannelData`；`registry.transactions.lock_history`；`backend/auth.py`；`backend/http_server.py`；`backend/seed.py`；`app.js` 的导航、landing 和页面上下文。禁止复制预览 `align_pair` 作为计算网格。

## 测试和提交约定

执行时先读取设计全文。使用既有隔离 worktree；按 worktree 技能核实，不另复制演示版。PowerShell 在工作区运行：

```powershell
$invPython = 'C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe'
$invGit = 'C:/Users/hp/AppData/Local/hermes/git/cmd/git.exe'
$env:PYTHONUTF8 = '1'
```

每个任务列出的 unittest 命令，红灯必须因新增断言或缺少实现失败，不能把配置缺失算作红灯。绿灯要求退出 0、0 failures/errors/skipped；新增浏览器用例还须检查 runner 的 `ok:true`。只读计划阶段不启动数据库、不运行产品测试。

下面各任务给出关键断言代码；代码中的case对象由该任务明确描述的fixture创建，其他列出的失败用例也须实现，不能只完成示例断言。

数据库使用已校验的独立 `oilwell_waveforms_test`、127.0.0.1:3822 和 registry sidecar。Task 6 加专用 inversion sidecar/角色，不修改原配置指向，不把任何测试变量指向生产库。fixture 在执行迁移/清理之前核验集群 system identifier、owner、数据库注释、角色及专用目录；失败必须停止。密码由已有私密配置和凭据存储提供，不打印、不提交。

Task6的私密sidecar目录固定 `data/waveform-test/inversion-sidecar`，由工具生成 `identity.json` 和 `inversion-test.local.toml`；`OILWELL_INVERSION_TEST_CONFIG` 只允许指向该配置的已解析绝对路径。已有 `OILWELL_WAVEFORM_TEST_CONFIG` 与 `OILWELL_WAVEFORM_PROVISION_IDENTITY` 保留父fixture绑定。先完成provision工具的无数据库红绿测试，再在已授权隔离环境建立sidecar并进入schema/权限红绿测试；缺少凭据本身不算红灯测试通过。

每项第 5 步：只暂存该任务 Files 所列且实际修改的文件，执行 `& $invGit diff --cached --check`，检查差异、提交并记录红绿证据与 HEAD。不得 `git add .` 或提交凭据、数据库、临时报告。独立审查按用户选择的执行方式进行，不把旧分支测试计为新功能通过。

## Task 1 严格参数与输入类型合同

**Files:** Create `backend/inversion/__init__.py`、`errors.py`、`contracts.py`、`parameter_contracts.py`、`policy.py`、`series.py`；Create `tests/inversion_vectors.py`、`tests/test_inversion_contracts.py`。

**Interfaces:**

- `contracts.py` 定义 `InversionCommand(actor: Actor, key: UUID, expected: VersionPrecondition, reason: str)`；复用 WP-04 `Actor`，不复用把版本 0 当有效的旧命令。`VersionPrecondition` 为不存在 `absent` 或正整数版本 `match`，其他取值拒绝。
- 同文件定义 `JsonValue` 为严格JSON递归类型、`JsonObject` 为只读字符串键映射，以及 `AuthorityRef(domain: str,user_id: int,session_id: int)`、`AuthoritySnapshot(ref: AuthorityRef,permissions: frozenset[str],checked_at: datetime,user_version: int)`。这些类型在Task9和11共用，不能依赖后续API模块。
- `parameter_contracts.parse_parameter_set(raw: bytes, *, draft: bool=False) -> ParameterSet`；`parameter_contracts.missing_fields(p: ParameterSet) -> tuple[str,...]`。类型均 frozen；原始规范正文和 SHA256 保留。`canonical_bytes(value: JsonValue) -> bytes` 使用严格 JSON、规范十进制字符串，不通过浮点再生成参数。
- `policy.parse_policy(raw: bytes) -> AlgorithmPolicy`；包含数值裕量、拟合界限、误差/覆盖率界限、参考最大间隔、补绘时间/角跨度、相对误差最小分母，均显式提供，不设置“现场通用”值。
- `series.Sample(time_ns: Fraction, index: int, value_si: Fraction|None, quality: int)`；`SampleSeries(role: str, samples: tuple[Sample,...], source: ChannelPin)`，索引来自原始文件。`PredictionInputs(power: SampleSeries, crank_angle: SampleSeries, cycle: CycleWindow, power_window_ns: int, time_bound_ns: Fraction)`；`ReferenceInputs(load: SampleSeries, displacement: SampleSeries)`。预测对象不能包含后者。
- `series.ChannelPin` 包含 capture/stream/channel身份、manifest摘要、校准/采样证据pins和原始首末索引；`CycleWindow(start_ns: Fraction,end_ns: Fraction,physical_cycle_id: str)`，有效区间含端点但绑定有效期仍为半开。`ParameterSet` 的组字段对应下表，`AlgorithmPolicy` 由policy模块定义，不用未验证dict代替类型。

参数字段由以下固定组组成，每组带 `evidence_refs`，证据 pin 为 `{id, content_sha256, review_version}`。单位在字段后缀固定，浏览器不提交自由单位表达式。

| 组 | 固定字段 |
| --- | --- |
| 身份 | `schema, assembly_id, assembly_version, source_kind, valid_from, valid_to, parent_version, provenance` |
| geometry | `pivot_x_m, pivot_y_m, crank_radius_m, pitman_length_m, rear_arm_m, horsehead_radius_m, branch, rotation_sign, displacement_sign` |
| phase | `channel_pin, zero_reading_rad, encoder_direction, unwrap_kind, evidence_refs`；`unwrap_kind` 仅 `wrapped_2pi` 或 `unwrapped` |
| bodies | `crank_inertia_kg_m2, crank_first_moment_x_kg_m, crank_first_moment_y_kg_m, beam_inertia_kg_m2, beam_first_moment_x_kg_m, beam_first_moment_y_kg_m, pitman_mass_kg, pitman_com_fraction, pitman_inertia_kg_m2, gravity_m_s2, component_ledger` |
| drive | `supply_kind, ratio, upstream_inertia_kg_m2, loss_grid, crank_friction_Nm, viscous_friction_Nm_s, evidence_refs`；`supply_kind=direct_line_induction` |
| domain | `power_W, speed_rad_s, acceleration_rad_s2, temperature_K, supply_voltage_V, supply_frequency_Hz, bandwidth_Hz, window_ns`；区间均上下界 |
| uncertainty | `torque_bound_Nm, factor_bound_m, speed_bound_rad_s, numerical_budget_Nm, numerical_factor_budget_m, evidence_refs`；覆盖批准域的保守包络，非概率 |
| policy | `AlgorithmPolicy` 的固定正文和摘要；模型版本与 policy 摘要共同固定 |

`loss_grid` 固定为 `power_W[]、motor_speed_rad_s[]、loss_W[][]、error_W[][]、evidence_refs`；`component_ledger` 为部件 ID/名称/`upstream|crank|beam|pitman` 归属，禁止重复部件。`provenance` 包括来源机构/日期/附件 pins、标定周期范围；审核身份由服务写入而非客户端冒填。

为避免新增另一套条件审批工作流，首版 `provenance.operating_conditions` 保存有ID的运行条件证据记录：适用起止时间、温度/电压/频率的含不确定性界限、来源说明及附件pins，最多64条；随参数版本一起独立审批。运行请求仅选择其中的记录ID，快照值由服务端取出，不接受用户临时填写“已批准”的温度或供电值。新增条件依据需要参数后继版本，但不改旧物理参数正文。`provenance.channel_bindings` 对各必需角色固定 `native_waveform|modbus`、ChannelPin、独立接线/安装依据；Modbus另外绑定device/map_version/hash/point_id，Task8核查观测期分配。

- [ ] **1 写失败测试。** `test_parameter_contract_is_strict` 验证重复键、NaN/Infinity、未知字段、缺证据、未知 schema 和执行表达式拒绝；`test_loss_grid_boundaries` 断言每轴 2/64 可接受、1/65 拒绝、负损耗拒绝、二维尺寸一致；`test_no_implicit_parameters` 验证草稿缺失列表及提交缺参失败；`test_exact_index_and_reference_separation` 保留 `9007199254741001` 并断言 `PredictionInputs` 没有 load/displacement 字段。

```python
# test_exact_index_and_reference_separation
self.assertEqual(Sample(Fraction(1, 3), 9007199254741001, Fraction(2), 0).index, 9007199254741001)
self.assertFalse({'load', 'displacement'} & set(PredictionInputs.__dataclass_fields__))
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_contracts -v`。
- [ ] **3 实现上述类型/解析器。** JSON 256 KiB 上限，文本/UUID/摘要沿用现有严格规则；SI 数值解析为 Decimal/Fraction，明确零值与缺失不同。样本上限 32768、严格递增时间、原始索引及质量码检查；草稿不能作为计算参数。
- [ ] **4 绿灯。** 重跑同命令；加边界正文恰好 262144 字节可解析、262145 拒绝的有效填充测试，禁止先完整加载无界正文。
- [ ] **5 提交。** `feat(inversion): define strict parameter and series contracts`。

## Task 2 四连杆与刚体机械内核

**Files:** Create `backend/inversion/geometry.py`、`mechanics.py`；Create `tests/test_inversion_geometry.py`、`tests/test_inversion_mechanics.py`、`tests/inversion_geometry_oracle.py`。依赖 1。

**Interfaces:** `geometry.build_geometry(parameters: ParameterSet) -> BeamGeometry`；`BeamGeometry.at(theta_rad: float) -> GeometryPoint`，返回 `s_m,J_m,beta_rad,beta_prime,beta_second,P,R,C,psi_prime,psi_second` 及条件量；`mechanics.energy_terms(geometry: BeamGeometry, theta_rad: float, parameters: ParameterSet) -> EnergyTerms(U_prime_Nm,M_kg_m2,M_prime_kg_m2)`。无数据库/参考曲线依赖。

- [ ] **1 写失败测试。** `test_fourbar_closure_and_direction` 使用明确 synthetic 的 `B=(3,2), r=0.5,c=2,l=3,a=2` 独立两圆求交，整周闭合残差小于 `1e-10 m`；顺/逆转只改变正确的映射。`test_virtual_work_derivatives` 以独立中心差分 `h=1e-5 rad` 验证非病态点 J/二阶导数（绝对容差 `1e-7/1e-5`），不是现场精度。`test_no_double_counted_energy` 验证单一质量/惯量手算和零上游惯量；`test_degenerate_assembly_refused` 覆盖圆相切、分离、包含、闭环分支跳变及非有限几何。

```python
# test_fourbar_closure_and_direction; g 为本用例的 GeometryPoint
self.assertAlmostEqual(math.dist(g.R, g.P), 3.0, delta=1e-10)
self.assertAlmostEqual(math.dist(g.R, (3.0, 2.0)), 2.0, delta=1e-10)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_geometry tests.test_inversion_mechanics -v`。
- [ ] **3 实现四连杆闭合及隐式一/二阶导数。** 使用设计第 5 节坐标和绝对悬点载荷定义；由 `|B|±r` 的距离范围先检查整周可装配性，再求固定分支。极值搜索固定容差和上限，不扫描参考图找死点。计算势能导数、等效惯量及其导数，按 component ledger 单次计入。求解误差超政策时拒绝，不以 epsilon 改几何。
- [ ] **4 绿灯。** 重跑同命令；独立 oracle 不 import 被测 geometry/mechanics，比较至少 72 个非病态角点和两死点邻域。
- [ ] **5 提交。** `feat(inversion): add beam geometry and rigid-body energy model`。

## Task 3 有理时间与曲柄运动估计

**Files:** Create `backend/inversion/motion.py`、`small_linalg.py`；Create `tests/test_inversion_motion.py`。依赖 1、2。

**Interfaces:** `motion.estimate_motion(inputs: PredictionInputs, parameters: ParameterSet, policy: AlgorithmPolicy) -> tuple[MotionPoint,...]`；`MotionPoint` 固定原功率索引/时间、θ/ω/α、原角度索引七元组、拟合残差/条件数、速度误差界与状态。`small_linalg.cubic_fit(times_s: tuple[float,...], angles_rad: tuple[float,...]) -> CubicFit` 为七行四列 Householder QR，不用正规方程。

- [ ] **1 写失败测试。** `test_cubic_motion_goldens` 以 `theta=1+2t+3t²+4t³` 验证 θ/ω/α，正常相对时间下绝对误差 `1e-9`；`test_large_epoch_is_translation_invariant` 在整数纳秒大时间原点平移后结果不变；`test_seven_points_without_gap_or_extrapolation` 覆盖 6/7 点、重复时间、非好质量、缺口、周期外及单侧窗口；`test_mean_support_crosses_cycle` 中心在区间内但窗口越界时无效；`test_noisy_or_reversed_motion_refused` 超批准残差/条件数、未解释反转、速度不确定区间跨零拒用。

```python
# test_cubic_motion_goldens; 目标 t=0，七个样本包围该点
self.assertAlmostEqual(point.theta_rad, 1.0, delta=1e-9)
self.assertAlmostEqual(point.omega_rad_s, 2.0, delta=1e-9)
self.assertAlmostEqual(point.alpha_rad_s2, 6.0, delta=1e-9)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_motion -v`。
- [ ] **3 实现相位映射和确定性窗口。** 精确相对时间转换后取目标附近七个连续有效样本，内部优先三前四后，边缘在同一选择区间移窗，不能跨无效点。角度回绕依据已批准转向和速度上界，存在多圈混叠解时拒绝。保留窗口中心语义，校准后的时间不重复修正。
- [ ] **4 绿灯。** 同命令；另跑 `& $invPython -m unittest tests.test_waveform_timing -v`，证明底层时间语义未回归。
- [ ] **5 提交。** `feat(inversion): estimate synchronized crank motion with bounded fits`。

## Task 4 带符号功率反演与不可观测门禁

**Files:** Create `backend/inversion/losses.py`、`solver.py`；Create `tests/test_inversion_solver.py`、`tests/test_inversion_losses.py`。依赖 1—3。

**Interfaces:** `losses.lookup_loss(parameters: ParameterSet, power_W: float, motor_speed_rad_s: float) -> LossPoint(loss_W,error_W)`；`solver.solve_load(*,drive_Nm: float,energy: EnergyTerms,motion: MotionPoint,J_m: float,parameters: ParameterSet) -> LoadPoint`；`solver.predict(inputs: PredictionInputs, parameters: ParameterSet, policy: AlgorithmPolicy) -> PredictionSeries`。`PredictionSeries.points` 与所有原生功率中心一一对应，点含 `time_ns,power_index,s_m,load_N,error_bound_N,status,reason_codes,intermediates`；无效载荷为 None。

- [ ] **1 写失败测试。** `test_loss_is_nonnegative_and_signed_domain` 检查双线性手算、跨零和边界，域外拒绝；`test_signed_energy_balance` 检查正驱动/回馈及 `Tdrive=(Pe-L)/ω-Iupα`；`test_absolute_load_no_static_offset` 无静载荷重复添加；`test_deadpoint_error_bound` 对 `N=10,J=.5,uN=1,uJ=.1` 断言名义载荷 20、误差界 7.5，J=0.1/0/负值分别处理；`test_reference_cannot_affect_prediction` 预测接口拒绝额外参考输入；`test_oversize_and_nonfinite_refused` 检查溢出/点数。

```python
# test_deadpoint_error_bound; 理想测试的附加数值预算为显式零
self.assertAlmostEqual(point.load_N, 20.0)
self.assertAlmostEqual(point.error_bound_N, 7.5)
self.assertIsNone(deadpoint.load_N)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_solver tests.test_inversion_losses -v`。
- [ ] **3 实现设计公式。** `N=Tdrive-U'-Mα-.5M'ω²-(Tc+bω)`；总误差界采用已批准uN/uJ加各自独立数值预算。损耗表误差已经属于uN覆盖范围，审核/准入应验证覆盖依据，不再次无条件相加；未知覆盖拒算。推导写在函数说明并用手算验证。点状态使用设计枚举；保留超范围中间量但不可把坏载荷当有效。全部点无效或覆盖不足时系列明确不适用，不生成成功曲线。
- [ ] **4 绿灯。** 同命令及 `tests.test_inversion_geometry tests.test_inversion_mechanics tests.test_inversion_motion`；有无速度变化分别验证机械能守恒，用独立分析构造的力而非旧演示模板。
- [ ] **5 提交。** `feat(inversion): solve signed surface loads with observability bounds`。

## Task 5 独立比较与隔离补绘

**Files:** Create `backend/inversion/comparison.py`、`display_fill.py`；Create `tests/test_inversion_comparison.py`、`tests/test_inversion_display_fill.py`。依赖 4。

**Interfaces:** `comparison.compare(prediction: PredictionSeries, reference: ReferenceInputs, policy: AlgorithmPolicy) -> ComparisonReport`；`display_fill.fill_deadpoints(prediction: PredictionSeries, policy: AlgorithmPolicy) -> FilledSeries`。report 的 `primary`、`by_stroke`、`coverage`、`invalid_reasons`、`work`、`independence` 与另算 `filled_comparison` 不混写；每个 Metric 含 value 或 null、unit、reason。

- [ ] **1 写失败测试。** `test_constant_reference_keeps_absolute_error`：时间 `[0,1,3]s`、参考 10N、预测 12N，MAE/RMSE/bias=2N，相关系数 null。`test_time_weighting_not_sample_average`：误差线性 `[0,2,2]` 时 bias=`5/3`、RMSE=`sqrt(28/9)`；`test_work_preserves_traversal` 1m×10N 矩形循环有符号功10J，倒序为-10J。`test_gap_never_contributes_work_or_coverage` 断点两侧不连线，完整周期功 null。`test_fill_cannot_change_primary` 补绘前后主指标及预测摘要相同，填补点有来源且只覆盖 unobservable。

```python
# test_constant_reference_keeps_absolute_error
self.assertEqual(report.primary['load_mae'].value, 2.0)
self.assertIsNone(report.primary['load_correlation'].value)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_comparison tests.test_inversion_display_fill -v`。
- [ ] **3 实现精确时间分段积分。** 每段两端有效才纳入；线性误差的 bias 积分用梯形、平方误差用 `dt*(e0²+e0*e1+e1²)/3`，绝对误差跨零时分段。插值记录参考两端索引和权重，超过最大间隔拒绝。冲程按 `Jω` 符号，过零分段。补绘只使用预测有效两端，时间线性，周期接缝单独标假设，不用参考曲线。
- [ ] **4 绿灯。** 同命令，增加改变参考载荷/位移后预测逐点字节不变、仅比较结果变化；无时长、零相对分母、孤立点、负功均有明确结果。
- [ ] **5 提交。** `feat(inversion): compare independent cards and isolate display fills`。

## Task 6 隔离数据库与原子事务基础

**Files:** Create `backend/inversion/{migrate,privileges,transactions,capacity}.py`、`sql/001_inversion.sql`、`sql/grants.sql`；Create `tools/provision_inversion_test.py`、`tests/inversion_support.py`、`tests/test_inversion_{schema,privileges,transactions,capacity,provision}.py`。依赖 1，可与 2—5 独立推进。

**Interfaces:** `migrate.migrate(settings: dict, *, expected_database: str, limits: InversionLimits) -> dict`；`privileges.probe_role(connect, role: str) -> None`；`transactions.run(command: InversionCommand, apply, *, dependencies: LockPlan) -> Receipt`；`capacity.reserve(cur, *, run_id: UUID, limits: InversionLimits) -> Reservation` 与 `settle(cur, reservation: Reservation, used_bytes: int) -> None`。锁方案是内部固定枚举，客户端不能传 SQL/表名。

`capacity.InversionLimits` 固定全局约束中的槽数/队列/点数/暂存字节/截止时间及配置的耐久存储总额度；未配置总额度不得启用。`Reservation` 保存run_id、预留输入/结果字节和结算版本。`transactions.Receipt` 为subject_id/version/content_sha256/replayed；`LockPlan` 为所需guard/稳定依赖ID的内部类型。apply签名固定 `(cur,command) -> Change`，Change含待写审计action/subject/response；数据库提交成功后才返回Receipt。

- [ ] **1 写失败测试。** `test_wrong_cluster_causes_zero_ddl`、`test_no_escalation_of_receiver`、`test_immutable_payload_update_rejected`；`test_actor_key_body_replay` 同 actor/key/body 同结果、不同正文409；`test_audit_receipt_and_business_rollback_together`；`test_queue_and_byte_budget_race` 并发不能超1运行槽/16等待或字节限额，重复结算不负数。

```python
# test_audit_receipt_and_business_rollback_together; 注入审计写入失败
self.assertEqual((business_rows, audit_rows, receipt_rows), (0, 0, 0))
```

- [ ] **2 红灯。** 在核验过的既有隔离集群中新增本模块 sidecar 后运行 `& $invPython -m unittest tests.test_inversion_provision tests.test_inversion_schema tests.test_inversion_privileges tests.test_inversion_transactions tests.test_inversion_capacity -v`。管理员或集群身份不可得则报告环境缺口，不改业务库补救。
- [ ] **3 实现独立 owner/reader/operator，迁移和 fixture。** 表覆盖设计第7节所有对象，正文和摘要不可更新/删除，状态仅由追加事件及受约束状态表改变。跨schema依赖保存不可变ID/hash并在服务层同事务核对，不新增影响既有测试独立性的跨schema级联外键。fixture 持有既有 TEST_LOCK，并只重置已列举的本测试 inversion 表；涉及父波形/台账夹具时沿用其已校验 fixture，不扩大 TRUNCATE。容量持久配置含请求数/点数/字节限额，失败也结算。迁移记录校验脚本与结构摘要，禁止启动自动迁移。
- [ ] **4 绿灯。** 同命令；schema 完整/部分应用/内容漂移、角色继承和 PUBLIC 权限均覆盖。SQL 语句和锁超时分别3s/1s，测试确认事务未泄漏。
- [ ] **5 提交。** `feat(inversion): add isolated durable schema and transaction limits`。

## Task 7 机械参数档案与独立审批

**Files:** Create `backend/inversion/{parameters,models,selections}.py`；Create `tests/test_inversion_parameters.py`、`tests/test_inversion_models.py`。依赖 2、6。

**Interfaces:** `ParameterService.create_assembly(body: JsonObject, command: InversionCommand) -> Receipt`、`assign(...)`、`create_version(...)`、`submit(id: UUID, command: InversionCommand)`、`review(id: UUID, decision: str, command: InversionCommand)`、`revoke(id: UUID, command: InversionCommand)`；`ModelService.register(installed_id: str, command)`/`review`/`revoke`；`SelectionService.select(body, command)`/`rollback(selection_id: UUID, command)`。ModelRecord 绑定已安装文件、Python/依赖和算法政策摘要，无可执行上传。

上列 `assign/create_version/select` 均为 `(body: JsonObject,command: InversionCommand) -> Receipt`，所有command均为同一严格类型；submit/review/revoke/register/rollback均返回Receipt。ModelService的review/revoke与ParameterService同签名，只改变目标类型。`ParameterService.get(id: UUID) -> ParameterRecord` 返回固定参数正文/摘要及追加审批状态，`ModelService.get(id: UUID) -> ModelRecord` 返回固定制品与审批；Task8使用这些读模型，不依赖HTTP。

- [ ] **1 写失败测试。** `test_half_open_assignment_and_parameter_validity` 覆盖有效期端点和时钟不确定区间；`test_creator_submitter_not_reviewer` 同人编制/提交可行，自批包括admin拒绝；`test_synthetic_cannot_bind_production`；`test_parameter_attachment_and_loss_provenance_required`；`test_only_compatible_approved_rollback` 当前/撤销/未知版本不得作为回退目标；`test_later_install_does_not_rewrite_history`。

```python
# test_later_install_does_not_rewrite_history
self.assertEqual(old_version_after.content_sha256, old_version_before.content_sha256)
self.assertEqual(old_version_after.valid_to, old_version_before.valid_to)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_parameters tests.test_inversion_models -v`。
- [ ] **3 实现不可变版本及追加审批。** 参数提交调用 Task2 几何验证和 Task1 完整性检查；附件调用既有受审计能力，固定 ID/hash/用途，文件摘要不等于真实性。审核记录写实际actor和理由。新安装仅登记未批准 `beam_energy_balance/1` 制品，必须由与登记者不同的人批准；回退用测试制品验证，生产清单不增加假版本。
- [ ] **4 绿灯。** 同命令；显式验证相邻区间合法、重叠绑定拒绝、移井后的历史物理配置仍可追溯，提交回复丢失重试不重复审批/版本。
- [ ] **5 提交。** `feat(inversion): govern mechanical parameters and model selections`。

## Task 8 冻结成员完整输入与历史依据

**Files:** Create `backend/inversion/{input_acquisition,input_contracts,history}.py`、`backend/waveforms/member_exports.py`、`backend/waveforms/sql/006_member_research.sql`；Modify `backend/waveforms/{dataset_context,registry_context,registry_dependency,migrate,privileges}.py`、`backend/waveforms/sql/grants.sql`、`tests/waveform_support.py`；Create `tests/test_inversion_input.py`、`tests/test_inversion_history.py`、`tests/test_waveform_member_research.py`，完善 `tests/inversion_vectors.py`。依赖 7。

**Interfaces:**

- `dataset_context.DatasetContext.load_member_prepared(cur, version_id: UUID, ordinal: int) -> PreparedMember`；`registry_context.resolve_with_cursor(cur, sources, *, utc_start, start_ns, end_ns, uncertainty_ns) -> RegistrySnapshot`，不另开连接。
- `member_exports.MemberResearchExporter.create(version_id: UUID, ordinal: int, command: Command) -> MemberExportRecord`；`open(id: UUID, *, actor: Actor) -> MemberExportContext`；context 的 `stream_capture(key: CaptureKey)` 为单次迭代器，逐文件/原文读取审计先于字节。新增内部表保存 member ordinal/证据摘要，不修改已有全数据集导出语义，不新增匿名下载端点。
- `input_acquisition.prepare_input(request: RunRequest, actor: Actor, *, check: Callable[[],None]) -> PreparedInput`；`history.recheck_with_cursor(cur, snapshot: InputSnapshot) -> tuple[str,...]`。PreparedInput 分别保存 `prediction_inputs`、`reference_inputs`、已批准参数、policy 和规范快照，重计算前不能遗漏引用正文。
- `input_contracts.parse_run_request(raw: bytes) -> RunRequest` 定义Task11表中固定运行字段，末项为 `operating_condition_ids: tuple[str,...]`；同模块定义 `InputSnapshot`（设计4.1固定身份/正文/摘要）及 `PreparedInput`。`PreparedMember` 由新增dataset_context方法定义，只含单成员证书、至多两份文件描述/计划、成员适用性；不得携带整个数据集的其他成员。

- [ ] **1 写失败测试。** `test_member_is_bounded_with_1000_other_members` 读取计划最多2capture、不调用全数据集 `load_prepared` 或预览；`test_frozen_selection_cannot_gain_angle`；`test_canonical_hash_all_chunks_and_single_calibration`；`test_modbus_map_covers_uncertain_observation` 缺分配/错误hash/未批准/跨边界拒绝，native_waveform 不伪造点表；`test_independent_conditions_and_calibration_cycles` 未知温度/供电、客户端伪造条件记录、当前周期参与标定不得通过现场独立评价；`test_current_check_uses_same_transaction`。

```python
# test_member_is_bounded_with_1000_other_members; 监视实际原文读取身份
self.assertEqual(set(read_capture_ids), selected_capture_ids)
self.assertLessEqual(len(set(read_capture_ids)), 2)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_input tests.test_inversion_history tests.test_waveform_member_research -v`。
- [ ] **3 实现窄成员读取和只读点表投影。** 006 迁移、registry-owner 受控投影配置分别显式执行；新 inversion operator 只有核对所需元数据/视图和既有锁函数权限，不能读取原文基表或获得 receiver/owner 成员资格。准备原文使用 WP-04 reader/operator 独立连接，在业务锁外；提交重验只用同游标元数据。按冻结文件计划核验完整规范文件和所有块，但只把选中角色/索引区间投影为计算值。每个文件重新创建 actor 绑定一次性 context；耗时循环调用 check，超过32768不抽样。新成员导出表纳入父fixture明确的RESET_TABLES，不用CASCADE绕过依赖；保留旧schema升级及角色探测测试。
- [ ] **4 绿灯及接缝审查。** 同命令，再运行 `& $invPython -m unittest tests.test_waveform_datasets tests.test_waveform_export tests.test_waveform_registry_dependency tests.test_waveform_timing tests.test_registry_maps -v`。独立检查输入独立性、点表证据、锁顺序、计费及单成员上限，问题修复后只重跑受影响测试。
- [ ] **5 提交。** `feat(inversion): pin audited member inputs and historical applicability`。

## Task 9 有界持久运行与恢复

**Files:** Create `backend/inversion/{runs,worker,compute_child,result_codec}.py`；Create `tests/inversion_worker.py`、`tests/test_inversion_runs.py`、`tests/test_inversion_worker.py`、`tests/test_inversion_recovery.py`。依赖 4、5、8。

**Interfaces:** `RunService.create(body: RunRequest, command: InversionCommand, authority: AuthorityRef) -> RunRecord`、`claim(owner: UUID) -> Attempt|None`、`cancel(id,command)`、`retry(id,command,authority)`、`publish(attempt: Attempt, prepared: PreparedResult, authority: AuthoritySnapshot) -> ResultRecord`；`worker.run_once(runtime: WorkerRuntime) -> bool`；`compute_child.compute(input_path: Path, output_path: Path) -> None`。路径仅由监护进程创建，HTTP 不能控制。`result_codec.encode(prediction: PredictionSeries, comparison: ComparisonReport, snapshot: InputSnapshot) -> PreparedResult` 输出有界固定列块及摘要。

`RunRecord/Attempt/ResultRecord` 由runs模块定义，分别固定请求状态、owner/generation/deadline、结果ID/hash；`PreparedResult` 由result_codec定义，包含固定输入摘要及有界结果块。`WorkerRuntime` 在worker模块定义为配置和注入的prepare/refresh_authority/publish服务，不import后续HTTP runtime，Task11负责组装。每个attempt的受监护执行子进程覆盖输入准备、纯计算、编码及提交全过程；监护进程用单调时钟计总期限，并拥有终止/等待责任。

- [ ] **1 写失败测试。** `test_committed_request_survives_http_disconnect`；`test_same_key_returns_same_run`；`test_claim_one_slot_and_fenced_publication`；`test_restarted_owner_fails_without_autoretry`；`test_cancel_and_commit_have_one_terminal`；`test_timeout_covers_prepare_and_serialize`；`test_timeout_during_commit_reconciles_durable_terminal`；`test_limits_clean_only_owned_temp`；`test_publish_rechecks_revoked_input`。取消 queued 请求立即终态并释放预留，不能等领取。

```python
# test_same_key_returns_same_run
self.assertEqual(first.id, replay.id)
self.assertEqual(durable_request_count, 1)
self.assertLessEqual(durable_result_count, 1)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_runs tests.test_inversion_worker tests.test_inversion_recovery -v`。
- [ ] **3 实现请求/attempt/结果状态机。** 领取记 owner/generation/总deadline，心跳不能延长；使用数据库单槽租约和代际阻止旧进程发布。Windows 使用 spawn 子进程，监护只终止/等待其明确创建的子进程，配合子进程控制通道和整体截止时间。断开HTTP不取消耐久任务；显式取消可终止计算。输出先生成/核验16MiB内固定列JSON块，再短事务重验依赖、终态/所有者及当前权限，原子保存结果/审计/结算。失主 attempt 尚无耐久终态时置failed/worker_interrupted；超时或失联先按原run/generation核对提交结果，不能覆盖已经completed的记录。显式 retry 新run；数据或授权不合格 blocked，资源/内部错误 failed。
- [ ] **4 绿灯。** 同命令；期限测试用可控时钟/故障注入并加一次真实子进程回收测试，包括准备阶段阻塞。核验回复丢失、磁盘满、数据库提交失败、旧generation迟到，既无重复结果也无孤儿进程/预留资源。纯计算函数不接收数据库凭据，受监护执行进程仅允许配置的数据库访问；不得执行用户代码或跟随参数中的外部URL。
- [ ] **5 提交。** `feat(inversion): run bounded durable calculations with recovery`。

## Task 10 结果复核与审计导出

**Files:** Create `backend/inversion/results.py`、`exports.py`；Create `tests/test_inversion_results.py`、`tests/test_inversion_export.py`。依赖 9。

**Interfaces:** `ResultService.get(id: UUID, *, actor: Actor) -> ResultView`、`review(id,decision,command)`、`revoke(id,command)`；`ResultExporter.create(result_id: UUID, intent: Literal['quantitative','evidence'], format: Literal['json','csv'], command) -> ExportRecord`、`open(id, *, actor) -> ResultExportContext`。context 首次取字节先检查当前授权/依赖并持久审计。

- [ ] **1 写失败测试。** `test_completed_is_not_approved`；`test_result_cannot_be_self_reviewed`；`test_related_revoke_restricts_but_keeps_history`；`test_unrelated_registry_revision_preserves_validity`；`test_export_audit_failure_yields_zero_bytes`；`test_evidence_export_is_explicitly_restricted`；`test_csv_formula_injection_and_secret_redaction`。

```python
# test_export_audit_failure_yields_zero_bytes
self.assertEqual(bytes_received, b'')
self.assertEqual(successful_download_receipts, 0)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_results tests.test_inversion_export -v`。
- [ ] **3 实现只追加复核和当前限制。** ResultView 分开保存/返回 execution、source、review、current_applicability、field_validation；现场状态最多单次对照，低误差不自动改变资格。定量导出要求当前适用且已独立批准；证据导出需额外 audit.read、显式intent，含restricted标记/原因。JSON/CSV包含完整计算点及原始索引，不仅页面抽样点；不包含密码、原始账户资料或本机路径。
- [ ] **4 绿灯。** 同命令；导出创建后撤销依赖、另一个actor下载、旧session、空指标和部分有效图都覆盖，历史摘要不变。审计通过后传输中失败只能中止，不能返回完整成功回执。
- [ ] **5 提交。** `feat(inversion): govern results and audited exports`。

## Task 11 当前授权 API 与可选运行时

**Files:** Create `backend/inversion/{api,routes,permissions,queries,config,runtime,__main__}.py`、`config/inversion.example.toml`；Modify `backend/auth.py`、`backend/http_server.py`、`backend/seed.py`、`server.py`；Create `tests/test_inversion_{api,permissions,startup,config,authority}.py`。依赖 10。

**Interfaces:** `build_inversion_runtime(config_path: Path|None, *, auth: AuthService) -> InversionRuntime`，状态 `disabled|misconfigured|ready|degraded`；`InversionApi(handler).dispatch() -> bool`。`AuthService.capture_background_authority(token: str, permissions: frozenset[str]) -> AuthorityRef` 与 `refresh_background_authority(ref: AuthorityRef, permissions: frozenset[str]) -> User`；AuthorityRef 只含固定domain/user_id/session_id，不保存bearer token/密码。Task9以依赖注入调用刷新，生产实现此时接通。

后台任务在领取和发布前校验原session未过期/撤销、用户与角色active及实时权限；登出后的未完成请求blocked。SQLite与PostgreSQL不宣称原子撤权，记录检查时点；读取/导出仍每次重新鉴权。

所有资源在 `/api/production-inversion` 下：

| 方法和路径 | 严格正文/响应及权限 |
| --- | --- |
| GET `/health`、`/assemblies`、`/assemblies/{id}`、`/parameters`、`/parameters/{id}`、`/models` | `inversion.read`；列表 offset/limit 默认0/50、最大100 |
| POST `/assemblies`、`/assignments`、`/parameter-versions` | `{command,body}`；参数版本正文为 Task1 合同，parameters.manage |
| POST `/parameters/{id}/submit`、`/parameters/{id}/review`、`/parameters/{id}/revoke` | submit/revoke 为 `{command}`；review 为 `{command,body:{decision}}`，decision仅approve/reject；submit需manage，其余review |
| POST `/models`、`/models/{id}/review`、`/models/{id}/revoke` | 安装清单 ID，不接受代码；models.manage/review |
| GET `/assemblies/{id}/selections`；POST `/selections`、`/selections/{id}/rollback` | 选用正文含assembly_id、parameter_id/hash/review_version、model_id/hash/review_version；models.manage |
| POST `/runs/preflight`、`/runs` | 固定 `dataset_version_id, dataset_sha256, member_ordinal, pair_id, pair_sha256, pair_review_version, assembly_id, parameter_id, parameter_sha256, parameter_review_version, model_id, model_sha256, model_review_version, selection_version, operating_condition_ids`；run+waveform read/raw.read/export |
| GET `/runs`、`/runs/{id}`；POST `/runs/{id}/cancel`、`/runs/{id}/retry` | read；写需run，取消本人或administrator，重试重新检查原固定输入 |
| GET `/results/{id}`、`/results/{id}/points`、`/results/{id}/comparison` | read；points 用严格 offset/limit，最大1000，精确索引/时间为字符串 |
| POST `/results/{id}/review`、`/results/{id}/revoke` | review，禁止自批 |
| POST `/exports`；GET `/exports/{id}` | 固定 result_id/intent/format，export+waveform read/raw.read/export；evidence另需audit.read |
| GET `/audit` | audit.read，有界过滤/分页；无任意SQL |

`command` 为 `{key, expected, reason}`，key为UUID，expected为Task1前提；actor只取鉴权身份。`runs/preflight` 是有界元数据检查，不创建run/结果、不读取原始样本、不占执行槽；返回缺失列表不承诺后续一定成功。run持久提交成功返回202+run_id；幂等重放返回同记录。POST其他资源提交成功返回201/200；合同400、未登录401、权限403、不存在404、版本冲突409、容量/服务不可用503，错误体 `{code,message,details}` 不泄露SQL或配置。

有状态POST采用 `{command,body}`，无额外正文的动作省略body；preflight仅 `{body}`，作为无副作用检查不要求写入幂等回执。Task9的AuthorityRef只能由服务端在已鉴权请求中产生，不是运行请求的可写字段。

默认角色：administrator全9项；production_engineer read/run/review；algorithm_engineer read/parameters.manage/run/models.manage/export；maintenance_engineer read/parameters.manage；reviewer read/parameters.review/review/models.review/audit.read。运行仍需三项waveform能力：不自动扩张旧waveform权限，缺失时管理员显式授予；页面显示缺哪些权限，不能宣称仅凭岗位即可运行。

- [ ] **1 写失败测试。** `test_authority_is_fresh_before_replay_claim_and_commit`、`test_worker_never_persists_bearer`、`test_new_permissions_granted_once_not_restored`、`test_disabled_runtime_never_falls_back`、`test_unknown_method_path_headers_and_fields_refused`、`test_body_limit_before_json_parse`、`test_startup_does_not_ddl_or_start_worker`、`test_preflight_is_not_calculation`。

```python
# test_preflight_is_not_calculation; 合格元数据的预检请求
self.assertEqual(response.status, 200)
self.assertEqual((new_runs, raw_reads, occupied_slots), (0, 0, 0))
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_api tests.test_inversion_permissions tests.test_inversion_startup tests.test_inversion_config tests.test_inversion_authority -v`。
- [ ] **3 实现上述路由、鉴权和配置。** server新增 `--inversion-config` 并贯穿 launch/run/create_server；HTTP auth/me新增 capability，关闭时无DDL或后台自动启动。独立worker入口 `python -m backend.inversion worker --config <private-path>`。后台身份刷新直接使用同一已有SQLite文件的只读连接、固定查询和短超时，不调用会建目录/改WAL模式的普通Database.connection；缺库/缺表/锁超时拒绝，不能初始化空身份库。配置绑定同一已核验数据库的角色/额度及WP04依赖；不在URL/日志回显连接。继承Host/Origin/no-store/多认证头拒绝，实际URI精确匹配和严格JSON。不为新模块更换现有身份模型。
- [ ] **4 绿灯及安全集成审查。** 同命令，再跑 `& $invPython -m unittest tests.test_auth tests.test_waveform_api tests.test_waveform_permissions tests.test_waveform_startup tests.test_registry_permissions -v`。独立检查跨库授权时点、路径绕过、权限默认、同事务依赖核对和worker生存周期。
- [ ] **5 提交。** `feat(inversion): expose authorized API and optional runtime`。

## Task 12 参数中心与页面接入

**Files:** Create `frontend/pages/production-parameters.js`、`frontend/inversion/{client,parameters,forms}.js`；Modify `app.js`、`styles.css`；Create `tests/test_inversion_browser.py`、`tests/fixtures/inversion_parameters_check.html`、`tests/fixtures/inversion_fixture_runner.cjs`。依赖 11。

**Interfaces:** `createInversionClient(ctx, {request}) -> client`，提供 `get,submit,dispose,recoverPending`；`async renderProductionParameters(ctx) -> Promise<boolean>` 在ctx.workspace挂载页面；内部 `mountProductionParameters(ctx) -> {ready: Promise<void>,dispose: Function}`，renderer注册ctx.registerDispose。client以ctx.actorId/authSession界定缓存生命周期，固定幂等正文/key；未知结果先查询，不能自动新key重发。

- [ ] **1 写失败交互测试。** `test_parameters_page_disabled_and_missing_evidence`；`test_parameter_units_versions_and_review_separation`；`test_admin_sees_all_pages_but_no_self_approve`；`test_pending_request_keeps_key_after_timeout`；`test_landing_sidebar_and_deeplink_use_same_capability`。真正操作DOM表单/按钮并检查HTTP边界，不只查源码字符串。

```javascript
// test_pending_request_keeps_key_after_timeout; fixture 收集实际请求
assert.equal(retry.command.key, original.command.key);
assert.deepEqual(retry.body, original.body);
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_browser.InversionParametersBrowserTests -v`。
- [ ] **3 实现参数字段分组和版本差异。** 新路由 `production-parameters`；同时处理app导航、landing和深链接disabled消息。机械身份/有效期、来源附件、缺参列表、参数草稿/提交/审批、模型选用/回退由现有权限决定。安装模型清单只展示真实条目。复用样式但不编辑演示版参数默认值；所有来源文本转义。
- [ ] **4 绿灯。** 同命令，另 `& $invPython -m unittest tests.test_static_assets tests.test_waveform_browser -v`；页面离开及时abort/dispose，401/403不以刷新恢复过期权限，登录新session才重置状态。
- [ ] **5 提交。** `feat(inversion-ui): add mechanical parameter workspace`。

## Task 13 双图验证与结果复核页面

**Files:** Create `frontend/pages/production-inversion.js`、`frontend/pages/production-results.js`、`frontend/inversion/{workbench,charts,results}.js`；Modify `app.js`、`styles.css`、`tests/test_inversion_browser.py`；Create `tests/fixtures/inversion_chart_check.html`、`inversion_workbench_check.html`、`inversion_results_check.html`。依赖 11，Task12的client完成后集成。

**Interfaces:** `async renderProductionInversion(ctx) -> Promise<boolean>` / `mountProductionInversion(ctx) -> {ready,dispose}`；`async renderProductionResults(ctx) -> Promise<boolean>` / `mountProductionResults(ctx) -> {ready,dispose}`，均沿用Task12挂载/释放约定；`charts.mountComparison(element, {prediction,reference,filled,metadata}) -> {dispose,setVisibility}`。输入只接受server结果对象，filled默认隐藏。分页点在最大32768边界内完整读取、验证同result摘要，取消后不渲染旧请求。

- [ ] **1 写失败交互测试。** `test_two_card_toggles_hover_and_keyboard_two_decimals`；`test_quality_gap_and_deadpoint_fill_are_distinct`；`test_unknown_metrics_show_reason_not_zero`；`test_large_indices_remain_exact_strings`；`test_all_chart_types_use_small_hover_only_points`；`test_stale_selection_and_revoked_retry_refused`；`test_logout_stops_poll_and_denial_is_sticky`；`test_result_states_not_collapsed_into_success`。

```javascript
// test_large_indices_remain_exact_strings; 实际悬停/键盘操作后的 tooltip
assert(tooltip.textContent.includes('9007199254741001'));
assert(tooltip.textContent.includes('1.23'));
assert(tooltip.textContent.includes('32.46'));
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_browser.InversionWorkbenchBrowserTests tests.test_inversion_browser.InversionResultsBrowserTests -v`。
- [ ] **3 实现工作台和复核页。** 路由 `production-inversion`、`production-results`；选择冻结成员/参数模型，预检和202运行状态用可取消、非重叠轮询，不对原输入自调相位。功率、运动、中间转矩和双图统一悬停两位小数、原始索引精确字符串。借鉴waveform SVG断线与键盘逻辑，不用演示页单根polyline跨缺口连接。参数/冲程/冲次/载荷范围/来源/覆盖率/复核状态围绕图显示，未接工况模型不编诊断。补绘虚线明确标识，UI开关不发送重计算或改变主指标。复核/撤销/导出均走真实API。
- [ ] **4 绿灯。** 同命令及 `& $invPython -m unittest tests.test_static_assets tests.test_analysis_pages tests.test_dual_dynagraph_ui -v`。使用现有Edge模式验证实际SVG悬停/聚焦与截断布局，保留截图用于交付；DOM/HTTP fixture测试不能替代Task14真实API集成。
- [ ] **5 提交。** `feat(inversion-ui): add dual-card comparison and result review`。

## Task 14 隔离端到端与部署回退交付

**Files:** Create `tests/test_inversion_end_to_end.py`、`tests/test_inversion_verification_tool.py`、`tools/verify_inversion.py`、`docs/功图反演部署与回退.md`；Modify `docs/年度工作任务逐项响应.md`；完善测试向量、配置示例及任务已定义测试，不修改冻结演示包。依赖全部。

**Interfaces:** `verify_inversion.main(argv=None) -> int`，支持 `--config <isolated-private-path>`、`--output <ignored-report-directory>`、`--include-existing-regression`。报告固定当前源码清单摘要、测试名/数量/耗时/结果、环境身份、启动/停止归属、软件/现场边界，不继承旧报告冒充本次执行。

- [ ] **1 写失败验收测试。** `test_complete_parameter_to_review_export_flow` 从synthetic/bench完整WP04文件导入、来源/采样/同步/四通道证据、pair、冻结成员开始，经两个独立用户参数批准、HTTP提交、真实worker、双图数据、复核和审计导出；`test_revoke_after_run_keeps_evidence_only`；`test_supported_previous_model_rollback_does_not_rewrite_result` 使用仅测试制品；`test_verifier_rejects_missing_skipped_or_stale_evidence`。不得直接插入“成功结果”代替流程。

```python
# test_revoke_after_run_keeps_evidence_only
self.assertEqual(history_after.content_sha256, history_before.content_sha256)
self.assertEqual(view_after.current_applicability, 'restricted')
self.assertFalse(quantitative_export_allowed)
```

- [ ] **2 红灯。** `& $invPython -m unittest tests.test_inversion_end_to_end tests.test_inversion_verification_tool -v`。
- [ ] **3 实现验证器和部署文档。** 记录生产默认关闭、显式迁移/最小权限、WP04 006与registry投影安装、worker独立启动、身份存储可读性、预算和健康检查；回退停新请求/明确归属worker、保留schema/结果/审计，不删历史。年度矩阵仅写实际完成的软件能力、证据和尚待现场试验项。公开示例不得包含真实凭据/机组参数，不自行重建已发布厂家包。
- [ ] **4 最终绿灯与整体验证。** 先同命令，再 `& $invPython -m unittest discover -s tests -p 'test_inversion_*.py' -v`；接缝全部通过后验证器只执行一次必要 `unittest discover -s tests -p 'test_*.py'` 回归，实际发现数量为分母，0 failures/errors/skipped。失败只重跑受影响层并记录来源，不反复无条件全跑。独立整分支审查完成后核验原工作区、演示目录/ZIP/tag与8765进程未变，恢复本轮明确启动的隔离服务原状态。
- [ ] **5 提交。** `test(inversion): verify production workflow and document deployment`。交付本地变更、测试报告链接、已知现场限制；不自行上线、合并或更新其他工作包PR。

## 自查覆盖与执行方式

设计第1—4节由Task1/6/7/8覆盖；第5节由2/3/4；第6节由5/13；第7—8节由6/9/11；第9—10节由7/10/11；第11节由12/13；第12—13节由各针对性测试及14覆盖。Review Focus 五项均有明确归属测试。全局约束随每项任务生效，不能在局部实现中默认豁免。

推荐逐项实施并独立审查：物理符号、时间同步、证据授权和恢复流程的接口相互依赖，及早发现错误比最后统一排查更合适。保留分层测试，不因增加审查重复全量回归。另一选择是在本任务中由主实施者连续实现，最后再做独立整分支审查；往返较少，但局部设计错误可能较晚暴露。

用户审核此计划并选择执行方式后开始Task1。首次可交付检查点是严格合同及独立数值基准，不需要连接现场设备；现场精度认证仍需真实设备、标定与独立试验数据，不能用软件试验替代。
