import { charts } from "../charts.js";
import { escapeHtml } from "../components.js";

const METRIC_LABELS = { power_kw: "有功功率", active_power: "有功功率", load: "悬点载荷", displacement: "位移", pressure: "油压", temperature: "温度", current: "电流", voltage: "电压", fillage: "充满度", frequency: "电源频率", power_factor: "功率因数", stroke: "冲程", strokes_per_minute: "冲次" };

function latestByMetric(items) {
  const result = new Map();
  for (const item of items || []) result.set(item.metric, item);
  return [...result.values()];
}

export async function renderWellDetail({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  let wellId = params.id;
  if (!wellId) {
    const list = await api.get("/api/wells?page_size=1");
    wellId = list.items[0]?.id;
    if (!wellId) { workspace.innerHTML = `${pageHeader(page)}${components.empty("油井台账暂无记录")}`; return true; }
  }
  const [detail, telemetry] = await Promise.all([
    api.get(`/api/wells/${encodeURIComponent(wellId)}`),
    api.get(`/api/wells/${encodeURIComponent(wellId)}/telemetry?interval=1h`),
  ]);
  if (!isCurrent()) return false;
  const well = detail.well;
  const production = [...(detail.production_history || [])].reverse();
  const power = (telemetry.items || []).filter((item) => ["power_kw", "active_power"].includes(item.metric));
  const metrics = latestByMetric(telemetry.items);
  const alarmColumns = [{ key: "alarm_type", label: "报警类型" }, { key: "severity", label: "级别" }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }, { key: "created_at", label: "发生时间", render: (value) => format.dateTime(value) }];
  const orderColumns = [{ key: "id", label: "工单号" }, { key: "title", label: "工单标题" }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }, { key: "due_at", label: "要求完成", render: (value) => format.dateTime(value) }];
  let returnState = {};
  try { returnState = JSON.parse(params.return_state || "{}"); } catch (_error) { returnState = {}; }
  workspace.innerHTML = `${pageHeader(page, `<div class="header-actions"><button id="backToWells" class="button secondary" type="button">返回台账</button>${detail.device ? `<button class="button primary" type="button" data-page="devices" data-id="${escapeHtml(detail.device.id)}">关联终端</button>` : ""}</div>`)}
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(well.code)}</span><h2>${escapeHtml(well.name)}</h2><p>${escapeHtml(detail.unit.name)} · ${escapeHtml(detail.group.name)}</p></div><dl><div><dt>运行状态</dt><dd>${components.statusTag(well.status).html}</dd></div><div><dt>低产井</dt><dd>${well.is_low_output ? "是" : "否"}</dd></div><div><dt>数据版本</dt><dd>V${escapeHtml(well.version)}</dd></div><div><dt>最新采集</dt><dd>${escapeHtml(format.dateTime(detail.latest_telemetry?.measured_at))}</dd></div></dl></section>
    <section class="metric-grid detail-metrics">
      <article class="metric-card primary"><p>日产液</p><strong>${format.number(detail.latest_production?.daily_fluid, 1)}<small>m³</small></strong><span>${escapeHtml(detail.latest_production?.day || "—")}</span></article>
      <article class="metric-card success"><p>日产油</p><strong>${format.number(detail.latest_production?.daily_oil, 1)}<small>t</small></strong><span>生产日报</span></article>
      <article class="metric-card cyan"><p>泵效</p><strong>${format.percent(detail.latest_production?.pump_efficiency, 1)}</strong><span>生产计算</span></article>
      <article class="metric-card purple"><p>日耗电</p><strong>${format.number(detail.latest_production?.power_kwh, 1)}<small>kWh</small></strong><span>生产日报</span></article>
      <article class="metric-card warning"><p>诊断结论</p><strong class="text-value">${escapeHtml(detail.latest_diagnosis?.diagnosis_type || "待分析")}</strong><span>可信度 ${format.percent(detail.latest_diagnosis?.confidence, 1)}</span></article>
      <article class="metric-card ${detail.active_alarms.length ? "danger" : "success"}"><p>活动报警</p><strong>${detail.active_alarms.length}<small>条</small></strong><span>${detail.active_work_orders.length} 张处理中工单</span></article>
    </section>
    <section class="detail-grid">
      <article class="panel chart-panel wide"><header><div><p class="eyebrow">30-DAY PRODUCTION</p><h2>生产趋势</h2></div><span>最近 ${production.length} 个生产日</span></header>${charts.line(production, { xKey: "day", yKey: "daily_fluid", title: `${well.code}日产液趋势`, unit: "m³" })}</article>
      <article class="panel info-panel"><header><div><p class="eyebrow">TERMINAL LINK</p><h2>关联终端</h2></div></header>${detail.device ? `<dl><div><dt>终端编号</dt><dd>${escapeHtml(detail.device.id)}</dd></div><div><dt>型号</dt><dd>${escapeHtml(detail.device.device_type)}</dd></div><div><dt>状态</dt><dd>${components.statusTag(detail.device.status).html}</dd></div><div><dt>最新心跳</dt><dd>${escapeHtml(format.dateTime(detail.device.latest_heartbeat?.reported_at))}</dd></div></dl><button class="button secondary full" type="button" data-page="devices" data-id="${escapeHtml(detail.device.id)}">查看终端运行详情</button>` : components.empty("尚未关联测控终端")}</article>
      <article class="panel chart-panel wide"><header><div><p class="eyebrow">TELEMETRY</p><h2>采集指标与有功功率</h2></div><span>${escapeHtml(telemetry.timezone)}</span></header>${charts.line(power, { xKey: "bucket", yKey: "value", title: `${well.code}有功功率趋势`, unit: "kW", color: "#7257d7" })}<div class="reading-strip">${metrics.slice(0, 8).map((item) => `<div><span>${escapeHtml(METRIC_LABELS[item.metric] || item.metric)}</span><strong>${format.number(item.value, 2)}</strong><small>${escapeHtml(item.unit || "")}</small></div>`).join("")}</div></article>
      <article class="panel info-panel"><header><div><p class="eyebrow">INTELLIGENCE</p><h2>诊断与策略</h2></div></header><dl><div><dt>最新功图</dt><dd>${escapeHtml(detail.latest_dynagraph?.id || "待采集")}</dd></div><div><dt>功图状态</dt><dd>${escapeHtml(detail.latest_dynagraph?.status || "—")}</dd></div><div><dt>诊断建议</dt><dd>${escapeHtml(detail.latest_diagnosis?.recommendation || "暂无")}</dd></div><div><dt>当前策略</dt><dd>${escapeHtml(detail.active_strategy?.id || "未启用")}</dd></div><div><dt>策略状态</dt><dd>${detail.active_strategy ? components.statusTag(detail.active_strategy.status).html : "—"}</dd></div></dl><button class="button secondary full" type="button" data-page="dynagraph-analysis" data-param-well-id="${escapeHtml(well.id)}">进入功图分析</button></article>
    </section>
    <section class="panel tabbed-records"><header><div><p class="eyebrow">TRACEABLE ACTIONS</p><h2>报警与处置记录</h2></div><span>所有状态来自业务库</span></header><div class="record-columns"><div><h3>活动报警</h3>${components.table(alarmColumns, detail.active_alarms)}</div><div><h3>未闭环工单</h3>${components.table(orderColumns, detail.active_work_orders)}</div></div></section>`;
  workspace.querySelector("#backToWells").addEventListener("click", () => navigate("wells", returnState));
  workspace.querySelectorAll("[data-page=devices][data-id]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); navigate("devices", { id: button.dataset.id }); }));
  return true;
}
