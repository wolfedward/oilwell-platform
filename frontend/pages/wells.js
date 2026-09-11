import { escapeHtml } from "../components.js";

const STATUS_OPTIONS = [
  ["", "全部状态"], ["operating", "当前开井"], ["running", "运行中"], ["intermittent", "间抽"],
  ["stopped", "停机"], ["maintenance", "维护"], ["offline", "离线"],
];

function options(items, selected, placeholder) {
  return [`<option value="">${escapeHtml(placeholder)}</option>`, ...items.map((item) =>
    `<option value="${escapeHtml(item.id)}" ${String(item.id) === String(selected || "") ? "selected" : ""}>${escapeHtml(item.name)}</option>`
  )].join("");
}

function queryFrom(state, page = state.page || 1, includePage = true) {
  const query = new URLSearchParams();
  for (const key of ["unit_id", "group_id", "status", "low_output", "keyword"]) {
    if (state[key]) query.set(key, state[key]);
  }
  if (includePage) {
    query.set("page", String(page));
    query.set("page_size", "15");
  }
  return query.toString();
}

export async function renderWellList({ api, components, format, workspace, page, pageHeader, params, navigate, isCurrent }) {
  const state = { page: Math.max(1, Number(params.page) || 1), unit_id: params.unit_id || "", group_id: params.group_id || "", status: params.status || "", low_output: params.low_output || "", keyword: params.keyword || "" };
  const [catalog, result] = await Promise.all([
    api.get("/api/wells?page=1&page_size=100"),
    api.get(`/api/wells?${queryFrom(state)}`),
  ]);
  if (!isCurrent()) return false;
  const units = [...new Map(catalog.items.map((item) => [item.unit.id, item.unit])).values()];
  const allGroups = [...new Map(catalog.items.map((item) => [item.group.id, { ...item.group, unit_id: item.unit.id }])).values()];
  const groups = state.unit_id ? allGroups.filter((item) => item.unit_id === state.unit_id) : allGroups;
  const columns = [
    { key: "code", label: "井号", render: (value, row) => components.trusted(`<button class="link-button" type="button" data-page="well-detail" data-id="${escapeHtml(row.id)}" data-param-return-state="${escapeHtml(JSON.stringify(state))}">${escapeHtml(value)}</button>`) },
    { key: "name", label: "油井名称" },
    { key: "unit", label: "单位", render: (value) => value?.name },
    { key: "group", label: "井组", render: (value) => value?.name },
    { key: "status", label: "运行状态", render: (value) => components.statusTag(value) },
    { key: "low_output", label: "低产井", render: (value) => value ? components.trusted('<span class="flag warning">是</span>') : "否" },
    { key: "latest_production", label: "日产液(m³)", render: (value) => format.number(value?.daily_fluid, 1) },
    { key: "latest_diagnosis", label: "最新诊断", render: (value) => value?.diagnosis_type || "未诊断" },
    { key: "active_alarm_count", label: "活动报警", render: (value) => Number(value) ? components.trusted(`<span class="alarm-count">${escapeHtml(value)}</span>`) : "0" },
    { key: "action", label: "操作", render: (_value, row) => components.trusted(`<button class="button mini secondary" type="button" data-page="well-detail" data-id="${escapeHtml(row.id)}" data-param-return-state="${escapeHtml(JSON.stringify(state))}">进入工作台</button>`) },
  ];
  workspace.innerHTML = `${pageHeader(page, `<button id="exportWells" class="button secondary" type="button">导出当前结果</button>`)}
    <section class="panel filter-panel"><form id="wellFilters" class="filter-grid">
      <div class="form-field"><label for="wellUnit">单位</label><select id="wellUnit" name="unit_id">${options(units, state.unit_id, "全部单位")}</select></div>
      <div class="form-field"><label for="wellGroup">井组</label><select id="wellGroup" name="group_id">${options(groups, state.group_id, "全部井组")}</select></div>
      <div class="form-field"><label for="wellStatus">运行状态</label><select id="wellStatus" name="status">${STATUS_OPTIONS.map(([value, label]) => `<option value="${value}" ${value === state.status ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="form-field"><label for="wellLowOutput">低产井</label><select id="wellLowOutput" name="low_output"><option value="">全部</option><option value="1" ${state.low_output === "1" ? "selected" : ""}>仅低产井</option><option value="0" ${state.low_output === "0" ? "selected" : ""}>非低产井</option></select></div>
      <div class="form-field keyword"><label for="wellKeyword">关键字</label><input id="wellKeyword" name="keyword" value="${escapeHtml(state.keyword)}" placeholder="井号或油井名称" /></div>
      <div class="filter-actions"><button class="button primary" type="submit">查询</button><button id="resetWellFilters" class="button secondary" type="button">重置</button></div>
    </form></section>
    <section class="panel list-panel"><header><div><p class="eyebrow">WELL LEDGER</p><h2>油井台账</h2></div><span>共 ${escapeHtml(result.total)} 口</span></header>
      <div class="panel-body">${components.table(columns, result.items)}
        <nav class="pagination" aria-label="分页"><span>共 ${escapeHtml(result.total)} 条</span><button type="button" data-list-page="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""}>上一页</button><span>第 ${state.page} / ${Math.max(1, result.pages)} 页</span><button type="button" data-list-page="${state.page + 1}" ${state.page >= result.pages ? "disabled" : ""}>下一页</button></nav>
      </div></section>`;
  const collect = () => Object.fromEntries(new FormData(workspace.querySelector("#wellFilters")));
  workspace.querySelector("#wellFilters").addEventListener("submit", (event) => { event.preventDefault(); navigate("wells", { ...collect(), page: "1" }); });
  workspace.querySelector("#resetWellFilters").addEventListener("click", () => navigate("wells", {}));
  workspace.querySelectorAll("[data-list-page]").forEach((button) => button.addEventListener("click", () => navigate("wells", { ...state, page: button.dataset.listPage })));
  workspace.querySelector("#wellUnit").addEventListener("change", () => navigate("wells", { ...collect(), group_id: "", page: "1" }));
  workspace.querySelector("#exportWells").addEventListener("click", async () => {
    try {
      await api.download(`/api/wells.csv?${queryFrom(state, 1, false)}`, `油井台账-${new Date().toISOString().slice(0, 10)}.csv`);
      components.toast("当前筛选结果已导出");
    } catch (error) { components.toast(error.message, "error"); }
  });
  return true;
}
