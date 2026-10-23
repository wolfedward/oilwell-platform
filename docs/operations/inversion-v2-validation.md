# 反演 v2 隔离验证与回退

本说明只覆盖任务书 Y1-01、Y1-04、Y3-06 的有限合成输入软件验证。源码、离线证明、HTTP 审批、计算结果、独立账户复核及导出分别记录。安装就绪不是计算成功；软件账户的审批不是人类签字；同源合成比较不是现场精度认证。

## 不变量

- 不修改原演示版、旧审阅实例、旧包、旧报告或业务数据库。不使用旧数据库测试夹具，不运行 `discover` 或 `tools.verify_inversion`。
- 发布源必须完整 clean HEAD；目标必须不存在。生成器、检查器、HTTP 编排均运行在相同 Git blob 字节的导出树，不依赖工作目录换行规则。v1 仍用原有指纹流程。
- 新环境 CPython 3.13.2 / Windows x64 GIL；python-flint 0.9.0 wheel SHA-256 为 `1f3a3672d589e376d47288dcebcb712997dc719880a4dbf1779bf0f2b20d0293`。逐文件核验原生库、许可证、安装清单与运行时；只在新实例安装。
- 原阈值不变：原生 1025 点、原始/联合覆盖率至少 0.80、RMSE 不大于 500 N；180 秒运行期限；输入 64 MiB、结果 16 MiB、附件 4 MiB、一个运行槽和 16 个排队槽。
- 全部正例离线证明通过后才创建新 lab；任何失败保留文件，不调低阈值或省略场景。smoke 全链路与独立审查通过后才运行 extended。

## 显式相关回归白名单

在任务专用数值 Python 环境、仓库根目录运行下列明确列表。测试仅使用临时文件、内存服务、临时浏览器或自己创建的无凭据计算子进程。`testpack_contracts` 的宿主符号链接条件性 skip 需在报告中单列；这不允许 live 测试 skip。

```powershell
$taskTests = @(
 'tests.test_inversion_interval_runtime',
 'tests.test_inversion_quadrature_v2',
 'tests.test_inversion_extended_inventory_v2.InventoryGuardTests',
 'tests.test_inversion_v1_seal',
 'tests.test_inversion_v2_provision',
 'tests.test_inversion_contracts_v2',
 'tests.test_inversion_motion_bounds',
 'tests.test_testpack_generator_v2',
 'tests.test_inversion_coverage_sources',
 'tests.test_inversion_coverage_physics',
 'tests.test_inversion_solver_v2',
 'tests.test_testpack_budget_v2',
 'tests.test_inversion_governance_v2_unit',
 'tests.test_inversion_input_v2',
 'tests.test_inversion_models_v2_unit',
 'tests.test_inversion_codec_v2',
 'tests.test_inversion_compute_v2',
 'tests.test_inversion_result_dispatch',
 'tests.test_testpack_inversion_http_v2',
 'tests.test_testpack_report_v2',
 'tests.test_inversion_browser_v2',
 'tests.test_inversion_v2_release',
 'tests.test_testpack_owned_fault_v2',
 'tests.test_inversion_contracts',
 'tests.test_inversion_motion',
 'tests.test_inversion_comparison',
 'tests.test_inversion_display_fill',
 'tests.test_testpack_contracts',
 'tests.test_testpack_mechanics',
 'tests.test_testpack_inversion_binding',
 'tests.test_testpack_lab',
 'tests.test_testpack_runtime_integrity',
 'tests.test_testpack_waveform_run',
 'tests.test_testpack_viewer_details',
 'tests.test_inversion_browser',
 'tests.test_testpack_report',
 'tests.test_review_deploy',
 'tests.test_inversion_worker.CodecTests.test_fixed_column_validation_rejects_unknown_shape',
 'tests.test_inversion_worker.CodecTests.test_fixed_columns_preserve_exact_time_index_and_fraction_diagnostics',
 'tests.test_inversion_worker.CodecTests.test_reference_edges_round_trip_and_legacy_columns_remain_immutable',
 'tests.test_inversion_worker.CodecTests.test_reference_edge_predecessor_is_bound_across_chunks',
 'tests.test_inversion_worker.CodecTests.test_result_metadata_alone_is_subject_to_aggregate_limit'
)
& $taskPython -B -m unittest @taskTests -v
```

不得换成整个 `test_inversion_worker/results/api` 模块；其中旧 DB 类会迁移或清空专用旧库。

## 新隔离制品

变量 `$taskRoot` 指向全新的专用目录，父目录已存在且不在仓库或旧部署内。`$taskRepo` 是当前 clean 源仓库。

```powershell
& $taskPython -B -m tools.testpack.deliver_v2 prepare --root $taskRoot --source $taskRepo --profile smoke
```

发布器建立 `$taskRoot/source/code` 不可变源、独立 venv、`pack-v2-smoke`、离线报告和 `lab-v2-smoke`。两实例端口：monitoring HTTP 18784 / PostgreSQL 56442 / MQTT 19894；review HTTP 18780 / PostgreSQL 56443 / MQTT 19895；Modbus 15030。任一端口占用即拒绝，绝不停止其他服务。公开日志不含账户密码。

批量离线制包进程限时：smoke 为 900 秒，含 44 条独立正例轨迹的 extended 为 7200 秒。它只限制整包生成工具，不改变单 case 的区间算子预算、精度或正式 worker 的 180 秒期限；不得把整包准备时间写成单次反演耗时。

`release.json` 的 `ready` 只代表组件/数值运行时就绪，`approved_execution_status` 仍为 `not_run`。

```powershell
$env:OILWELL_TESTPACK_INVERSION_V2_LIVE='1'
$env:OILWELL_TESTPACK_LIVE_ROOT="$taskRoot/lab-v2-smoke"
$env:OILWELL_TESTPACK_LIVE_PACK="$taskRoot/pack-v2-smoke"
$env:OILWELL_TESTPACK_LIVE_DELIVERY="$taskRoot/lab-v2-smoke/run-v2-001"
& $taskPython -B -m unittest tests.test_testpack_inversion_live_v2 -v
```

测试入口使用安装树的 Python 和源码执行 HTTP 编排，不将工作树代码混入安装树。显式 opt-in 后缺路径、缺审批、缺证明、缺场景、角色或资源未恢复均报错，不 skip。smoke 必须得到 4 正例 + 4 故障 + 9 权限/撤销/进程 guards，共 17 项；extended 为 44 + 4 + 9，共 57 项。

保留每次 HTTP command intent（原 key 和请求体）、完整 outcome、导出文件、实际 1000+25 分页的原生预测/证明、运行/复核/导出 ID 与摘要。`verified-live.json` 绑定完整 outcome、资源观测和公开报告 SHA。

`resource-observations.json` 使用现有 `review_inversion_reader`，只读事务、3 秒语句/1 秒锁超时，无数据库 owner 连接或权限提升。记录 claim 到终态事件耗时（不是计算 CPU 耗时）、请求/快照/结果规范字节、分块及附件摘要、容量结算、staging 恢复。逻辑容量不包括 PostgreSQL 行/索引/WAL/磁盘开销；不得宣称它代表整机磁盘占用。

超时 guard 暂停本轮已核验的无凭据计算子进程，保留监督进程的真实 180 秒期限。结果只证明期限执行、进程回收和没有迟到结果；不冒称实际尝试过迟到 SQL 发布。后者的 generation/owner fence 由独立纯测试验证。

Windows venv 的记录 PID 可能属于转发启动器。故障工具只接受明确的 `worker [转发] → spawn supervisor → [转发] compute` 角色链：逐层校验实际可执行文件、完整参数、父 PID、输入 request，并持有所有原生进程句柄后再次核对身份。转发器与真正解释器的命令行可能完全相同；只暂停最后的 base Python 计算进程，不暂停转发器或监督进程。未知层级、多个候选、身份变化均拒绝。独立小测试使用新建 venv、创建即挂起后纳入独占 job 的临时进程，先验证实际五层链、心跳暂停/恢复及全树回收；不连接数据库或既有服务。这类小测试不能替代实际 HTTP 的取消与 180 秒超时验证。

## 回退及扩展

先核对 release 与子实例所有权，停用本轮两个新实例，保留数据库、结果、审计、包和日志：

```powershell
& $taskPython -B -m tools.testpack.deliver_v2 stop --root $taskRoot
```

停止凭据及精确 PID/创建时间/命令/数据库 system identifier 仍由既有部署器验证。`rollback.json` 记录停用 ID；只能是该 release 的新实例。若初始化中断尚未形成完整 lab，先人工检查 retained `release.json` / `private/lab.json` / 子 marker，仅对记录属于本轮且已到可停止状态的子实例执行 `tools.review_deploy stop`，不得按端口批量杀进程。

smoke 独立审查通过并释放本轮端口后，以另一个新 `$taskRoot` 和 `--profile extended` 执行同流程。禁止覆盖 smoke。最后阅读器用 `view --port 0` 申请实际可用 loopback socket，记录工具真实返回的 URL；不臆测固定端口。

公开报告仍保留未执行的遥测/研究/浏览器等其他阶段为 `not_run`，整体可能为 `incomplete`；反演验证通过不等于全部年度任务完成。阶段验收追加资料必须列出现场、设备、真实载荷/位移对照、标定与安全联锁尚未验证项。

## 2026-10-23 积分证明修订与全量原生清点

生成端与独立来源校验端分别采用固定64分段的中点二阶修正和对称四阶余项。Arb系数为 `c_k=P^(k)/k!`，每段计算 `T=h*c0_mid+h³*c2_mid/12`、`R=h⁵*sup_panel|c4|/80`；完整平均包围为 `(ΣT+[-ΣR,ΣR])/w`。整段、完整根区间及中心求值误差均保留；不能仅取中点的四阶项。

功率状态输入序列 `prec=6`、局部 `cap=6`，系数工作精度仍128-bit、单线程。根计算仍用原cap4及原夹持规则；缺失四阶系数、非有限数、非法解析域、超位长或无法唯一舍入仍拒绝。此项不改变180秒worker期限、原噪声、通道步长、覆盖率和载荷误差阈值。新方法必须新建包、源码身份、安装及审批，旧通过/失败报告不改写。

普通白名单只运行快速 `InventoryGuardTests`；不要直接运行整个 `test_inversion_extended_inventory_v2` 模块。实际清点必须在本轮新smoke闭环和审查通过、新extended准备成功之后显式执行：

```powershell
$env:OILWELL_QUADRATURE_EXTENDED_RELEASE = $taskExtended
& $taskPython -B -m unittest tests.test_inversion_extended_inventory_v2.ExtendedNativeTests -v
if ($LASTEXITCODE -ne 0) { throw 'Extended native inventory failed' }
```

缺少明确目录时该测试失败而非skip。实际检查重新读取44正例全部1025帧、四通道，共45100帧/180400原码，并核对四故障证明。原生格式本身没有scale/offset字段，所以额外读取并核对每个cycle的外部元数据，不能把解码器默认值当原始声明。

检查入口仅用新导出树自己的Python/cwd重建输入绑定与校验摘要，避免工作树换行或代码身份混入已安装报告；重新核对release/export/pack、全部实际波形及离线证明的摘要。该过程不启动服务、不访问数据库、不重新运行物理轨迹，也不创建审批。它证明本轮实际生成、编码、离线独立校验记录的完整性和关联，不是第二次独立物理验证或现场精度证明。正式HTTP闭环17/57项仍单独执行和报告。
