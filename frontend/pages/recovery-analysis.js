import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

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
  const candidates = config.ranked_candidates || [];
  const rejected = candidates.filter((item) => !item.feasible);
  const observationColumns = [
    { key: "shut_in_minutes", label: "关井时间", render: (value) => `${format.number(value)} min` },
    { key: "liquid_rate_m3_day", label: "恢复液量", render: (value) => `${format.number(value, 3)} m³/d` },
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
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(config.recovery_test_id || "—")}</span><h2>${escapeHtml(strategy.well_name)} 渗流恢复分析</h2><p>试验时间 ${escapeHtml(format.dateTime(config.recovery_tested_at))} · 模型 ${escapeHtml(model.model_version || "—")}</p></div><dl><div><dt>时间常数 τ</dt><dd>${escapeHtml(format.number(model.tau_minutes, 1))} min</dd></div><div><dt>拟合优度 R²</dt><dd>${escapeHtml(format.number(model.r_squared, 4))}</dd></div><div><dt>观测点</dt><dd>${escapeHtml(format.number(model.observation_count))}</dd></div><div><dt>候选制度</dt><dd>${escapeHtml(format.number(candidates.length))}</dd></div></dl></section>
    <section class="detail-grid"><article class="panel wide"><header><div><p class="eyebrow">RECOVERY CURVE</p><h2>恢复观测点与拟合曲线</h2></div><span>${escapeHtml(model.formula || "q(t) 恢复模型")}</span></header><div class="panel-body">${charts.line(observations, { xKey: "shut_in_minutes", yKey: "liquid_rate_m3_day", title: "关井恢复曲线", unit: " m³/d" })}${components.table(observationColumns, observations)}</div></article>
      <article class="panel info-panel"><header><div><p class="eyebrow">MODEL EVIDENCE</p><h2>拟合参数</h2></div></header><dl><div><dt>基础产液能力</dt><dd>${escapeHtml(format.number(model.baseline, 3))} m³/d</dd></div><div><dt>恢复容量</dt><dd>${escapeHtml(format.number(model.capacity, 3))} m³/d</dd></div><div><dt>试验单位</dt><dd>时间 min / 液量 m³/d</dd></div><div><dt>策略版本</dt><dd>${escapeHtml(strategy.current_version_id)}</dd></div></dl></article></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">RANKED CANDIDATES</p><h2>候选制度排序</h2></div>${editButton}</header><div class="panel-body">${components.table(candidateColumns, candidates, { emptyText: "尚未生成候选制度" })}<aside class="inline-note"><strong>淘汰原因：</strong>${escapeHtml(rejected.length ? rejected.map(reasonText).join("；") : "当前保存的候选均通过服务端安全约束；未通过项不进入可执行版本。")}</aside></div></section>`;
  workspace.querySelector("#backStrategy").addEventListener("click", () => navigate("strategy-center", { id: strategy.id }));
  workspace.querySelector("#editRecoveryStrategy")?.addEventListener("click", () => navigate("strategy-editor", { id: strategy.id }));
  return true;
}
