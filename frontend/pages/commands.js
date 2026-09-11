import { escapeHtml } from "../components.js";

const STAGES = ["sent", "received", "validated", "executing", "success"];
const STAGE_LABEL = { sent: "已发送", received: "终端接收", validated: "安全校验", executing: "执行写入", success: "执行成功", failed: "执行失败" };

function commandQuery(params) {
  const query = new URLSearchParams({ page: String(Math.max(1, Number(params.page) || 1)), page_size: "20" });
  for (const key of ["device_id", "strategy_id", "state"]) if (params[key]) query.set(key, params[key]);
  return query.toString();
}

function safetyChecks(command) {
  const payload = command.payload || {};
  return [
    ["数字签名", typeof command.signature === "string" && command.signature.length === 64],
    ["幂等标识", Boolean(command.idempotency_key)],
    ["有效期", Boolean(command.issued_at && command.expires_at && new Date(command.expires_at) > new Date(command.issued_at))],
    ["版本绑定", Boolean(command.strategy_version_id && payload.strategy_version_id === command.strategy_version_id)],
  ];
}

export async function renderCommands({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const result = await api.get(`/api/commands?${commandQuery(params)}`);
  const commandId = params.id || result.items[0]?.id;
  const command = commandId ? await api.get(`/api/commands/${encodeURIComponent(commandId)}`) : null;
  if (!isCurrent()) return false;
  const canControl = permissions.has("devices.control");
  const columns = [
    { key: "id", label: "指令编号", render: (value) => components.trusted(`<button type="button" class="link-button" data-command-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "device_id", label: "目标终端" }, { key: "strategy_id", label: "关联策略" },
    { key: "command_type", label: "指令类型" }, { key: "attempt_count", label: "尝试次数" },
    { key: "state", label: "状态", render: (value) => components.statusTag(value) },
    { key: "created_at", label: "创建时间", render: (value) => format.dateTime(value) },
  ];
  const receipts = command?.receipts || [];
  const receivedStates = new Set(receipts.map((item) => item.state));
  workspace.innerHTML = `${pageHeader(page, canControl ? '<button type="button" id="processCommands" class="button primary">处理待发送指令</button>' : "")}
    <section class="panel filter-panel"><form id="commandFilters" class="filter-grid"><div class="form-field"><label for="commandDevice">终端编号</label><input id="commandDevice" name="device_id" value="${escapeHtml(params.device_id || "")}" /></div><div class="form-field"><label for="commandState">执行状态</label><select id="commandState" name="state"><option value="">全部状态</option>${["queued","sent","received","validated","executing","success","failed"].map((value) => `<option value="${value}" ${params.state === value ? "selected" : ""}>${escapeHtml(STAGE_LABEL[value] || value)}</option>`).join("")}</select></div><div class="filter-actions"><button class="button secondary" type="submit">查询</button></div></form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">SIGNED COMMAND LEDGER</p><h2>控制指令台账</h2></div><span>共 ${escapeHtml(result.total)} 条</span></header><div class="panel-body">${components.table(columns, result.items, { emptyText: "暂无控制指令" })}</div></section>
    ${command ? `<section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">SIGNED PAYLOAD</p><h2>签名载荷与有效期</h2></div>${components.statusTag(command.state).html}</header><dl><div><dt>指令编号</dt><dd class="wrap">${escapeHtml(command.id)}</dd></div><div><dt>目标终端</dt><dd>${escapeHtml(command.device_id)}</dd></div><div><dt>开 / 关井</dt><dd>${escapeHtml(command.payload?.on_minutes ?? "—")} / ${escapeHtml(command.payload?.off_minutes ?? "—")} min</dd></div><div><dt>签发时间</dt><dd>${escapeHtml(format.dateTime(command.issued_at))}</dd></div><div><dt>有效期</dt><dd>${escapeHtml(format.dateTime(command.expires_at))}</dd></div><div><dt>数字签名</dt><dd class="wrap mono">${escapeHtml(command.signature || "历史记录未签名")}</dd></div></dl><h3>安全校验</h3><ul class="check-list">${safetyChecks(command).map(([label, ok]) => `<li class="${ok ? "passed" : "failed"}"><b>${ok ? "✓" : "!"}</b>${escapeHtml(label)}<span>${ok ? "通过" : "不完整"}</span></li>`).join("")}</ul></article>
      <article class="panel wide"><header><div><p class="eyebrow">RECEIPT STREAM</p><h2>分阶段回执</h2></div><button type="button" id="refreshCommand" class="button secondary">刷新回执</button></header><div class="panel-body"><ol class="trace-timeline">${STAGES.map((stage) => { const receipt = receipts.find((item) => item.state === stage); return `<li class="${receivedStates.has(stage) ? "complete" : "pending"}"><i></i><div><strong>${escapeHtml(STAGE_LABEL[stage])}</strong><span>${escapeHtml(format.dateTime(receipt?.receipt_at))}</span><p>${escapeHtml(receipt?.detail?.reason || receipt?.status || "等待终端回执")}</p></div></li>`; }).join("")}${command.state === "failed" ? `<li class="failed"><i></i><div><strong>执行失败</strong><p>${escapeHtml(command.failure_reason || "终端未返回成功回执")}</p></div></li>` : ""}</ol></div></article></section>` : ""}`;
  workspace.querySelector("#commandFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("commands", Object.fromEntries(new FormData(event.currentTarget))); });
  workspace.querySelectorAll("[data-command-id]").forEach((button) => button.addEventListener("click", () => navigate("commands", { ...params, id: button.dataset.commandId })));
  workspace.querySelector("#refreshCommand")?.addEventListener("click", () => navigate("commands", { id: command.id }));
  workspace.querySelector("#processCommands")?.addEventListener("click", async () => {
    try { const summary = await api.post("/api/commands/process", {}); components.toast(`处理完成：成功 ${summary.success || 0}，失败 ${summary.failed || 0}`); await navigate("commands", command ? { id: command.id } : {}); }
    catch (error) { components.toast(error.message || "指令处理失败", "error"); }
  });
  return true;
}
