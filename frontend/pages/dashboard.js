import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

function paramsMarkup(params) {
  return Object.entries(params).map(([key, value]) =>
    `data-param-${key.replaceAll("_", "-")}="${escapeHtml(value)}"`
  ).join(" ");
}

export async function renderDashboard({ api, format, workspace, page, pageHeader, isCurrent }) {
  const data = await api.get("/api/dashboard");
  if (!isCurrent()) return false;

  const number = (value, digits = 0) => value == null ? "—" : format.number(value, digits);
  const trendSeries = Array.isArray(data.trends) ? data.trends : [];
  const latestTrend = trendSeries[trendSeries.length - 1] || {};
  const cards = [
    ["油井总数", number(data.total_wells), "口", "primary", "wells", {}, "生产对象台账"],
    ["当前开井", number(data.active_wells), "口", "success", "wells", { status: "operating" }, "运行 / 间抽状态"],
    ["低产井", number(data.low_output_wells), "口", "warning", "wells", { low_output: "1" }, "进入低产井清单"],
    ["在线终端", number(data.devices.online), "台", "cyan", "devices", { status: "online" }, `终端总数 ${number(data.devices.total)} 台`],
    ["数据质量", number(data.quality.open_events), "条", data.quality.open_events ? "warning" : "success", "data-quality", { status: "open" }, "待处理质量事件"],
    ["活动报警", number(data.alarms.active), "条", data.alarms.active ? "danger" : "success", "alarms", {}, "待确认与处置"],
    ["高危报警", number(data.alarms.high), "条", data.alarms.high ? "danger" : "success", "alarms", { severity: "high" }, "高危 / 严重级别"],
    ["待审批策略", number(data.strategies.pending_strategies), "项", data.strategies.pending_strategies ? "warning" : "success", "approvals", {}, "进入独立审批队列"],
  ];
  const shortcuts = [
    ["acquisition", "采集监控", "查看报文接收、校验与队列状态", "采"],
    ["dynagraph-analysis", "功图智能分析", "从原始电功图追溯诊断结论", "图"],
    ["data-quality", "数据质量", "处理缺失、延迟与交付异常", "质"],
    ["strategy-center", "节能策略", "审批、下发并跟踪策略成效", "策"],
    ["work-orders", "工单闭环", "跟进异常处置与独立验证", "单"],
    ["reports", "运营报表", "导出服务端可对账业务明细", "报"],
  ];

  workspace.innerHTML = `${pageHeader(page, `<span class="data-time"><b>实时数据时点</b> ${escapeHtml(format.dateTime(data.data_timestamp))}</span>`)}
    <section class="dashboard-layer situation-layer" aria-labelledby="situationTitle">
      <div class="layer-heading"><span>01</span><div><p class="eyebrow">RUNNING SITUATION</p><h2 id="situationTitle">生产运行态势</h2></div><small>当前状态按服务端数据时点汇总，点击指标下钻</small></div>
      <div class="metric-grid dashboard-metrics">${cards.map(([label, value, unit, tone, target, params, hint]) => `
        <button type="button" class="metric-card ${tone}" data-page="${target}" ${paramsMarkup(params)}>
          <p>${label}</p><strong>${escapeHtml(value)}<small>${unit}</small></strong><span>${escapeHtml(hint)} →</span>
        </button>`).join("")}</div>
    </section>

    <section class="dashboard-layer analysis-layer" aria-labelledby="analysisTitle">
      <div class="layer-heading"><span>02</span><div><p class="eyebrow">DAILY TREND & RESULTS</p><h2 id="analysisTitle">生产日报与节能结果</h2></div><small>生产日 ${escapeHtml(data.production.day || "—")} · 近 14 个生产日</small></div>
      <div class="dashboard-trends">
        <article class="panel trend-panel"><header><div><p class="eyebrow">DAILY FLUID</p><h2>日产液趋势</h2></div><span>生产日报日级值 · m³</span></header>
          ${charts.line(trendSeries, { xKey: "day", yKey: "daily_fluid", title: "近14个生产日日产液趋势", unit: "m³", color: "#1e6ee8" })}
          <div class="trend-data-strip"><div><span>日产液趋势最新值</span><strong>${escapeHtml(number(latestTrend.daily_fluid, 1))} m³</strong></div><div><span>日产油趋势最新值</span><strong>${escapeHtml(number(latestTrend.daily_oil, 1))} t</strong></div><div><span>平均泵效趋势最新值</span><strong>${escapeHtml(latestTrend.pump_efficiency == null ? "—" : format.percent(latestTrend.pump_efficiency, 1))}</strong></div></div>
        </article>
        <article class="panel trend-panel energy-trend"><header><div><p class="eyebrow">DAILY ENERGY</p><h2>日耗电趋势</h2></div><span>生产日报日级值 · kWh</span></header>
          ${charts.line(trendSeries, { xKey: "day", yKey: "power_kwh", title: "近14个生产日日耗电趋势", unit: "kWh", color: "#13a38c" })}
          <div class="trend-data-strip"><div><span>日耗电趋势最新值</span><strong>${escapeHtml(number(latestTrend.power_kwh, 1))} kWh</strong></div><div><span>序列口径</span><strong>production_daily</strong></div></div>
        </article>
      </div>
      <div class="dashboard-results">
        <article class="panel result-panel production-panel"><header><div><p class="eyebrow">LATEST PRODUCTION</p><h2>最新生产汇总</h2></div><span>${escapeHtml(data.production.day || "—")}</span></header>
          <div class="result-values"><div><span>日产液</span><strong>${escapeHtml(number(data.production.daily_fluid, 1))}</strong><small>m³</small></div><div><span>日产油</span><strong>${escapeHtml(number(data.production.daily_oil, 1))}</strong><small>t</small></div><div><span>日耗电</span><strong>${escapeHtml(number(data.production.power_kwh, 1))}</strong><small>kWh</small></div><div><span>平均泵效</span><strong>${escapeHtml(data.production.pump_efficiency == null ? "—" : format.percent(data.production.pump_efficiency, 1))}</strong><small>全井算术平均</small></div></div>
          <p class="source-note">日级口径：服务端按最新生产日汇总 production_daily，和实时状态快照分开标识。</p>
        </article>
        <article class="panel result-panel energy-result"><header><div><p class="eyebrow">ENERGY EFFECT</p><h2>节能成效</h2></div><button type="button" class="link-button" data-page="energy-overview">查看效果明细 →</button></header>
          <div class="result-values"><div><span>基准耗电</span><strong>${escapeHtml(number(data.energy.baseline_energy_kwh, 1))}</strong><small>kWh</small></div><div><span>实施后耗电</span><strong>${escapeHtml(number(data.energy.after_energy_kwh, 1))}</strong><small>kWh</small></div><div><span>累计节电量</span><strong>${escapeHtml(number(data.energy.energy_saved_kwh, 1))}</strong><small>kWh</small></div><div><span>累计节电率</span><strong>${escapeHtml(data.energy.saving_rate == null ? "—" : format.percent(data.energy.saving_rate, 1))}</strong><small>${escapeHtml(number(data.energy.comparable_strategy_count))} 项可比策略</small></div></div>
          <p class="source-note">效果口径：仅汇总服务端判定覆盖充分的策略；覆盖不足 ${escapeHtml(number(data.energy.insufficient_coverage_count))} 项。</p>
        </article>
      </div>
    </section>

    <section class="dashboard-layer action-layer" aria-labelledby="actionTitle">
      <div class="layer-heading"><span>03</span><div><p class="eyebrow">EXCEPTION & ACTION</p><h2 id="actionTitle">异常待办与业务入口</h2></div><small>从异常发现直达处置与留痕</small></div>
      <div class="dashboard-actions">
        <article class="panel exception-workbench"><header><div><p class="eyebrow">EXCEPTION WORKBENCH</p><h2>异常待办</h2></div><span>3 类队列</span></header><div class="exception-list">
          <button type="button" data-page="alarms"><span class="exception-icon danger">警</span><span><strong>活动报警</strong><small>其中高危报警 ${escapeHtml(number(data.alarms.high))} 条</small></span><b>${escapeHtml(number(data.alarms.active))}</b><i>→</i></button>
          <button type="button" data-page="data-quality" data-param-status="open"><span class="exception-icon warning">质</span><span><strong>数据质量待处理</strong><small>已修复 ${escapeHtml(number(data.quality.resolved_events))} 条</small></span><b>${escapeHtml(number(data.quality.open_events))}</b><i>→</i></button>
          <button type="button" data-page="approvals"><span class="exception-icon purple">审</span><span><strong>待审批策略</strong><small>已生效 ${escapeHtml(number(data.strategies.effective_strategies))} 项</small></span><b>${escapeHtml(number(data.strategies.pending_strategies))}</b><i>→</i></button>
        </div></article>
        <article class="panel quick-entry"><header><div><p class="eyebrow">BUSINESS SHORTCUTS</p><h2>重点业务入口</h2></div><span>6 个真实业务模块</span></header><div>${shortcuts.map(([id, label, description, icon]) => `<button type="button" data-page="${id}"><em>${icon}</em><span><strong>${label}</strong><small>${description}</small></span><b>→</b></button>`).join("")}</div></article>
      </div>
    </section>`;
  return true;
}
