import { escapeHtml } from "../components.js";
import { renderDeviceDetail } from "./device-detail.js";

const DEVICE_STATUS = [["", "全部状态"], ["online", "在线"], ["offline", "离线"], ["fault", "故障"], ["maintenance", "维护"]];

function deviceQuery(state) {
  const query = new URLSearchParams({ page: String(state.page), page_size: "15" });
  for (const key of ["unit_id", "well_id", "status", "keyword"]) if (state[key]) query.set(key, state[key]);
  return query.toString();
}

export async function renderDeviceList({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  if (params.id) return renderDeviceDetail({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent });
  const state = { page: Math.max(1, Number(params.page) || 1), unit_id: params.unit_id || "", well_id: params.well_id || "", status: params.status || "", keyword: params.keyword || "" };
  const [catalog, result] = await Promise.all([api.get("/api/devices?page_size=100"), api.get(`/api/devices?${deviceQuery(state)}`)]);
  if (!isCurrent()) return false;
  const units = [...new Map(catalog.items.map((item) => [item.unit_id, item.unit_id])).values()].filter(Boolean);
  const columns = [
    { key: "id", label: "终端编号", render: (value) => components.trusted(`<button type="button" class="link-button" data-device-id="${escapeHtml(value)}">${escapeHtml(value)}</button>`) },
    { key: "serial_number", label: "设备序列号" }, { key: "device_type", label: "终端型号" }, { key: "firmware_version", label: "固件版本" },
    { key: "well_code", label: "关联井号" }, { key: "well_name", label: "油井名称" }, { key: "unit_id", label: "单位编号" },
    { key: "status", label: "运行状态", render: (value) => components.statusTag(value) },
    { key: "action", label: "操作", render: (_value, row) => components.trusted(`<button type="button" class="button mini secondary" data-device-id="${escapeHtml(row.id)}">运行详情</button>`) },
  ];
  workspace.innerHTML = `${pageHeader(page)}<section class="panel filter-panel"><form id="deviceFilters" class="filter-grid device-filters">
    <div class="form-field"><label for="deviceUnit">单位</label><select id="deviceUnit" name="unit_id"><option value="">全部单位</option>${units.map((value) => `<option value="${escapeHtml(value)}" ${value === state.unit_id ? "selected" : ""}>${escapeHtml(value)}</option>`).join("")}</select></div>
    <div class="form-field"><label for="deviceStatus">设备状态</label><select id="deviceStatus" name="status">${DEVICE_STATUS.map(([value, label]) => `<option value="${value}" ${value === state.status ? "selected" : ""}>${label}</option>`).join("")}</select></div>
    <div class="form-field keyword"><label for="deviceKeyword">关键字</label><input id="deviceKeyword" name="keyword" value="${escapeHtml(state.keyword)}" placeholder="终端号、序列号、井号" /></div>
    <div class="filter-actions"><button type="submit" class="button primary">查询</button><button type="button" id="resetDeviceFilters" class="button secondary">重置</button></div>
  </form></section><section class="panel list-panel"><header><div><p class="eyebrow">CONTROL TERMINALS</p><h2>测控终端台账</h2></div><span>共 ${escapeHtml(result.total)} 台</span></header><div class="panel-body">${components.table(columns, result.items)}
    <nav class="pagination"><span>共 ${escapeHtml(result.total)} 条</span><button type="button" data-list-page="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${state.page} 页</span><button type="button" data-list-page="${state.page + 1}" ${(state.page * 15) >= result.total ? "disabled" : ""}>下一页</button></nav></div></section>`;
  const collect = () => Object.fromEntries(new FormData(workspace.querySelector("#deviceFilters")));
  workspace.querySelector("#deviceFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("devices", { ...collect(), page: "1" }); });
  workspace.querySelector("#resetDeviceFilters").addEventListener("click", () => navigate("devices", {}));
  workspace.querySelectorAll("[data-list-page]").forEach((button) => button.addEventListener("click", () => navigate("devices", { ...state, page: button.dataset.listPage })));
  workspace.querySelectorAll("[data-device-id]").forEach((button) => button.addEventListener("click", () => navigate("devices", { id: button.dataset.deviceId })));
  return true;
}

export { renderDeviceDetail } from "./device-detail.js";
