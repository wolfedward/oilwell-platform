import { escapeHtml } from "../components.js";

const EMPTY = () => ({ items: [], total: 0, page: 1, page_size: 100 });
const STATUS = {
  available: ["中心可查询", "success", "统计截至本次刷新；设备状态按接收与测量时刻共同判断"],
  unavailable: ["中心暂不可用", "failed", "请检查中心连接后刷新，统计暂不提供"],
  not_configured: ["中心未配置", "warning", "当前尚未启用中心采集连接"],
};

export async function loadCentralHealth(api, path = "/api/central/health") {
  try {
    return await api.get(path);
  } catch (error) {
    if (error.status === 401 || error.status === 403) throw error;
    return { status: "unavailable", summary: {}, links: [] };
  }
}

export async function loadCentralSnapshot(api, filters = {}, canReadTelemetry = true) {
  const health = await loadCentralHealth(api);
  if (health.status !== "available" || !canReadTelemetry) return { health, telemetry: EMPTY() };
  const query = new URLSearchParams({ page: String(filters.page || 1), page_size: "100" });
  for (const key of ["gateway_id", "device_id", "point_code", "quality"]) {
    if (filters[key]) query.set(key, filters[key]);
  }
  for (const key of ["from", "to", "received_before"]) {
    if (filters[key]) {
      const date = new Date(filters[key]);
      if (Number.isNaN(date.getTime())) return { health, telemetry: EMPTY(), queryError: "请输入有效的起止时间" };
      query.set(key, date.toISOString());
    }
  }
  try {
    return { health, telemetry: await api.get(`/api/central/telemetry?${query}`) };
  } catch (error) {
    if (error.status === 401 || error.status === 403) throw error;
    if (error.status === 422) return { health, telemetry: EMPTY(), queryError: "查询条件无效，请检查设备/测点编码和时间范围（最多 31 天）" };
    return { health: { status: "unavailable", summary: {}, links: [] }, telemetry: EMPTY() };
  }
}

function metric(label, value, unit, note, kind = "cyan") {
  return `<article class="metric-card ${kind}"><p>${escapeHtml(label)}</p><strong>${escapeHtml(value)}<small>${escapeHtml(unit)}</small></strong><span>${escapeHtml(note)}</span></article>`;
}

export function centralQualityMarkup(health, components, format) {
  const [label, kind, help] = STATUS[health?.status] || STATUS.unavailable;
  const available = health?.status === "available";
  const summary = health?.summary || {};
  const number = (key, digits = 0) => {
    if (!available || summary[key] == null) return "—";
    const value = summary[key];
    return typeof value === "string" && /^\d+$/.test(value)
      ? new Intl.NumberFormat("zh-CN").format(BigInt(value)) : format.number(value, digits);
  };
  const links = health?.links || [];
  return `<section aria-label="中心数据质量">
    <div class="layer-heading"><span>中</span><div><p class="eyebrow">CENTRAL ACQUISITION</p><h2>中心采集链路</h2></div><div class="central-state">${components.statusTag(kind, label).html}</div></div>
    <p class="central-note">${escapeHtml(help)}${health?.generated_at ? ` · ${escapeHtml(format.dateTime(health.generated_at))}` : ""}</p>
    <div class="metric-grid central-metrics">
      ${metric("已接收网关 / 终端", `${number("gateway_count")} / ${number("device_count")}`, "", `在线 ${number("online_devices")} · 延迟 ${number("delayed_devices")} · 离线 ${number("offline_devices")}`, available ? "cyan" : "warning")}
      ${metric("24 小时接收样本", number("samples_24h"), "点", "以中心接收时间统计")}
      ${metric("坏质量 / 可疑", `${number("bad_samples_24h")} / ${number("uncertain_samples_24h")}`, "点", "按上行质量码统计", available && !(summary.bad_samples_24h || summary.uncertain_samples_24h) ? "success" : "warning")}
      ${metric("P95 传输时差", number("p95_delivery_ms", 1), "ms", "接收时间减测量时间，含缓存等待")}
      ${metric("观测序列缺口", number("sequence_gap_count_24h"), "处", `窗口内缺失 ${number("missing_sequences_24h")} 个周期`, summary.sequence_gap_count_24h ? "danger" : "cyan")}
      ${metric("设备时钟超前", number("clock_ahead_samples_24h"), "点", "测量比接收超前超过 120 秒", summary.clock_ahead_samples_24h ? "warning" : "cyan")}
    </div>
    <section class="panel list-panel"><header><div><p class="eyebrow">LINK FRESHNESS</p><h2>网关与终端状态</h2></div><span>${available ? `${links.length} 条已接收链路` : "状态暂不可用"}</span></header><div class="panel-body">${components.table([
      { key: "gateway_id", label: "网关" }, { key: "device_id", label: "终端" },
      { key: "status", label: "状态", render: (value) => components.statusTag(value === "delayed" ? "warning" : value, { online: "在线", delayed: "数据延迟", offline: "离线" }[value]) },
      { key: "last_received_at", label: "最后接收", render: (value) => format.dateTime(value) },
      { key: "last_measured_at", label: "最新测量", render: (value) => format.dateTime(value) },
      { key: "age_seconds", label: "未接收时长", render: (value) => `${format.number(value)} s` },
      { key: "sample_count_24h", label: "24h 样本" }, { key: "bad_sample_count_24h", label: "坏质量" },
    ], links, { emptyText: available ? "中心尚未接收到终端数据" : help, rowKey: "device_id" })}</div></section>
    <p class="central-note">序列缺口按近 24 小时收到的样本，在各网关、终端和启动会话内去重统计；不推断窗口之外的丢失。未接收不超过 120 秒且测量新鲜时为在线，超过 360 秒未接收时为离线。</p>
  </section>`;
}

export function centralOperationsMarkup(snapshot, components, format) {
  const telemetry = snapshot?.telemetry || EMPTY();
  const pages = Math.max(1, Math.ceil(telemetry.total / telemetry.page_size));
  const available = snapshot?.health?.status === "available";
  return `${centralQualityMarkup(snapshot?.health, components, format)}
    <section class="panel list-panel central-samples"><header><div><p class="eyebrow">MEASUREMENT HISTORY</p><h2>中心测点记录</h2></div><span>${available && !snapshot.queryError ? `共 ${telemetry.total} 条` : "查询暂不可用"}</span></header><div class="panel-body">
      ${snapshot.queryError ? `<p class="inline-error" role="alert">${escapeHtml(snapshot.queryError)}</p>` : components.table([
        { key: "measured_at", label: "测量时间", render: (value) => format.dateTime(value) },
        { key: "gateway_id", label: "网关" }, { key: "device_id", label: "终端" }, { key: "point_code", label: "测点编码" },
        { key: "value", label: "工程值", render: (value, row) => `${Number(value).toFixed(2)} ${row.unit || ""}`.trim() },
        { key: "quality", label: "质量", render: (value) => components.statusTag({ good: "success", uncertain: "warning", bad: "failed" }[value], { good: "良好", uncertain: "可疑", bad: "无效" }[value]) },
        { key: "sequence", label: "序号" }, { key: "received_at", label: "中心接收", render: (value) => format.dateTime(value) },
        { key: "message_id", label: "报文标识" }, { key: "source", label: "采集来源" },
      ], telemetry.items, { emptyText: available ? "当前时间和筛选条件下暂无测点记录" : "中心不可查询，未提供测点数据", rowKey: "message_id" })}
      <nav class="pagination" aria-label="中心测点分页"><span>第 ${telemetry.page || 1} / ${pages} 页</span><button type="button" data-central-page="${(telemetry.page || 1) - 1}" ${!available || telemetry.page <= 1 ? "disabled" : ""}>上一页</button><button type="button" data-central-page="${(telemetry.page || 1) + 1}" ${!available || telemetry.page >= pages ? "disabled" : ""}>下一页</button></nav>
    </div></section>`;
}

export async function mountCentralOperations({ api, components, format, root, isCurrent, permissions }) {
  const canReadTelemetry = permissions?.has("telemetry.read") ?? true;
  const state = { filters: {}, paused: false, generation: 0, lastWindow: null, frozenWindow: null };
  root.innerHTML = `<section class="panel filter-panel"><form id="centralFilters" class="central-filters">
    ${[["gateway_id", "网关编号"], ["device_id", "终端编号"], ["point_code", "测点编码"]].map(([name, label]) => `<div class="form-field"><label for="central-${name}">${label}</label><input id="central-${name}" name="${name}" maxlength="128" placeholder="全部" /></div>`).join("")}
    <div class="form-field"><label for="central-quality">质量</label><select id="central-quality" name="quality"><option value="">全部</option><option value="good">良好</option><option value="uncertain">可疑</option><option value="bad">无效</option></select></div>
    <div class="form-field"><label for="central-from">开始时间</label><input id="central-from" type="datetime-local" name="from" /></div>
    <div class="form-field"><label for="central-to">结束时间</label><input id="central-to" type="datetime-local" name="to" /></div>
    <div class="filter-actions"><button class="button primary" type="submit">查询</button><button class="button secondary" type="reset">重置</button><button id="refreshCentral" class="button secondary" type="button">刷新</button><button id="pauseCentral" class="button secondary" type="button">暂停自动刷新</button></div>
    <small class="central-filter-help">默认近 24 小时；可查历史，最长 31 天。每 15 秒刷新状态，翻页时暂停自动刷新。</small>
  </form></section><div id="centralResults">${components.skeleton(3)}</div>`;
  const results = root.querySelector("#centralResults");
  const form = root.querySelector("#centralFilters");
  const pause = root.querySelector("#pauseCentral");
  const refreshButton = root.querySelector("#refreshCentral");
  const refresh = async () => {
    const generation = ++state.generation;
    refreshButton.disabled = true;
    try {
      const snapshot = await loadCentralSnapshot(api, { ...state.filters, ...state.frozenWindow }, canReadTelemetry);
      if (!isCurrent() || generation !== state.generation) return;
      state.lastWindow = snapshot.telemetry?.window || null;
      results.innerHTML = canReadTelemetry ? centralOperationsMarkup(snapshot, components, format) : centralQualityMarkup(snapshot.health, components, format);
    } catch (error) {
      if (isCurrent() && generation === state.generation) results.innerHTML = components.errorPanel(error);
    } finally {
      if (isCurrent() && generation === state.generation) refreshButton.disabled = false;
    }
  };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    state.filters = { ...Object.fromEntries(new FormData(form)), page: 1 };
    state.frozenWindow = null;
    for (const key of ["gateway_id", "device_id", "point_code"]) state.filters[key] = state.filters[key].trim();
    refresh();
  });
  form.addEventListener("reset", () => { state.filters = {}; state.frozenWindow = null; refresh(); });
  refreshButton.addEventListener("click", () => {
    state.filters.page = 1;
    state.frozenWindow = null;
    refresh();
  });
  pause.addEventListener("click", () => {
    state.paused = !state.paused;
    pause.textContent = state.paused ? "继续自动刷新" : "暂停自动刷新";
    if (!state.paused) { state.filters.page = 1; state.frozenWindow = null; refresh(); }
  });
  results.addEventListener("click", (event) => {
    const button = event.target.closest("[data-central-page]");
    if (!button || button.disabled) return;
    state.filters = { ...state.filters, page: Number(button.dataset.centralPage) };
    state.frozenWindow = state.lastWindow;
    state.paused = true;
    pause.textContent = "继续自动刷新";
    refresh();
  });
  await refresh();
  const poll = async () => {
    if (!isCurrent()) return;
    if (!state.paused && !refreshButton.disabled) await refresh();
    if (isCurrent()) window.setTimeout(poll, 15_000);
  };
  if (isCurrent()) window.setTimeout(poll, 15_000);
}
