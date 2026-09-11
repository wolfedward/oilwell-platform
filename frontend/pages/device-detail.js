import { escapeHtml } from "../components.js";

const SCENARIO_LABELS = { normal: "正常采集", supply_shortage: "供液不足", pump_leak: "泵漏失", empty_pump: "空抽", sensor_drift: "传感器漂移", packet_loss: "通信丢包", offline: "终端离线" };
const REGISTER_LABELS = { current: "电流", voltage: "电压", active_power: "有功功率", power_factor: "功率因数", frequency: "电源频率", stroke: "冲程", strokes_per_minute: "冲次", fillage: "充满度" };

function durationLabel(seconds, format) {
  const value = Number(seconds);
  if (!Number.isFinite(value)) return "—";
  if (value < 60) return `${value} 秒前`;
  if (value < 3600) return `${format.number(value / 60, 0)} 分钟前`;
  return `${format.number(value / 3600, 1)} 小时前`;
}

function safeDetail(value) {
  try {
    const parsed = typeof value === "string" ? JSON.parse(value) : value;
    if (!parsed || typeof parsed !== "object") return String(value || "—");
    return Object.entries(parsed).slice(0, 4).map(([key, item]) => `${key}: ${typeof item === "object" ? JSON.stringify(item) : item}`).join("；");
  } catch (_error) { return String(value || "—"); }
}

export async function renderDeviceDetail({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  let deviceId = params.id;
  if (!deviceId) {
    const list = await api.get("/api/devices?page_size=1");
    deviceId = list.items[0]?.id;
    if (!deviceId) { workspace.innerHTML = `${pageHeader(page)}${components.empty("测控终端台账暂无记录")}`; return true; }
  }
  const [device, registerData] = await Promise.all([api.get(`/api/devices/${encodeURIComponent(deviceId)}`), api.get(`/api/devices/${encodeURIComponent(deviceId)}/registers`)]);
  if (!isCurrent()) return false;
  const heartbeat = device.latest_heartbeat;
  const registers = Object.entries(registerData.registers || {}).map(([code, value]) => ({ code, name: REGISTER_LABELS[code] || code, value }));
  const selfChecks = (device.recent_events || []).filter((item) => String(item.event_type).includes("self_check"));
  const channelColumns = [{ key: "channel_code", label: "通道" }, { key: "name", label: "名称" }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }, { key: "sample_interval_ms", label: "采样周期", render: (value) => `${format.number(value)} ms` }, { key: "scale", label: "倍率", render: (value) => format.number(value, 3) }, { key: "offset", label: "偏移", render: (value) => format.number(value, 3) }];
  const messageColumns = [{ key: "id", label: "报文ID" }, { key: "frame_kind", label: "报文类型" }, { key: "sequence_text", label: "序号" }, { key: "reported_at", label: "终端时标", render: (value) => format.dateTime(value) }, { key: "status", label: "接收状态", render: (value) => components.statusTag(value) }];
  const commandColumns = [{ key: "id", label: "指令编号", render: (value) => components.trusted(`<button class="link-button" type="button" data-page="device-command" data-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) }, { key: "command_type", label: "指令类型" }, { key: "issued_at", label: "下发时间", render: (value) => format.dateTime(value) }, { key: "attempt_count", label: "尝试次数" }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }];
  const registerColumns = [{ key: "code", label: "寄存器" }, { key: "name", label: "参数名称" }, { key: "value", label: "实时值", render: (value) => typeof value === "number" ? format.number(value, 3) : String(value) }];
  const eventColumns = [{ key: "event_type", label: "事件类型" }, { key: "detail", label: "记录内容", render: (value) => safeDetail(value) }, { key: "created_at", label: "发生时间", render: (value) => format.dateTime(value) }, { key: "status", label: "状态", render: (value) => components.statusTag(value) }];
  workspace.innerHTML = `${pageHeader(page, `<div class="header-actions"><button id="backDevices" class="button secondary" type="button">返回终端台账</button>${device.capabilities.scenario_control ? '<button id="scenarioButton" class="button primary" type="button">运行场景</button>' : ""}</div>`)}
    <section class="detail-hero panel"><div><span class="object-code">${escapeHtml(device.id)}</span><h2>${escapeHtml(device.device_type)}</h2><p>${escapeHtml(device.serial_number)} · 关联 ${escapeHtml(device.well_code || "未关联油井")}</p></div><dl><div><dt>设备状态</dt><dd>${components.statusTag(device.status).html}</dd></div><div><dt>固件版本</dt><dd>${escapeHtml(device.firmware_version)}</dd></div><div><dt>通信方式</dt><dd>本机协议接入</dd></div><div><dt>当前场景</dt><dd>${escapeHtml(SCENARIO_LABELS[device.scenario?.name] || device.scenario?.name || "正常采集")}</dd></div></dl></section>
    <section class="metric-grid terminal-metrics"><article class="metric-card ${heartbeat?.stale ? "danger" : "success"}"><p>心跳状态</p><strong class="text-value">${heartbeat ? (heartbeat.stale ? "已超时" : "正常") : "无记录"}</strong><span>${escapeHtml(durationLabel(heartbeat?.age_seconds, format))}</span></article><article class="metric-card primary"><p>最后心跳</p><strong class="text-value small">${escapeHtml(format.dateTime(heartbeat?.reported_at))}</strong><span>超时阈值 ${device.capabilities.heartbeat_stale_seconds} 秒</span></article><article class="metric-card cyan"><p>采集通道</p><strong>${device.channels.length}<small>路</small></strong><span>${device.channels.filter((item) => item.enabled).length} 路启用</span></article><article class="metric-card purple"><p>实时寄存器</p><strong>${registers.length}<small>项</small></strong><span>终端状态 ${escapeHtml(registerData.state)}</span></article><article class="metric-card warning"><p>通信报文</p><strong>${device.recent_messages.length}<small>条</small></strong><span>最近运行记录</span></article><article class="metric-card primary"><p>控制指令</p><strong>${device.recent_commands.length}<small>条</small></strong><span>可追溯签名与状态</span></article></section>
    <section class="device-sections">
      <article class="panel wide-section"><header><div><p class="eyebrow">ACQUISITION CHANNELS</p><h2>采集通道</h2></div><span>参数变更采用版本校验</span></header><div class="panel-body">${components.table(channelColumns, device.channels)}</div></article>
      <article class="panel"><header><div><p class="eyebrow">LIVE REGISTERS</p><h2>实时寄存器</h2></div><span>${escapeHtml(format.dateTime(heartbeat?.reported_at))}</span></header><div class="panel-body">${components.table(registerColumns, registers)}</div></article>
      <article class="panel"><header><div><p class="eyebrow">SELF CHECK</p><h2>自检记录</h2></div><span>终端事件源</span></header><div class="panel-body">${components.table(eventColumns, selfChecks, { emptyText: "当前没有自检事件记录" })}</div></article>
      <article class="panel wide-section"><header><div><p class="eyebrow">MESSAGE TRACE</p><h2>通信报文</h2></div><button class="button mini secondary" type="button" data-page="acquisition" data-param-device-id="${escapeHtml(device.id)}">查看全部报文</button></header><div class="panel-body">${components.table(messageColumns, device.recent_messages)}</div></article>
      ${device.recent_commands.length ? `<article class="panel wide-section"><header><div><p class="eyebrow">COMMAND HISTORY</p><h2>控制指令</h2></div><button class="button mini secondary" type="button" data-page="commands" data-param-device-id="${escapeHtml(device.id)}">进入指令中心</button></header><div class="panel-body">${components.table(commandColumns, device.recent_commands)}</div></article>` : ""}
      <article class="panel wide-section"><header><div><p class="eyebrow">TERMINAL EVENTS</p><h2>终端事件</h2></div><span>场景变更与运行记录</span></header><div class="panel-body">${components.table(eventColumns, device.recent_events)}</div></article>
    </section>`;
  workspace.querySelector("#backDevices").addEventListener("click", () => navigate("devices", {}));
  workspace.querySelector("#scenarioButton")?.addEventListener("click", () => showScenarioModal({ api, components, device, navigate }));
  return true;
}

function showScenarioModal({ api, components, device, navigate }) {
  const content = document.createElement("form");
  content.className = "modal-form";
  const allowed = device.capabilities?.allowed_scenarios || [];
  const selected = allowed.includes(device.scenario?.name) ? device.scenario.name : "normal";
  content.innerHTML = `<div class="inline-note">运行场景会驱动该终端生成对应的采集、功图与质量记录，所有操作写入审计日志。</div><div class="form-field"><label for="scenarioName">运行场景</label><select id="scenarioName">${allowed.map((name) => `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(SCENARIO_LABELS[name] || name)}</option>`).join("")}</select></div><div class="form-field"><label for="scenarioDuration">持续时长（分钟）</label><input id="scenarioDuration" type="number" min="1" max="${escapeHtml(device.capabilities.maximum_scenario_minutes)}" value="30" /></div><p class="inline-error" hidden></p>`;
  let busy = false;
  components.modal({ title: `${device.id} 运行场景`, content, actions: [{ label: "取消", kind: "secondary", onClick: ({ close }) => close() }, { label: "确认执行", kind: "primary", onClick: async ({ close }) => {
    if (busy) return;
    busy = true;
    const errorNode = content.querySelector(".inline-error"); errorNode.hidden = true;
    try {
      await api.post(`/api/devices/${encodeURIComponent(device.id)}/scenarios`, { name: content.querySelector("#scenarioName").value, duration_minutes: Number(content.querySelector("#scenarioDuration").value) });
      close(); components.toast("终端运行场景已生效"); navigate("devices", { id: device.id });
    } catch (error) { errorNode.textContent = error.message; errorNode.hidden = false; busy = false; }
  } }] });
}
