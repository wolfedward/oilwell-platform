import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

function recoveryCurve(observations, fitted) {
  const safe = (items) => items.filter((p) => Number.isFinite(p.shut_in_minutes) && Number.isFinite(p.liquid_rate_m3_day));
  const observed = safe(observations), model = safe(fitted);
  const points = [...observed, ...model];
  if (!points.length) return '<p>等待恢复试验观测。</p>';
  const xmax = Math.max(1, ...points.map((p) => p.shut_in_minutes));
  const ymax = Math.max(1, ...points.map((p) => p.liquid_rate_m3_day)) * 1.08;
  const x = (value) => 58 + value / xmax * 630;
  const y = (value) => 218 - value / ymax * 190;
  const series = (values, name, label, color, dashed) => `<path fill="none" stroke="${color}" stroke-width="2.5" ${dashed ? 'stroke-dasharray="5 4"' : ''} d="${values.map((p, i) => `${i ? 'L' : 'M'}${x(p.shut_in_minutes).toFixed(2)},${y(p.liquid_rate_m3_day).toFixed(2)}`).join(' ')}"/><g class="chart-points" style="--chart-color:${color}">${values.map((p) => `<circle ${charts.pointAttributes({ series: name, seriesLabel: label, x: p.shut_in_minutes, y: p.liquid_rate_m3_day, xUnit: "min", yUnit: "m³/d" })} cx="${x(p.shut_in_minutes)}" cy="${y(p.liquid_rate_m3_day)}" r="6"/>`).join('')}</g>`;
  const ticks = Array.from({length: 5}, (_, i) => `<line x1="58" y1="${y(ymax * i / 4)}" x2="688" y2="${y(ymax * i / 4)}"/><text x="50" y="${y(ymax * i / 4) + 4}" text-anchor="end">${(ymax * i / 4).toFixed(1)}</text><text x="${x(xmax * i / 4)}" y="240" text-anchor="middle">${Math.round(xmax * i / 4)}</text>`).join('');
  return `<figure class="chart"><figcaption>恢复观测点与模型拟合 · <span style="color:#b66a1f">橙色虚线：观测</span> · <span style="color:#1e6ee8">蓝色实线：拟合</span></figcaption><svg viewBox="0 0 720 270" data-plot-left="58" data-plot-right="688" data-plot-top="28" data-plot-bottom="218" role="img" aria-label="关井时间与恢复产液能力"><g class="chart-grid">${ticks}</g><text x="58" y="17">恢复液量 m³/d</text><text x="365" y="264" text-anchor="middle">关井时间 min</text>${series(model,'recovery-fitted','模型拟合','#1e6ee8',false)}${series(observed,'recovery-observed','恢复观测','#b66a1f',true)}</svg></figure>`;
}

function reasonText(candidate) {
  const reasons = candidate.violations || [];
  return reasons.length ? reasons.join("；") : candidate.feasible ? "符合全部安全约束" : "服务端未返回淘汰原因";
}

export async function renderRecoveryAnalysis({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  let strategyId = params.id || params.strategy_id;
  if (!strategyId) {
    const query = new URLSearchParams({ page_size: "100" });
    if (params.well_id) query.set("well_id", params.well_id);
    const list = await api.get(`/api/strategies?${query}`);
    strategyId = list.items.find((item) => item.configuration?.recovery_model)?.id;
  }
  if (!strategyId) {
    workspace.innerHTML = `${pageHeader(page)}${components.empty("暂无已完成恢复试验及策略计算记录")}`;
    return true;
  }
  const strategy = await api.get(`/api/strategies/${encodeURIComponent(strategyId)}`);
  if (!isCurrent()) return false;
  const config = strategy.configuration || {};
  const model = config.recovery_model || {};
  const observations = config.recovery_observations || [];
  const fitted = config.recovery_fitted_curve || [];
  const observationRows = observations.map((point) => ({...point, fitted_rate: Number.isFinite(model.tau_minutes) ? model.baseline + model.capacity * (1 - Math.exp(-point.shut_in_minutes / model.tau_minutes)) : null}));
  const candidates = config.ranked_candidates || [];
  const rejected = candidates.filter((item) => !item.feasible);
  const observationColumns = [
    { key: "shut_in_minutes", label: "关井时间", render: (value) => `${format.number(value)} min` },
    { key: "liquid_rate_m3_day", label: "恢复液量", render: (value) => `${format.number(value, 3)} m³/d` },
    { key: "fitted_rate", label: "拟合液量", render: (value) => `${format.number(value, 3)} m³/d` },
  ];
  const candidateColumns = [
    { key: "on_minutes", label: "开井" }, { key: "off_minutes", label: "关井" },
    { key: "starts_per_day", label: "日启停" },
    { key: "predicted_liquid_m3", label: "预计日产液", render: (value) => `${format.number(value, 3)} m³` },
    { key: "predicted_energy_kwh", label: "预计耗电", render: (value) => format.energy(value) },
    { key: "saving_rate", label: "预计节电率", render: (value) => format.percent(value, 1) },
    { key: "feasible", label: "约束判定", render: (value, row) => components.trusted(`<span class="status-tag ${value ? "success" : "failed"}" title="${escapeHtml(reasonText(row))}">${value ? "可执行" : "已淘汰"}</span>`) },
  ];
  const editButton = permissions.has("strategy.create") && strategy.status === "draft"
    ? `<button type="button" id="editRecoveryStrategy" class="button primary">调整候选制度</button>` : "";
  workspace.innerHTML = `${pageHeader(page, `<button type="button" id="backStrategy" class="button secondary">返回策略中心</button>`)}
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(config.recovery_test_id || "—")}</span><h2>${escapeHtml(strategy.well_name)} 渗流恢复分析</h2><p>试验时间 ${escapeHtml(format.dateTime(config.recovery_tested_at))} · 模型 ${escapeHtml(model.model_version || "—")}</p></div><dl><div><dt>时间常数 τ</dt><dd>${escapeHtml(format.number(model.tau_minutes, 1))} min</dd></div><div><dt>拟合优度 R²</dt><dd>${escapeHtml(format.number(model.r_squared, 6))}</dd></div><div><dt>观测点</dt><dd>${escapeHtml(format.number(model.observation_count))}</dd></div><div><dt>候选制度</dt><dd>${escapeHtml(format.number(candidates.length))}</dd></div></dl></section>
    <section class="detail-grid"><article class="panel wide"><header><div><p class="eyebrow">RECOVERY CURVE</p><h2>恢复观测点与拟合曲线</h2></div><span>${escapeHtml(model.formula || "q(t) 恢复模型")}</span></header><div class="panel-body">${recoveryCurve(observations, fitted)}${components.table(observationColumns, observationRows)}</div></article>
      <article class="panel info-panel"><header><div><p class="eyebrow">MODEL EVIDENCE</p><h2>拟合参数</h2></div></header><dl><div><dt>基础产液能力</dt><dd>${escapeHtml(format.number(model.baseline, 3))} m³/d</dd></div><div><dt>恢复容量</dt><dd>${escapeHtml(format.number(model.capacity, 3))} m³/d</dd></div><div><dt>试验单位</dt><dd>时间 min / 液量 m³/d</dd></div><div><dt>策略版本</dt><dd>${escapeHtml(strategy.current_version_id)}</dd></div></dl></article></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">RANKED CANDIDATES</p><h2>候选制度排序</h2></div>${editButton}</header><div class="panel-body">${components.table(candidateColumns, candidates, { emptyText: "尚未生成候选制度" })}<aside class="inline-note"><strong>淘汰原因：</strong>${escapeHtml(rejected.length ? rejected.map(reasonText).join("；") : "当前保存的候选均通过服务端安全约束；未通过项不进入可执行版本。")}</aside></div></section>`;
  workspace.querySelector("#backStrategy").addEventListener("click", () => navigate("strategy-center", { id: strategy.id }));
  workspace.querySelector("#editRecoveryStrategy")?.addEventListener("click", () => navigate("strategy-editor", { id: strategy.id }));
  return true;
}
