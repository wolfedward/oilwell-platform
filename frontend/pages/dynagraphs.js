import { escapeHtml } from "../components.js";

const STATUS_OPTIONS = [["", "全部状态"], ["unprocessed", "待分析"], ["analyzed", "已分析"], ["invalid", "不可分析"], ["available", "可用"]];
const CONDITION_LABELS = { normal: "正常", supply_shortage: "供液不足", intermittent: "间歇供液", pump_leak: "泵阀漏失", empty_pump: "抽空" };

function queryFor(state) {
  const query = new URLSearchParams({ page: String(state.page), page_size: "20" });
  if (state.well_id) query.set("well_id", state.well_id);
  if (state.status) query.set("status", state.status);
  return query.toString();
}

export async function renderDynagraphList({ api, components, format, workspace, page, pageHeader, params, permissions, navigate, isCurrent }) {
  const state = { page: Math.max(1, Number(params.page) || 1), well_id: params.well_id || "", status: params.status || "" };
  const result = await api.get(`/api/dynagraphs?${queryFor(state)}`);
  if (!isCurrent()) return false;
  const canAnalyze = permissions.has("diagnosis.run");
  const pageAnalyzed = result.items.filter((item) => item.status === "analyzed").length;
  const pageDiagnosed = result.items.filter((item) => item.latest_condition).length;
  const pageAbnormal = result.items.filter((item) => item.latest_condition && item.latest_condition !== "normal").length;
  const pageTraceable = result.items.filter((item) => item.raw_message_id || item.source_type).length;
  const columns = [
    { key: "id", label: "功图编号", render: (value) => components.trusted(`<button type="button" class="link-button" data-open-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "well_id", label: "井号" },
    { key: "well_name", label: "油井名称" },
    { key: "device_id", label: "采集终端" },
    { key: "captured_at", label: "采集时间", render: (value) => format.dateTime(value) },
    { key: "status", label: "分析状态", render: (value) => components.statusTag(value) },
    { key: "latest_condition", label: "最新诊断", render: (value, row) => value ? `${CONDITION_LABELS[value] || value} · ${format.percent(row.latest_confidence)}` : "尚未分析" },
    { key: "raw_message_id", label: "原始报文", render: (value) => value ? `#${value}` : "历史归档" },
    { key: "action", label: "操作", render: (_value, row) => components.trusted(`<div class="header-actions"><button type="button" class="button mini secondary" data-open-id="${escapeHtml(row.id)}">查看链路</button>${canAnalyze ? `<button type="button" class="button mini primary" data-analyze-id="${escapeHtml(row.id)}">${row.status === "analyzed" ? "重新分析" : "运行分析"}</button>` : ""}</div>`) },
  ];
  const pages = Math.max(1, Math.ceil(result.total / result.page_size));
  workspace.innerHTML = `${pageHeader(page)}<section class="archive-summary" aria-label="功图档案概览"><article><span>档案总量</span><strong>${escapeHtml(result.total)}</strong><small>当前筛选范围</small></article><article><span>已完成分析</span><strong>${pageAnalyzed}</strong><small>当前页 ${result.items.length} 份</small></article><article><span>已有诊断</span><strong>${pageDiagnosed}</strong><small>异常工况 ${pageAbnormal} 份</small></article><article><span>来源可追溯</span><strong>${pageTraceable}</strong><small>原始报文 / 归档来源</small></article></section><section class="panel filter-panel"><form id="dynagraphFilters" class="filter-grid device-filters">
    <div class="form-field"><label for="dynagraphWell">井号</label><input id="dynagraphWell" name="well_id" value="${escapeHtml(state.well_id)}" placeholder="例如 W-001" /></div>
    <div class="form-field"><label for="dynagraphStatus">分析状态</label><select id="dynagraphStatus" name="status">${STATUS_OPTIONS.map(([value, label]) => `<option value="${value}" ${state.status === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="inline-note">档案直接关联采集终端、原始报文、模型运行和诊断结果。</div>
    <div class="filter-actions"><button class="button primary" type="submit">查询</button><button id="resetDynagraph" class="button secondary" type="button">重置</button></div>
  </form></section><section class="panel list-panel"><header><div><p class="eyebrow">DYNAGRAPH ARCHIVE</p><h2>电功图与示功图档案</h2></div><span>共 ${escapeHtml(result.total)} 份</span></header><div class="panel-body">${components.table(columns, result.items)}${components.pagination({ page: result.page, pages, total: result.total })}</div></section>`;
  const values = () => Object.fromEntries(new FormData(workspace.querySelector("#dynagraphFilters")));
  workspace.querySelector("#dynagraphFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("dynagraphs", { ...values(), page: "1" }); });
  workspace.querySelector("#resetDynagraph").addEventListener("click", () => navigate("dynagraphs", {}));
  workspace.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => navigate("dynagraphs", { ...state, page: button.dataset.page })));
  workspace.querySelectorAll("[data-open-id]").forEach((button) => button.addEventListener("click", () => navigate("dynagraph-analysis", { id: button.dataset.openId })));
  workspace.querySelectorAll("[data-analyze-id]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "分析计算中…";
    try {
      await api.post(`/api/dynagraphs/${encodeURIComponent(button.dataset.analyzeId)}/analyze`, {});
      components.toast("功图转换、诊断和产液量计算已完成");
      navigate("dynagraph-analysis", { id: button.dataset.analyzeId });
    } catch (error) {
      components.toast(error.message || "分析运行失败", "error");
      button.disabled = false;
      button.textContent = "重新运行";
    }
  }));
  return true;
}
