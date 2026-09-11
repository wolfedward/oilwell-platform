import { escapeHtml } from "../components.js";

const KIND_LABELS = {
  heartbeat: "心跳",
  telemetry: "遥测",
  electric_card: "电功图",
};

function filtered(items, filters) {
  return items.filter((item) => (
    (!filters.device_id || item.device_id?.toLowerCase().includes(filters.device_id.toLowerCase()))
    && (!filters.kind || item.kind === filters.kind)
    && (!filters.status || item.status === filters.status)
  ));
}

function queueMarkup(snapshot, format) {
  const queue = snapshot.queue || {};
  const worker = snapshot.worker || {};
  const depth = worker.mailbox_depth ?? 0;
  const capacity = worker.mailbox_capacity ?? 0;
  return `<section class="metric-grid terminal-metrics" aria-label="采集运行指标">
    <article class="metric-card success"><p>累计接收</p><strong>${escapeHtml(format.number(worker.accepted || 0))}<small>帧</small></strong><span>业务校验通过</span></article>
    <article class="metric-card warning"><p>校验拒绝</p><strong>${escapeHtml(format.number(worker.rejected || 0))}<small>帧</small></strong><span>保留原始报文</span></article>
    <article class="metric-card ${worker.failed ? "danger" : "success"}"><p>处理异常</p><strong>${escapeHtml(format.number(worker.failed || 0))}<small>次</small></strong><span>后台采集隔离</span></article>
    <article class="metric-card cyan"><p>采集批次</p><strong>${escapeHtml(format.number(worker.batches || 0))}<small>批</small></strong><span>受控队列处理</span></article>
    <article class="metric-card"><p>主邮箱深度</p><strong>${escapeHtml(depth)}<small>/ ${escapeHtml(capacity)}</small></strong><span>当前待处理批次</span></article>
    <article class="metric-card ${queue.dropped ? "danger" : "success"}"><p>回退队列</p><strong>${escapeHtml(format.number(queue.queued || 0))}<small>帧</small></strong><span>累计丢弃 ${escapeHtml(format.number(queue.dropped || 0))} 帧</span></article>
  </section>`;
}

function messageTable(components, format, items) {
  return components.table([
    { key: "id", label: "报文编号", render: (value) => components.trusted(`<button class="link-button" type="button" data-message-id="${escapeHtml(value)}">#${escapeHtml(value)}</button>`) },
    { key: "device_id", label: "终端编号" },
    { key: "sequence", label: "报文序号", render: (value) => components.trusted(`<span class="mono">${escapeHtml(value)}</span>`) },
    { key: "kind", label: "报文类型", render: (value) => KIND_LABELS[value] || value },
    { key: "reported_at", label: "终端时间", render: (value) => format.dateTime(value) },
    { key: "received_at", label: "接收时间", render: (value) => format.dateTime(value) },
    { key: "digest", label: "校验摘要", render: (value) => components.trusted(`<span class="mono" title="${escapeHtml(value)}">${escapeHtml(value ? `${value.slice(0, 12)}…` : "—")}</span>`) },
    { key: "status", label: "接收状态", render: (value) => components.statusTag(value) },
  ], items, { emptyText: "当前筛选条件下暂无采集报文" });
}

function renderMessageDetail(components, format, message) {
  const payload = message.payload ? JSON.stringify(message.payload, null, 2) : "无可解析载荷";
  return `<div class="modal-form">
    <div class="inline-note">原始报文以 Base64 只读展示；摘要用于核对接收字节是否发生变化。</div>
    <dl>
      <div><dt>终端 / 序号</dt><dd>${escapeHtml(message.device_id || message.claimed_device_id || "未知")} / ${escapeHtml(message.sequence)}</dd></div>
      <div><dt>接收状态</dt><dd>${components.statusTag(message.status).html}</dd></div>
      <div><dt>接收时间</dt><dd>${escapeHtml(format.dateTime(message.received_at))}</dd></div>
      <div><dt>校验摘要</dt><dd class="mono wrap">${escapeHtml(message.digest)}</dd></div>
      <div><dt>拒绝原因</dt><dd>${escapeHtml(message.rejection_code || "无")}</dd></div>
    </dl>
    <div class="form-field"><label>解析载荷</label><textarea rows="9" readonly>${escapeHtml(payload)}</textarea></div>
    <div class="form-field"><label>原始帧 Base64</label><textarea rows="5" class="mono" readonly>${escapeHtml(message.raw_frame_base64 || "")}</textarea></div>
  </div>`;
}

export async function renderAcquisitionStream({ api, components, format, workspace, page, pageHeader, params, isCurrent }) {
  const state = {
    cursor: 0,
    paused: false,
    items: [],
    filters: { device_id: params.device_id || "", kind: params.kind || "", status: params.status || "" },
    snapshot: { queue: {}, worker: {} },
  };
  const first = await api.get("/api/acquisition/stream?after_id=0&limit=100");
  if (!isCurrent()) return false;
  state.cursor = first.cursor || 0;
  state.items = first.items || [];
  state.snapshot = first;
  workspace.innerHTML = `${pageHeader(page, `<div class="header-actions"><span id="streamState" class="data-time">实时接收中</span><button id="streamToggle" class="button secondary" type="button">暂停刷新</button></div>`)}
    <div id="streamMetrics">${queueMarkup(state.snapshot, format)}</div>
    <section class="panel filter-panel"><form id="streamFilters" class="filter-grid device-filters">
      <div class="form-field"><label for="streamDevice">终端编号</label><input id="streamDevice" name="device_id" value="${escapeHtml(state.filters.device_id)}" placeholder="例如 D-001" /></div>
      <div class="form-field"><label for="streamKind">报文类型</label><select id="streamKind" name="kind"><option value="">全部类型</option>${Object.entries(KIND_LABELS).map(([value, label]) => `<option value="${value}" ${state.filters.kind === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="form-field"><label for="streamStatus">接收状态</label><select id="streamStatus" name="status"><option value="">全部状态</option>${[["accepted", "已接收"], ["replayed", "补传接收"], ["duplicate", "重复"], ["rejected", "已拒绝"]].map(([value, label]) => `<option value="${value}" ${state.filters.status === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="filter-actions"><button class="button primary" type="submit">应用筛选</button><button id="clearStreamFilters" class="button secondary" type="button">重置</button></div>
    </form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">LIVE ACQUISITION</p><h2>实时通信报文</h2></div><span id="streamCount"></span></header><div class="panel-body" id="streamTable"></div></section>`;

  const draw = () => {
    if (!isCurrent()) return;
    const visible = filtered(state.items, state.filters);
    workspace.querySelector("#streamMetrics").innerHTML = queueMarkup(state.snapshot, format);
    workspace.querySelector("#streamTable").innerHTML = messageTable(components, format, visible);
    workspace.querySelector("#streamCount").textContent = `缓存 ${state.items.length} 条 · 当前显示 ${visible.length} 条`;
  };
  draw();

  workspace.querySelector("#streamToggle").addEventListener("click", (event) => {
    state.paused = !state.paused;
    event.currentTarget.textContent = state.paused ? "继续刷新" : "暂停刷新";
    workspace.querySelector("#streamState").textContent = state.paused ? "已暂停，保留当前视图" : "实时接收中";
  });
  workspace.querySelector("#streamFilters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.filters = Object.fromEntries(new FormData(event.currentTarget));
    draw();
  });
  workspace.querySelector("#clearStreamFilters").addEventListener("click", () => {
    state.filters = { device_id: "", kind: "", status: "" };
    workspace.querySelector("#streamFilters").reset();
    draw();
  });
  const showMessage = async (messageId) => {
    try {
      const message = await api.get(`/api/acquisition/messages/${encodeURIComponent(messageId)}`);
      components.modal({ title: `报文 #${message.id}`, content: components.trusted(renderMessageDetail(components, format, message)), actions: [{ label: "关闭", onClick: ({ close }) => close() }] });
    } catch (error) {
      components.toast(error.message || "报文详情读取失败", "error");
    }
  };
  workspace.addEventListener("click", (event) => {
    const button = event.target.closest("[data-message-id]");
    if (button) showMessage(button.dataset.messageId);
  });

  if (params.message_id) await showMessage(params.message_id);
  const poll = async () => {
    if (!isCurrent()) return;
    if (!state.paused) {
      try {
        const next = await api.get(`/api/acquisition/stream?after_id=${encodeURIComponent(state.cursor)}&limit=100`);
        if (!isCurrent()) return;
        state.cursor = next.cursor ?? state.cursor;
        state.items = [...state.items, ...(next.items || [])].slice(-240);
        state.snapshot = next;
        draw();
      } catch (error) {
        if (isCurrent()) workspace.querySelector("#streamState").textContent = `刷新失败：${error.message}`;
      }
    }
    if (isCurrent()) window.setTimeout(poll, 2_000);
  };
  window.setTimeout(poll, 2_000);
  return true;
}
