import { escapeHtml } from "../components.js";

function userModal({ api, components, user, roles, refresh }) {
  const creating = !user;
  const form = document.createElement("form");
  form.className = "modal-form";
  form.innerHTML = `${creating ? '<div class="form-field"><label for="accountName">登录账号</label><input id="accountName" required maxlength="120" /></div>' : `<div class="inline-note">账号 ${escapeHtml(user.username)}，记录版本 ${escapeHtml(user.version)}</div>`}<div class="form-field"><label for="displayName">显示名称</label><input id="displayName" required maxlength="120" value="${escapeHtml(user?.display_name || "")}" /></div><div class="form-field"><label for="roleId">岗位角色</label><select id="roleId">${roles.filter((item) => item.status === "active").map((item) => `<option value="${item.id}" ${item.id === user?.role_id ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select></div><div class="form-field"><label for="userStatus">账号状态</label><select id="userStatus"><option value="active" ${user?.status !== "disabled" ? "selected" : ""}>启用</option><option value="disabled" ${user?.status === "disabled" ? "selected" : ""}>停用</option></select></div><div class="form-field"><label for="newPassword">${creating ? "初始密码" : "重置密码（留空不改）"}</label><input id="newPassword" type="password" ${creating ? "required" : ""} minlength="10" autocomplete="new-password" /></div><p class="inline-error" hidden></p>`;
  let busy = false;
  components.modal({ title: creating ? "新建平台账号" : "编辑平台账号", content: form, actions: [
    { label: "取消", onClick: ({ close }) => close() },
    { label: "保存", kind: "primary", onClick: async ({ close }) => {
      if (busy) return; busy = true;
      const errorNode = form.querySelector(".inline-error"); errorNode.hidden = true;
      const body = { display_name: form.querySelector("#displayName").value.trim(), role_id: Number(form.querySelector("#roleId").value), status: form.querySelector("#userStatus").value };
      const password = form.querySelector("#newPassword").value;
      if (password) body.password = password;
      if (creating) body.username = form.querySelector("#accountName").value.trim(); else body.version = user.version;
      try {
        if (creating) await api.post("/api/users", body); else await api.patch(`/api/users/${user.id}`, body);
        close(); components.toast(creating ? "账号已创建" : "账号已更新"); await refresh();
      } catch (error) { errorNode.textContent = error.message; errorNode.hidden = false; busy = false; }
    } },
  ] });
}

export async function renderUsers({ api, components, workspace, page, pageHeader, permissions, navigate, isCurrent }) {
  const [users, roles] = await Promise.all([api.get("/api/users"), api.get("/api/roles")]);
  if (!isCurrent()) return false;
  const canManage = permissions.has("users.manage");
  const columns = [
    { key: "username", label: "登录账号" }, { key: "display_name", label: "姓名" },
    { key: "role_code", label: "岗位编码" }, { key: "status", label: "状态", render: (value) => components.statusTag(value) },
    { key: "version", label: "版本" },
    ...(canManage ? [{ key: "action", label: "操作", render: (_value, row) => components.trusted(`<button type="button" class="button mini secondary" data-edit-user="${row.id}">编辑</button>`) }] : []),
  ];
  const refresh = () => navigate("users", { refresh: Date.now() });
  workspace.innerHTML = `${pageHeader(page, canManage ? '<button id="createUser" class="button primary" type="button">新建账号</button>' : "")}<section class="metric-grid"><article class="metric-card primary"><p>平台账号</p><strong>${users.length}<small>个</small></strong><span>启用 ${users.filter((item) => item.status === "active").length} 个</span></article><article class="metric-card cyan"><p>岗位角色</p><strong>${roles.length}<small>类</small></strong><span>最小权限分配</span></article></section><section class="panel list-panel"><header><div><p class="eyebrow">IDENTITY ACCESS</p><h2>用户账号台账</h2></div><span>所有变更写入审计日志</span></header><div class="panel-body">${components.table(columns, users)}</div></section>`;
  workspace.querySelector("#createUser")?.addEventListener("click", () => userModal({ api, components, roles, refresh }));
  workspace.querySelectorAll("[data-edit-user]").forEach((button) => button.addEventListener("click", () => userModal({ api, components, user: users.find((item) => String(item.id) === button.dataset.editUser), roles, refresh })));
  return true;
}
