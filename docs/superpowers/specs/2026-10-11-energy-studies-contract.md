# WP06 恢复与有限候选影子调度契约

继承 integrated-review-release 全局约束。只实现液面深度的局部经验模型，不复用旧产液率 fitter，不计算未观测的“真实节能率”。所有输出 `shadow_only=true`, `dispatch_allowed=false`, `field_validation=false`。

## 恢复纯函数

`backend.studies.recovery.fit_recovery(payload)->dict`，版本 `recovery_depth/1`。

仅允许 payload `{schema:'recovery.depth/1',well_id,datum,unit:'m',shutdown_at,resolution_m,accuracy_m,policy,observations}`。well_id非空<=128，datum非空<=256，shutdown_at必须带时区。resolution_m>0，accuracy_m>=0或null，两者<=1000；null表示无测量不确定度依据，不擅自补0。

policy 全字段显式：`tau_min_minutes`(0,10080)、`tau_max_minutes`(tau_min,10080]、`tau_grid_points`整数16..256、`min_r_squared`[0,1]、`max_rmse_m`(0,10000]、`max_gap_minutes`(0,10080]、`max_relative_tau_spread`(0,100]、`max_equilibrium_spread_m`(0,100000]。软件筛选值不是行业安全阈值。

observations6..128项 `{at,depth_m,quality,pump_state}`，UTC时间严格递增且>=shutdown_at、跨度<=10080min，depth_m有限非bool且[0,100000]，quality valid/invalid/unknown，pump_state off/on/unknown。未知字段/无时区/重复时间/错单位作为 StudyError 422；不自动排序掩盖乱序。

物理拟合：深度向下为正，`d(t)=d_eq+A*exp(-t/tau)`，t相对shutdown分钟。对显式tau范围建立确定性对数网格；各tau解两参数线性最小二乘，按(SSE,tau)选最优。保留负/非物理最优并拒绝，不悄悄夹到0。要求d_eq>=0、A>0。有效起点必须与shutdown同刻；有效点>=6且所有观测pump_state='off'，有效点最大间隔<=policy，变化超过2*resolution，R²达标、RMSE达标、最优tau不在搜索边界，否则返回status='rejected'和reason_codes，不伪造参数。

逐点留一拟合做敏感性（允许留一后5点），记录tau、eq范围；任一非物理/边界或spread超过显式阈值则拒绝。该范围不是置信区间。输出最多5组真实拟合联合参数情景：主拟合、tau最小/最大、eq最小/最大，去重；禁止随意组合参数极值。经验深度残差包络为max_abs_residual + (accuracy_m如果已知，否则0)，同时独立记录accuracy未知。没有动态外推保证。

成功输出固定 `{status:'fitted',well_id,model_version:'recovery_depth/1',input_digest,parameters:{equilibrium_depth_m,initial_depth_m,tau_minutes},support:{depth_min_m,depth_max_m,duration_minutes},scenarios:[{equilibrium_depth_m,initial_depth_m,tau_minutes}],fitted_points:[{at,observed_m,predicted_m,residual_m}],metrics:{sse_m2,rmse_m,max_abs_residual_m,r_squared},uncertainty:{method,tau_min_minutes,tau_max_minutes,equilibrium_depth_min_m,equilibrium_depth_max_m,depth_residual_envelope_m,measurement_accuracy_known,statistical_confidence:null},accepted_count,excluded_count,excluded_points,reason_codes,assumptions,shadow_only,dispatch_allowed,field_validation}`。

拒绝输出同well_id/model_version/input_digest、status/reason_codes/计数/原始观测质量说明，parameters/scenarios可空，不能用0填缺失。无效质量点记录excluded原因，不能被拟合或连线跨过超限缺口。

## 有限时域调度

`backend.studies.energy_schedule.optimize_schedule(payload)->dict`，版本 `shadow_schedule/1`。

payload仅 `{schema:'energy.schedule/1',horizon_start,horizon_end,production_day_timezone:'Asia/Shanghai',site_power_limit_kw,max_simultaneous_starts,wells}`。时区明确转换到+08:00，起止必须当地00:00，完整1..7天。site_power_limit_kw>0且<=1e6，max_simultaneous_starts整数1..4。最多4口井，井号唯一。

每井仅：`well_id,recovery_record_id,parameter_basis,initial_depth_m,initial_state,initial_state_elapsed_minutes,annulus_storage_area_m2,pump_intake_depth_m,minimum_submergence_m,pumping_liquid_rate_m3_h,run_input_power_kw,standby_input_power_kw,startup_input_power_kw,startup_duration_minutes,min_on_minutes,min_off_minutes,max_starts_per_calendar_day,minimum_liquid_m3_per_calendar_day,terminal_max_depth_m,candidates`。内部由服务另注入`recovery`完整且已批准的fit结果，HTTP调用者禁止提供该键。recovery.well_id必须相同且status=fitted，artifact参数/支持界仍严格校验。

所有物理数显式、有限、非bool，深度0..100000；面积(0,100]m²；泵液量(0,10000]m³/h；电输入功率[0,1e6]kW；时间按整数min，初态持续0..10080、startup0..60、min_on/min_off1..1440；每日starts1..1440；日产液0..1e6m³；parameter_basis非空<=2000并由用户说明来源。initial_state on/off。不允许以铭牌机械功率假称输入功率，无依据可以研究但warnings标明声明参数未独立验证。

每井candidates1..8，`{candidate_id,segments:[{state:'on'|'off',duration_minutes}]}`；ID唯一<=64，segments1..128，时长整数1..10080，合计精确覆盖时域，相邻不能同状态。首段可等于初态（计入已持续时长），或在初态已达到最短时长时转换；末段也须满足最短时长，不能截断规避。

逐段模拟的物料平衡：令u=on?1:0，q=泵液量/60，e=exp(-Δ/tau)，

`d_next = d_eq + (d-d_eq)*e + u*q*tau/A*(1-e)`。

段液量u*q*Δ，电量按实际分段输入kW*分钟/60。停止用standby，启动时段用startup，其他开机用run。初态已开且持续不足startup时保留剩余启动功率，但不新增一次启动。每次真实off→on按当地日历日计一次。按状态边界/启动结束/当地午夜切分，逐日累计。

每个实际拟合情景单独评估液面范围、最大恢复支持时长、沉没度及终点深度；状态与支撑检查在各单调段端点，不能将上游未拟合结果作为模型。关机连续时长含初态已停时长，不能超recovery.support.duration_minutes。所有深度须处于原support范围；超过返回model_extrapolation，不外扩为“可信”。终点不能高于terminal_max_depth_m。每个完整日液量必须>=指定最低量，starts<=上限。

预计算每井每候选的情景/每日/功率区间，枚举完整有限候选笛卡尔积。组合数<=4096，`组合数 * 各井最大候选分段边界数之和 * 3`<=1_000_000，否则资源上限明确拒绝，不删候选。汇总每组合的全部时间边界检查瞬时总功率及同时启动数，不能用平均值替代。按总电量、峰值功率、启动总数、候选ID稳定排序。返回前20可行候选、最多10拒绝候选及完整拒绝类别计数；不声称连续空间全局最优。

输出 `{status:'feasible'|'no_feasible_candidate',algorithm_version,search_space:'supplied_candidates',search_complete:true,evaluated_combinations,selected_candidate_id,ranked_candidates,no_feasible_evidence,actual_energy_saving_kwh:null,shadow_only:true,dispatch_allowed:false,field_validation:false,assumptions}`。候选含schedule_by_well、daily_liquid_m3/daily_energy_kwh/daily_starts、peak_power_kw、depth_trace和scenario_results、violations。深度trace每情景每段边界，输出总量有界。纯函数不查库；复核/选用由StudyService处理。

## 效果描述

`backend.studies.energy_effects.evaluate_effects(payload)->dict` 版本 `energy_effects/1`，payload `{schema:'energy.effects/1',well_id,baseline,treatment,min_coverage_fraction,comparability}`。两组均 `{start,end,intervals}`，intervals最多512个 `{start,end,liquid_m3,energy_kwh,quality}`，非重叠、在声明时段内、时间带时区，量非负有限，quality valid/invalid/unknown。比较两阶段不能时间重叠。min_coverage_fraction显式(0,1]。comparability仅 `{same_meter,consistent_units,similar_operating_conditions,other_changes_recorded}`四个bool；这只是声明，不成为因果证明。

真实有效区间加总液量、电量、观测小时、coverage；无覆盖返回null，不除0；coverage不足或任一可比性false给insufficient_evidence。仅报告观测时段每小时液量/电量的描述性差值，actual_energy_saving_kwh=null、causal_effect_proven=false；不能对缺口补算节电量。

## 依据

- <https://www.itl.nist.gov/div898/handbook/pmd/section1/pmd142.htm>：非线性回归、残差与异常敏感性。
- <https://www.nature.com/articles/s41598-020-59094-0>：间抽补液与井筒储量建模背景。本项目一阶模型和离散有限候选算法为明确的软件简化，不冒称论文已验证本实现。
- <https://doi.org/10.1016/j.cor.2017.09.012>：抽油井调度约束研究，非本实现性能证明。
- <https://www.echometer.com/Portals/0/Quick%20Reference%20Guides/q_liqlev_hi.pdf>：液面测量条件/来源要求。
