import { escapeHtml } from "../components.js";

const REPORTS = [
  ["production", "生产运行"], ["diagnosis", "功图诊断"],
  ["data-quality", "数据质量"], ["strategy", "策略执行"],
  ["energy", "节能效果"], ["work-orders", "工单闭环"],
];
const LABELS = {
  record_count: "记录数", well_count: "油井数", daily_oil_m3: "累计产油量 (m³)",
  daily_fluid_m3: "累计产液量 (m³)", energy_kwh: "累计电量 (kWh)",
  diagnosis_count: "诊断数", reviewed_count: "已复核", event_count: "质量事件",
  open_count: "待处理", closed_count: "已闭环", strategy_count: "策略数",
  effective_count: "已生效", energy_saved_kwh: "节电量 (kWh)", saving_rate: "节电率",
  work_order_count: "工单数", overdue_count: "逾期数", timely_close_rate: "按时闭环率",
};

function display(value, format) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return format.number(value, Math.abs(value) < 1 ? 3 : 2);
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export async function renderReports({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  const type = REPORTS.some(([value]) => value === params.type) ? params.type : "production";
  const query = new URLSearchParams();
  if (params.from) query.set("from", params.from);
  if (params.to) query.set("to", params.to);
  const report = await api.get(`/api/reports/${type}${query.size ? `?${query}` : ""}`);
  if (!isCurrent()) return false;
  const summary = Object.entries(report.summary || {}).filter(([, value]) => typeof value !== "object").slice(0, 8);
  const rows = report.items || [];
  const keys = rows.length ? Object.keys(rows[0]).filter((key) => !["source_tables"].includes(key)).slice(0, 9) : [];
  const columns = keys.map((key) => ({ key, label: LABELS[key] || key.replaceAll("_", " "), render: (value) => display(value, format) }));
  workspace.innerHTML = `${pageHeader(page, '<button id="exportReport" class="button primary" type="button">导出当前报表</button>')}
    <section class="panel filter-panel"><form id="reportFilters" class="filter-grid">
      <div class="form-field"><label for="reportType">报表类型</label><select id="reportType" name="type">${REPORTS.map(([value, label]) => `<option value="${value}" ${value === type ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="form-field"><label for="reportFrom">开始时间</label><input id="reportFrom" name="from" type="date" value="${escapeHtml(params.from || "")}" /></div>
      <div class="form-field"><label for="reportTo">结束时间</label><input id="reportTo" name="to" type="date" value="${escapeHtml(params.to || "")}" /></div>
      <div class="filter-actions"><button class="button secondary" type="submit">生成报表</button></div>
    </form></section>
    <section class="metric-grid">${summary.map(([key, value]) => `<article class="metric-card primary"><p>${escapeHtml(LABELS[key] || key.replaceAll("_", " "))}</p><strong class="text-value">${escapeHtml(display(value, format))}</strong><span>按当前筛选实时计算</span></article>`).join("")}</section>
    <section class="panel list-panel"><header><div><p class="eyebrow">COMPUTED REPORT</p><h2>${escapeHtml(REPORTS.find(([value]) => value === type)?.[1])}明细</h2></div><span>生成于 ${escapeHtml(format.dateTime(report.generated_at))}</span></header><div class="panel-body">${components.table(columns, rows, { emptyText: "当前范围没有可汇总记录" })}</div></section>`;
  workspace.querySelector("#reportFilters").addEventListener("submit", (event) => {
    event.preventDefault(); navigate("reports", Object.fromEntries(new FormData(event.currentTarget)));
  });
  workspace.querySelector("#exportReport").addEventListener("click", async () => {
    await api.download(`/api/reports/${type}.csv${query.size ? `?${query}` : ""}`, `${type}-report.csv`);
    components.toast("报表已导出");
  });
  return true;
}
