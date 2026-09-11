import { escapeHtml } from "../components.js";

const COMMAND_LABELS = { set_intermittent_cycle: "设置间抽周期", rollback_cycle: "回退间抽周期" };

function detailText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 0); } catch (_error) { return String(value); }
}

export async function renderDeviceCommand({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  let commandId = params.id;
  if (!commandId) {
    const query = new URLSearchParams({ page_size: "1" });
    if (params.device_id) query.set("device_id", params.device_id);
    const list = await api.get(`/api/commands?${query}`);
    commandId = list.items[0]?.id;
    if (!commandId) { workspace.innerHTML = `${pageHeader(page)}${components.empty("暂无控制指令")}`; return true; }
  }
  const command = await api.get(`/api/commands/${encodeURIComponent(commandId)}`);
  if (!isCurrent()) return false;
  const receiptColumns = [{ key: "attempt_number", label: "阶段/次数" }, { key: "state", label: "回执状态", render: (value) => components.statusTag(value) }, { key: "receipt_at", label: "回执时间", render: (value) => format.dateTime(value) }, { key: "status", label: "处理结果", render: (value) => components.statusTag(value) }, { key: "detail", label: "结果说明", render: (value) => detailText(value) }];
  const payload = command.payload || {};
  const isSigned = Boolean(command.signature && command.idempotency_key && command.issued_at && command.expires_at);
  const traceTitle = isSigned ? "签名指令载荷" : "历史协议指令";
  const traceEyebrow = isSigned ? "SIGNED PAYLOAD" : "LEGACY PROTOCOL RECORD";
  workspace.innerHTML = `${pageHeader(page, `<button id="backCommand" class="button secondary" type="button">返回终端</button>`)}<section class="detail-hero panel"><div><span class="object-code">${escapeHtml(command.id)}</span><h2>${escapeHtml(COMMAND_LABELS[command.command_type] || command.command_type)}</h2><p>终端 ${escapeHtml(command.device_id)} · 策略 ${escapeHtml(command.strategy_id || "—")}</p></div><dl><div><dt>执行状态</dt><dd>${components.statusTag(command.status).html}</dd></div><div><dt>尝试次数</dt><dd>${escapeHtml(command.attempt_count)} / ${escapeHtml(command.max_attempts)}</dd></div><div><dt>下发时间</dt><dd>${escapeHtml(format.dateTime(command.issued_at))}</dd></div><div><dt>完成时间</dt><dd>${escapeHtml(format.dateTime(command.completed_at))}</dd></div></dl></section><section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">${traceEyebrow}</p><h2>${traceTitle}</h2></div></header>${isSigned ? "" : '<div class="inline-note">该记录来自早期通信协议，仅保留原始执行结果；新下发指令均采用签名与幂等校验。</div>'}<dl><div><dt>开井时长</dt><dd>${escapeHtml(payload.on_minutes ?? "—")} 分钟</dd></div><div><dt>关井时长</dt><dd>${escapeHtml(payload.off_minutes ?? "—")} 分钟</dd></div><div><dt>过期时间</dt><dd>${escapeHtml(format.dateTime(command.expires_at))}</dd></div><div><dt>幂等键</dt><dd class="wrap">${escapeHtml(command.idempotency_key || "—")}</dd></div><div><dt>数字签名</dt><dd class="wrap mono">${escapeHtml(command.signature || "—")}</dd></div></dl></article><article class="panel wide"><header><div><p class="eyebrow">RECEIPT TIMELINE</p><h2>分阶段回执</h2></div><span>失败原因 ${escapeHtml(command.failure_reason || "无")}</span></header><div class="panel-body">${components.table(receiptColumns, command.receipts || [])}</div></article></section>`;
  workspace.querySelector("#backCommand").addEventListener("click", () => navigate("devices", { id: command.device_id }));
  return true;
}
