import {escapeHtml as esc} from '../components.js';

export const KINDS = {
  condition_training: {label:'工况训练', page:'condition-lab', schema:'condition.training/1', fields:[['features','特征名称与单位','json'],['rows','样本、井组与训练/验证/测试分区','json'],['rules','分析人声明的规则基线','json'],['options','训练与未知判定参数','json']]},
  condition_prediction: {label:'工况推理', page:'condition-lab', schema:'condition.prediction/1', fields:[['model_record_id','已批准并选用的训练记录','dependency'],['rows','待推理的新样本','json'],['confidence_min','最低模型分数','number'],['ood_margin','训练范围外扩倍数','number']]},
  recovery_fit: {label:'液面恢复拟合', page:'recovery-lab', schema:'recovery.depth/1', fields:[['well_id','井号','text'],['datum','液面深度基准面','text'],['unit','深度单位（必须为 m）','text'],['shutdown_at','关井时刻（含时区）','text'],['resolution_m','测量分辨率 m','number'],['accuracy_m','测量准确度 m；未知填 null','nullable'],['policy','显式拟合筛选条件','json'],['observations','液面观测点、质量与泵状态','json']]},
  energy_schedule: {label:'有限候选影子调度', page:'recovery-lab', schema:'energy.schedule/1', fields:[['horizon_start','时域开始（北京时间当地午夜）','text'],['horizon_end','时域结束（北京时间当地午夜）','text'],['production_day_timezone','生产日时区','text'],['site_power_limit_kw','站点瞬时功率上限 kW','number'],['max_simultaneous_starts','同时启动上限','number'],['wells','井参数与候选启停段','json']]},
  energy_effects: {label:'观测效果比较', page:'recovery-lab', schema:'energy.effects/1', fields:[['well_id','井号','text'],['baseline','基线时段与真实观测区间','json'],['treatment','处理时段与真实观测区间','json'],['min_coverage_fraction','最低覆盖比例','number'],['comparability','可比性逐项声明','json']]},
  motor_spectrum: {label:'电流频谱证据', page:'motor-lab', schema:'motor.spectrum/1', fields:[['capture','瞬时电流采样、质量与校准来源','json'],['motor','电机类型、转速与电源依据','json'],['policy','显式研究阈值','json']]},
  protection_replay: {label:'保护规则回放', page:'motor-lab', schema:'motor.protection/1', fields:[['settings','规则、延时、回差与有效期','json'],['frames','逐帧三相观测与复位请求','json']]},
};

const formatted = value => value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
export function payloadEditor(kind, payload, dependencies = []) {
  const definition = KINDS[kind];
  const fields = definition.fields.map(([name,label,type]) => {
    const value = payload?.[name];
    if (type === 'json') return `<label class="study-field study-field-wide">${esc(label)}<textarea data-study-field="${name}" data-value-type="json" rows="${name === 'rows' || name === 'wells' || name === 'frames' ? 10 : 6}" spellcheck="false">${esc(formatted(value))}</textarea></label>`;
    if (type === 'dependency') return `<label class="study-field">${esc(label)}<select data-study-field="${name}" data-value-type="text"><option value="">选择当前可用记录</option>${dependencies.map(item => `<option value="${esc(item.id)}" ${item.id === value ? 'selected' : ''}>${esc(item.title)} · ${esc(item.id.slice(0,8))}</option>`).join('')}</select></label>`;
    return `<label class="study-field">${esc(label)}<input data-study-field="${name}" data-value-type="${type}" value="${esc(formatted(value))}" ${type === 'number' ? 'type="number" step="any"' : 'type="text"'} /></label>`;
  }).join('');
  return `<div class="study-payload-fields"><p class="study-boundary">输入须有真实来源和单位；下方示例按钮仅填入软件测试向量。</p><div class="study-form-grid">${fields}</div>
    <details class="study-advanced"><summary>高级：编辑完整 payload JSON</summary><p>高级编辑优先于上方字段。服务端逐项校验字段、数值与大小。</p><textarea data-study-advanced rows="18" spellcheck="false">${esc(payload ? JSON.stringify(payload,null,2) : '')}</textarea></details></div>`;
}

function finite(value, label) {
  const number = Number(value);
  if (!value.trim() || !Number.isFinite(number)) throw Error(`${label}须填写有限数值`);
  return number;
}
export function readPayload(container, kind) {
  const definition = KINDS[kind];
  const advanced = container.querySelector('[data-study-advanced]');
  if (advanced?.closest('details')?.open && advanced.value.trim()) {
    const parsed = JSON.parse(advanced.value);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object' || parsed.schema !== definition.schema) throw Error('完整 JSON 须为当前研究类型的 payload 对象');
    return parsed;
  }
  const payload = {schema: definition.schema};
  for (const [name,label,type] of definition.fields) {
    const field = container.querySelector(`[data-study-field="${name}"]`);
    if (!field) throw Error(`缺少${label}`);
    const raw = field.value.trim();
    if (type === 'json') {if (!raw) throw Error(`${label}不能为空`);payload[name] = JSON.parse(raw);}
    else if (type === 'number') payload[name] = finite(raw,label);
    else if (type === 'nullable') payload[name] = raw.toLowerCase() === 'null' || !raw ? null : finite(raw,label);
    else {if (!raw) throw Error(`${label}不能为空`);payload[name] = raw;}
  }
  return payload;
}

export async function readPayloadFile(file, kind) {
  if (!file || file.size > 8 * 1024 * 1024) throw Error('选择不超过 8 MiB 的 JSON 文件');
  const value = JSON.parse(await file.text());
  const payload = value?.payload && value?.kind === kind ? value.payload : value;
  if (!payload || Array.isArray(payload) || payload.schema !== KINDS[kind].schema) throw Error('文件 schema 与当前研究类型不符');
  return payload;
}
