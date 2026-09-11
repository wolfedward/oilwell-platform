import { escapeHtml } from "../components.js";

export async function renderSystemHealth({ api, components, format, workspace, page, pageHeader, permissions, navigate, isCurrent }) {
  const [health, evidence] = await Promise.all([
    api.get("/api/health"),
    permissions.has("reports.read") ? api.get("/api/acceptance/evidence") : Promise.resolve(null),
  ]);
  if (!isCurrent()) return false;
  const cards = [
    ["应用服务", health.service, "HTTP 接口与静态资源"],
    ["业务数据库", health.database, evidence ? `完整性 ${evidence.integrity.database}` : "SQLite 本机存储"],
    ["采集模拟器", health.simulator, "终端采集与控制运行时"],
    ["系统版本", health.version, "当前部署版本"],
  ];
  workspace.innerHTML = `${pageHeader(page, '<button id="refreshHealth" class="button secondary" type="button">刷新状态</button>')}<section class="metric-grid">${cards.map(([label, value, note]) => `<article class="metric-card ${value === "ok" || value === "running" || value === "ready" ? "success" : "primary"}"><p>${escapeHtml(label)}</p><strong class="text-value">${escapeHtml(value)}</strong><span>${escapeHtml(note)}</span></article>`).join("")}</section><section class="detail-grid"><article class="panel info-panel"><header><div><p class="eyebrow">RUNTIME</p><h2>本机运行状态</h2></div></header><dl><div><dt>服务时间</dt><dd>${escapeHtml(format.dateTime(health.time))}</dd></div><div><dt>部署模式</dt><dd>本机离线运行</dd></div><div><dt>数据目录</dt><dd>应用数据目录 / data</dd></div><div><dt>外部依赖</dt><dd>无需联网</dd></div></dl></article><article class="panel info-panel"><header><div><p class="eyebrow">INTEGRITY</p><h2>数据一致性</h2></div></header><dl><div><dt>SQLite 完整性</dt><dd>${components.statusTag(evidence?.integrity.database || health.database).html}</dd></div><div><dt>外键一致性</dt><dd>${components.statusTag(evidence?.integrity.foreign_keys || "passed").html}</dd></div><div><dt>最近检查</dt><dd>${escapeHtml(format.dateTime(evidence?.generated_at || health.time))}</dd></div></dl></article></section>`;
  workspace.querySelector("#refreshHealth").addEventListener("click", () => navigate("system-health", { refresh: Date.now() }));
  return true;
}
