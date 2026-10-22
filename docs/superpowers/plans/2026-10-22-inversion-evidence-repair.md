# 功图反演误差证据与正式软件闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在固定有限合成输入范围内补齐有严格误差依据的 v2 反演与真实审批、计算、比较、导出流程，保留 v1 和所有既有交付。

**Architecture:** v2 使用独立合同、准入与求解模块，经既有治理 API 和所属 worker 执行；名义预测仅读取实际功率和角度。独立生成器与服务端检查器分别实现物理公式，共用受版本固定的区间基础库，不共享求解器或参考数组。最后只发布新的隔离软件验证环境和追加验收资料。

**Tech Stack:** Windows x64、CPython 3.13.2、Fraction、python-flint 0.9.0 / Arb、现有 PostgreSQL/HTTP/JavaScript 与 unittest 工具链。

**Spec:** ../specs/2026-10-22-inversion-evidence-repair-design.md（2026-10-22 已获用户确认）。

状态：实施计划，待用户审阅并选择执行方式。此文件不代表测试已执行或功能已完成。代码基线 2427eee06eb4267c37a526db2d4c600709c1659c；数值基线 f779f96626b233136427c2f0982ef25e15afa66b。

## Global Constraints

- source_kind=synthetic；purpose=software_pair_test；scope=finite_synthetic_input/1；field_accuracy_certified=false、not_field_validated。不把合成数据称为脱敏实测数据。
- v1 manifest 的 contracts.py、parameter_contracts.py、series.py、policy.py、geometry.py、mechanics.py、small_linalg.py、motion.py、losses.py、solver.py、comparison.py、display_fill.py 字节不变。
- 不改动或启停原 8765、8770、8772、8774、18882、18883 安装、原 smoke/extended a4 包、旧报告与阶段验收 ZIP；不推送远端。
- 新合同严格区分 inversion.parameter-set/2、inversion.algorithm-policy/2、beam_energy_balance/2、inversion.coverage-contract/1、inversion.input/2、inversion.result/2；OWP-WAVE/1.0 不变。
- 新角度 scale=0.00000001 rad、offset=0、nearest_even、signed i32；8 秒、128 Hz、1025 帧；不循环挑选精度、seed 或正例。
- 固定基础速度界 1e-5 rad/s；完整物理分子界 100 N·m；物理因子界 1e-4 m；数值转矩 0.01 N·m；数值因子/位移 1e-7 m；逐点载荷误差 1000 N。
- 原始预测覆盖和共同比较支撑覆盖分别≥0.80；original_valid_support 的非空载荷 RMSE≤500 N。显示补绘不得参与上述指标。
- 七点三次拟合：跨度≤250000000 ns、角跨度≤0.5 rad、残差≤1e-4 rad、条件数≤10000；numerical_margin=0.001。
- reference_max_gap_ns=20000000；fill_max_time_span_ns=20000000；fill_max_angle_span_rad=0.2；relative_error_min_denominator_N=1。
- 外层域：P∈[-250000,250000] W、ω∈[0.1,2] rad/s、α∈[-2,2] rad/s²、F∈[0,100000] N、T∈[250,350] K、V∈[300,500] V、f∈[45,55] Hz、平均窗∈[1,10000000] ns、带宽∈[1,1000] Hz。完整 Ω 下界须严格大于 0.1。
- 常量损耗网格：P∈[-250000,250000] W、电机转速∈[1,250] rad/s、loss=1200 W、loss_error=0.02 W；完整查询区间必须有覆盖。
- 参数/API≤256 KiB；单附件≤4 MiB，仅 PDF/PNG/JPEG；原始文件≤32 MiB、解码≤16 MiB、worker 输入≤64 MiB、结果≤16 MiB；每通道≤32768 点；每运行一个 pair、最多两个 capture。
- 执行槽=1、等待数=16、每运行逻辑预留=80 MiB、总期限=180 秒。新 lab 的附件 bucket=32 MiB、全局=128 MiB；逐点证明计入结果上限。
- 数值新增限额：128-bit、单线程、十进制输入文本≤128 字符、Fraction 分子/分母与中间量各≤4096 bit、权重缓存≤128 窗形、证明缓存≤2048 项；每个功率平均窗固定 64 个子区间，每 case≤524288 次区间物理算子求值。不得自适应加精度或绕过总期限。
- 只接受已安装的固定刚性四杆族，游梁/连杆惯性及质量矩为零，下游非零质量矩/惯性仅曲柄；运动 θ(t)=θ0+rate·t+εsin(rate·t)。不支持的物理项、未知误差来源或不可判定区间直接拒绝。
- 真实 HTTP 使用两账号独立复核；admin 不能自审；测试账号审批不是人员签字。preflight/202 不代表算法通过。
- 禁止运行 unittest discover、旧 DB 测试基类或 tools.verify_inversion 来验证新 lab；它们有固定旧库清理行为。全部新 live 测试只连接本轮所属实例，不直接写审批表。

## Review Focus

1. 攻击者缩小噪声、M4 或功率生成界并重算所有可控 SHA：仍须由服务端独立推导识别，不能因摘要自洽而放行（任务 4、6）。
2. 七点残差为零的线性/二次输入误差、偏边界窗口及巨大绝对 epoch：误差不得丢失，时序不得重复计费（任务 3）。
3. DLL/依赖缺失、区间预算耗尽、半途取消：没有裸浮点回退、假 completed 或迟到结果发布（任务 1、7、8）。
4. 请求超时重试、参数追加版本与 1025 点分页：同 key 恢复，不重审批、不丢最后 25 点（任务 8）。
5. 撤销后阅读/导出、旧 schema 和非合成来源：保留历史 SHA，禁止当前定量导出；不能重写 v1 或把同周期比较称独立实测（任务 6、7、9）。

---

## 文件边界与执行环境

实施开始先用 using-git-worktrees 技能检查并复用当前合适工作树；禁止在演示版目录编码。以下路径均相对 C:/Users/hp/.codex/worktrees/production-device-registry/外专双百。新增模块沿用项目的小文件布局。

| 任务 | 主要文件（新增，除明确标为修改） | 单一责任 |
| --- | --- | --- |
| 1 | requirements-inversion-v2.txt；backend/inversion/interval_runtime.py；tools/provision_inversion_v2.py；tests/fixtures/inversion_v1_seal.json | 新依赖、资源边界、原模型封存 |
| 2 | coverage_contracts.py、parameter_contracts_v2.py、policy_v2.py、version_dispatch.py（backend/inversion 下） | 严格合同、摘要 DAG、版本解析 |
| 3 | backend/inversion/motion_bounds.py | 实际窗口精确权重及 θ/ω/α 误差 |
| 4 | backend/inversion/synthetic_registry.py、coverage_sources.py；tools/testpack/config_v2.py、forward_v2.py、cycles_v2.py、build_v2.py；修改 tools/testpack/storage.py、__main__.py；config/testpack/inversion-v2-{smoke,extended}.json | 封存配置、独立正向生成、服务端原码依据核验 |
| 5 | backend/inversion/coverage_physics.py、solver_v2.py；tools/testpack/budget_v2.py、validate_v2.py；修改 tools/testpack/__main__.py；tests/inversion_v2_oracle.py | 完整预算、固定包络求解、独立预算报告 |
| 6 | 修改 backend/inversion/{parameters,history,models,selections,input_acquisition,input_metadata}.py；新增 model_manifest_v2.py、input_contracts_v2.py | 治理和归档输入接入 |
| 7 | 新增 result_codec_v2.py、result_dispatch.py；修改 compute_child.py、worker.py、runs.py、results.py、exports.py、queries.py、runtime.py | 有界子进程、存储/读取/导出版本分派 |
| 8 | tools/testpack/inversion_binding_v2.py、inversion_http_v2.py、inversion_run_v2.py、owned_v2_verify.py；修改 lab_http.py、lab.py、__main__.py、delivery.py、waveform_run.py | 新所属环境的真实闭环和拒绝断言 |
| 9 | 修改 tools/testpack/report.py、viewer.py、viewer_details.py、verify_viewer.cjs，frontend/inversion/{results,diagnostics,charts}.js | 真实状态、双图和误差明细 |
| 10 | 修改 tools/review_deploy_{source,config,worker,ready}.py；新增 tools/testpack/deliver_v2.py、docs/operations/inversion-v2-validation.md | 已提交制品部署、定向完整回归、追加验收资料 |

每个任务另建同名专项测试，具体如下。数值测试不继承 DB 测试基类。提交仅包含该任务明确文件，不执行 git add .。

命令约定（变量不是待填写参数）：任务 1 创建唯一新目录 C:/Users/hp/AppData/Local/OilWell/inversion-v2-20261022-r1；存在则停止，不复用/覆盖。令 $taskRoot 为该路径、$taskRepo 为上文工作树、$taskPython 为 $taskRoot/venv/Scripts/python.exe、$taskNode 为 C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe。全部 unittest 在 $taskRepo 执行；设置 PYTHONDONTWRITEBYTECODE=1、PYTHONIOENCODING=utf-8。下文只列模块名的测试列表，精确调用统一为 `& $taskPython -B -m unittest <按原顺序列出的模块/方法全名> -v`，不能扩大到自动发现。环境创建只发生在计划获准执行后。

## 共同类型约定

所有新记录为 frozen dataclass，序列为 tuple，mapping 冻结，实际 wire 数值使用规范十进制或精确有理字符串，不在 JSON 放 float NaN/Infinity。失败抛出现有 InversionError；下列纯数值拒绝码由边界统一加 inversion. 前缀。符号 Q=fractions.Fraction，Interval=interval_runtime.RationalInterval(lo:Q, hi:Q)，端点是向外包围，不从显示字符串回读。

- 任务 2 在 coverage_contracts.py 定义 CoverageContract(body:Mapping, payload_sha256:str, raw_sha256:str)；在 parameter_contracts_v2.py 定义 ParameterSetV2(ParameterSet 子类，新增 coverage_contract:CoverageContract、parameter_core_sha256:str)。保留原 schema/完整 raw，不构造伪 v1 正文。AlgorithmPolicy 直接复用原精确 dataclass，但通过 v2 parser 填写 v2 schema/algorithm。
- 任务 2 在 coverage_contracts.py 定义 ChannelEvidence(role:str, raw:tuple[int,...], time_ns:tuple[Q,...], quality:tuple[int,...], scale:Q, offset:Q, pin:ChannelPin, original_file_sha256:str, bound_file_sha256:str, channel_sha256:str)；PredictionEvidence(power:ChannelEvidence, angle:ChannelEvidence, pack_id:str, case_id:str, config_sha256:str, generator_sha256:str)。不得包含 load/displacement 参考数组。
- 任务 3 定义 MotionBounds(power_index:int, time_ns:Q, angle_indices:tuple[int,...], status:str, reason_codes:tuple[str,...], exact_center:tuple[Q,Q,Q]|None, base:tuple[Q,Q,Q]|None, physical:tuple[Q,Q,Q]|None, numerical:tuple[Q,Q,Q]|None)。三元组顺序固定 θ/ω/α；所有原生点均有记录，不可用数值为 None 而不是零。
- 任务 4 在 synthetic_registry.py 定义 CaseDefinition(case_id:str, config_sha256:str, body:Mapping)，在 coverage_sources.py 定义 SourceProof(status:str, reason_codes:tuple[str,...], case:CaseDefinition, derivative_bounds:tuple[Q,Q,Q,Q]|None, angle_error_rad:tuple[Q,...]|None, differential_time_s:tuple[Q,...]|None, common_time_s:Q|None, power_error_W:tuple[Q,...]|None, evidence_sha256:str)。status 仅 verified/refused；verified 时所有界必须非空，refused 的未计算值为 None。导数顺序 Ω/A/D3/M4；误差数组按完整原生索引对应，bound_window 只传选中的七项。只有检查器能构造合格 SourceProof。
- 任务 5 定义 AdmissionPoint(power_index:int, status:str, reason_codes:tuple[str,...], motion:MotionBounds, physical_N_error_Nm:Q|None, physical_J_error_m:Q|None, numerical_N_error_Nm:Q|None, numerical_J_error_m:Q|None, numerical_s_error_m:Q|None)；PredictionV2(prediction:PredictionSeries, evidence:tuple[AdmissionPoint,...], source_proof:SourceProof)。原 PredictionSeries 类型与比较语义不变。
- 任务 7 定义 PreparedResultV2(snapshot:InputSnapshot, metadata:bytes, chunks:tuple[bytes,...])，提供与 PreparedResult 相同的 body/result_bytes/status/content_sha256/diagnostics 只读属性；PreparedResultAny=PreparedResult|PreparedResultV2，仅分派器显式接受这两种。
- 任务 8 定义 InversionOutcome(case_id:str, assertion_status:str, expected_outcome:str, observed_outcome:str, phase:str, reason_code:str|None, approved_execution_status:str, artifacts:tuple[str,...])；报告不能将 assertion_status 当 approved_execution_status。

## Task 1: 锁定严格数值运行环境与旧版保护

**Files:** 新增文件见边界表；新增 tests/test_inversion_interval_runtime.py、tests/test_inversion_v1_seal.py、tests/test_inversion_v2_provision.py。

**Interfaces:** interval_runtime.proof_context() 为固定 128-bit/1 线程上下文；enclose(value:flint.arb)->RationalInterval；require_runtime()->Mapping；check_fraction(value:Q)->Q。provision_inversion_v2.provision(root:Path, *, base_python:Path)->Path 只新建标识目录/venv/运行清单，不修改当前 .venv，不连接数据库。

- [ ] 写失败测试：test_outward_endpoints 断言 enclose(arb(1)/3).lo≤Q(1,3)≤hi、区间平方根包含 sqrt(2) 的有理上下夹界；test_exact_rational_conversion 不经 float；test_missing_dependency_is_unavailable 拒绝而非回退；test_bit_limit 以 4097-bit 输入触发 proof_resource_limit；test_v1_files_match_seal 核对所有受保护源码。
  核心断言：`assert enclose(arb(1)/3).lo <= Q(1,3) <= enclose(arb(1)/3).hi`；`assert caught.code == 'inversion.proof_resource_limit'`。缺 Arb 时旧 v1 导入/读取仍可用；该测试不要求真有 Arb。
- [ ] 以现有 Python 运行 `python -B -m unittest tests.test_inversion_interval_runtime tests.test_inversion_v1_seal tests.test_inversion_v2_provision -v`；预期因新增接口未实现而失败，不能以 ImportError 被 skip 代替红灯。
- [ ] 实现上述接口与固定限额。固定 wheel 为 python_flint-0.9.0-cp313-cp313-win_amd64.whl，SHA256=1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293；requirements-inversion-v2.txt 使用 ==0.9.0 和该 hash。仅下载二进制、--require-hashes，校验 wheel 内原生 DLL/PYD、RECORD、许可证及 CPython 非自由线程 ABI；生成安装清单纳入后续模型指纹。封存文件 SHA 从基线 Git blob 计算，不从实施后的文件生成期望值。
- [ ] 运行 provision CLI：`& 'C:/Users/hp/Documents/外专双百/.venv/Scripts/python.exe' -B -m tools.provision_inversion_v2 --root $taskRoot`；其中 base_python 使用当前解释器。仅向新 venv 安装既有 pinned production 依赖及新增 pinned wheel；核对安装清单后用 $taskPython 重跑上述三模块，预期 0 failures/errors/skips。依赖/ABI/许可清单不成立即停止。
- [ ] 提交本任务明确文件：`git commit -m "feat: isolate verified inversion v2 numeric runtime"`。

## Task 2: 严格 v2 合同与无环摘要

**Files:** 边界表四模块；新增 tests/test_inversion_contracts_v2.py、tests/inversion_v2_support.py、tests/fixtures/inversion_v2_contract.json。

**Interfaces:** parse_policy_v2(raw:bytes)->AlgorithmPolicy；parse_parameter_set_v2(raw:bytes, *, draft:bool=False)->ParameterSetV2；parse_coverage(raw:bytes)->CoverageContract；parameter_core_bytes(body:Mapping)->bytes；coverage_payload_bytes(body:Mapping)->bytes；version_dispatch.parse_parameters(raw:bytes, *, draft:bool=False)->ParameterSet|ParameterSetV2、missing_parameters(value:ParameterSet|ParameterSetV2)->tuple[str,...]。tests.inversion_v2_support.fixture_body()->dict 从固定纯 fixture 深拷贝，不访问 DB。

- [ ] 写 test_v2_identity_preserved：断言 raw、SHA、schema 均保留 v2，policy 精确类型仍是 AlgorithmPolicy；test_core_digest_dag：只改 PDF pin 不改 core/payload，却改变最终参数 SHA；test_core_all_numeric_fields：逐字段改 geometry/drive/domain/uncertainty/phase/conditions/channel pin 必改变 core；test_unknown_statistical_or_missing_bounds：未知字段、重复键、负界、sigma 冒充绝对界、field_pair_input 一律拒绝。
  fixture_body 经规范编码为 raw 后：`assert parsed.raw == raw and parsed.schema == 'inversion.parameter-set/2'`；`assert type(parsed.policy) is AlgorithmPolicy`；只改 PDF pin 的 before/after：`assert parameter_core_bytes(before) == parameter_core_bytes(after)`、`assert sha256(canonical_bytes(before)).digest() != sha256(canonical_bytes(after)).digest()`。
- [ ] 运行 `& $taskPython -B -m unittest tests.test_inversion_contracts_v2 -v`，预期缺接口/预期断言失败。
- [ ] 实现接口和明确 JSON 字段白名单；参数 core 白名单按设计 §4.3 包含普通物理依据 pin，排除 coverage_contract、人读预算 PDF pin 和审批事件；PDF 绑定不含自身 pin 的 payload。每合同一个 case。定义两预测通道的原码、质量、时间、pin 规范摘要。v2 可复用 v1 稳定单组解析器，但不换 schema 调用整个 v1 parser。草稿缺项可显示 missing；submit/run 不得接受缺项。fixture 明确其合成与未批准性质。
- [ ] v2 provenance 正文保留原 provenance 除 coverage 外的全部字段，移除旧八项全域 coverage 声明；使用独立解析器构造原 Provenance 值对象，内部 coverage=None。唯一机器准入依据是顶层 coverage_contract；普通附件/校准周期/操作条件继续严格核对。不得调用旧 _provenance/_coverage 或精确限定 v1 的 missing_fields 解析 v2。
  新增 test_pdf_pin_cannot_hide_physical_evidence：人读预算 PDF 的 pin 不得同时充当普通物理依据 pin；否则拒绝，不能借 core 排除规则隐藏物理依据变更。
- [ ] 同时跑 `tests.test_inversion_contracts_v2 tests.test_inversion_contracts tests.test_inversion_v1_seal`，预期全部通过且 v1 字节不变；256 KiB 含请求信封边界、128 字符/4096-bit 资源限制有边界测试。
- [ ] 提交明确文件：`git commit -m "feat: add versioned finite-input inversion contracts"`。

## Task 3: 实际七点窗口的独立运动误差

**Files:** backend/inversion/motion_bounds.py；tests/test_inversion_motion_bounds.py。

**Interfaces:** exact_weights(offsets_s:tuple[Q,...])->tuple[tuple[Q,...],tuple[Q,...],tuple[Q,...]]；bound_window(*, power_index:int, center_ns:Q, samples:tuple[Sample,...], nominal:MotionPoint, angle_error_rad:tuple[Q,...], differential_time_s:tuple[Q,...], common_time_s:Q, derivative_bounds:tuple[Q,Q,Q,Q])->MotionBounds。nominal 来自既有缩放 QR，exact_weights 只作独立证明；不接收参考数据。

- [ ] 写 test_polynomial_reproduction_all_positions：七种中心位置与非均匀、非对齐窗口均满足 Σwrj*xj^k=r!·1(k=r)；test_zero_residual_linear_quadratic_errors：线性/二次扰动残差为零仍有速度/加速度基础界；test_epoch_translation：增加 10^18 ns 不改变界；test_common_clock_not_jitter：共同时间分别用 Ω/A/D3，不双计；test_gap_and_support_refused：缺口/窗外没有数值伪填。
  对 exact_weights 返回的三行逐阶断言：`assert sum(w*x**k for w,x in zip(weights[r], offsets)) == (math.factorial(r) if k == r else 0)`；线性噪声案：`assert bounds.base[1] > Q(1,100000)`，不能减残差后通过。
- [ ] 运行 `& $taskPython -B -m unittest tests.test_inversion_motion_bounds -v`，预期新接口测试失败。
- [ ] 实现精确消元与多项式重现自检、128 项上限缓存；执行设计 §5.2 公式，bω_base≤1e-5 才成立。physical.ω 固定使用 1e-5 加残差和共同时间项，不以较小 bω 代替。QR 与 exact_center 差进入 numerical 分账；方向/零位映射要与实际 nominal 一致。时序扩展后的整个窗必须有 M4 支持，不以中心值代替。
- [ ] 运行 `tests.test_inversion_motion_bounds tests.test_inversion_motion tests.test_inversion_v1_seal`；中心速度权重绝对和=448/3 s^-1，最边界=9728/21 s^-1，加速度中心=163840/21 s^-2、边界=1064960/21 s^-2。原 ±1e-6 rad 噪声不获得基础速度准入。
- [ ] 提交：`git commit -m "feat: bound native motion derivatives with exact window weights"`。

## Task 4: 新固定生成器与服务端输入依据核验

**Files:** 边界表任务4全部文件；新增 tests/test_testpack_generator_v2.py、tests/test_inversion_coverage_sources.py。

**Interfaces:** config_v2.parse_config_v2(body:dict)->PackConfigV2（继承 PackConfig，新增 generator_version='finite-input/2'）、config_dict_v2(config:PackConfigV2)->dict（schema=owp.testpack.config/2）；synthetic_registry.case_definition(config_sha256:str, case_id:str)->CaseDefinition；forward_v2.forward_enclosures(case:CaseDefinition, time_s:Q)->Mapping[str,RationalInterval]；cycles_v2.generate_cycles_v2(config:PackConfigV2)->tuple[Cycle,...]；build_v2.build_v2(config:PackConfigV2,target:Path)->PackIndex；coverage_sources.verify_sources(evidence:PredictionEvidence, parameters:ParameterSetV2)->SourceProof。

- [ ] 写 test_preregistered_cases：smoke 恰含 base/balance/friction/speed/noise/missing/clipped/shifted 机理/信号场景，extended 保留原 40 个参数变体；seed=20261022、start_utc='2026-09-30T16:00:00.000000Z'、固定所有 variant 规则；test_integer_rounding_and_i32：角度 1e-8、最近偶数、全周期无溢出，禁止回绕规避；test_rehash_cannot_shrink_bounds：分别缩小 M4/噪声/功率界并重签全部可控摘要仍拒绝；test_reference_not_accepted_by_source_checker：类型接口没有参考数组。
  生成角度通道：`assert angle.scale == '0.00000001' and len(angle.raw) == 1025`；篡改后的 `assert proof.status == 'refused' and 'coverage_basis_mismatch' in proof.reason_codes`；支持类型：`assert set(PredictionEvidence.__dataclass_fields__).isdisjoint({'load','displacement','reference'})`。
- [ ] 运行 `tests.test_testpack_generator_v2 tests.test_inversion_coverage_sources`，预期失败；只读旧 fixture 作兼容比较，不覆盖旧包。
- [ ] 实现独立 forward：解析四杆几何导数/势能导数，功率均值使用固定 64 子区间的中点积分及区间 P'' 余项 Σ(h^3·sup|P''|/24)，再除平均窗长；P'' 用解析/区间自动微分，不用差分步长加倍当证明。固定编码前功率计算误差上限 0.01 W、角度 1e-20 rad；无法证明即停止。零位 θ0/下死点使用固定 96 次有界二分的区间根夹持，根/几何偏置误差计入生成误差，不伪称精确。生成值选择经包围证明的有理中点再量化，边界舍入不确定则拒绝。
- [ ] 实现服务端独立 coverage_sources：仅使用安装 registry 内 config/case 常量及独立物理公式重新核对每个 P/angle 原码、quality、时间、噪声/时移规则和编码误差；不得导入 forward_v2、mechanics 正向函数或生产 predict/geometry/motion 作为期望。由解析族推导 Ω/A/D3/M4；声明界仅可覆盖计算界，不能自己成立。registry 不含自己的文件 SHA；模型 manifest 封存 registry，合同 generator SHA 对比安装制品固定源指纹，避免生成器/registry 自引用。
- [ ] storage.write_pack/load_pack/pack_identity 仅增加 config/2 解析与编码分派，原 config/1 逻辑和清单形状不变；不让 PackConfigV2 流入只认旧字段的 config_dict。新的 generator_fingerprint_v2()->dict 使用 version=owp.testpack.generator/2，指纹文件集合同时包括新正向模块、config 与实际使用的 registry/interval 基础模块，v1 fingerprint 函数不改；_generator 按版本校验，load_pack 旧包不要求当前指纹相等。新增 roundtrip/旧包 fixture 测试，证明新 schema 可写可读。
- [ ] 重跑本任务两模块及 `tests.test_testpack_contracts tests.test_testpack_mechanics tests.test_inversion_v1_seal`，要求新生成可重复、旧 scale 和旧算法不变；包 manifest 仍为 owp.testpack/1。记录实际字节、计算计数/用时；不得因四正例不通过改 seed/规则。
- [ ] 提交：`git commit -m "feat: generate sealed v2 inputs and verify their error sources"`。

## Task 5: 完整物理/数值预算及 v2 求解

**Files:** 边界表任务5全部文件；新增 tests/test_inversion_coverage_physics.py、tests/test_inversion_solver_v2.py、tests/test_testpack_budget_v2.py。

**Interfaces:** coverage_physics.admit_point(inputs:PredictionInputs, parameters:ParameterSetV2, motion:MotionPoint, bounds:MotionBounds, proof:SourceProof)->AdmissionPoint；solver_v2.predict_v2(inputs:PredictionInputs, parameters:ParameterSetV2, evidence:PredictionEvidence)->PredictionV2；budget_v2.inspect_v2(evidence:PredictionEvidence, parameters:ParameterSetV2)->Mapping；validate_v2.validate_pack_v2(pack:PackIndex)->tuple[CheckResult,...]。离线结论包含所有原生点分类/界，不返回批准状态；CLI validate 按配置版本调用，v1 validate_pack 不改。

- [ ] 写 test_full_numerator：独立 oracle 对惯性 Iup+Icrank、θ 重力/J、ω 倒数/摩擦/损耗分别扰动；test_physical_vs_numeric_limits：100、1e-4、0.01、1e-7 的等号/刚超界使用精确有理案例，超界无有效载荷；test_entire_box_domain：区间越域但中心在域仍拒绝；test_reference_mutation_only_changes_comparison：预测不变而 comparison 变；test_original_support：无效点/死点断开覆盖，不用填线增加覆盖。
  使用独立构造且其余条件合格的阈值案例：`assert at_limit.status == 'valid'`、`assert just_over.status != 'valid'`；比较隔离测试：`assert prediction_before == prediction_after and comparison_before != comparison_after`。
- [ ] 运行本任务三个 test 模块，预期失败。
- [ ] 实现完整 N/J 区间包围，独立名义中心为精确拟合三元组；参数/功率/损耗/时序/平均与瞬时语义误差全计入，计算差单列。保留原 nominal QR 与浮点几何/能量计算，仅在证明成立后以固定 uN=100+0.01、uJ=1e-4+1e-7 求载荷误差。可以调用旧字节封存的纯数值函数取得 nominal，不能调用其旧准入结论替代新检查。预测返回原 PredictionSeries 类型加旁路有界证据；比较只复用已测兼容的 compare()，不得改其原始支撑语义。
- [ ] tests/inversion_v2_oracle.py 独立实现解析常量/有理手算与物理变换案例；禁止调用生产 predict/solve_load/motion/geometry 形成期望。测试四正例逐个达到 coverage≥0.80、共同 coverage≥0.80、RMSE≤500 且非空，噪声/缺样/削顶/时移保留明确拒绝结果。源证明中的未知误差必须阻断，不默认零。
- [ ] validate_pack_v2 复用未变的协议/清单/其他业务检查，并替换旧 not_run 反演占位为离线有限预算断言（仍不是 approved execution）。在原包临时本地绑定上计算，再于任务8实际归档绑定后重新计算；离线 UUID 不作为真实审批证据。pack 的所有原码必须重新读取/hash，而不是信任 metadata 里的 passed。
- [ ] 运行 `tests.test_inversion_coverage_physics tests.test_inversion_solver_v2 tests.test_testpack_budget_v2 tests.test_inversion_comparison tests.test_inversion_display_fill tests.test_inversion_v1_seal`。对实际四正例记录每点最差预算和运行时间；任何必需正例失败则停止进入真实审批，保留失败，不通过删样或改准入阈值修饰报告。
- [ ] 提交：`git commit -m "feat: admit finite-input inversion with separate physical and numerical bounds"`；此处为第一个独立审查节点，审查误差依据、独立性和固定阈值。

## Task 6: v2 参数、模型与受审计输入接入

**Files:** 边界表任务6全部文件；新增 tests/test_inversion_governance_v2_unit.py、tests/test_inversion_input_v2.py、tests/test_inversion_models_v2_unit.py。

**Interfaces:** model_manifest_v2.installed_v2()->InstalledModel（原精确类型，不用子类）；InstalledModel.snapshot() 增加 installed_id='beam_energy_balance/2' 的明确分支，返回 v2 schema/算法/源码/数值依赖/registry 指纹，v1 分支原值不变。version_dispatch 保持任务 2 签名；InputAcquisition.prepare_input(request,actor,*,check) 保持既有签名，v2 返回 input_contracts_v2.PreparedInputV2（PreparedInput 冻结子类，另增 prediction_evidence:PredictionEvidence）。

- [ ] 写 test_schema_model_mismatch、自审/缺权限/陈旧版本/字段缺失测试；test_installed_v1_snapshot_unchanged；test_actual_archive_to_evidence：原码、校准 scale、索引、quality、manifest 来自受审计 member exporter，不能从客户端参数正文伪造；test_same_cycle_is_not_independent：真实绝对 ns 与 physical cycle key 得出 overlap、independent_comparison=false；test_field_input_rejected。
  pin 解析后的断言：`assert acquired.prediction_evidence.power.raw == archived_power_raw`；`assert same_cycle_metadata['independent_comparison'] is False`；v1 快照在依赖和环境相同条件下 `assert snapshot_after == snapshot_before`。
- [ ] 运行本任务三个纯测试模块，预期失败；不要引入 GovernanceCase/InversionDatabaseCase。
- [ ] 更新非数值治理层显式分派。submit 校验字段/附件/合同与 registry 的可核对依据，运行端仍重算实际原文；选择必须严格匹配 v2 模型和参数，不允许仅 ID 正确而算法错配。v1 snapshot 的字段、顺序规范值、哈希语义不变；缺依赖只使 v2 unavailable，不影响 v1。
- [ ] InputAcquisition 在现有归档原文读取中核验完整文件 SHA 后提取 P/angle 原码及限定元数据；新快照固定该提取内容及实际归档 pin。worker 重算该提取内容的摘要/校准及逐窗证明，不接收客户端原文或 URL；参考数组仍只传比较分支。保存原包→绑定副本→归档的映射，保留最终参数 SHA 和 core SHA 两条身份。
- [ ] 运行三个新模块和 `tests.test_inversion_contracts tests.test_inversion_v1_seal`；检查 SQL 表已有 JSON 正文足够，不新增/自动运行 DDL。若确需迁移，停止并补兼容迁移设计，不在此任务临时扩张。
- [ ] 提交：`git commit -m "feat: govern and acquire v2 inversion inputs without bypasses"`。

## Task 7: 子进程、版本化结果与当前适用性

**Files:** 边界表任务7全部文件；新增 tests/test_inversion_codec_v2.py、tests/test_inversion_compute_v2.py、tests/test_inversion_result_dispatch.py。

**Interfaces:** result_codec_v2.encode_v2(value:PredictionV2,comparison:ComparisonReport,snapshot:InputSnapshot)->PreparedResultV2；result_dispatch.read_result(path:Path,snapshot:InputSnapshot)->PreparedResultAny、write_result(prepared:PreparedResultAny,path:Path)->None、validate_result(prepared:PreparedResultAny)->None、decode_result(snapshot:InputSnapshot,metadata:bytes,chunks:tuple[bytes,...])->PreparedResultAny。ComparisonReport 是 backend.inversion.comparison 既有类型；compute_child.compute(input_path,output_path) 外部签名不变。

- [ ] 写 test_v1_roundtrip_unchanged、test_v2_exact_proof_roundtrip、test_bad_schema_or_chunk_rejected、test_aggregate_16mib_limit、test_compute_has_no_reference_dependency、test_runtime_missing_no_fallback。定义 1025 点证据含各原生索引和七点窗口且无重复；非法点 None 不变零。worker timeout/cancel/fencing 使用所属临时子进程测试，不能操作已有进程。
  `assert read_result(path,snapshot).content_sha256 == prepared.content_sha256`；`assert prepared.result_bytes <= 16*1024**2`；原生记录解码后 `assert len(proof_rows) == len(prediction_rows) == 1025`；超界案例要求捕获 inversion.result_size_limit。
- [ ] 运行 `tests.test_inversion_codec_v2 tests.test_inversion_compute_v2 tests.test_inversion_result_dispatch`，预期失败。
- [ ] 新 codec 沿用每块≤256 行、固定列和规范数值，增加 proof 块并让 prediction/proof 索引严格一一对应；全部块计入16 MiB。未知列/重复块/缺证据/伪状态拒绝。旧 result_codec.py 保持原函数/格式，外层 dispatcher 调用旧 reader。runs.publish 与 results._load 的精确类型限制改为明确两类型分派，不接受 duck typing；worker.py 的结果读取改调用 read_result。queries.py 对 v2 逐点证据和固定诊断码增加明确白名单，不能静默漏掉理由或证明块。
- [ ] compute 以 schema+已安装 model 双核验选择 v2，重新 verify_sources/admit 后才能发布；查询/导出从数据库当前依赖重新判断，不信任历史 metadata applicable。结果后撤销保留原 SHA；定量导出拒绝，具有审计权限的证据导出仍明确 evidence_only。保留 64/16 MiB、180 秒、fencing、取消与容量释放规则。
- [ ] 跑三个新模块及旧 CodecTests 的五个纯方法：test_fixed_column_validation_rejects_unknown_shape、test_fixed_columns_preserve_exact_time_index_and_fraction_diagnostics、test_reference_edges_round_trip_and_legacy_columns_remain_immutable、test_reference_edge_predecessor_is_bound_across_chunks、test_result_metadata_alone_is_subject_to_aggregate_limit（均位于 tests.test_inversion_worker.CodecTests）。不能运行整个 worker 模块或整类。预期全通过、无 skip、所有权进程已退出。
- [ ] 提交：`git commit -m "feat: persist and read bounded v2 inversion evidence"`。

## Task 8: 所属新 lab 的真实 HTTP/worker 闭环

**Files:** 边界表任务8全部文件；新增 tests/test_testpack_inversion_http_v2.py、tests/testpack_owned_lab_support.py、tests/test_testpack_inversion_live_v2.py。

**Interfaces:** inversion_binding_v2.bind_v2(case:CaseDefinition,pins:Mapping,contract:CoverageContract)->bytes；InversionSession.post(path:str,body:dict|None=None,*,actor:str,expected_version:int|None,key:str,expected_status:int=200)->dict；read_all_points(result_id:str)->tuple[dict,...]；wait_run(run_id:str,*,deadline:float)->dict；inversion_run_v2.run_inversion_cases(lab:LabHandle,pack:PackIndex,run_root:Path)->tuple[InversionOutcome,...]。owned_v2_verify.run(root:Path,pack:Path,delivery:Path)->Mapping 仅走所属 HTTP/worker，不继承旧 DB fixture。

- [ ] 写纯 HTTP 替身测试：test_namespace_error_codes（顶层 code 与 waveform error.code）；test_append_parameter_expected_version；test_unknown_outcome_reuses_key_and_body；test_two_pages_exactly_1025（1000+25，核对 SHA/total/offset，无缺重）；test_negative_not_approved_success；test_wrong_lab_pack_process_identity。预期状态/phase/code 精确匹配而非只检查非200。
  `assert len(points) == len({p['power_index'] for p in points}) == 1025`；`assert replayed_command['key'] == original_command['key']`；负例结果 `assert outcome.assertion_status == 'passed' and outcome.approved_execution_status == 'refused'`。
- [ ] 运行 `tests.test_testpack_inversion_http_v2 tests.test_testpack_lab tests.test_testpack_runtime_integrity tests.test_testpack_waveform_run`，先看到新增行为失败。
- [ ] 实现两套信封分离：反演 command.key/expected/reason，waveform 原有信封不变。只首次创建/首次结果复核用 absent，追加参数版本用当前最大版本，其余按真实响应。timeout 后用 GET requests/{key}?action=... 查询 actor 作用域回执，禁止换 key。LabHttp 增加有界流式下载（≤16 MiB 结果导出，其他路由仍原限额），校验 Content-Length 与实收字节。
- [ ] 接入设计 §7.2 的完整十步：先离线预算闭合、真实 waveform 附件/证据/import/pair/dataset，参数 PDF 和最终合同摘要链，作者提交/另一账号审核；模型每 lab 注册一次，通过 GET /models 核对；selection、preflight、202、真实 CLI worker、completed/result、全量分页、独立结果复核、导出。PDF 使用现有 evidence_report 路径生成，并另查 documents/pdf 技能要求；不允许上传 JSON 附件或写审批表。
- [ ] 新 lab 端口保护补 18882/18883，提供显式 ports 文件（固定本轮 monitoring:18784/56442/19894，review:18780/56443/19895，modbus:15030）；占用即停止，不能停止占用者。live 开关固定 OILWELL_TESTPACK_INVERSION_V2_LIVE=1，路径用既有 OILWELL_TESTPACK_LIVE_ROOT/PACK/DELIVERY。显式开启后配置缺失必须失败，不能 skip。
- [ ] 四正例闭合后才执行独立负例：noise/missing/clipped/shifted、错 pin/权限、自审、陈旧版本、预检后撤销、运行中权限丢失、结果后撤销、取消/180 秒超时和迟到 worker。各负例使用独立绑定资源，不能污染后续正例；旧历史结果 SHA 不变。
- [ ] 纯测试通过后提交：`git commit -m "feat: exercise approved inversion through owned HTTP workers"`。真实 live 执行放到任务10，必须使用已提交且清洁的制品；此处仅先完成代码和纯协议测试，不能记真实闭环通过。

## Task 9: 报告、双图及误差明细展示

**Files:** 边界表任务9展示文件；新增 tests/test_testpack_report_v2.py、tests/test_inversion_browser_v2.py、tests/fixtures/inversion_v2_results_check.html；复用既有 tests/fixtures/inversion_fixture_runner.cjs，无需改其 CLI。

**Interfaces:** report.project_inversion_outcome(value:InversionOutcome)->dict；viewer_details.inversion_detail(pack:PackIndex,report:Mapping,case_id:str)->dict；前端现有 mount/结果 client 接口保持，增加 v2 metadata/proof 分支，不创建另一个运行 API。

- [ ] 写 test_expected_refusal_separate_from_approved、test_no_secret_paths_tokens_in_public_report、test_v1_report_still_readable；浏览器 fixture 用真实 codec 形状固定预测/参考两条线，断言可各自勾选、无效段断线、默认点隐藏、悬停 x/y 均两位小数、误差/理由/来源可见、1025 点不截断。
  报告 `assert public['approved_execution_status'] == 'refused'`；hover 选定 1/3 与 2/3 的 fixture 点：`assert tooltipX === '0.33' && tooltipY === '0.67'`；这些为显示断言，不截断导出的原值。
- [ ] 运行 `& $taskPython -B -m unittest tests.test_testpack_report_v2 tests.test_testpack_viewer_details tests.test_inversion_browser tests.test_inversion_browser_v2 -v`，预期新增展示断言失败。新浏览器模块继承无 DB 的 BrowserFixtureCase，fixture='inversion_v2_results_check.html'，以 v2-proof/v2-refused 模式、1120/760 宽度执行；只打开临时本地 fixture，不连接现有服务。
- [ ] 实现报告字段白名单：assertion_status、expected/observed_outcome、phase/reason_code、approved_execution_status 单独保留。正例成功必须满足全部预算/coverage/RMSE/真实执行条件；缺审批、queued、timeout、failed、refused 不合并。源始终 synthetic，同周期保留 not independent，不显示 field-certified。
- [ ] 重跑前一步的完整命令。BrowserFixtureCase 自动填充本地模块 URL、NODE_PATH 并调用 node runner(url,width,screenshot)，不可直接对未替换模板运行 runner。验证 public projection 不含令牌/DSN/个人绝对路径；原始数值全精度导出，只有 UI hover 两位小数。
- [ ] 提交：`git commit -m "feat: expose v2 inversion proof and truthful execution status"`。

## Task 10: 新制品部署、分层回归与追加资料

**Files:** 边界表发布文件；新增 tests/test_inversion_v2_release.py；任务8 live 模块在此执行。

**Interfaces:** deliver_v2.deliver(root:Path,source:Path,*,profile:str)->Mapping，profile∈{smoke,extended}；发布器要求新目标、clean HEAD、所有权标记与安装清单，不覆盖任何旧目录。review_deploy_source 明确把新模块、两份新配置、requirements-inversion-v2.txt 纳入白名单；依赖安装只影响新 lab。

- [ ] 写 test_release_contains_v2_runtime_and_registry、test_dirty_source_existing_target_refused、test_missing_proof_stays_blocked、test_rollback_disables_only_owned_v2；运行 `tests.test_inversion_v2_release` 先失败。实现发布清单/就绪 v2 能力检查与 rollback 操作说明；就绪不是反演成功。
  release 检查 `assert 'backend/inversion/coverage_sources.py' in exported_paths`、`assert installed_wheel_sha256 == '1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293'`；回退动作记录 `assert stopped_instance_ids <= owned_new_instance_ids`。
- [ ] 用 docs/operations/inversion-v2-validation.md 明确列出本计划新增纯测试模块白名单、上述旧 codec 五方法及已有相关纯测试；执行一次相关完整回归，不能用模糊 discover。新 live helper 增加独立 namespace/最小权限查询验证和故障注入后的资源/权限恢复核验，不借旧库权限。
- [ ] 提交发布代码与文档：`git commit -m "feat: package isolated inversion v2 validation release"`，核对 clean HEAD；生成新 smoke 包：`& $taskPython -B -m tools.testpack generate --config "$taskRepo/config/testpack/inversion-v2-smoke.json" --output "$taskRoot/pack-v2-smoke"`；validate 输出到新的 offline-v2-smoke.json。四正例离线失败即停，不审批。
- [ ] 在 $taskRoot/lab-v2-smoke 创建新实例，用任务8固定 ports；lab-init 新增 --ports-file 参数。启动只管理已记录进程；新实例安装需包含已锁定数值 wheel、许可证/原生 SHA。设置 live 的 ROOT/PACK/DELIVERY（delivery=$taskRoot/lab-v2-smoke/run-v2-001），运行 `& $taskPython -B -m unittest tests.test_testpack_inversion_live_v2 -v`。完成标志为无skip、四正例各有真实 completed/result/审核/导出及两种 coverage/RMSE，所有拒绝断言 phase/code 匹配。
- [ ] 记录每次 worker 实际用时、输入/输出/附件占用、排队/逻辑容量回收及完整分页摘要。≤180秒/≤原容量有实测证据才通过。此处第二个独立审查节点，检查真实链路、资源所有权、权限/撤销、报告与源码一致性。失败保留 blocked/failed 产物并停止扩展，不放宽限制。
- [ ] smoke 达标后在新 pack-v2-extended、lab-v2-extended、run-v2-001 运行同一流程；先仅停本轮 smoke 所属实例释放本轮端口，保留目录/数据，再复用此组空闲端口。40 变体规则不改，所有预注册场景逐个报告；原非反演任务继续沿现有路径，不能因本轮反演完成而被计为全部项目完成。
- [ ] 使用 pdf 技能制作“阶段验收追加资料”，只写本轮实际检查/源码/包/报告/结果/导出 SHA、失败项与待现场/硬件验证项；关联 Y1-01、Y1-04、Y3-06，不重新声称全部年度任务完成。输出到新专用追加目录，不改旧 PDF/ZIP；渲染逐页检查并核验归档清单。
- [ ] 发布新阅读入口，端口由已验证空闲 socket 分配并记录；不承诺固定未验证 URL。最终比较 v1 seal、旧包/报告/部署清单与实施前摘要，核对 git diff --check 和清洁状态；交付报告中区分“代码完成、软件验证通过、现场未验证”。远端上传另按用户授权执行。

## 停止与回退

任何依赖/精确证明/原阈值/强制正例/容量/兼容性/所有权检查失败，都保留原证据并报告具体 gap；不自动转新估计器、不减少噪声、不调阈值、不抹去负例。回退只禁用本轮 v2 新运行并停用其所属 worker/新实例，保留所有结果、原文和审计；不得删除数据库以让报告“干净”。

## 计划自审记录（仅计划核对，不是测试结果）

- 设计 §1—3：全局约束、任务1/5/10；§4：任务2/4/6；§5：任务1/3/4/5；§6：任务6/7；§7：任务8/10；§8：任务7/9；§9—10：各任务负例、任务8/10；§11：任务10。
- v1 类型精确检查：ParameterSetV2 子类兼容 geometry，policy/prediction/InstalledModel 保持原精确类型；PreparedResult 两类型由显式 dispatcher 处理，worker 读取、runs.publish 与 results._load 均列入任务7。比较返回类型核对为 ComparisonReport，非 ComparisonResult。
- 源码/配置导出白名单、依赖 manifest、原文提取范围、摘要 DAG、原生逐点计数、独立 reviewer、旧测试库危险均有归属任务；没有隐含数据库迁移。
- 五项 Review Focus 均有对应失败测试。生成器不等于误差证明，审批不等于数值通过；四正例及扩展包只能由后续实际结果决定是否完成。

## 依赖与执行交接

任务顺序 1→2→3→4→5→6→7→8→9→10。共享数值接口只在前置任务通过后消费；独立只读审查和资料核对可并行，不能多人同时修改相同文件。保持已同意的分层验证，不为单次文档变化重复长时间 DB 回归。

建议采用 Subagent-driven：每个可测试任务由独立实施者完成并审查，数值闭环和真实 HTTP 集成特别核查。另一选择为 Native：当前主代理顺序实施，最终独立全分支审查；仍不得省略设计规定的两个关键审查节点。请用户审阅计划并选择方式后再执行，不需要逐个小步骤重新确认。

数值依赖资料（2026-10-22 只读核对）：[PyPI 0.9.0](https://pypi.org/project/python-flint/0.9.0/)、[发布 JSON 与 wheel SHA](https://pypi.org/pypi/python-flint/0.9.0/json)、[128-bit context 用法](https://python-flint.readthedocs.io/en/latest/general.html)、[Arb 包围端点接口](https://python-flint.readthedocs.io/en/stable/arb.html)。选择库不代表本项目误差已被证明；所有新命令均尚待实施执行。
