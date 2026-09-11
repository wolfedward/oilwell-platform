import { escapeHtml } from "../components.js";

function reportQuery(params) {
  const query = new URLSearchParams();
  for (const key of ["unit_id", "well_id", "strategy_id", "from", "to"]) {
    if (params[key]) query.set(key, params[key]);
  }
  return query.toString();
}

function coverage(value, format) {
  return value == null ? "—" : format.percent(value, 1);
}

export async function renderEnergyOverview({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const query = reportQuery(params);
  const report = await api.get(`/api/reports/energy${query ? `?${query}` : ""}`);
  if (!isCurrent()) return false;
  const summary = report.summary || {};
  const columns = [
    { key: "well_name", label: "油井" },
    { key: "strategy_id", label: "策略编号" },
    { key: "baseline_energy_kwh", label: "基准耗电", render: (value) => format.energy(value) },
    { key: "after_energy_kwh", label: "实施后耗电", render: (value) => format.energy(value) },
    { key: "energy_saved_kwh", label: "节电量", render: (value) => value == null ? "覆盖不足" : format.energy(value) },
    { key: "saving_rate", label: "节电率", render: (value) => value == null ? "—" : format.percent(value, 1) },
    { key: "liquid_change_rate", label: "产量保持", render: (value) => value == null ? "待补齐数据" : format.percent(1 + Number(value), 1) },
    { key: "after_coverage_rate", label: "数据覆盖率", render: (value, row) => components.trusted(`<span title="基准 ${escapeHtml(coverage(row.baseline_coverage_rate, format))}">${escapeHtml(coverage(value, format))}</span>`) },
    { key: "action", label: "效果评估", render: (_value, row) => components.trusted(`<button type="button" class="button mini secondary" data-effect-id="${escapeHtml(row.strategy_id)}">查看证据</button>`) },
  ];
  const createButton = permissions.has("strategy.create")
    ? '<button type="button" id="newEnergyStrategy" class="button primary">新建节能策略</button>' : "";
  workspace.innerHTML = `${pageHeader(page, `<span class="data-time">计算时间 ${escapeHtml(format.dateTime(report.generated_at))}</span>`)}
    <section class="metric-grid" aria-label="节能效果指标">
      <article class="metric-card primary"><p>已实施策略</p><strong>${escapeHtml(format.number(summary.strategy_count))}<small>项</small></strong><span>来自策略及指令台账</span></article>
      <article class="metric-card success"><p>有效对比策略</p><strong>${escapeHtml(format.number(summary.comparable_strategy_count))}<small>项</small></strong><span>功率覆盖率达到核算条件</span></article>
      <article class="metric-card cyan"><p>累计节电量</p><strong>${escapeHtml(format.number(summary.energy_saved_kwh, 1))}<small>kWh</small></strong><span>同周期基准对比</span></article>
      <article class="metric-card purple"><p>综合节电率</p><strong>${escapeHtml(summary.saving_rate == null ? "—" : format.percent(summary.saving_rate, 1))}</strong><span>服务端实时计算</span></article>
      <article class="metric-card warning"><p>覆盖不足</p><strong>${escapeHtml(format.number(summary.insufficient_coverage_count))}<small>项</small></strong><span>不计入节电汇总</span></article>
      <article class="metric-card success"><p>实施后产液</p><strong>${escapeHtml(format.number(summary.after_liquid_m3, 1))}<small>m³</small></strong><span>产量保持核验</span></article>
    </section>
    <section class="panel filter-panel"><form id="energyFilters" class="filter-grid">
      <div class="form-field"><label for="energyWell">油井编号</label><input id="energyWell" name="well_id" value="${escapeHtml(params.well_id || "")}" placeholder="如 W-001" /></div>
      <div class="form-field"><label for="energyFrom">评估起始</label><input id="energyFrom" name="from" type="date" value="${escapeHtml(params.from || "")}" /></div>
      <div class="form-field"><label for="energyTo">评估截止</label><input id="energyTo" name="to" type="date" value="${escapeHtml(params.to || "")}" /></div>
      <div class="filter-actions"><button class="button secondary" type="submit">重新核算</button>${createButton}</div>
    </form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">MEASURED EFFECT</p><h2>策略效果评估</h2></div><span>数据来源：${escapeHtml((report.source_tables || []).join("、"))}</span></header>
      <div class="panel-body">${components.table(columns, report.items || [], { emptyText: "当前条件下暂无可核算策略" })}</div>
    </section>`;
  workspace.querySelector("#energyFilters").addEventListener("submit", (event) => {
    event.preventDefault();
    navigate("energy-overview", Object.fromEntries(new FormData(event.currentTarget)));
  });
  workspace.querySelector("#newEnergyStrategy")?.addEventListener("click", () => navigate("strategy-editor", {}));
  workspace.querySelectorAll("[data-effect-id]").forEach((button) => button.addEventListener("click", () => navigate("strategy-center", { id: button.dataset.effectId, tab: "effect" })));
  return true;
}
