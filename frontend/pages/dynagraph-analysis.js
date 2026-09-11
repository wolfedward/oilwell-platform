import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

const CONDITION_LABELS = { normal: "正常", supply_shortage: "供液不足", intermittent: "间歇供液", pump_leak: "泵阀漏失", empty_pump: "抽空" };
const PROFILE_LABELS = {
  normal: "泵工作正常", traveling_valve_leak: "游动凡尔漏失", standing_valve_leak: "固定凡尔漏失",
  double_valve_leak: "双凡尔漏失", rod_break: "抽油杆断脱", tubing_leak: "油管漏失", supply_shortage: "供液不足",
  gas_interference: "气体影响", gas_lock: "气锁", discharge_blockage: "出油管线部分堵塞", filter_blockage: "滤沙器/附件堵塞",
  high_water_cut: "油井高含水", tight_packing: "盘根盒过紧", sand_obstruction: "砂阻", partial_plunger_exit: "活塞部分脱出工作筒",
  upstroke_hang: "上挂", downstroke_hang: "下挂", stuck_plunger: "活塞被卡", loose_liner: "衬套松动", flowing_well: "连抽带喷",
};
const COMPARISON_METRICS = [
  ["mae_kn", "平均绝对误差", "kN", 3], ["max_error_kn", "最大误差", "kN", 3], ["rmse_kn", "均方根误差", "kN", 3],
  ["area_error_percent", "面积偏差", "%", 2], ["stroke_error_m", "冲程偏差", "m", 4], ["correlation", "曲线相关系数", "", 4],
  ["upstroke_rmse_kn", "上冲程 RMSE", "kN", 3], ["downstroke_rmse_kn", "下冲程 RMSE", "kN", 3],
];
const FEATURE_LABELS = {
  max_load_kn: ["最大载荷", "kN"], min_load_kn: ["最小载荷", "kN"], area_kn_m: ["功图面积", "kN·m"],
  fillage: ["充满度", ""], upstroke_load_kn: ["上冲程载荷", "kN"], downstroke_load_kn: ["下冲程载荷", "kN"],
  load_difference_kn: ["载荷差", "kN"], top_edge_tilt_kn_per_m: ["上边斜率", "kN/m"], bottom_edge_tilt_kn_per_m: ["下边斜率", "kN/m"],
  closure_error: ["闭合误差", ""], turning_points: ["转折点", "个"],
};

function finite(value, fallback = 0) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(-1e12, Math.min(1e12, parsed));
}

function electricCurve(raw, intermediate, { smoothedOnly = false } = {}) {
  const width = 720, height = 290, left = 66, right = 22, top = 24, bottom = 56;
  const rawItems = Array.isArray(raw) ? raw : [];
  const smoothItems = Array.isArray(intermediate) ? intermediate : [];
  if (!rawItems.length && !smoothItems.length) return '<div class="chart-empty">暂无电功图点集</div>';
  const legacySequentialPhase = rawItems.length > 0 && rawItems.every((item, index) => {
    const phase = Number(item.phase);
    return Number.isFinite(phase) && Math.abs(phase - index) <= 1e-9;
  });
  const rawPoints = rawItems.map((item, index) => ({ x: legacySequentialPhase ? index / rawItems.length * 360 : finite(item.phase), y: finite(item.power) }));
  const smoothPoints = smoothItems.map((item, index) => ({ x: finite(item.phase_deg, index), y: finite(item.power_kw) }));
  const points = [...rawPoints, ...smoothPoints];
  const minY = Math.min(...points.map((item) => item.y), 0);
  const maxY = Math.max(...points.map((item) => item.y), minY + 1);
  const plotWidth = width - left - right, plotHeight = height - top - bottom;
  const x = (value) => left + Math.max(0, Math.min(360, finite(value))) / 360 * plotWidth;
  const y = (value) => top + (maxY - value) / (maxY - minY) * (height - top - bottom);
  const line = (items) => items.map((item) => `${x(item.x).toFixed(1)},${y(item.y).toFixed(1)}`).join(" ");
  const divisions = [0, .25, .5, .75, 1];
  const xTicks = divisions.map((ratio) => { const px = left + ratio * plotWidth; return `<g data-axis="x" class="chart-tick"><line x1="${px.toFixed(1)}" y1="${top}" x2="${px.toFixed(1)}" y2="${height - bottom}" /><text x="${px.toFixed(1)}" y="${height - bottom + 19}" text-anchor="middle">${(ratio * 360).toFixed(0)}</text></g>`; }).join("");
  const yTicks = divisions.map((ratio) => { const py = top + ratio * plotHeight; return `<g data-axis="y" class="chart-tick"><line x1="${left}" y1="${py.toFixed(1)}" x2="${width - right}" y2="${py.toFixed(1)}" /><text x="${left - 10}" y="${(py + 4).toFixed(1)}" text-anchor="end">${(maxY - ratio * (maxY - minY)).toFixed(2)}</text></g>`; }).join("");
  const pointTitles = (items, kind) => items.map((item) => `<circle class="electric-point ${kind}" ${charts.pointAttributes({ series: kind, seriesLabel: kind === "raw" ? "原始电功图" : "滤波功率", x: item.x, y: item.y, xUnit: "°", yUnit: "kW" })} cx="${x(item.x).toFixed(1)}" cy="${y(item.y).toFixed(1)}" r="4"><title>${kind === "raw" ? "原始" : "滤波"}：曲柄角 ${item.x.toFixed(1)}°，有功功率 ${item.y.toFixed(3)} kW</title></circle>`).join("");
  return `<figure class="chart"><svg viewBox="0 0 ${width} ${height}" data-plot-left="${left}" data-plot-right="${width - right}" data-plot-top="${top}" data-plot-bottom="${height - bottom}" role="img" aria-label="电功图原始与滤波功率曲线"><desc>曲柄角(°)与有功功率(kW)坐标</desc><rect class="plot-frame" x="${left}" y="${top}" width="${plotWidth}" height="${plotHeight}" /><g class="chart-grid electric-grid">${xTicks}${yTicks}</g>
    ${smoothedOnly ? "" : `<polyline points="${line(rawPoints)}" fill="none" stroke="#94a3b8" stroke-width="1.6" />`}
    ${smoothPoints.length ? `<polyline points="${line(smoothPoints)}" fill="none" stroke="#1e6ee8" stroke-width="2.4" />` : ""}
    <g class="chart-points electric-points">${smoothedOnly ? "" : pointTitles(rawPoints, "raw")}${pointTitles(smoothPoints, "smooth")}</g><text class="chart-axis-title" x="${left + plotWidth / 2}" y="${height - 10}" text-anchor="middle">曲柄角 (°)</text><text class="chart-axis-title" x="16" y="${top + plotHeight / 2}" text-anchor="middle" transform="rotate(-90 16 ${top + plotHeight / 2})">有功功率 (kW)</text></svg></figure>`;
}

function definitionTable(components, rows, emptyText) {
  return components.table([
    { key: "name", label: "参数" }, { key: "value", label: "数值" }, { key: "unit", label: "单位" }, { key: "basis", label: "说明" },
  ], rows, { emptyText });
}

function sampledTorque(intermediate) {
  if (!Array.isArray(intermediate)) return [];
  return intermediate.filter((_item, index) => index % 10 === 0 || index === intermediate.length - 1);
}

function pointSet(values) {
  return (Array.isArray(values) ? values : []).map((item) => ({
    displacement: item.displacement_m ?? item.displacement,
    load: item.load_kn ?? item.load,
  }));
}

function comparisonQuality(comparison, format) {
  if (!comparison) return { label: "待建立双卡对比", tone: "pending", note: "该存量档案未包含载荷位移传感器原始点。" };
  const rmse = finite(comparison.rmse_kn);
  const correlation = finite(comparison.correlation);
  if (rmse <= 1.5 && correlation >= .98) return { label: "拟合优良", tone: "active", note: `RMSE ${format.number(rmse, 3)} kN，相关系数 ${format.number(correlation, 4)}` };
  if (rmse <= 3 && correlation >= .94) return { label: "拟合可用", tone: "warning", note: `RMSE ${format.number(rmse, 3)} kN，可继续标定` };
  return { label: "建议重新标定", tone: "failed", note: `RMSE ${format.number(rmse, 3)} kN，偏差超出推荐范围` };
}

function alarmAssessment(data) {
  const code = data.profile_code || data.sample_data?.profile_code;
  const diagnosis = data.latest_diagnosis?.condition;
  if (!diagnosis) return { label: "待分析", tone: "pending" };
  if (diagnosis === "normal") return { label: "无报警", tone: "active" };
  if (["rod_break", "stuck_plunger", "gas_lock"].includes(code) || diagnosis === "empty_pump") return { label: "紧急报警", tone: "failed" };
  return { label: "工况预警", tone: "warning" };
}

function analysisMarkup(data, runs, context) {
  const { components, format } = context;
  const conversion = data.conversion || {};
  const raw = data.sample_data?.electric_samples || [];
  const intermediate = conversion.intermediate || [];
  const features = data.features || {};
  const diagnosis = data.latest_diagnosis;
  const production = data.latest_production;
  const run = data.latest_model_run || runs.items?.[0];
  const parameters = data.parameters || conversion.parameters || data.sample_data?.mechanism || {};
  const profileCode = data.profile_code || conversion.profile_code || data.sample_data?.profile_code;
  const profileLabel = data.profile_label || conversion.profile_label || data.sample_data?.profile_label || PROFILE_LABELS[profileCode] || profileCode || "待识别";
  const comparison = data.comparison || conversion.comparison;
  const comparisonModel = data.comparison_model_version || conversion.comparison_model_version || "待运行";
  const quality = comparisonQuality(comparison, format);
  const alarm = alarmAssessment(data);
  const featureRows = Object.entries(features).map(([key, value]) => ({ name: FEATURE_LABELS[key]?.[0] || key, value: key === "fillage" ? format.percent(value) : format.number(value, 4), unit: FEATURE_LABELS[key]?.[1] || "", basis: key === "fillage" ? "载荷跨度标定" : "服务端转换结果" }));
  const torqueRows = sampledTorque(intermediate).map((item) => ({ phase: `${format.number(item.phase_deg, 1)}°`, power: format.number(item.power_kw, 3), motor: format.number(item.motor_torque_nm, 2), crank: format.number(item.crank_torque_nm, 2), balance: format.number(item.balance_torque_nm, 2), net: format.number(item.net_torque_nm, 2), inferred: item.load_inferred ? "有效" : "死点排除" }));
  const rules = diagnosis?.all_rule_hits || [];
  const candidates = diagnosis?.candidates || [];
  const templateComparison = candidates.map((item) => ({
    label: CONDITION_LABELS[item.condition] || item.condition,
    value: finite(item.template_score) * 100,
  }));
  const productionRows = production ? [
    { name: "泵径", value: format.number(production.diameter_mm, 2), unit: "mm", basis: "泵参数" },
    { name: "有效冲程", value: format.number(production.effective_stroke_m, 4), unit: "m", basis: "冲程 × 充满度" },
    { name: "每日冲次", value: format.number(production.strokes_per_day, 1), unit: "次/日", basis: "冲次 × 1,440" },
    { name: "理论排量", value: format.number(production.theoretical_m3_day, 3), unit: "m³/d", basis: "泵面积 × 冲程 × 每日冲次" },
    { name: "充满度", value: format.percent(production.fillage), unit: "", basis: production.fillage_basis },
    { name: "泵效", value: format.percent(production.pump_efficiency), unit: "", basis: "井筒效率参数" },
    { name: "修正系数", value: format.number(production.correction_coefficient, 3), unit: "", basis: "现场修正" },
    { name: "估算产液量", value: format.number(production.estimated_m3_day, 3), unit: "m³/d", basis: production.formula },
  ] : [];
  const dynagraphPoints = pointSet(conversion.points || data.converted_points);
  const sensorPoints = pointSet(data.sensor_points || conversion.sensor_points || data.sample_data?.sensor_samples);
  const comparisonRows = COMPARISON_METRICS.map(([key, label, unit, digits]) => ({
    key, label, value: comparison ? format.number(comparison[key], digits) : "—", unit,
  }));
  const polishedRodPower = conversion.energy?.card_power_kw;
  const workflow = [
    ["电功图", raw.length ? "已采集" : "待采集"], ["转矩换算", intermediate.length ? "已转换" : "待转换"],
    ["示功图", dynagraphPoints.length ? "已生成" : "待生成"], ["智能诊断", diagnosis ? "已诊断" : "待诊断"],
    ["产液计量", production ? "已估算" : "待估算"], ["人工复核", data.reviews?.length ? "已复核" : "待复核"],
  ];
  return `<section class="detail-hero panel"><div><span class="object-code">${escapeHtml(data.id)}</span><h2>${escapeHtml(data.well_name)} · ${escapeHtml(data.well_id)}</h2><p>终端 ${escapeHtml(data.device_id || "历史归档")} · 原始报文 ${escapeHtml(data.raw_message_id ? `#${data.raw_message_id}` : "归档记录")}</p></div><dl><div><dt>分析状态</dt><dd>${components.statusTag(data.status).html}</dd></div><div><dt>采集时间</dt><dd>${escapeHtml(format.dateTime(data.captured_at))}</dd></div><div><dt>模型版本</dt><dd>${escapeHtml(run?.model_version || data.model_version || "待运行")}</dd></div><div><dt>模型运行</dt><dd>${escapeHtml(run?.id || "—")}</dd></div></dl></section>
    <section class="panel workflow-panel"><header><div><p class="eyebrow">OPERATIONS CHAIN</p><h2>采集到复核闭环</h2></div><span>电功图 → 转矩换算 → 示功图 → 智能诊断 → 产液计量 → 人工复核</span></header><ol>${workflow.map(([label, status], index) => `<li class="${status.startsWith("已") ? "complete" : "pending"}"><b>${index + 1}</b><span>${label}</span><small>${status}</small></li>`).join("")}</ol></section>
    <section class="detail-grid"><article class="panel chart-panel wide"><header><div><p class="eyebrow">RAW ELECTRIC CARD</p><h2>原始电功图</h2></div><span>${raw.length} 个采样点</span></header>${electricCurve(raw, [], {})}</article><article class="panel info-panel"><header><div><p class="eyebrow">TRACEABILITY</p><h2>模型版本与审计记录</h2></div></header><dl><div><dt>功图模型</dt><dd>${escapeHtml(run?.component_versions?.dynagraph || run?.model_version || "—")}</dd></div><div><dt>诊断模型</dt><dd>${escapeHtml(run?.component_versions?.diagnosis || "—")}</dd></div><div><dt>模板版本</dt><dd>${escapeHtml(run?.component_versions?.template || diagnosis?.template_version || "—")}</dd></div><div><dt>产量模型</dt><dd>${escapeHtml(run?.component_versions?.production || production?.model_version || "—")}</dd></div></dl><button type="button" class="button secondary full" data-audit-link>查看审计记录</button></article></section>
    <section class="detail-grid"><article class="panel chart-panel wide"><header><div><p class="eyebrow">FILTERED POWER</p><h2>滤波结果</h2></div><span>灰色原始 · 蓝色五点平滑</span></header>${electricCurve(raw, intermediate)}</article><article class="panel info-panel"><header><div><p class="eyebrow">ENERGY CHECK</p><h2>能量一致性</h2></div></header><dl>${Object.entries(conversion.energy || {}).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(format.number(value, 4))}</dd></div>`).join("") || "<div><dt>状态</dt><dd>待分析</dd></div>"}</dl></article></section>
    <section class="panel list-panel" style="margin-bottom:14px"><header><div><p class="eyebrow">TORQUE CONVERSION</p><h2>转矩换算</h2></div><span>抽样展示 ${torqueRows.length} / ${intermediate.length} 点</span></header><div class="panel-body">${components.table([{ key: "phase", label: "曲柄角" }, { key: "power", label: "功率 kW" }, { key: "motor", label: "电机转矩 N·m" }, { key: "crank", label: "曲柄转矩 N·m" }, { key: "balance", label: "平衡转矩 N·m" }, { key: "net", label: "净转矩 N·m" }, { key: "inferred", label: "载荷换算" }], torqueRows, { emptyText: "运行分析后显示转矩换算过程" })}</div></section>
    <section class="panel dynagraph-comparison-panel" data-dynagraph-comparison><div class="dynagraph-status-head"><div><span>传感器工况样本</span><strong>${escapeHtml(profileLabel)}</strong><small>${escapeHtml(profileCode || "profile_pending")}</small></div><div class="right"><span>工况报警判断</span><strong class="status-tag ${escapeHtml(alarm.tone)}">${escapeHtml(alarm.label)}</strong><small>机器诊断：${escapeHtml(CONDITION_LABELS[diagnosis?.condition] || diagnosis?.condition || "待分析")}</small></div></div>
      <div class="dynagraph-chart-head"><div><p class="eyebrow">DUAL SURFACE DYNAGRAPH</p><h2>反演示功图与传感器示功图对比</h2><p>共用位移/载荷坐标域，可逐点悬浮查看差异。</p></div><fieldset class="dynagraph-toggles"><legend>曲线显示</legend><label class="legend-inferred"><input id="toggleInferred" type="checkbox" checked /> <i></i>电功图反演示功图</label><label class="legend-measured"><input id="toggleMeasured" type="checkbox" checked ${sensorPoints.length ? "" : "disabled"} /> <i></i>载荷位移传感器示功图</label></fieldset></div>
      <div class="dynagraph-operating-strip"><div><span>冲程</span><strong>${escapeHtml(format.number(parameters.stroke_m, 2))}</strong><small>m</small></div><div><span>冲次</span><strong>${escapeHtml(format.number(parameters.strokes_per_minute, 2))}</strong><small>min⁻¹</small></div><div><span>最小载荷</span><strong>${escapeHtml(format.number(features.min_load_kn, 2))}</strong><small>kN</small></div><div><span>最大载荷</span><strong>${escapeHtml(format.number(features.max_load_kn, 2))}</strong><small>kN</small></div><div><span>载荷差</span><strong>${escapeHtml(format.number(features.load_difference_kn, 2))}</strong><small>kN</small></div><div><span>功图面积</span><strong>${escapeHtml(format.number(features.area_kn_m, 2))}</strong><small>kN·m</small></div><div><span>光杆功率</span><strong>${escapeHtml(format.number(polishedRodPower, 3))}</strong><small>kW</small></div></div>
      <div class="dynagraph-chart-body"><div class="dynagraph-plot">${charts.dynagraph(dynagraphPoints, { title: `${data.well_id} 双示功图对比`, xLabel: "位移 (m)", yLabel: "载荷 (kN)", reference: sensorPoints })}</div><aside class="dynagraph-side-metrics"><header><span>算法评价</span><strong class="status-tag ${escapeHtml(quality.tone)}">${escapeHtml(quality.label)}</strong></header><p>${escapeHtml(quality.note)}</p><dl>${comparisonRows.map((item) => `<div data-comparison-metric="${item.key}"><dt>${escapeHtml(item.label)}</dt><dd>${escapeHtml(item.value)} <small>${escapeHtml(item.unit)}</small></dd></div>`).join("")}</dl><footer>对比模型 ${escapeHtml(comparisonModel)}</footer></aside></div>
      <footer class="dynagraph-result-footer"><div><span>状态</span><strong>${escapeHtml(profileLabel)}</strong></div><div><span>产液量</span><strong>${escapeHtml(format.number(production?.estimated_m3_day, 2))} m³/d</strong></div><div><span>充满度</span><strong>${escapeHtml(format.percent(features.fillage))}</strong></div><div><span>泵效</span><strong>${escapeHtml(format.percent(production?.pump_efficiency))}</strong></div><div><span>置信度</span><strong>${escapeHtml(diagnosis ? format.percent(diagnosis.confidence) : "—")}</strong></div><div class="advice"><span>处置建议</span><strong>${escapeHtml(diagnosis?.recommendation || "完成分析后生成处置建议。")}</strong></div></footer></section>
    <section class="detail-grid"><article class="panel list-panel wide"><header><div><p class="eyebrow">FEATURE VECTOR</p><h2>特征参数</h2></div></header><div class="panel-body">${definitionTable(components, featureRows, "运行分析后显示特征参数")}</div></article><article class="panel info-panel"><header><div><p class="eyebrow">CALIBRATION</p><h2>反演标定参数</h2></div></header><dl><div><dt>平衡转矩</dt><dd>${escapeHtml(format.number(parameters.balance_torque_nm, 2))} N·m</dd></div><div><dt>平衡相位</dt><dd>${escapeHtml(format.number(parameters.balance_phase_deg, 2))}°</dd></div><div><dt>减速比</dt><dd>${escapeHtml(format.number(parameters.gear_ratio, 2))}</dd></div><div><dt>传动效率</dt><dd>${escapeHtml(format.percent(parameters.transmission_efficiency))}</dd></div></dl></article></section>
    <section class="detail-grid"><article class="panel list-panel wide"><header><div><p class="eyebrow">EXPLAINABLE DIAGNOSIS</p><h2>诊断依据</h2></div><span>${diagnosis ? `${CONDITION_LABELS[diagnosis.condition] || diagnosis.condition} · ${format.percent(diagnosis.confidence)}` : "待分析"}</span></header><div class="panel-body"><h3>规则命中</h3>${components.table([{ key: "description", label: "判据" }, { key: "condition", label: "指向工况", render: (value) => CONDITION_LABELS[value] || value }, { key: "observed", label: "观测值", render: (value) => format.number(value, 4) }, { key: "threshold", label: "阈值", render: (value) => format.number(value, 4) }, { key: "severity", label: "强度", render: (value) => format.percent(value) }], rules, { emptyText: "暂无命中规则" })}<h3>候选工况</h3>${components.table([{ key: "condition", label: "候选工况", render: (value) => CONDITION_LABELS[value] || value }, { key: "combined_score", label: "综合得分", render: (value) => format.percent(value) }, { key: "template_score", label: "模板相似度", render: (value) => format.percent(value) }, { key: "rule_score", label: "规则得分", render: (value) => format.percent(value) }, { key: "template_version", label: "模板版本" }], candidates, { emptyText: "运行分析后显示候选工况" })}</div></article><article class="panel info-panel"><header><div><p class="eyebrow">RECOMMENDATION</p><h2>诊断结论</h2></div></header><dl><div><dt>机器结论</dt><dd>${escapeHtml(CONDITION_LABELS[diagnosis?.condition] || diagnosis?.condition || "待分析")}</dd></div><div><dt>可信度</dt><dd>${escapeHtml(diagnosis ? format.percent(diagnosis.confidence) : "—")}</dd></div><div><dt>模板</dt><dd>${escapeHtml(diagnosis?.template_version || "—")}</dd></div><div><dt>复核版本</dt><dd>${escapeHtml(diagnosis?.review_version || "—")}</dd></div></dl><p class="source-note">${escapeHtml(diagnosis?.recommendation || "分析完成后生成可解释建议。")}</p></article></section>
    <section class="panel chart-panel" style="margin-bottom:14px"><header><div><p class="eyebrow">TEMPLATE SIMILARITY</p><h2>模板相似度对比</h2></div><span>服务端候选工况模板评分</span></header>${charts.bar(templateComparison, { title: "候选工况模板相似度对比", unit: "%" })}</section>
    <section class="panel list-panel" style="margin-bottom:14px"><header><div><p class="eyebrow">LIQUID ESTIMATE</p><h2>产液量计算</h2></div><span>${production ? `估算 ${format.number(production.estimated_m3_day, 2)} m³/d` : "待分析"}</span></header><div class="production-flow"><div><b>1</b><strong>机构参数</strong><span>泵径、冲程、冲次</span></div><i>→</i><div><b>2</b><strong>有效冲程与充满度</strong><span>${production ? `${format.number(production.effective_stroke_m, 3)} m · ${format.percent(production.fillage)}` : "等待功图特征"}</span></div><i>→</i><div><b>3</b><strong>日产液估算</strong><span>${production ? `${format.number(production.estimated_m3_day, 3)} m³/d` : "等待模型计算"}</span></div></div><div class="panel-body">${definitionTable(components, productionRows, "运行分析后显示公式因子")}</div></section>`;
}

export async function renderDynagraphAnalysis(context) {
  const { api, components, workspace, page, pageHeader, params, permissions, navigate, isCurrent } = context;
  let dynagraphId = params.id;
  if (!dynagraphId) {
    const listing = await api.get("/api/dynagraphs?page_size=1");
    dynagraphId = listing.items?.[0]?.id;
    if (!dynagraphId) { workspace.innerHTML = `${pageHeader(page)}${components.empty("暂无功图档案")}`; return true; }
  }
  const [data, runs] = await Promise.all([
    api.get(`/api/dynagraphs/${encodeURIComponent(dynagraphId)}`),
    api.get(`/api/model-runs?dynagraph_id=${encodeURIComponent(dynagraphId)}&page_size=20`),
  ]);
  if (!isCurrent()) return false;
  const canAnalyze = permissions.has("diagnosis.run");
  const suffix = `<div class="header-actions"><button class="button secondary" type="button" id="analysisBack">返回档案</button>${data.latest_diagnosis ? '<button class="button secondary" type="button" id="analysisReview">进入诊断复核</button>' : ""}${canAnalyze ? `<button class="button primary" type="button" id="analysisRun">${data.status === "analyzed" ? "重新运行分析" : "运行智能分析"}</button>` : ""}</div>`;
  workspace.innerHTML = `${pageHeader(page, suffix)}${analysisMarkup(data, runs, context)}`;
  const comparisonPanel = workspace.querySelector("[data-dynagraph-comparison]");
  const inferredToggle = workspace.querySelector("#toggleInferred");
  const measuredToggle = workspace.querySelector("#toggleMeasured");
  const syncDynagraphSeries = (changed) => {
    if (!inferredToggle || !measuredToggle || !comparisonPanel) return;
    if (!inferredToggle.checked && !measuredToggle.checked) {
      changed.checked = true;
      components.toast("至少保留一条示功图曲线", "error");
    }
    comparisonPanel.querySelectorAll('[data-series="inferred"]').forEach((element) => element.toggleAttribute("hidden", !inferredToggle.checked));
    comparisonPanel.querySelectorAll('[data-series="measured"]').forEach((element) => element.toggleAttribute("hidden", !measuredToggle.checked));
  };
  inferredToggle?.addEventListener("change", (event) => syncDynagraphSeries(event.currentTarget));
  measuredToggle?.addEventListener("change", (event) => syncDynagraphSeries(event.currentTarget));
  workspace.querySelector("#analysisBack").addEventListener("click", () => navigate("dynagraphs", { well_id: data.well_id }));
  workspace.querySelector("#analysisReview")?.addEventListener("click", () => navigate("diagnosis-review", { id: data.id }));
  workspace.querySelector("[data-audit-link]")?.addEventListener("click", () => navigate("audit", { object_type: "dynagraph", object_id: data.id }));
  workspace.querySelector("#analysisRun")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = "转换与诊断计算中…";
    try {
      await api.post(`/api/dynagraphs/${encodeURIComponent(data.id)}/analyze`, {});
      components.toast("智能分析已完成并写入模型运行记录");
      navigate("dynagraph-analysis", { id: data.id });
    } catch (error) {
      components.toast(error.message || "智能分析失败", "error");
      event.currentTarget.disabled = false;
      event.currentTarget.textContent = "重新运行分析";
    }
  });
  return true;
}
