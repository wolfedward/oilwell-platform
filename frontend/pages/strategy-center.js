import { escapeHtml } from "../components.js";

const DECIDED = new Set(["approved", "rejected", "effective", "paused", "rolled_back"]);

async function action(task, components) {
  try { await task(); } catch (error) { components.toast(error.message || "操作失败", "error"); }
}

function strategyQuery(params) {
  const query = new URLSearchParams({ page: String(Math.max(1, Number(params.page) || 1)), page_size: "15" });
  for (const key of ["well_id", "status"]) if (params[key]) query.set(key, params[key]);
  return query.toString();
}

function dispatchDialog({ strategy, device, api, components, navigate }) {
  const selected = strategy.configuration?.selected_candidate || {};
  const content = document.createElement("div");
  content.innerHTML = `<div class="form-stack"><div class="inline-note"><strong>安全校验：</strong>仅批准状态、当前不可变版本且关联终端在线时允许生成签名指令。</div><dl class="detail-list"><div><dt>目标终端</dt><dd>${escapeHtml(device?.id || "未找到关联终端")}</dd></div><div><dt>策略版本</dt><dd>${escapeHtml(strategy.current_version_id)}</dd></div><div><dt>指令载荷</dt><dd>开井 ${escapeHtml(selected.on_minutes)} min / 关井 ${escapeHtml(selected.off_minutes)} min</dd></div><div><dt>有效期</dt><dd>服务端签发后 15 分钟</dd></div><div><dt>预期结果</dt><dd>终端写入间抽周期并返回分阶段回执</dd></div></dl></div>`;
  components.modal({ title: "下发策略确认", content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label: "确认下发", kind: "primary", onClick: ({ close }) => action(async () => {
      const command = await api.post(`/api/strategies/${encodeURIComponent(strategy.id)}/dispatch`, {});
      close(); components.toast("签名指令已进入发送队列");
      await navigate("commands", { id: command.id });
    }, components) },
  ] });
}

function rollbackDialog(strategy, { api, components, navigate }) {
  const content = document.createElement("form");
  content.innerHTML = '<div class="form-field"><label for="rollbackReason">回退原因</label><textarea id="rollbackReason" required minlength="2" maxlength="500" placeholder="说明测量效果或安全复核结论"></textarea></div>';
  components.modal({ title: "回退到上一有效制度", content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label: "确认回退", kind: "danger", onClick: ({ close }) => action(async () => {
      const reason = content.querySelector("textarea").value.trim();
      if (reason.length < 2) { components.toast("请填写至少 2 个字的回退原因", "error"); return; }
      const command = await api.post(`/api/strategies/${encodeURIComponent(strategy.id)}/rollback`, { reason });
      close(); components.toast("回退指令已进入发送队列");
      await navigate("commands", { id: command.id });
    }, components) },
  ] });
}

export async function renderStrategyCenter({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const result = await api.get(`/api/strategies?${strategyQuery(params)}`);
  let detail = null;
  const detailId = params.id || result.items[0]?.id;
  if (detailId) detail = await api.get(`/api/strategies/${encodeURIComponent(detailId)}`);
  if (!isCurrent()) return false;
  const canCreate = permissions.has("strategy.create");
  const canSubmit = permissions.has("strategy.submit");
  const canControl = permissions.has("devices.control");
  const columns = [
    { key: "id", label: "策略编号", render: (value) => components.trusted(`<button class="link-button" type="button" data-strategy-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "well_name", label: "油井" }, { key: "name", label: "策略名称" },
    { key: "configuration_version", label: "配置版本", render: (value) => `V${value || "—"}` },
    { key: "status", label: "状态", render: (value) => components.statusTag(value) },
    { key: "updated_at", label: "更新时间", render: (value) => format.dateTime(value) },
  ];
  const selected = detail?.configuration?.selected_candidate || {};
  const detailActions = detail ? [
    canCreate && detail.status === "draft" ? '<button type="button" id="editStrategy" class="button secondary">编辑草稿</button>' : "",
    canSubmit && detail.status === "draft" ? '<button type="button" id="submitCenterStrategy" class="button primary">提交审批</button>' : "",
    canCreate && DECIDED.has(detail.status) ? '<button type="button" id="copyStrategy" class="button secondary">复制修订</button>' : "",
    canControl && detail.status === "approved" ? '<button type="button" id="dispatchStrategy" class="button primary">下发策略</button>' : "",
    canControl && detail.status === "effective" ? '<button type="button" id="rollbackStrategy" class="button danger">效果异常回退</button>' : "",
  ].join("") : "";
  workspace.innerHTML = `${pageHeader(page, canCreate ? '<button type="button" id="createStrategy" class="button primary">新建策略</button>' : "")}
    <section class="panel filter-panel"><form id="strategyFilters" class="filter-grid"><div class="form-field"><label for="strategyWellFilter">油井编号</label><input id="strategyWellFilter" name="well_id" value="${escapeHtml(params.well_id || "")}" /></div><div class="form-field"><label for="strategyStatus">状态</label><select id="strategyStatus" name="status"><option value="">全部状态</option>${[["draft","草稿"],["pending_approval","待审批"],["approved","已批准"],["effective","已生效"],["rejected","已驳回"],["rolled_back","已回退"]].map(([value,label]) => `<option value="${value}" ${params.status === value ? "selected" : ""}>${label}</option>`).join("")}</select></div><div class="filter-actions"><button class="button secondary" type="submit">查询</button></div></form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">VERSIONED STRATEGIES</p><h2>策略中心</h2></div><span>共 ${escapeHtml(result.total)} 项</span></header><div class="panel-body">${components.table(columns, result.items, { emptyText: "暂无符合条件的策略" })}</div></section>
    ${detail ? `<section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">CURRENT CONTROL</p><h2>${escapeHtml(detail.name)}</h2></div>${components.statusTag(detail.status).html}</header><dl><div><dt>开井时长</dt><dd>${escapeHtml(selected.on_minutes ?? "—")} min</dd></div><div><dt>关井时长</dt><dd>${escapeHtml(selected.off_minutes ?? "—")} min</dd></div><div><dt>预计节电率</dt><dd>${escapeHtml(selected.saving_rate == null ? "—" : format.percent(selected.saving_rate, 1))}</dd></div><div><dt>预计日产液</dt><dd>${escapeHtml(format.number(selected.predicted_liquid_m3, 3))} m³</dd></div></dl><div class="form-actions">${detailActions}</div></article>
      <article class="panel wide"><header><div><p class="eyebrow">IMMUTABLE HISTORY</p><h2>不可变版本与审批记录</h2></div><button type="button" id="openRecovery" class="button secondary">查看渗流依据</button></header><div class="panel-body"><h3>不可变版本</h3>${components.table([{key:"version_number",label:"版本"},{key:"id",label:"版本编号"},{key:"status",label:"封存状态",render:(value)=>components.statusTag(value)},{key:"created_at",label:"形成时间",render:(value)=>format.dateTime(value)}], detail.versions || [])}<h3>审批记录</h3>${components.table([{key:"id",label:"审批编号"},{key:"object_version",label:"业务版本"},{key:"decision",label:"结论",render:(value)=>components.statusTag(value || "pending")},{key:"comment",label:"审批意见"},{key:"decided_at",label:"审批时间",render:(value)=>format.dateTime(value)}], detail.approvals || [], { emptyText: "尚未提交审批" })}<div class="form-actions"><button type="button" id="effectTrace" class="button secondary">效果追踪</button></div></div></article></section>` : ""}`;
  workspace.querySelector("#strategyFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("strategy-center", Object.fromEntries(new FormData(event.currentTarget))); });
  workspace.querySelector("#createStrategy")?.addEventListener("click", () => navigate("strategy-editor", {}));
  workspace.querySelectorAll("[data-strategy-id]").forEach((button) => button.addEventListener("click", () => navigate("strategy-center", { ...params, id: button.dataset.strategyId })));
  workspace.querySelector("#editStrategy")?.addEventListener("click", () => navigate("strategy-editor", { id: detail.id }));
  workspace.querySelector("#openRecovery")?.addEventListener("click", () => navigate("recovery-analysis", { id: detail.id }));
  workspace.querySelector("#effectTrace")?.addEventListener("click", () => navigate("energy-overview", { strategy_id: detail.id }));
  workspace.querySelector("#submitCenterStrategy")?.addEventListener("click", () => action(async () => { const updated = await api.post(`/api/strategies/${encodeURIComponent(detail.id)}/submit`, { version: detail.version }); components.toast("策略已提交审批"); await navigate("strategy-center", { id: updated.id }); }, components));
  workspace.querySelector("#copyStrategy")?.addEventListener("click", () => action(async () => { const copied = await api.post(`/api/strategies/${encodeURIComponent(detail.id)}/copy`, {}); components.toast("已复制为新的草稿"); await navigate("strategy-editor", { id: copied.id }); }, components));
  workspace.querySelector("#dispatchStrategy")?.addEventListener("click", () => action(async () => { const devices = await api.get(`/api/devices?well_id=${encodeURIComponent(detail.well_id)}&page_size=1`); dispatchDialog({ strategy: detail, device: devices.items[0], api, components, navigate }); }, components));
  workspace.querySelector("#rollbackStrategy")?.addEventListener("click", () => rollbackDialog(detail, { api, components, navigate }));
  return true;
}
