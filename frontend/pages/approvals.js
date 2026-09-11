import { escapeHtml } from "../components.js";

function openDecision(strategy, decision, { api, components, navigate }) {
  const label = decision === "approve" ? "批准" : "驳回";
  const content = document.createElement("form");
  content.innerHTML = `<div class="inline-note"><strong>独立审批：</strong>创建人和提交人不能审批本人策略，服务端将复核职责分离及不可变版本。</div><div class="form-field"><label for="approvalComment">审批意见</label><textarea id="approvalComment" required minlength="2" maxlength="500" placeholder="填写参数、产量和安全约束复核结论"></textarea></div>`;
  components.modal({ title: `${label}策略 ${strategy.id}`, content, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label: `确认${label}`, kind: decision === "approve" ? "primary" : "danger", onClick: async ({ close }) => {
      const comment = content.querySelector("textarea").value.trim();
      if (comment.length < 2) { components.toast("审批意见至少填写 2 个字", "error"); return; }
      try {
        await api.post(`/api/strategies/${encodeURIComponent(strategy.id)}/${decision}`, { version: strategy.version, comment });
        close(); components.toast(`策略已${label}`); await navigate("approvals", {});
      } catch (error) { components.toast(error.message || "审批失败", "error"); }
    } },
  ] });
}

export async function renderApprovals({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const query = new URLSearchParams({ status: "pending_approval", page: String(Math.max(1, Number(params.page) || 1)), page_size: "20" });
  if (params.well_id) query.set("well_id", params.well_id);
  const result = await api.get(`/api/strategies?${query}`);
  if (!isCurrent()) return false;
  const canApprove = permissions.has("strategy.approve");
  const columns = [
    { key: "id", label: "策略编号" }, { key: "well_name", label: "油井" },
    { key: "name", label: "策略名称" }, { key: "current_version_id", label: "不可变版本" },
    { key: "submitted_at", label: "提交时间", render: (value) => format.dateTime(value) },
    { key: "created_by", label: "创建人编号" }, { key: "submitted_by", label: "提交人编号" },
    { key: "action", label: "审批操作", render: (_value, row) => components.trusted(canApprove ? `<div class="table-actions"><button type="button" class="button mini primary" data-decision="approve" data-strategy-id="${escapeHtml(row.id)}">批准</button><button type="button" class="button mini danger" data-decision="reject" data-strategy-id="${escapeHtml(row.id)}">驳回</button></div>` : '<span class="muted">只读查看</span>') },
  ];
  workspace.innerHTML = `${pageHeader(page)}<section class="metric-grid"><article class="metric-card warning"><p>待独立审批</p><strong>${escapeHtml(format.number(result.total))}<small>项</small></strong><span>审批人须与创建、提交人员不同</span></article><article class="metric-card primary"><p>审批规则</p><strong>双人<small>职责分离</small></strong><span>意见与结论进入审计链</span></article></section>
    <section class="panel filter-panel"><form id="approvalFilters" class="filter-grid"><div class="form-field"><label for="approvalWell">油井编号</label><input id="approvalWell" name="well_id" value="${escapeHtml(params.well_id || "")}" /></div><div class="filter-actions"><button class="button secondary" type="submit">筛选</button></div></form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">SEPARATION OF DUTIES</p><h2>策略独立审批</h2></div><span>${canApprove ? "具备审批权限" : "当前账号为只读权限"}</span></header><div class="panel-body">${components.table(columns, result.items, { emptyText: "当前没有待审批策略" })}<p class="source-note">审批意见为必填项；批准或驳回均针对当前业务版本，过期页面将由服务端返回版本冲突。</p></div></section>`;
  workspace.querySelector("#approvalFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("approvals", Object.fromEntries(new FormData(event.currentTarget))); });
  workspace.querySelectorAll("[data-decision]").forEach((button) => button.addEventListener("click", () => {
    const strategy = result.items.find((item) => item.id === button.dataset.strategyId);
    if (strategy) openDecision(strategy, button.dataset.decision, { api, components, navigate });
  }));
  return true;
}
