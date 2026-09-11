const STATUS_LABELS = {
  active: "运行中", running: "运行中", online: "在线", success: "成功",
  intermittent: "间抽", maintenance: "维护", completed: "待复核", closed: "已闭环",
  resolved: "已解决", repaired: "已修复", effective: "已生效", approved: "已批准",
  warning: "预警", pending: "待处理", submitted: "待审批", pending_approval: "待审批",
  queued: "待发送", sent: "已发送", succeeded: "成功", accepted: "已接收",
  passed: "通过", received: "已接收", validated: "校验通过", executing: "执行中",
  acknowledged: "已确认", assigned: "已派发", dispatched: "已派发", in_progress: "处理中",
  new: "未确认", open: "待处理", unprocessed: "待分析", rolled_back: "已回滚",
  failed: "失败", fault: "故障", offline: "离线", stopped: "停机",
};
const TRUSTED_HTML = Symbol("trustedHtml");

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function asNode(content) {
  if (content instanceof Node) return content;
  const wrapper = document.createElement("div");
  if (content?.[TRUSTED_HTML]) wrapper.innerHTML = content.html;
  else wrapper.textContent = String(content ?? "");
  return wrapper;
}

function cellHtml(content) {
  return content?.[TRUSTED_HTML] ? content.html : escapeHtml(content ?? "—");
}

export const components = {
  trusted(html) {
    return Object.freeze({ [TRUSTED_HTML]: true, html: String(html) });
  },

  table(columns, rows, { emptyText = "暂无符合条件的记录", rowKey = "id" } = {}) {
    if (!rows?.length) return this.empty(emptyText);
    const head = columns.map((column) => `<th scope="col">${escapeHtml(column.label)}</th>`).join("");
    const body = rows.map((row) => {
      const cells = columns.map((column) => {
        const value = column.render ? column.render(row[column.key], row) : row[column.key];
        return `<td>${cellHtml(value)}</td>`;
      }).join("");
      return `<tr data-row-key="${escapeHtml(row[rowKey] ?? "")}">${cells}</tr>`;
    }).join("");
    return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  },

  pagination({ page = 1, pages = 1, total = 0 } = {}) {
    return `<nav class="pagination" aria-label="分页">
      <span>共 ${escapeHtml(total)} 条</span>
      <button type="button" data-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
      <span>第 ${escapeHtml(page)} / ${escapeHtml(Math.max(1, pages))} 页</span>
      <button type="button" data-page="${page + 1}" ${page >= pages ? "disabled" : ""}>下一页</button>
    </nav>`;
  },

  modal({ title, content, actions = [] }) {
    const previousFocus = document.activeElement;
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `<section class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <header><h2 id="modalTitle">${escapeHtml(title)}</h2><button class="icon-close" type="button" aria-label="关闭">×</button></header>
      <div class="modal-content"></div><footer class="modal-actions"></footer>
    </section>`;
    overlay.querySelector(".modal-content").append(asNode(content));
    const close = () => {
      const app = document.querySelector("#app");
      if (app) app.inert = false;
      overlay.remove();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
    actions.forEach((action) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `button ${action.kind || "secondary"}`;
      button.textContent = action.label;
      button.addEventListener("click", () => action.onClick?.({ close, overlay }));
      overlay.querySelector(".modal-actions").append(button);
    });
    overlay.querySelector(".icon-close").addEventListener("click", close);
    overlay.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
      if (event.key === "Tab") {
        const focusable = [...overlay.querySelectorAll(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )];
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });
    const app = document.querySelector("#app");
    if (app) app.inert = true;
    document.body.append(overlay);
    overlay.querySelector("button")?.focus();
    return { close, element: overlay };
  },

  closeAllModals() {
    document.querySelectorAll(".modal-overlay").forEach((overlay) => overlay.remove());
    const app = document.querySelector("#app");
    if (app) app.inert = false;
  },

  toast(message, kind = "success") {
    const region = document.querySelector("#toastRegion");
    if (!region) return;
    const item = document.createElement("div");
    item.className = `toast ${kind}`;
    item.setAttribute("role", kind === "error" ? "alert" : "status");
    item.textContent = message;
    region.append(item);
    window.setTimeout(() => item.remove(), 4_500);
  },

  formField({ id, label, type = "text", value = "", required = false, hint = "" }) {
    return `<div class="form-field"><label for="${escapeHtml(id)}">${escapeHtml(label)}</label>
      <input id="${escapeHtml(id)}" name="${escapeHtml(id)}" type="${escapeHtml(type)}"
        value="${escapeHtml(value)}" ${required ? "required" : ""} ${hint ? `aria-describedby="${escapeHtml(id)}Hint"` : ""} />
      ${hint ? `<small id="${escapeHtml(id)}Hint">${escapeHtml(hint)}</small>` : ""}</div>`;
  },

  statusTag(status, label) {
    const safeStatus = String(status || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "");
    return this.trusted(`<span class="status-tag ${safeStatus}">${escapeHtml(label || STATUS_LABELS[safeStatus] || status || "未知")}</span>`);
  },

  skeleton(lines = 4) {
    return `<div class="skeleton" role="status" aria-label="正在加载">${Array.from(
      { length: Math.max(1, lines) }, (_, index) => `<span style="--line:${index}"></span>`,
    ).join("")}</div>`;
  },

  errorPanel(error, { retry } = {}) {
    const status = Number(error?.status || 0);
    let title = "数据加载失败";
    let message = error?.message || "请稍后重试";
    if (status === 403) {
      title = "当前账号无此权限";
      message = "可切换账号，或联系管理员调整岗位权限。";
    } else if (status === 409) {
      title = "数据已被其他操作更新";
      message = "请刷新后核对最新版本，再重新提交。";
    } else if (error?.code === "network_error" || error?.code === "timeout") {
      title = "本机服务暂不可用";
      message = "请确认服务已启动，随后重试。";
    }
    return `<section class="state-panel error" role="alert"><span class="state-icon">!</span>
      <div><h2>${escapeHtml(title)}</h2><p>${escapeHtml(message)}</p>
      ${retry ? '<button type="button" class="button primary" data-action="retry">重新加载</button>' : ""}</div></section>`;
  },

  empty(message = "暂无记录") {
    return `<div class="state-panel empty"><span class="state-icon">○</span><p>${escapeHtml(message)}</p></div>`;
  },
};
