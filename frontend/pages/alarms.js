import { escapeHtml } from "../components.js";

const ALARM_ACTIONS = {
  new: [["confirmed", "确认报警"], ["processing", "开始处置"], ["false_positive", "标记误报"]],
  confirmed: [["assigned", "标记已派发"], ["processing", "开始处置"], ["false_positive", "标记误报"]],
  acknowledged: [["assigned", "标记已派发"], ["processing", "开始处置"]],
  assigned: [["processing", "开始处置"], ["escalated", "升级处理"]],
  processing: [["closed", "关闭报警"], ["escalated", "升级处理"]],
  escalated: [["confirmed", "重新确认"], ["assigned", "重新派发"], ["processing", "开始处置"]],
};
const TYPE_LABELS = { supply_shortage: "供液不足", command_delivery_failed: "指令执行失败", strategy_exception: "策略异常" };

function openTransition(alarm, target, label, { api, components, navigate }) {
  const content = document.createElement("form");
  content.innerHTML = `<div class="form-field"><label for="alarmComment">处置说明</label><textarea id="alarmComment" required minlength="2" maxlength="500" placeholder="记录判断依据和处置结果"></textarea></div>`;
  components.modal({ title: `${label} · ${alarm.id}`, content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label, kind: target === "closed" || target === "false_positive" ? "danger" : "primary", onClick: async ({ close }) => {
      const comment = content.querySelector("textarea").value.trim();
      if (comment.length < 2) { components.toast("处置说明至少填写 2 个字", "error"); return; }
      try { await api.post(`/api/alarms/${encodeURIComponent(alarm.id)}/transition`, { target, comment }); close(); components.toast("报警处置状态已更新"); await navigate("alarms", { id: alarm.id }); }
      catch (error) { components.toast(error.message || "报警处置失败", "error"); }
    } },
  ] });
}

function openOrder(alarm, { api, components, navigate }) {
  const content = document.createElement("form");
  content.innerHTML = `<div class="form-field"><label for="orderKind">工单类型</label><select id="orderKind"><option value="production_adjustment">生产参数调整</option><option value="inspection">异常工况巡检</option><option value="equipment_repair">设备检修</option><option value="safety_check">安全复核</option></select></div><div class="form-field"><label for="orderAssignee">负责人用户编号</label><input id="orderAssignee" type="number" min="1" required placeholder="输入具备处置职责的用户编号" /></div><div class="form-field"><label for="orderDue">要求完成时间</label><input id="orderDue" type="datetime-local" /></div>`;
  components.modal({ title: `创建工单 · ${alarm.id}`, content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label: "确认创建", kind: "primary", onClick: async ({ close }) => {
      const assignee = Number(content.querySelector("#orderAssignee").value);
      if (!Number.isInteger(assignee) || assignee < 1) { components.toast("请输入有效的负责人用户编号", "error"); return; }
      const due = content.querySelector("#orderDue").value;
      const body = { alarm_id: alarm.id, kind: content.querySelector("#orderKind").value, assignee_id: assignee };
      if (due) body.due_at = new Date(due).toISOString();
      try { const order = await api.post("/api/work-orders", body); close(); components.toast("工单已创建并关联报警"); await navigate("work-orders", { id: order.id }); }
      catch (error) { components.toast(error.message || "创建工单失败", "error"); }
    } },
  ] });
}

export async function renderAlarms({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const query = new URLSearchParams({ page: String(Math.max(1, Number(params.page) || 1)), page_size: "20" });
  for (const key of ["well_id", "status", "severity"]) if (params[key]) query.set(key, params[key]);
  const result = await api.get(`/api/alarms?${query}`);
  const alarmId = params.id || result.items[0]?.id;
  const alarm = alarmId ? await api.get(`/api/alarms/${encodeURIComponent(alarmId)}`) : null;
  if (!isCurrent()) return false;
  const canAcknowledge = permissions.has("alarms.ack");
  const columns = [
    { key: "id", label: "报警编号", render: (value) => components.trusted(`<button class="link-button" type="button" data-alarm-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "well_name", label: "油井" }, { key: "alarm_type", label: "报警类型", render: (value) => TYPE_LABELS[value] || value },
    { key: "severity", label: "级别", render: (value) => components.statusTag(value) },
    { key: "status", label: "处置状态", render: (value) => components.statusTag(value) },
    { key: "created_at", label: "发生时间", render: (value) => format.dateTime(value) },
  ];
  const actions = alarm && canAcknowledge ? (ALARM_ACTIONS[alarm.status] || []).map(([target, label]) => `<button type="button" class="button ${target === "closed" || target === "false_positive" ? "danger" : "secondary"}" data-alarm-target="${target}">${label}</button>`).join("") : "";
  const canCreateOrder = alarm && canAcknowledge && !["closed", "false_positive"].includes(alarm.status);
  workspace.innerHTML = `${pageHeader(page)}<section class="panel filter-panel"><form id="alarmFilters" class="filter-grid"><div class="form-field"><label for="alarmWell">油井编号</label><input id="alarmWell" name="well_id" value="${escapeHtml(params.well_id || "")}" /></div><div class="form-field"><label for="alarmStatus">处置状态</label><select id="alarmStatus" name="status"><option value="">全部状态</option>${["new","confirmed","assigned","processing","escalated","closed","false_positive"].map((value) => `<option value="${value}" ${params.status === value ? "selected" : ""}>${value}</option>`).join("")}</select></div><div class="filter-actions"><button class="button secondary" type="submit">查询</button></div></form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">ALARM OPERATIONS</p><h2>报警中心</h2></div><span>共 ${escapeHtml(result.total)} 条</span></header><div class="panel-body">${components.table(columns, result.items, { emptyText: "当前没有报警记录" })}</div></section>
    ${alarm ? `<section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">SOURCE OBJECT</p><h2>${escapeHtml(TYPE_LABELS[alarm.alarm_type] || alarm.alarm_type)}</h2></div>${components.statusTag(alarm.status).html}</header><dl><div><dt>来源对象</dt><dd>${escapeHtml(alarm.source_object_type)} / ${escapeHtml(alarm.source_object_id)}</dd></div><div><dt>关联诊断</dt><dd>${escapeHtml(alarm.diagnosis_id || "—")}</dd></div><div><dt>关联策略</dt><dd>${escapeHtml(alarm.strategy_id || "—")}</dd></div><div><dt>关联指令</dt><dd>${escapeHtml(alarm.command_id || "—")}</dd></div><div><dt>当前处置状态</dt><dd>${escapeHtml(alarm.status)}</dd></div><div><dt>处置结论</dt><dd>${escapeHtml(alarm.resolution_comment || "尚未关闭")}</dd></div></dl></article><article class="panel wide"><header><div><p class="eyebrow">LEGAL ACTIONS</p><h2>报警处置</h2></div><span>${canAcknowledge ? "仅显示当前状态允许的操作" : "当前账号只读"}</span></header><div class="panel-body"><div class="form-actions">${actions}${canCreateOrder ? '<button type="button" id="createAlarmOrder" class="button primary">创建工单</button>' : ""}${!actions && !canCreateOrder ? '<span class="muted">当前状态无可执行操作</span>' : ""}</div><p class="source-note">每次确认、派发、升级和关闭均由服务端状态机校验并写入审计记录。</p></div></article></section>` : ""}`;
  workspace.querySelector("#alarmFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("alarms", Object.fromEntries(new FormData(event.currentTarget))); });
  workspace.querySelectorAll("[data-alarm-id]").forEach((button) => button.addEventListener("click", () => navigate("alarms", { ...params, id: button.dataset.alarmId })));
  workspace.querySelectorAll("[data-alarm-target]").forEach((button) => button.addEventListener("click", () => openTransition(alarm, button.dataset.alarmTarget, button.textContent, { api, components, navigate })));
  workspace.querySelector("#createAlarmOrder")?.addEventListener("click", () => openOrder(alarm, { api, components, navigate }));
  return true;
}
