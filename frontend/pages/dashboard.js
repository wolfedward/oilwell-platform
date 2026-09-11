import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

export async function renderDashboard({ api, format, workspace, page, pageHeader, isCurrent }) {
  const data = await api.get("/api/dashboard");
  if (!isCurrent()) return false;
  const cards = [
    ["油井总数", format.number(data.total_wells), "口", "primary", "wells", {}],
    ["当前开井", format.number(data.active_wells), "口", "success", "wells", { status: "operating" }],
    ["低产井", format.number(data.low_output_wells), "口", "warning", "wells", { low_output: "1" }],
    ["在线终端", format.number(data.devices.online), "台", "cyan", "devices", { status: "online" }],
    ["活动报警", format.number(data.alarms.active), "条", data.alarms.active ? "danger" : "success", "alarms", {}],
    ["累计节电率", format.percent(data.energy.saving_rate), "", "purple", "energy-overview", {}],
  ];
  workspace.innerHTML = `${pageHeader(page, `<span class="data-time">数据时点 ${escapeHtml(format.dateTime(data.data_timestamp))}</span>`)}
    <section class="metric-grid" aria-label="生产运行指标">${cards.map(([label, value, unit, tone, target, params]) => `
      <button type="button" class="metric-card ${tone}" data-page="${target}" ${Object.entries(params).map(([key, item]) => `data-param-${key.replaceAll("_", "-")}="${escapeHtml(item)}"`).join(" ")}>
        <p>${label}</p><strong>${value}<small>${unit}</small></strong><span>点击查看业务明细 →</span></button>`).join("")}</section>
    <section class="dashboard-layout">
      <article class="panel trend-panel"><header><div><p class="eyebrow">14-DAY OPERATING TREND</p><h2>产液与耗电趋势</h2></div><span>${escapeHtml(data.production.day || "—")}</span></header>
        <div class="trend-legend"><span><i class="blue"></i>全场日产液 m³</span><span>数据来源：生产日报</span></div>
        ${charts.line(data.trends, { xKey: "day", yKey: "daily_fluid", title: "近14日全场日产液趋势", unit: "m³" })}
      </article>
      <article class="panel state-breakdown"><header><div><p class="eyebrow">OPERATING STATE</p><h2>运行与接入状态</h2></div></header>
        <dl><div><dt>运行 / 间抽</dt><dd>${format.number(data.active_wells)}</dd></div><div><dt>停机</dt><dd>${format.number(data.stopped_wells)}</dd></div><div><dt>离线 / 维护</dt><dd>${format.number(data.offline_wells)}</dd></div><div><dt>数据质量待处理</dt><dd>${format.number(data.quality.open_events)}</dd></div><div><dt>待审批策略</dt><dd>${format.number(data.strategies.pending_strategies)}</dd></div></dl>
        <button class="button secondary" type="button" data-page="wells">进入油井台账</button>
      </article>
      <article class="panel production-panel"><header><div><p class="eyebrow">LATEST PRODUCTION DAY</p><h2>最新生产日汇总</h2></div><span>${escapeHtml(data.production.day || "—")}</span></header>
        <div class="production-values"><div><span>日产液</span><strong>${format.number(data.production.daily_fluid, 1)}</strong><small>m³</small></div><div><span>日产油</span><strong>${format.number(data.production.daily_oil, 1)}</strong><small>t</small></div><div><span>平均泵效</span><strong>${format.percent(data.production.pump_efficiency, 1)}</strong><small>全井算术平均</small></div></div>
        <p class="source-note">全部指标由服务端按最新生产日实时汇总，可逐项下钻到井、终端、报警和策略记录。</p>
      </article>
      <article class="panel quick-entry"><header><div><p class="eyebrow">QUICK ACCESS</p><h2>重点业务入口</h2></div></header><div>${[
        ["dynagraph-analysis", "功图智能分析", "追溯电功图到诊断结论"], ["strategy-center", "节能策略中心", "查看审批、执行与效果"], ["work-orders", "工单闭环", "跟进处置和独立验证"], ["reports", "服务端报表", "按条件导出可对账明细"],
      ].map(([id, label, description]) => `<button type="button" data-page="${id}"><strong>${label}</strong><span>${description}</span><b>→</b></button>`).join("")}</div></article>
    </section>`;
  return true;
}
