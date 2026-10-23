# 功率积分误差证明修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户此前选择 Native，由主代理实施；关键数值和真实集成审查保留。

**Goal:** 在不改变原阈值及旧制品的前提下，修复双端功率平均值的严格包围，验证全部预注册合成输入，并接续原 Task 10 的隔离部署和验收追加交付。

**Architecture:** 生成端与独立校验端分别实现固定 64 分段的中点二阶修正和对称四阶余项。只提高功率状态序列阶数，不改变名义反演、合同或工作精度。新版本经 smoke 离线与真实闭环审查后，才进入 extended 全量验证和新阅读器/资料交付。

**Tech Stack:** Windows x64、CPython 3.13.2、Fraction、python-flint 0.9.0 / FLINT 3.6.0、现有 unittest、PostgreSQL/HTTP 与独立 worker。

**Spec:** [功率积分误差证明修订设计](../specs/2026-10-23-inversion-integration-proof-repair-design.md)，提交 `b5d097c9697a0d2ad560a2319fdd7f979904d1b5` 的正文，用户于 2026-10-23 回复“确认 实施”予以确认。规格文件保留提交时正文，其待审状态由本条审批记录补充；不修改历史文件。

本计划状态：待用户审阅。执行方式沿用 Native，无需重新选择；计划确认后按内部检查点连续执行，不逐步请求批准。产品基线为 `748c7eb54d030c4568d55a17dbdd61dbb4ce9a84`，当前设计基线为 `b5d097c`。

## Global Constraints

- 响应任务书 Y1-01、Y1-04、Y3-06；`source_kind=synthetic`、`purpose=software_pair_test`、`field_accuracy_certified=false`，不称脱敏实测数据或现场标定。
- 原设计和计划中未被本修订明确替换的约束继续有效。v1 的 12 个封存数值文件、旧设计/计划/报告/包及 8765/8770/8772/8774/18882/18883 安装不改、不启停；远端不推送。
- 固定 128-bit、单线程、64 子区间、4096-bit 有理数及中间量、每 case≤524288 次物理区间求值；权重缓存≤128、证明缓存≤2048、根缓存≤128、轨迹缓存≤4。不得自适应提高精度或分段。
- 功率状态输入 `prec=6`、对应上下文 `cap=6`；根夹持/判别仍使用原规则，生成端固定 96 次二分。每普通原生点 129 次状态求值，1025 帧为 132225 次；时移校验额外计费，缓存命中不免除原校验收费。
- 每周期 8 秒、128 Hz、1025 帧；seed=20261022；四通道 scale 分别为 0.01 W、1e-8 rad、0.001 kN、1e-6 m，offset=0、nearest_even、signed i32。40 变体与四故障的参数、噪声、质量和时间规则不改。
- 编码前功率声明 0.01 W、角度声明 1e-20 rad、损耗界 0.02 W；包围宽度分别≤0.02 W、≤2e-20 rad，且必须另证唯一编码。
- 原物理/数值预算不变：基础速度 1e-5 rad/s；物理分子 100 N·m、因子 1e-4 m；数值分子 0.01 N·m、因子/位移 1e-7 m；逐点载荷误差 1000 N。原始/共同比较覆盖分别≥0.80，原始共同有效支撑非空 RMSE≤500 N。
- 原 180 秒总期限、一个执行槽、16 个等待请求、每运行预留 80 MiB、输入 64 MiB、结果 16 MiB、附件 4 MiB、参数/API 256 KiB、原文 32 MiB、解码 16 MiB 不变。其他域、窗口、带宽和政策数值继承原计划 Global Constraints。
- 不改变公开函数接口、schema、模型家族或审批语义；不新建运行时模块，不共用新增物理/积分 helper。两端独立证明，不读取参考数组或借用对方包围。
- 使用新 source/checker/generator 摘要、新包、新隔离安装和新审批；不覆盖 smoke-r4、失败 extended-r1 或复用旧批准。只管理本轮已核验所有权的进程和数据库。
- 禁止 `unittest discover`、整个旧 worker/results/api 测试模块、旧数据库基类、`tools.verify_inversion` 和直接写审批表。只运行下列定向测试及原运维文档的显式纯测试白名单。

## Review Focus

1. 功率求导丢失一阶，缺失 c4 被当作零导致假精确：Task 1 检查 `power.prec > 4`、截断拒绝和实际高阶系数。
2. 中点 c4 为零而整段非零、根区间和中心系数存在不确定性：Task 1 用精确多项式与区间系数验证完整包围，不只比较两端结果。
3. 真正半码边界仍存在，负数最近偶数及 i32 边界不能被修复抹掉：Task 1 保留精确舍入和歧义拒绝测试。
4. 通过缓存、异常退出或超大有理数绕过资源与上下文限制：Task 1 验证 cap/精度/线程恢复、位长和求值拒绝；Task 2 实测 worker 期限/容量。
5. 新代码错误复用旧指纹或少一个扩展场景仍报告成功：Task 2 指纹与历史兼容测试；Task 3 实际包原生全量清点和源/报告绑定，缺项或错源拒绝。

---

## 文件边界与执行环境

| 文件 | 职责与改动边界 |
| --- | --- |
| `tools/testpack/forward_v2.py` | 独立生成端积分、局部序列上下文及所需域检查；不改根搜索与编码政策 |
| `backend/inversion/coverage_sources.py` | 独立来源积分、域和序列检查；保持每原码核验、瞬时平均差及声明预算检查 |
| 新 `tests/test_inversion_quadrature_v2.py` | 两套独立公式、原失败点、数学/域/资源负例 |
| `tests/test_inversion_models_v2_unit.py`、`tests/test_inversion_v2_release.py` | 两处源码进入完整指纹和导出、旧快照不可复用的保护性测试 |
| 新 `tests/test_inversion_extended_inventory_v2.py` | 本轮实际 extended 制品、离线证据及四通道全量计数的只读验收入口；不启动服务、不制造审批 |
| `docs/operations/inversion-v2-validation.md` | 追加新公式、测试命令及新验证入口；保留旧记录范围 |
| 本轮 `.superpowers/sdd/2026-10-23-integration-proof-repair/` | 私有过程日志、审查、发布身份及最终资料脚本；不加入产品制品，不覆盖旧 scratch |

执行前重新核对当前已存在的 linked worktree、分支、clean 状态与待提交内容；复用 `C:/Users/hp/.codex/worktrees/production-device-registry/外专双百`，不回到原演示目录。变量约定：

```powershell
$taskRepo = 'C:/Users/hp/.codex/worktrees/production-device-registry/外专双百'
$taskPython = 'C:/Users/hp/AppData/Local/OilWell/inversion-v2-20261022-r2/venv/Scripts/python.exe'
$taskSmoke = 'C:/Users/hp/AppData/Local/OilWell/inversion-v2-release-20261023-quadrature-smoke-r1'
$taskExtended = 'C:/Users/hp/AppData/Local/OilWell/inversion-v2-release-20261023-quadrature-extended-r1'
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
```

两个发布目标在编制计划时不存在；执行时须再次核对。任何目标/日志已存在则拒绝覆盖并记录新的明确尝试身份。每条原生命令后立即检查 `$LASTEXITCODE`，非零即停止；不能只依赖 `$ErrorActionPreference`。长命令用可轮询会话，保留输出并持续通报，不长时间阻塞交互。外层日志独占新路径；不得改写旧报告或旧 `release.json`。

### Task 1 双端积分修复与定向数学验证

**Files:** 修改两个既有积分文件；新增 `tests/test_inversion_quadrature_v2.py`。

**Interfaces:** 两文件各自实现私有 `_panel_quadrature(midpoint, segment, h:Q)->tuple[object,Q]`，输入为各自求出的 Arb power series，返回 `(correction_ball, symmetric_radius)`；object 的实际类型为 `flint.arb`，只在现有 `proof_context` 中使用，不新增顶层原生库导入。两文件各自将 `_context(*, series_cap:int=4)` 限定为 4 或 6，默认根计算仍为 4；积分入口显式传 6。公开 `forward_enclosures(...)`、`verify_sources(...)` 和 `_expected(config_sha,physics_id,index)` 返回语义不变。

- [ ] 新增 `QuadratureV2Tests.test_known_extended_rounding_boundary`：从 `installed_config('extended')` 的 canonical SHA 获取 variant-00，分别调用生成端 `forward_enclosures(case,Q(99,64))` 和校验端 `_expected(config_sha,case.case_id,198)`；要求两端自己的量化函数均得 4382705，功率区间相交且宽度≤0.02 W。先用旧源码运行该方法，应在唯一量化断言前复现 `inversion.proof_rounding_ambiguous`，保存 RED 日志，不修改原数据。
- [ ] 新增两个 helper 的精确公式测试。对 panel=[−1,1]，逐一测试 P=1、t、t²、t³，中心修正分别包含 2、0、`Q(2,3)`、0，余项为零；混合 P=1+t+t²+t³ 包含 `Q(8,3)`，余项为零。对 ±t⁴，完整积分分别为 ±`Q(2,5)`，对称余项至少覆盖 `Q(2,5)`；对 t⁵，整段 c4 范围覆盖 [−5,5]，余项≥2，禁止用中点 c4=0。所有例子分别喂给两端 helper，不以其中一端作 oracle。
- [ ] 补 `test_uncertain_center_and_root_are_not_dropped`：给 c0/c2 注入精确有理包围，结果必须包含逐项区间计算的端点；对 P(t,r)=(1+r)t⁴+rt²+r、r∈[1,2]、panel=[−1,1]，最终包围包含精确积分范围 `[Q(52,15),Q(98,15)]`。对原注册机构，透传记录两端各自所有 `_state` 调用接收的是完整 root 区间，实际 c0/c4 包围分别与端点根计算有交集；不把区间计算的包含单调性当数学定理。补 `test_truncated_nonfinite_and_domain_refused`：`prec=4` 拒绝为 `proof_series_truncated`，NaN/无穷系数拒绝为 `proof_nonfinite`；测试专用奇异几何的零分母、非正可微根号参数或非法反三角域拒绝为 `coverage_basis_mismatch`。只有测试夹具可构造这些模型，不登记新工况。
- [ ] 补 `test_quantization_policy_unchanged`：两端对 ±1/2、±3/2 按最近偶数得到 0、±2；[49/100,51/100] 在 scale=1 时明确歧义拒绝，超过 signed i32 拒绝。补 `test_context_and_resources_restored`：原 cap/precision/threads 在正常与异常退出后相等；不支持 cap 拒绝；有理中间量超 4096 bit 或预算多消费一次均拒绝；冷缓存生成/校验单点各执行 129 次 `_state`。实际 SourceProof 缓存命中仍按原计费路径检查。
- [ ] 运行 `& $taskPython -B -m unittest tests.test_inversion_quadrature_v2 -v`，新接口缺失和原失败点必须红灯；已存在的舍入保护性用例可直接通过，不人为破坏正确代码制造失败。
- [ ] 实现两套独立 helper：`T=h*c0_mid+h³*c2_mid/12`，`R=h⁵*max(abs(c4_panel endpoints))/80`。h 必须是正 Fraction；对 h 的幂、系数端点、缩放和累计半径逐步执行现有 `check_fraction`；T 使用 Arb 保留计算误差。输入/输出序列阶数和所需有限系数检查不得省略。各自累计 T/R 后除窗口宽度，保留原宽度/唯一编码及平均瞬时差检查。
- [ ] 两套 `_state` 显式 `prec=6`，最终功率支持 c4；积分入口使用局部 cap=6、根入口默认 4。必要的全区间域检查留在各自 `_joint`/`_beta`/`_state`，不引入共享物理函数。沿用上述 `InversionError` 错误码及 `proof_resource_limit`；非法 cap 或非正/非 Fraction 的 h 抛 `ValueError`。不能返回零或 NaN 作为正常证明。原 96 次根夹持、极值搜索、registry 和其他通道的物理定义不改。
- [ ] 运行上面的新测试，加 `tests.test_inversion_interval_runtime tests.test_inversion_v1_seal`；再运行 `tests.test_testpack_generator_v2 tests.test_inversion_coverage_sources tests.test_inversion_coverage_physics tests.test_inversion_solver_v2 tests.test_testpack_budget_v2`。要求全部强制断言通过、实际四基础正例预算/覆盖/RMSE 合格；source 在运行期间冻结。此时不运行全部扩展轨迹。
- [ ] 做关键数值独立审查，覆盖推导系数、全 panel/根域、c4 截断、两端独立性和费用归属；处理 Critical/Important，记录未执行项。定向补测后提交精确通过的文件：`git commit -m "fix: tighten independent power integral enclosures"`。不能把这一步写成已完成部署。

### Task 2 指纹兼容与新 smoke 真实验证

**Files:** 扩充两个既有模型/发布测试；新增 `tests/test_inversion_extended_inventory_v2.py`；追加运维文档。正常实现无需修改运行时指纹清单，因为两个数值文件已在现有闭包中；若测试证明有漏项，先给出失败证据并在原闭包文件中修复，不新增运行时模块。

**Interfaces:** 新测试模块提供 `require_inventory(native_rows:list[dict], positive_ids:tuple[str,...])->dict` 和 `inspect_extended_release(root:Path)->dict`，仅为测试入口，不被产品导入。前者校验已解析每个正例的四个原生通道：精确 ID 集合44个、无重复、每通道1025个 i32 原码/质量、固定 role/unit/scale/offset、128Hz及起始索引0；全部正例质量为0。返回 `positive_cases=44, native_frames=45100, encoded_values=180400`。后者必须重新读取实际波形、SHA 和离线证明，不能只信 metadata 计数。

`native_rows` 每项严格包含 `case_id:str`、`sample_rate_hz:Q`、`first_sample_index:int`、`time_offset_ns:int`、`channels:list[dict]`；每个通道含 `role:str`、`unit:str`、`scale:Q`、`offset:Q`、`raw:tuple[int,...]`、`quality:tuple[int,...]`。role/unit 对应 active_power/W、crank_angle/rad、load/kN、displacement/m，恰好各一次；时间偏移0，scale/offset按 Global Constraints。禁止 bool 冒充 int。缺项、重复、错类型、错值统一抛 `AssertionError`，返回计数为整数，不修改输入。转换器从实际扫描描述取 scale/offset，并与 `decode_native` 的固定合同独立比对，不能仅使用解码器重建的默认值掩盖错误原始描述。

- [ ] 增加指纹保护性测试：以内存 `Path.read_bytes` 替身分别改变 `forward_v2.py`、`coverage_sources.py`，要求对应 generator/checker/installed snapshot 摘要发生变化；真实磁盘不写。验证两文件在新导出闭包中、v1 snapshot 与 seal 不变、旧包仍可按已存指纹读取。已有正确行为可直接绿灯；任何暴露的缺口先固定失败断言再修复。
- [ ] 写 `InventoryGuardTests`：构造 44 正例的完整计数夹具，分别删除一个 case/末帧/通道、重复 case、改变 scale、越界原码、非零正例质量，要求拒绝；只提供“全部通过”摘要而没有原生数组也拒绝。运行 `& $taskPython -B -m unittest tests.test_inversion_extended_inventory_v2.InventoryGuardTests -v`，缺接口先 RED，再实现测试 helper 并转 GREEN。夹具仅检查清点规则，不充当实际生成证据。
- [ ] 实现 `inspect_extended_release`：要求明确 `extended`、新 release/pack/offline 路径及同一 source commit；用现有 `verify_export`、`load_pack`、`positive_case_ids`、`decode_native` 重新验证每份实际文件、四通道描述与原码。调用原 `require_offline_proofs` 检查44个正例的独立来源验证、1025点、原覆盖/RMSE；另要求四故障的 finite-inversion 断言存在且 passed。缺字段、错源、错摘要或缺行均拒绝。返回计数、源身份、pack身份及所读文件/报告摘要；此入口不重新做第二轮全量物理计算，明确依赖本轮实际 generate→encode→validate 的同源记录。
- [ ] 新增 `ExtendedNativeTests.test_actual_extended_native_inventory`，要求环境变量 `OILWELL_QUADRATURE_EXTENDED_RELEASE` 明确指向本轮新根；缺省抛错，不 skip。断言上述 44/45100/180400 和四故障清单。此类只在 Task 3 的 extended 准备完成后单独运行，不加入普通纯测试白名单。在 `InventoryGuardTests` 增加 wrong source commit / missing offline proof 的短测试：用临时最小 release/报告夹具，仅替换底层包解码及文件校验 I/O；源绑定和证明行校验保留真实实现，分别改变源提交、删除一行证明后要求 `AssertionError`。实际 ExtendedNativeTests 禁止这些替身，不启动服务或修改产品文件。
- [ ] 运维文档追加公式/输入阶数/不改128-bit说明；把新 quadrature 模块和 `InventoryGuardTests` 加入普通显式白名单，单列实际 ExtendedNativeTests 命令及前置门禁。运行 `& $taskPython -B -m unittest tests.test_inversion_models_v2_unit tests.test_inversion_v2_release tests.test_inversion_extended_inventory_v2.InventoryGuardTests tests.test_inversion_v1_seal -v`，并保留已有 v1/v2 包往返测试结果。提交：`git commit -m "test: bind quadrature releases to complete native evidence"`。核对 clean HEAD 后冻结发布源，未授权推送。
- [ ] 准备新 smoke。先核验上述新目标不存在、端口18784/56442/19894、18780/56443/19895、15030空闲；占用即停，不杀占用进程。运行 `& $taskPython -B -m tools.testpack.deliver_v2 prepare --root $taskSmoke --source $taskRepo --profile smoke`。该入口从 clean Git blobs 导出并在导出树生成/校验，再初始化和启动所属 lab；必须核对新 source 身份、offline 四正例及新实例所有权。原生成工具限时900秒、离线校验7200秒，不是worker期限。任何失败保留目标并停止。
- [ ] 运行 smoke 实际测试：

```powershell
$env:OILWELL_TESTPACK_INVERSION_V2_LIVE='1'
$env:OILWELL_TESTPACK_LIVE_ROOT="$taskSmoke/lab-v2-smoke"
$env:OILWELL_TESTPACK_LIVE_PACK="$taskSmoke/pack-v2-smoke"
$env:OILWELL_TESTPACK_LIVE_DELIVERY="$taskSmoke/lab-v2-smoke/run-v2-001"
& $taskPython -B -m unittest tests.test_testpack_inversion_live_v2 -v
```

  外层测试从工作树执行；既有 wrapper 自动核对导出字节，以 review 安装树 Python/cwd 执行真正 HTTP 编排，清除继承的数据库/路径环境。不得改为从工作树直接混用 installed 数据。`release.status=ready` 只代表准备完成，成功依据是实际 `verified-live.json` 的17项：4正例、4故障、9安全检查、相符摘要及已恢复权限/暂存/队列资源；live不允许skip。
- [ ] 核对每个正例 completed、1025预测点及1025证明点、独立复核和导出、原覆盖/RMSE及实际≤180秒/原容量。由独立审查核对真实链路与资源恢复；仅运行期间权限丢失不写成发布时注入，固定 deadline 字段不写成实测耗时。通过后才运行 `& $taskPython -B -m tools.testpack.deliver_v2 stop --root $taskSmoke`；核验停用ID属于本轮且 `preserved_data=true`，保留全部新/旧记录。

### Task 3 扩展全量验证与验收追加交付

**Files:** 本轮私有日志/清点回执/审查/新追加资料；产品源码保持 Task 2 的冻结版本。原 Task 10 的剩余要求继续有效。只有发现并复现必须修复的问题才重开相应代码任务，并重新建立新源/新发布身份。

**Interfaces:** 消费 Task 2 的 `inspect_extended_release(root)`；实际 live仍用既有wrapper。资料脚本保留 `build(smoke,extended,target,review,integrity,repo)` 的CLI参数语义，但使用新脚本和本轮实际证据路径，不复用旧硬编码文件名。

- [ ] 在 smoke 审查通过且本轮端口释放后，运行 `& $taskPython -B -m tools.testpack.deliver_v2 prepare --root $taskExtended --source $taskRepo --profile extended`。生成工具限时7200秒、单worker仍180秒。实际生成48个cycle（44正例+4故障），每个正例四通道逐点编码；实际离线 `validate_pack_v2→predict_v2→verify_sources` 独立核验所有功率/角度原码。参考载荷/位移不宣称经过第二套现场真值校验。强制正例失败即停，不提交带失败依据的实际审批。
- [ ] 设置 `$env:OILWELL_QUADRATURE_EXTENDED_RELEASE=$taskExtended`，运行 `& $taskPython -B -m unittest tests.test_inversion_extended_inventory_v2.ExtendedNativeTests -v`。必须实际读到180400个正例原码，44个独立来源证明以及四故障断言。将 `inspect_extended_release` 返回值写入本轮新 `extended-native-inventory.json`，记录 source/pack/report/file摘要；这是当前生成与离线执行的完整性检查，不冒称重复独立数值证明。
- [ ] 将三个 live 路径改为 `$taskExtended/lab-v2-extended`、`$taskExtended/pack-v2-extended`、`$taskExtended/lab-v2-extended/run-v2-001`，同一 unittest 入口执行。要求实际57项完整通过（44+4+9），结果/审批/导出/资源回执与 source对应；`release.approved_execution_status` 仍可能为 not_run，不能手动翻转，使用实际 live 回执判断。
- [ ] 按运维文档完整纯测试白名单执行一次最终相关回归，并加入新 quadrature/InventoryGuardTests。使用新的脚本和 `final-pure.log`，不运行旧 scratch 中拒绝覆盖的 `final-pure.ps1`。固定源码期间不得编辑被指纹覆盖的文件；按原许可单列宿主 symlink skip，其他强制检查不 skip。最终全分支独立审查覆盖原未完分支及此次修复；处理 Critical/Important 后针对性补测，必要时重建新制品，不把旧通过报告绑定新代码。
- [ ] 从原验收生成器创建本轮新脚本，替换旧 smoke-r4/extended-r1 等硬编码证据文件名为本轮实际路径，并纳入本修订/实施计划和 full-native 回执。先用缺57项、错源摘要、缺最终审查、未恢复权限/容量的负例确认生成器拒绝；真实输入必须通过原17/57、4/44正例及摘要检查。仅复制允许公开的合成证据/源码/许可，排除凭据、DSN、private和command-intents；输出到全新追加目录，禁止覆盖旧PDF/ZIP。
- [ ] 按 pdf 技能生成并逐页渲染检查新预算PDF和最终追加资料，核对ZIP CRC、每成员SHA、清单及声明范围。按 browser 技能从新导出树运行 `tools.testpack view --pack <新extended包> --report <新public/report.json> --port 0`，读取真实返回URL并检查两图、悬停两位小数、完整分页/有效段及报告来源；不臆测固定端口，不使用旧阅读器代替本轮检查。
- [ ] 重新核对 v1 seal 和原有旧部署/包/报告摘要基线；旧数据库没有完整前置摘要时不宣称全库字节不变。检查原始源码与所有新安装导出清单、权限/暂存恢复、`git diff --check` 和 clean 状态。停用本轮 extended lab时只执行其owned stop并保留数据；新只读入口按交付需要保留。交付新入口、PDF/ZIP/摘要、实际通过项及仍待硬件/现场验证项，原 Task 10 仅在其全部门禁完成后标记完成。

## 停止和验证记录

任何域、严格包围、唯一编码、原阈值、强制场景、时限/容量、兼容或所有权检查不满足时，保存准确 case/index/phase/code 和已有回执并停止后续扩展。不能自动切换公式、提精度、增分段、减少噪声或变体，也不能重新写旧失败结果。初始化中断时先读本轮保留 marker，再按精确所有权处理，不按端口批量停服务。

每步记录命令、源码提交、起止时间、退出码、实际断言数和日志摘要。研究探针、纯测试、离线数学通过、真实软件执行与现场证据分别计数。旧 smoke-r4和351通过/1skip只保留为其原基线，不能替代本轮新结果。

## 计划自审与交接

规格第1—2节由边界和 Task1失败回归覆盖；第3—4节由独立helper/序列/域/均值差测试覆盖；第5节由 Task1资源拒绝和 Task2真实资源检查覆盖；第6节由 Task2指纹与新安装覆盖；第7—8节由 Task2/3分层闭环、清点及最终交付覆盖。五项 Review Focus 均已有具体测试归属。

两个 helper 具有相同私有签名但各自实现，不跨模块调用。`inspect_extended_release` 是只读验收测试入口，不是新的产品模型。原生成/离线验证负责实际区间计算，清点入口负责读取全部编码与追溯到该次计算；两者职责不互相替代。

本计划沿用已选择的 Native。请用户审阅本计划是否准确覆盖修复与交付要求；确认后主代理开始 Task 1，关键审查和失败停止由本计划内部门禁控制，不再逐条确认。
