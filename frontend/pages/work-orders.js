import { escapeHtml } from "../components.js";

const ORDER_ACTIONS = {
  pending: [["accepted", "接收工单"]],
  accepted: [["processing", "开始处理"]],
  processing: [["review", "提交验收复核"]],
  review: [["processing", "退回处理"], ["closed", "独立关闭"]],
  completed: [["processing", "退回处理"], ["closed", "独立关闭"]],
  overdue: [["accepted", "接收工单"], ["processing", "开始处理"], ["review", "提交验收复核"], ["closed", "独立关闭"]],
};
const KIND_LABELS = { inspection: "异常工况巡检", production_adjustment: "生产参数调整", equipment_repair: "设备检修", safety_check: "安全复核" };

function openTransition(order, target, label, { api, components, navigate }) {
  const content = document.createElement("form");
  content.innerHTML = `<div class="form-field"><label for="orderComment">处理记录</label><textarea id="orderComment" required minlength="2" maxlength="500" placeholder="填写现场处理、参数调整或验收复核结论"></textarea></div>${target === "closed" ? '<div class="inline-note"><strong>验收复核：</strong>关闭人需具备独立复核权限，服务端保留关闭人和时间。</div>' : ""}`;
  components.modal({ title: `${label} · ${order.id}`, content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label, kind: target === "closed" ? "primary" : "secondary", onClick: async ({ close }) => {
      const comment = content.querySelector("textarea").value.trim();
      if (comment.length < 2) { components.toast("处理记录至少填写 2 个字", "error"); return; }
      try { await api.post(`/api/work-orders/${encodeURIComponent(order.id)}/transition`, { target, comment }); close(); components.toast("工单状态已更新"); await navigate("work-orders", { id: order.id }); }
      catch (error) { components.toast(error.message || "工单操作失败", "error"); }
    } },
  ] });
}

function traceTimeline(trace, format) {
  if (!trace) return "";
  const links = [
    ["诊断结论", trace.diagnosis_id], ["报警记录", trace.alarm_id], ["节能策略", trace.strategy_id],
    ["审批记录", (trace.approval_ids || []).join("、")], ["控制指令", trace.command_id], ["工单闭环", trace.work_order_id],
  ];
  return `<ol class="trace-timeline">${links.map(([label, value]) => `<li class="${value ? "complete" : "pending"}"><i></i><div><strong>${escapeHtml(label)}</strong><span>${escapeHtml(value || "未关联")}</span></div></li>`).join("")}</ol><p class="source-note">统一追溯链生成时间：${escapeHtml(format.dateTime(trace.work_order?.updated_at))}；审计记录 ${escapeHtml((trace.audit_ids || []).length)} 条。</p>`;
}

export async function renderWorkOrders({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const query = new URLSearchParams({ page: String(Math.max(1, Number(params.page) || 1)), page_size: "20" });
  for (const key of ["well_id", "status", "assignee_id"]) if (params[key]) query.set(key, params[key]);
  const result = await api.get(`/api/work-orders?${query}`);
  const orderId = params.id || result.items[0]?.id;
  const [order, trace] = orderId ? await Promise.all([
    api.get(`/api/work-orders/${encodeURIComponent(orderId)}`),
    api.get(`/api/work-orders/${encodeURIComponent(orderId)}/trace`),
  ]) : [null, null];
  if (!isCurrent()) return false;
  const canManage = permissions.has("maintenance.manage");
  const canReview = permissions.has("diagnosis.review");
  const legal = order ? (ORDER_ACTIONS[order.status] || []).filter(([target]) => target === "closed" ? canReview : canManage) : [];
  const columns = [
    { key: "id", label: "工单编号", render: (value) => components.trusted(`<button type="button" class="link-button" data-order-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "well_name", label: "油井" }, { key: "kind", label: "工单类型", render: (value) => KIND_LABELS[value] || value },
    { key: "assignee_name", label: "负责人" }, { key: "due_at", label: "要求完成", render: (value) => format.dateTime(value) },
    { key: "status", label: "状态", render: (value) => components.statusTag(value) },
    { key: "overdue", label: "时效", render: (value) => value ? components.statusTag("warning", "已逾期") : "正常" },
  ];
  const logs = order?.logs || [];
  workspace.innerHTML = `${pageHeader(page, canManage ? '<button type="button" id="evaluateOverdue" class="button secondary">检查逾期工单</button>' : "")}
    <section class="panel filter-panel"><form id="orderFilters" class="filter-grid"><div class="form-field"><label for="orderWell">油井编号</label><input id="orderWell" name="well_id" value="${escapeHtml(params.well_id || "")}" /></div><div class="form-field"><label for="orderStatus">状态</label><select id="orderStatus" name="status"><option value="">全部状态</option>${["pending","accepted","processing","review","overdue","closed"].map((value) => `<option value="${value}" ${params.status === value ? "selected" : ""}>${value}</option>`).join("")}</select></div><div class="filter-actions"><button class="button secondary" type="submit">查询</button></div></form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">CLOSED-LOOP ORDERS</p><h2>工单闭环台账</h2></div><span>共 ${escapeHtml(result.total)} 条</span></header><div class="panel-body">${components.table(columns, result.items, { emptyText: "暂无工单记录" })}</div></section>
    ${order ? `<section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">ORDER ACTION</p><h2>${escapeHtml(order.title)}</h2></div>${components.statusTag(order.status).html}</header><dl><div><dt>工单类型</dt><dd>${escapeHtml(KIND_LABELS[order.kind] || order.kind)}</dd></div><div><dt>关联报警</dt><dd>${escapeHtml(order.alarm_id)}</dd></div><div><dt>负责人</dt><dd>${escapeHtml(order.assignee_name || order.assignee_id)}</dd></div><div><dt>要求完成</dt><dd>${escapeHtml(format.dateTime(order.due_at))}</dd></div><div><dt>关闭人</dt><dd>${escapeHtml(order.closed_by || "待验收")}</dd></div></dl><div class="form-actions">${legal.map(([target, label]) => `<button type="button" class="button ${target === "closed" ? "primary" : "secondary"}" data-order-target="${target}">${label}</button>`).join("")}${!legal.length ? '<span class="muted">当前账号在此状态下无可执行操作</span>' : ""}</div></article>
      <article class="panel wide"><header><div><p class="eyebrow">HANDLING LOG</p><h2>处理记录</h2></div><span>${logs.length} 次状态变更</span></header><div class="panel-body">${components.table([{key:"from_status",label:"原状态",render:(value)=>value || "创建"},{key:"to_status",label:"新状态",render:(value)=>components.statusTag(value)},{key:"comment",label:"处理说明"},{key:"changed_at",label:"操作时间",render:(value)=>format.dateTime(value)},{key:"actor_id",label:"操作人"}], logs, { emptyText: "暂无处理记录" })}</div></article>
      <article class="panel wide"><header><div><p class="eyebrow">UNIFIED TRACE</p><h2>统一追溯链</h2></div><span>诊断 → 报警 → 策略 → 审批 → 指令 → 工单</span></header><div class="panel-body">${traceTimeline(trace, format)}</div></article></section>` : ""}`;
  workspace.querySelector("#orderFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("work-orders", Object.fromEntries(new FormData(event.currentTarget))); });
  workspace.querySelectorAll("[data-order-id]").forEach((button) => button.addEventListener("click", () => navigate("work-orders", { ...params, id: button.dataset.orderId })));
  workspace.querySelectorAll("[data-order-target]").forEach((button) => button.addEventListener("click", () => openTransition(order, button.dataset.orderTarget, button.textContent, { api, components, navigate })));
  workspace.querySelector("#evaluateOverdue")?.addEventListener("click", async () => { try { const summary = await api.post("/api/work-orders/evaluate-overdue", {}); components.toast(`已检查 ${summary.evaluated || 0} 条，新增逾期 ${summary.overdue || 0} 条`); await navigate("work-orders", params); } catch (error) { components.toast(error.message || "逾期检查失败", "error"); } });
  return true;
}
