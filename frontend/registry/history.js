import {components, escapeHtml as esc} from "../components.js";
import {registryError, registryTime, registryPagination} from "./client.js";

const stateName = value => ({pending: "待核查登记", active: "台账已启用", suspended: "台账停用待复核", retired: "台账已退役"}[value] || value || "未匹配");
const timeColumn = (key, label) => ({key, label, render: value => components.trusted(`<time title="${esc(value || "开放区间")}">${esc(registryTime(value))}</time>`)});
const jsonColumn = (key, label) => ({key, label, render: value => components.trusted(`<pre class="registry-snapshot">${esc(JSON.stringify(value ?? null, null, 2))}</pre>`)});
const tabs = {state: ["状态历史", "state-history"], bindings: ["井绑定历史", "bindings"], assignments: ["期望点表历史", "point-map-assignments"], maps: ["点表版本", "point-maps"], audit: ["完整审计", "audit"]};

export function mountRegistryHistory({root, api, permissions, device, params = {}, isCurrent}) {
  let disposed = false, generation = 0, result = null, active = device ? "maps" : "resolution", filters = {};
  const current = () => !disposed && isCurrent() && root.isConnected;
  const allowed = name => permissions.has("registry.read") && (name === "resolution" ? permissions.has("telemetry.read") : name !== "audit" || permissions.has("audit.read"));
  if (!allowed(active)) return {dispose() { disposed = true; }};
  root.innerHTML = device ? `<div class="registry-actions" role="group" aria-label="登记历史">${Object.entries(tabs).filter(([key]) => allowed(key)).map(([key, [label]]) => `<button type="button" class="button secondary" data-action="history-${key}">${label}</button>`).join("")}</div>
    <form data-registry-audit-filter hidden class="registry-filters"><label>操作人编号<input name="actor_id"></label><label>动作<input name="action" placeholder="如 device.bind"></label>${timeInputs()}<button type="submit" class="button secondary">查询审计</button></form><div data-history-results></div>`
    : `<p class="registry-boundary">按测量时刻匹配历史归属；原始来源、质量码与匹配理由分别保留。未登记终端也可查询，不会自动登记。</p><form data-registry-resolution class="registry-filters">
      <label>核查方式<select name="mode"><option value="pair">网关＋终端</option><option value="well">生产井内部编号</option></select></label>
      <label>网关编号<input name="gateway_id" maxlength="128"></label><label>终端编号<input name="device_id" maxlength="128"></label>
      <label>井内部编号<input name="well_id" maxlength="36" disabled></label>${timeInputs()}
      <button type="submit" class="button primary">核查来源</button></form><p>默认近 24 小时，最长 31 天。时间输入使用含时区的 ISO 时间，如 2026-09-23T08:00:00+08:00。</p><div data-resolution-results>选择完整终端身份或井编号后查询。</div>`;
  const target = root.querySelector(device ? "[data-history-results]" : "[data-resolution-results]");
  function timeInputs() {
    return '<label>开始时间（含时区）<input name="from_time" placeholder="默认近 24 小时"></label><label>结束时间（含时区）<input name="to_time" placeholder="默认本次查询时刻"></label>';
  }
  function columns(name) {
    const period = [timeColumn("valid_from", "起始时间（北京时间）"), timeColumn("valid_to", "结束时间（北京时间）"), {key: "audit_event_id", label: "审计事件"}];
    if (name === "state") return [{key: "status", label: "登记状态", render: stateName}, ...period];
    if (name === "bindings") return [{key: "well_id", label: "井内部编号"}, ...period];
    if (name === "assignments") return [{key: "map_version", label: "平台期望版号"}, ...period];
    if (name === "audit") return [timeColumn("event_at", "操作时间（北京时间）"), {key: "action", label: "动作"}, jsonColumn("actor", "操作人"), {key: "reason", label: "原因"}, jsonColumn("before_data", "变更前"), jsonColumn("after_data", "变更后")];
    if (name === "maps") return [{key: "version", label: "版本"}, {key: "parent_version", label: "父版"}, {key: "sha256", label: "SHA-256"}, {key: "status", label: "复核状态"}, timeColumn("created_at", "形成时间"),
      {key: "version", label: "操作", render: (version, map) => components.trusted([
        ["view-map", "查看快照", true], ["review-map", "核对审核", map.status === "draft" && permissions.has("registry.review") && device.status !== "retired"],
        ["assign-map", "关联期望点表", map.status === "approved" && permissions.has("registry.maps.manage") && device.status !== "retired"],
      ].filter(([, , show]) => show).map(([action, label]) => `<button class="text-button" type="button" data-action="${action}" data-map-version="${esc(version)}">${label}</button>`).join(" "))}];
    return [timeColumn("measured_at", "测量时间（北京时间）"), {key: "gateway_id", label: "原网关"}, {key: "device_id", label: "原终端"}, {key: "point_code", label: "原测点"},
      {key: "value", label: "原工程值"}, {key: "unit", label: "原单位"}, {key: "sequence", label: "原序号"}, {key: "source", label: "原采集来源"}, {key: "quality", label: "原质量码"},
      {key: "matched", label: "归属匹配", render: value => value ? "已匹配" : "未匹配"}, jsonColumn("reasons", "核查理由"),
      {key: "well_id", label: "测量时刻井编号"}, {key: "sample_state", label: "测量时刻登记状态", render: stateName}, {key: "current_state", label: "当前登记状态", render: stateName},
      {key: "expected_map_version", label: "测量时刻期望点表"}, {key: "application_status", label: "实际采用情况", render: () => "终端实际版本未确认"}];
  }
  async function load(name, page = 1) {
    if (!allowed(name) || !current()) return;
    const stamp = ++generation, previous = result;
    active = name; target.textContent = "正在读取历史记录…";
    const query = new URLSearchParams({...filters, page: String(page), page_size: "20"});
    if (name === "audit") query.set("object_id", device.id);
    if (page > 1 && previous) {
      query.set("revision", previous.registry_revision); query.set("received_before", previous.received_before);
      if (name === "resolution") {
        query.set("query_total", previous.query_total);
        query.set("from_time", previous.from_time); query.set("to_time", previous.to_time);
      }
    }
    const path = name === "resolution" ? "/api/registry/resolved-telemetry" : name === "audit" ? "/api/registry/audit" : `/api/registry/devices/${encodeURIComponent(device.id)}/${tabs[name][1]}`;
    try {
      const data = await api.get(`${path}?${query}`);
      if (!current() || stamp !== generation) return;
      result = data;
      target.innerHTML = `<div class="registry-history-table">${components.table(columns(name), data.items, {emptyText: "当前范围暂无记录"})}</div>${registryPagination(name, data, "data-history-page")}`;
    } catch (error) {
      if (!current() || stamp !== generation) return;
      result = null; target.innerHTML = `<p role="alert" class="inline-error">${esc(registryError(error))}</p>`;
    }
  }
  function readTimes(form) {
    const values = {};
    for (const key of ["from_time", "to_time"]) {
      const value = form.elements[key].value.trim();
      if (value) {
        if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(value) || !Number.isFinite(Date.parse(value))) throw new Error("时间必须包含有效日期和明确时区。");
        values[key] = value;
      }
    }
    return values;
  }
  const click = event => {
    const button = event.target.closest("[data-action],[data-history-page]");
    if (!button || button.disabled) return;
    event.stopPropagation();
    if (button.dataset.historyPage) { load(button.dataset.historyPage, Number(button.dataset.pageNumber)); return; }
    const action = button.dataset.action;
    if (action.startsWith("history-") && tabs[action.slice(8)] && allowed(action.slice(8))) {
      filters = {}; result = null;
      root.querySelector("[data-registry-audit-filter]").hidden = action !== "history-audit";
      load(action.slice(8));
    } else if (["view-map", "review-map", "assign-map"].includes(action)) {
      const map = result?.items.find(item => item.version === button.dataset.mapVersion);
      if (map) root.dispatchEvent(new CustomEvent("registry:action", {bubbles: true, detail: {action, device, map}}));
    }
  };
  const submit = event => {
    const form = event.target.closest("[data-registry-resolution],[data-registry-audit-filter]");
    if (!form) return;
    event.preventDefault(); event.stopPropagation();
    try {
      const next = readTimes(form);
      if (device) {
        for (const key of ["actor_id", "action"]) if (form.elements[key].value.trim()) next[key] = form.elements[key].value.trim();
      } else {
        const keys = form.elements.mode.value === "well" ? ["well_id"] : ["gateway_id", "device_id"];
        for (const key of keys) {
          next[key] = form.elements[key].value.trim();
          if (!next[key]) throw new Error("请输入完整的网关与终端编号，或一个生产井内部编号。");
        }
      }
      filters = next; result = null; load(device ? "audit" : "resolution");
    } catch (error) { ++generation; result = null; target.textContent = error.message; }
  };
  root.addEventListener("click", click); root.addEventListener("submit", submit);
  if (device) load("maps");
  else {
    const form = root.querySelector("form");
    const modeChanged = () => {
      const well = form.elements.mode.value === "well";
      form.elements.well_id.disabled = !well;
      form.elements.gateway_id.disabled = well; form.elements.device_id.disabled = well;
    };
    for (const key of ["gateway_id", "device_id", "well_id"]) form.elements[key].value = params[key] || "";
    if (params.well_id) form.elements.mode.value = "well";
    form.elements.mode.addEventListener("change", modeChanged); modeChanged();
    if (params.view === "resolution" && (params.well_id || (params.gateway_id && params.device_id))) form.requestSubmit();
  }
  return {dispose() { disposed = true; ++generation; root.removeEventListener("click", click); root.removeEventListener("submit", submit); }};
}
