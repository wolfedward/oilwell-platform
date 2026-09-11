import { escapeHtml } from "../components.js";

const KIND_LABELS = {
  sequence_gap: "序列缺失",
  delay: "传输延迟",
  clock_drift: "时钟漂移",
  duplicate: "重复报文",
  range_outlier: "量程越界",
  schema_error: "结构异常",
  checksum_error: "校验失败",
  delivery_failure: "交付失败",
  completeness_check: "数据完整性",
};

function qualityQuery(state) {
  const query = new URLSearchParams({ page: String(state.page), page_size: "20" });
  for (const key of ["device_id", "kind", "status"]) if (state[key]) query.set(key, state[key]);
  return query.toString();
}

function missingText(row) {
  const values = row.missing_sequences || [];
  if (!values.length) return "—";
  return values.length <= 8 ? values.join("、") : `${values.slice(0, 8).join("、")} 等 ${values.length} 个`;
}

export async function renderQualityBoard({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const state = { page: Math.max(1, Number(params.page) || 1), device_id: params.device_id || "", kind: params.kind || "", status: params.status || "" };
  const [result, open, repaired, gaps, delivery] = await Promise.all([
    api.get(`/api/data-quality?${qualityQuery(state)}`),
    api.get("/api/data-quality?status=open&page_size=1"),
    api.get("/api/data-quality?status=repaired&page_size=1"),
    api.get("/api/data-quality?kind=sequence_gap&page_size=1"),
    api.get("/api/data-quality?kind=delivery_failure&page_size=1"),
  ]);
  if (!isCurrent()) return false;
  const canRepair = permissions.has("data_quality.repair");
  const columns = [
    { key: "id", label: "事件编号", render: (value) => `#${value}` },
    { key: "kind", label: "质量类型", render: (value) => KIND_LABELS[value] || value },
    { key: "device_id", label: "终端编号" },
    { key: "missing_sequences", label: "缺失序列", render: (_value, row) => missingText(row) },
    { key: "created_at", label: "发现时间", render: (value) => format.dateTime(value) },
    { key: "status", label: "处理状态", render: (value) => components.statusTag(value) },
    { key: "action", label: "处置", render: (_value, row) => {
      if (row.kind === "sequence_gap" && row.status === "open" && canRepair) return components.trusted(`<button type="button" class="button mini primary" data-repair-id="${escapeHtml(row.id)}">补传修复</button>`);
      if (row.raw_message_id) return components.trusted(`<button type="button" class="link-button" data-message-id="${escapeHtml(row.raw_message_id)}">查看报文</button>`);
      return "只读记录";
    } },
  ];
  const pages = Math.max(1, Math.ceil(result.total / result.page_size));
  workspace.innerHTML = `${pageHeader(page)}
    <section class="metric-grid" aria-label="质量统计">
      <button class="metric-card warning" type="button" data-quality-status="open"><p>待处理事件</p><strong>${escapeHtml(format.number(open.total))}<small>条</small></strong><span>进入待处理队列 →</span></button>
      <button class="metric-card success" type="button" data-quality-status="repaired"><p>已完成修复</p><strong>${escapeHtml(format.number(repaired.total))}<small>条</small></strong><span>查看修复结果 →</span></button>
      <button class="metric-card cyan" type="button" data-quality-kind="sequence_gap"><p>序列完整性</p><strong>${escapeHtml(format.number(gaps.total))}<small>条</small></strong><span>数据完整性与缺失范围 →</span></button>
      <button class="metric-card ${delivery.total ? "danger" : "success"}" type="button" data-quality-kind="delivery_failure"><p>交付失败</p><strong>${escapeHtml(format.number(delivery.total))}<small>条</small></strong><span>检查队列交付证据 →</span></button>
    </section>
    <section class="panel filter-panel"><form id="qualityFilters" class="filter-grid device-filters">
      <div class="form-field"><label for="qualityDevice">终端编号</label><input id="qualityDevice" name="device_id" value="${escapeHtml(state.device_id)}" placeholder="例如 D-001" /></div>
      <div class="form-field"><label for="qualityKind">质量类型</label><select id="qualityKind" name="kind"><option value="">全部类型</option>${Object.entries(KIND_LABELS).map(([value, label]) => `<option value="${value}" ${state.kind === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="form-field"><label for="qualityStatus">处理状态</label><select id="qualityStatus" name="status"><option value="">全部状态</option><option value="open" ${state.status === "open" ? "selected" : ""}>待处理</option><option value="repaired" ${state.status === "repaired" ? "selected" : ""}>已修复</option></select></div>
      <div class="filter-actions"><button class="button primary" type="submit">查询</button><button id="resetQuality" class="button secondary" type="button">重置</button></div>
    </form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">QUALITY CONTROL</p><h2>数据质量事件</h2></div><span>共 ${escapeHtml(result.total)} 条</span></header><div class="panel-body">${components.table(columns, result.items)}${components.pagination({ page: result.page, pages, total: result.total })}</div></section>`;

  const formValues = () => Object.fromEntries(new FormData(workspace.querySelector("#qualityFilters")));
  workspace.querySelector("#qualityFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("data-quality", { ...formValues(), page: "1" }); });
  workspace.querySelector("#resetQuality").addEventListener("click", () => navigate("data-quality", {}));
  workspace.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => navigate("data-quality", { ...state, page: button.dataset.page })));
  workspace.querySelectorAll("[data-quality-status]").forEach((button) => button.addEventListener("click", () => navigate("data-quality", { status: button.dataset.qualityStatus })));
  workspace.querySelectorAll("[data-quality-kind]").forEach((button) => button.addEventListener("click", () => navigate("data-quality", { kind: button.dataset.qualityKind })));
  workspace.querySelectorAll("[data-message-id]").forEach((button) => button.addEventListener("click", () => navigate("acquisition", { message_id: button.dataset.messageId })));
  workspace.querySelectorAll("[data-repair-id]").forEach((button) => button.addEventListener("click", () => {
    const event = result.items.find((item) => String(item.id) === button.dataset.repairId);
    if (!event) return;
    const content = components.trusted(`<div class="modal-form"><div class="inline-note">系统只从该终端的有界历史缓存补传，不生成中央替代数据。</div><dl><div><dt>终端</dt><dd>${escapeHtml(event.device_id)}</dd></div><div><dt>缺失序列</dt><dd>${escapeHtml(missingText(event))}</dd></div><div><dt>当前版本</dt><dd>${escapeHtml(event.version)}</dd></div></dl><p id="repairProgress" class="muted">确认后将逐帧校验、留存并更新缺口状态。</p></div>`);
    components.modal({
      title: `补传修复 #${event.id}`,
      content,
      actions: [
        { label: "取消", onClick: ({ close }) => close() },
        { label: "开始补传", kind: "primary", onClick: async ({ close, overlay }) => {
          const action = overlay.querySelector(".button.primary");
          action.disabled = true;
          action.textContent = "补传处理中…";
          overlay.querySelector("#repairProgress").textContent = "正在读取终端历史帧并重新校验，请稍候。";
          try {
            const outcome = await api.post(`/api/data-quality/${encodeURIComponent(event.id)}/repair`, { version: event.version });
            components.toast(outcome.status === "repaired" ? "缺失序列已全部补传" : `补传未完整，仍缺 ${outcome.unavailable?.length || 0} 个序列`, outcome.status === "repaired" ? "success" : "error");
            close();
            navigate("data-quality", state);
          } catch (error) {
            overlay.querySelector(".modal-content").innerHTML = components.errorPanel(error);
            action.disabled = true;
            action.textContent = "请关闭后重试";
          }
        } },
      ],
    });
  }));
  return true;
}
