import {escapeHtml as esc} from '../components.js';
import {chart} from './charts.js';
import {KINDS} from './forms.js';

const value = item => item === null || item === undefined ? '未知' : typeof item === 'number' ? (Number.isFinite(item) ? item.toFixed(2) : '未知') : String(item);
const cell = item => `<td>${esc(value(item))}</td>`;
function table(headings, rows) {
  if (!rows?.length) return '<p class="study-empty">无可展示的数据行。</p>';
  return `<div class="study-table-wrap"><table><thead><tr>${headings.map(label=>`<th scope="col">${esc(label)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell).join('')}</tr>`).join('')}</tbody></table></div>`;
}
const codeList = items => items?.length ? `<ul>${items.map(item=>`<li>${esc(typeof item === 'object' ? `${item.code || ''} ${item.message || ''}` : item)}</li>`).join('')}</ul>` : '';
const statusLabels = {completed:'计算完成',fitted:'拟合通过',feasible:'存在可行候选',descriptive:'描述性比较',rejected:'被拒绝',no_feasible_candidate:'没有可行候选',insufficient_evidence:'证据不足',unknown:'未知'};
export const statusText = code => statusLabels[code] || `未知状态（${code || '缺失'}）`;

export function studyList(records, selectedId) {
  if (!records.length) return '<p class="study-empty">暂无工程研究记录。选择研究类型并填写来源后运行；软件示例需手动载入。</p>';
  return `<div class="study-list">${records.map(record=>`<button type="button" data-study-record="${esc(record.id)}" class="study-list-row ${record.id===selectedId?'active':''}">
    <strong>${esc(record.title)}</strong><span>${esc(KINDS[record.kind]?.label || record.kind)} · ${esc(statusText(record.status))}</span>
    <small>${esc(record.source?.kind === 'software_fixture' ? '软件样例' : '提交观测')} · ${esc(record.review_status || 'unreviewed')} · ${esc(record.created_at || '')}</small></button>`).join('')}</div>`;
}

function conditionResult(result) {
  if (result.training_curve) {
    const curves = [
      {name:'训练损失',points:result.training_curve.map(row=>({x:row.epoch,y:row.train_loss}))},
      {name:'验证损失',points:result.training_curve.map(row=>({x:row.epoch,y:row.validation_loss}))},
    ];
    return chart('BP 训练与验证损失',curves,'轮','损失') + `<h3>相同测试集上的三种方法</h3>${table(['方法','覆盖率','总体准确率','接受样本准确率','宏平均 F1','未知数'],
      ['rule','centroid','bp'].map(key=>{const m=result.metrics?.[key]||{};return [key,m.coverage,m.accuracy,m.accepted_accuracy,m.macro_F1,m.unknown_count]}))}
      ${['rule','centroid','bp'].map(key=>{const m=result.metrics?.[key];return m ? `<details><summary>${esc(key)} 混淆矩阵</summary>${table(['真实 / 预测',...(m.prediction_columns||[])],(m.labels||[]).map((label,i)=>[label,...(m.confusion_matrix?.[i]||[])]))}</details>` : ''}).join('')}
      <p>最佳轮次：${esc(value(result.best_epoch))}；训练数据摘要：<code>${esc(result.data_digest||'未知')}</code></p>`;
  }
  return table(['样本','判定','未校准模型分数','原因'],(result.predictions||[]).map(row=>[row.sample_id,row.label,row.score,(row.reasons||[]).join(', ')]));
}
function recoveryResult(result) {
  if(result.status!=='fitted')return `<div class="study-result-warning"><strong>拟合被拒绝</strong>${codeList(result.reason_codes)}<p>有效 ${esc(value(result.accepted_count))} 点，排除 ${esc(value(result.excluded_count))} 点。未生成可供调度的恢复模型。</p></div>`;
  const rows=result.fitted_points||[];
  const start=rows.length?Date.parse(rows[0].at):NaN;
  const elapsed=row=>(Date.parse(row.at)-start)/60000;
  return `<div class="study-metrics"><span>恢复时间常数 <strong>${esc(value(result.parameters?.tau_minutes))} min</strong></span><span>RMSE <strong>${esc(value(result.metrics?.rmse_m))} m</strong></span><span>R² <strong>${esc(value(result.metrics?.r_squared))}</strong></span></div>
    ${chart('观测液面与经验拟合',[{name:'观测',points:rows.map(row=>({x:elapsed(row),y:row.observed_m}))},{name:'拟合',points:rows.map(row=>({x:elapsed(row),y:row.predicted_m}))}],'min','m')}
    ${table(['时间','观测深度 m','拟合深度 m','残差 m'],rows.map(row=>[row.at,row.observed_m,row.predicted_m,row.residual_m]))}
    <p>有效 ${esc(value(result.accepted_count))} 点；排除 ${esc(value(result.excluded_count))} 点。敏感性范围仅表示留一拟合变化，不是统计置信区间。</p>`;
}
function scheduleResult(result) {
  if(result.status==='no_feasible_candidate')return `<div class="study-result-warning"><strong>没有可行候选</strong><p>已完整检查 ${esc(value(result.evaluated_combinations))} 个提交的组合；未生成替代调度。</p>
    ${table(['拒绝原因','组合数'],Object.entries(result.no_feasible_evidence?.rejection_counts||{}))}</div>`;
  const candidates=result.ranked_candidates||[];
  const chosen=candidates[0];
  const timeline=chosen?Object.entries(chosen.schedule_by_well||{}).map(([well,part])=>{
    let cursor=0;const points=[];
    for(const segment of part.segments||[]){const level=segment.state==='on'?1:0;points.push({x:cursor,y:level});cursor+=segment.duration_minutes;points.push({x:cursor,y:level});}
    return {name:well,points};
  }):[];
  const dayEnergy=chosen?Object.entries(chosen.daily_energy_kwh||{}).map(([day,kwh],i)=>({x:i+1,y:kwh})):[];
  return `<p>已检查 ${esc(value(result.evaluated_combinations))} 个提交组合；以下仅为可行候选排名，不是连续空间最优解。</p>
    ${table(['候选','电量 kWh','峰值 kW','启动次数'],candidates.map(row=>[row.candidate_id,row.total_energy_kwh,row.peak_power_kw,row.total_starts]))}
    ${chosen?`<h3>首选候选：${esc(chosen.candidate_id)}</h3>${chart('启停时序',timeline,'min','开=1 / 关=0')}
      ${chart('每日电量',[{name:'电量',points:dayEnergy}],'日序号','kWh')}
      ${table(['井','启停段（状态 × 分钟）'],Object.entries(chosen.schedule_by_well||{}).map(([well,part])=>[well,(part.segments||[]).map(s=>`${s.state} ${s.duration_minutes} min`).join(' → ')]))}
      ${table(['生产日','液量 m³','电量 kWh','启动次数'],Object.keys(chosen.daily_energy_kwh||{}).map(day=>[day,chosen.daily_liquid_m3?.[day],chosen.daily_energy_kwh?.[day],chosen.daily_starts?.[day]]))}`:''}`;
}
function effectsResult(result) {
  return `<p>${result.status==='insufficient_evidence'?'证据不足，不能进行效果判断。':'以下仅为有效观测小时的描述性比较，不代表因果节能量。'}</p>${codeList(result.reason_codes)}
    ${table(['阶段','覆盖率','观测小时','液量 m³','电量 kWh','电量 kWh/观测小时'],[['基线',result.baseline],['处理',result.treatment]].map(([name,v])=>[name,v?.coverage_fraction,v?.observed_hours,v?.liquid_m3,v?.energy_kwh,v?.energy_kwh_per_observed_hour]))}
    <p>电量/观测小时差值：${esc(value(result.comparison?.energy_kwh_per_observed_hour_difference))} kWh/h；实际节能量：未知。</p>`;
}
function spectrumResult(result) {
  const s=result.spectrum||{};const xs=s.frequency_hz||[],ys=s.amplitude_A_peak||[];
  return `<div class="study-metrics"><span>物理支撑 <strong>${esc(result.support||'未知')}</strong></span><span>侧频判定 <strong>${esc(result.finding||'未知')}</strong></span><span>频率分辨率 <strong>${esc(value(s.bin_spacing_hz))} Hz</strong></span></div>
    ${chart('单相电流幅值谱',[{name:'峰值幅度',points:xs.map((x,i)=>({x,y:ys[i]}))}],'Hz','A peak')}
    <p>电流校准与抗混叠来源为提交声明，未在此独立验证。未确认断条故障。</p>${codeList(result.reasons)}${codeList(result.confounders)}`;
}
function protectionResult(result) {
  const rows=result.timeline||[];
  return `<p>仅回放“本应跳闸”状态，不向设备执行动作。共 ${esc(value(rows.length))} 帧、${esc(value(result.events?.length||0))} 个事件。</p>
    ${table(['帧','时间','资格','规则状态','抑制','本地复位'],rows.map(row=>[row.frame_index,row.at,row.qualification,Object.entries(row.rules||{}).map(([id,v])=>`${id}:${v.state}`).join('; '),row.inhibit?'是':'否',row.reset_decision]))}
    ${table(['帧','事件','规则'],(result.events||[]).map(event=>[event.frame_index,event.kind,event.rule_id||'—']))}`;
}

export function studyDetail(record) {
  const r=record.result||{};const kind=record.kind;
  const view=kind.startsWith('condition_')?conditionResult(r):kind==='recovery_fit'?recoveryResult(r):kind==='energy_schedule'?scheduleResult(r):kind==='energy_effects'?effectsResult(r):kind==='motor_spectrum'?spectrumResult(r):protectionResult(r);
  return `<article class="study-detail"><header><div><p class="eyebrow">${esc(KINDS[kind]?.label||kind)}</p><h2>${esc(record.title)}</h2></div><span class="study-status">${esc(statusText(record.status))}</span></header>
    <div class="study-metadata"><span>来源：<strong>${esc(record.source?.kind==='software_fixture'?'软件测试向量':'提交观测')}</strong></span><span>作者：${esc(record.actor_id||'未知')}</span><span>记录版本：${esc(value(record.version))}</span><span>复核：${esc(record.review_status||'unreviewed')}</span><span>影子选用：${record.is_selected?'是':'否'}</span><span>依赖当前有效：${record.dependency_current===false?'否':'是'}</span></div>
    <p>来源说明：${esc(record.source?.description||'未填写')}</p><p>来源引用：${esc((record.source?.references||[]).join('；')||'无')}</p>
    <div class="study-digests"><span>输入 SHA-256 <code>${esc(record.input_sha256||'未知')}</code></span><span>结果 SHA-256 <code>${esc(record.result_sha256||'未知')}</code></span><span>算法 ${esc(record.algorithm_version||'未知')}</span></div>
    <section class="study-output"><h3>计算结果</h3>${view}</section>
    <details><summary>查看提交的原始 payload</summary><pre class="study-json">${esc(JSON.stringify(record.payload||{},null,2))}</pre></details>
    <details><summary>依赖与历史事件</summary>${table(['依赖记录','类型','复核版本'],(record.dependencies||[]).map(d=>[d.record_id,d.kind,d.review_version]))}
      <h4>复核历史</h4>${table(['版本','决定','复核人','时间','意见'],(record.review_history||[]).map(x=>[x.version,x.decision,x.actor_id,x.created_at,x.comment]))}
      <h4>影子选用历史</h4>${table(['版本','选用记录','操作人','时间','意见'],(record.selection_history||[]).map(x=>[x.version,x.record_id,x.actor_id,x.created_at,x.comment]))}</details></article>`;
}
