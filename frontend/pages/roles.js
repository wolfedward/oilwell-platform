import { escapeHtml } from "../components.js";

function editRole({ api, components, role, allPermissions, refresh }) {
  const form = document.createElement("form"); form.className = "modal-form";
  form.innerHTML = `<div class="inline-note">角色编码 ${escapeHtml(role.code)} · 版本 ${role.version}</div><div class="form-field"><label for="roleName">角色名称</label><input id="roleName" value="${escapeHtml(role.name)}" required /></div><div class="form-field"><label for="roleStatus">状态</label><select id="roleStatus"><option value="active" ${role.status === "active" ? "selected" : ""}>启用</option><option value="disabled" ${role.status === "disabled" ? "selected" : ""}>停用</option></select></div><fieldset class="permission-grid"><legend>能力权限</legend>${allPermissions.map((code) => `<label><input type="checkbox" value="${escapeHtml(code)}" ${role.permissions.includes(code) ? "checked" : ""} />${escapeHtml(code)}</label>`).join("")}</fieldset><p class="inline-error" hidden></p>`;
  components.modal({ title: "配置岗位权限", content: form, actions: [{ label: "取消", onClick: ({ close }) => close() }, { label: "保存", kind: "primary", onClick: async ({ close }) => {
    try {
      await api.patch(`/api/roles/${role.id}`, { version: role.version, name: form.querySelector("#roleName").value.trim(), status: form.querySelector("#roleStatus").value, permissions: [...form.querySelectorAll('input[type="checkbox"]:checked')].map((item) => item.value) });
      close(); components.toast("角色权限已更新"); await refresh();
    } catch (error) { const node = form.querySelector(".inline-error"); node.textContent = error.message; node.hidden = false; }
  } }] });
}

export async function renderRoles({ api, components, workspace, page, pageHeader, permissions, navigate, isCurrent }) {
  const roles = await api.get("/api/roles"); if (!isCurrent()) return false;
  const canManage = permissions.has("roles.manage");
  const allPermissions = [...new Set(roles.flatMap((item) => item.permissions))].sort();
  const columns = [{ key: "code", label: "角色编码" }, { key: "name", label: "角色名称" }, { key: "permission_count", label: "权限数量", render: (_value, row) => row.permissions.length }, { key: "permissions", label: "能力范围", render: (value) => value.slice(0, 5).join("、") + (value.length > 5 ? ` 等 ${value.length} 项` : "") }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }, ...(canManage ? [{ key: "action", label: "操作", render: (_value, row) => components.trusted(`<button type="button" class="button mini secondary" data-role-id="${row.id}">配置</button>`) }] : [])];
  const refresh = () => navigate("roles", { refresh: Date.now() });
  workspace.innerHTML = `${pageHeader(page)}<section class="metric-grid"><article class="metric-card purple"><p>岗位角色</p><strong>${roles.length}<small>类</small></strong><span>按职责授权</span></article><article class="metric-card cyan"><p>权限能力</p><strong>${allPermissions.length}<small>项</small></strong><span>接口与页面双重校验</span></article></section><section class="panel list-panel"><header><div><p class="eyebrow">ROLE BASED ACCESS CONTROL</p><h2>角色权限矩阵</h2></div><span>管理员基础权限受保护</span></header><div class="panel-body">${components.table(columns, roles)}</div></section>`;
  workspace.querySelectorAll("[data-role-id]").forEach((button) => button.addEventListener("click", () => editRole({ api, components, role: roles.find((item) => String(item.id) === button.dataset.roleId), allPermissions, refresh })));
  return true;
}
