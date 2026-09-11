import { escapeHtml } from "../components.js";

function detailModal(components, item) {
  const before = item.details?.before || {};
  const after = item.details?.after || {};
  const content = document.createElement("div");
  content.className = "audit-detail";
  content.innerHTML = `<div class="inline-note">审计编号 ${item.id} · ${escapeHtml(item.action)} · ${escapeHtml(item.created_at)}</div><div class="comparison-grid"><section><h3>变更前</h3><pre>${escapeHtml(JSON.stringify(before, null, 2))}</pre></section><section><h3>变更后</h3><pre>${escapeHtml(JSON.stringify(after, null, 2))}</pre></section></div>`;
  components.modal({ title: "审计详情", content, actions: [{ label: "关闭", kind: "primary", onClick: ({ close }) => close() }] });
}

export async function renderAudit({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  const query = new URLSearchParams({ page: params.page || "1", page_size: "20" });
  if (params.action) query.set("action", params.action);
  if (params.object_type) query.set("object_type", params.object_type);
  const result = await api.get(`/api/audit?${query}`); if (!isCurrent()) return false;
  const columns = [{ key: "id", label: "编号" }, { key: "created_at", label: "时间", render: (value) => format.dateTime(value) }, { key: "actor_name", label: "操作人", render: (value, row) => value || `系统/${row.actor_id || "—"}` }, { key: "action", label: "动作" }, { key: "object_type", label: "对象类型" }, { key: "object_id", label: "对象编号" }, { key: "source_type", label: "来源" }, { key: "detail", label: "详情", render: (_value, row) => components.trusted(`<button type="button" class="link-button" data-audit-id="${row.id}">查看前后值</button>`) }];
  workspace.innerHTML = `${pageHeader(page)}<section class="panel filter-panel"><form id="auditFilters" class="filter-grid"><div class="form-field"><label for="auditAction">动作</label><input id="auditAction" name="action" value="${escapeHtml(params.action || "")}" placeholder="如 command.success" /></div><div class="form-field"><label for="auditObject">对象类型</label><select id="auditObject" name="object_type"><option value="">全部对象</option>${["energy_strategy", "device_command", "device", "user", "role", "work_order", "alarm"].map((value) => `<option value="${value}" ${value === params.object_type ? "selected" : ""}>${value}</option>`).join("")}</select></div><div class="filter-actions"><button class="button primary" type="submit">查询</button><button id="resetAudit" class="button secondary" type="button">重置</button></div></form></section><section class="panel list-panel"><header><div><p class="eyebrow">IMMUTABLE AUDIT TRAIL</p><h2>操作审计流水</h2></div><span>共 ${result.total} 条，只读留痕</span></header><div class="panel-body">${components.table(columns, result.items)}<nav class="pagination"><span>第 ${result.page} 页</span><button data-list-page="${result.page - 1}" ${result.page <= 1 ? "disabled" : ""}>上一页</button><button data-list-page="${result.page + 1}" ${result.page * result.page_size >= result.total ? "disabled" : ""}>下一页</button></nav></div></section>`;
  workspace.querySelector("#auditFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("audit", { ...Object.fromEntries(new FormData(event.currentTarget)), page: "1" }); });
  workspace.querySelector("#resetAudit").addEventListener("click", () => navigate("audit", {}));
  workspace.querySelectorAll("[data-list-page]").forEach((button) => button.addEventListener("click", () => navigate("audit", { action: params.action || "", object_type: params.object_type || "", page: button.dataset.listPage })));
  workspace.querySelectorAll("[data-audit-id]").forEach((button) => button.addEventListener("click", () => detailModal(components, result.items.find((item) => String(item.id) === button.dataset.auditId))));
  return true;
}
