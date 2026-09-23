import {escapeHtml as esc} from "../components.js";
import {createRegistryClient, registryError, registryTime, registryPagination} from "../registry/client.js";
import {mountRegistryForms} from "../registry/forms.js";
import {mountRegistryHistory} from "../registry/history.js";

const mounted = new WeakMap();
const STATES = {pending: "待核查登记", active: "台账已启用", suspended: "台账停用待复核", retired: "台账已退役（非物理停机）"};
const LINKS = {never_reported: "从未上报", online: "接收链路在线", delayed: "接收延迟", offline: "接收链路离线", clock_anomaly: "接收时钟异常"};
const TRANSPORTS = {modbus_tcp: "Modbus TCP（只读）", modbus_rtu: "Modbus RTU（只读）", mqtt_direct: "MQTT 直接上行"};
const button = (action, text, extra = "") => `<button class="button secondary" type="button" data-action="${action}" ${extra}>${text}</button>`;
const filters = {
  wells: [["area_code", "区块编号"], ["well_code", "井编号"]],
  devices: [["gateway_id", "网关编号"], ["device_id", "终端编号"]],
};

export async function renderFieldRegistry(context) {
  const {workspace, api, components, permissions, pageHeader, page, isCurrent} = context;
  mounted.get(workspace)?.dispose();
  const root = document.createElement("section");
  root.className = "field-registry";
  workspace.innerHTML = pageHeader(page);
  workspace.append(root);
  if (!permissions.has("registry.read")) {
    root.textContent = "没有生产设备台账查看权限。";
    return false;
  }
  let disposed = false, selection = 0, selected = null, forms = null, history = null, provenance = null;
  const current = () => !disposed && isCurrent() && workspace.contains(root);
  const client = createRegistryClient(api, {isCurrent: current});
  const state = Object.fromEntries(["wells", "devices"].map(key => [key, {filters: {}, generation: 0, result: null}]));
  const controller = {dispose() { disposed = true; ++selection; forms?.dispose(); history?.dispose(); provenance?.dispose(); client.dispose(); }};
  mounted.set(workspace, controller);
  root.innerHTML = `<p class="registry-boundary">生产目录与原始上行独立管理 · 仅登记、核查和观测；此页面不向设备下发配置或控制指令。</p>
    <div class="registry-directories">${["wells", "devices"].map(key => `<section class="panel">
      <header><div><p class="eyebrow">${key === "wells" ? "WELL DIRECTORY" : "DEVICE REGISTRY"}</p><h2>${key === "wells" ? "生产井目录" : "生产设备"}</h2></div>
        ${permissions.has(key === "wells" ? "registry.wells.manage" : "registry.devices.manage") ? button(key === "wells" ? "create-well" : "create-device", key === "wells" ? "登记生产井" : "登记终端") : ""}</header>
      <div class="panel-body"><form class="registry-filters" data-registry-filter="${key}">
        ${filters[key].map(([name, label]) => `<label>${label}<input name="${name}" maxlength="128" placeholder="全部" /></label>`).join("")}
        <label>登记状态<select name="status"><option value="">全部</option>${Object.entries(key === "wells" ? {active: "在用", archived: "已归档"} : STATES).map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}</select></label>
        <button class="button primary" type="submit">查询</button><button class="button secondary" type="button" data-registry-refresh="${key}">刷新</button>
      </form><div data-registry-results="${key}"></div></div></section>`).join("")}</div>
    <section class="panel registry-detail" data-registry-detail><div class="panel-body">选择终端，查看登记状态和历史。</div></section>
    ${permissions.has("telemetry.read") ? '<section class="panel"><header><h2>原始测点来源核查</h2></header><div class="panel-body" data-registry-provenance></div></section>' : ""}`;
  const detail = root.querySelector("[data-registry-detail]");
  function clearDetail() {
    ++selection; selected = null;
    root.dispatchEvent(new CustomEvent("registry:selection-start")); history?.dispose(); history = null;
    detail.innerHTML = '<div class="panel-body">选择终端，查看登记状态和历史。</div>';
  }
  function table(key, result) {
    if (!result.items.length) return `<p class="registry-empty">${key === "wells" ? "暂无生产井，请先登记生产井。" : "暂无生产设备，请登记已核实身份的终端。"}</p>`;
    const columns = key === "wells" ? [
      {key: "area_code", label: "区块"}, {key: "well_code", label: "井编号"}, {key: "name", label: "名称"},
      {key: "status", label: "状态", render: value => value === "active" ? "在用" : "已归档"}, {key: "version", label: "版本"},
      {key: "id", label: "操作", render: (id, row) => components.trusted(permissions.has("registry.wells.manage") && row.status === "active"
        ? button("edit-well", "编辑", `data-well-id="${esc(id)}"`) + button("archive-well", "归档", `data-well-id="${esc(id)}"`) : "只读")},
    ] : [
      {key: "name", label: "设备名称", render: (value, row) => components.trusted(`<button class="text-button" type="button" data-registry-device="${esc(row.id)}">${esc(value)}</button>`)},
      {key: "gateway_id", label: "网关"}, {key: "device_id", label: "终端"},
      {key: "transport", label: "协议", render: value => TRANSPORTS[value] || value},
      {key: "status", label: "登记状态", render: value => STATES[value] || value}, {key: "version", label: "版本"},
    ];
    return components.table(columns, result.items, {rowKey: "id"}) + registryPagination(key, result);
  }
  async function loadList(key, pageNumber = 1) {
    const data = state[key], generation = ++data.generation;
    const target = root.querySelector(`[data-registry-results="${key}"]`);
    const query = new URLSearchParams({...data.filters, page: String(pageNumber), page_size: "20"});
    if (pageNumber > 1 && data.result) {
      query.set("revision", data.result.registry_revision);
      query.set("received_before", data.result.received_before);
    }
    if (key === "devices") clearDetail();
    target.textContent = "正在读取生产台账…";
    try {
      const result = await api.get(`/api/registry/${key}?${query}`);
      if (!current() || generation !== data.generation) return;
      data.result = result;
      target.innerHTML = table(key, result);
    } catch (error) {
      if (!current() || generation !== data.generation) return;
      data.result = null;
      target.innerHTML = `<p role="alert" class="inline-error">${esc(registryError(error))}</p>`;
    }
  }
  async function selectDevice(id) {
    const generation = ++selection;
    root.dispatchEvent(new CustomEvent("registry:selection-start")); history?.dispose(); history = null;
    selected = null;
    detail.textContent = "正在核查终端…";
    const valid = () => current() && generation === selection;
    try {
      const device = await client.selectDevice(id);
      if (!device || !valid()) return;
      const query = new URLSearchParams({page_size: "20"});
      if (device.registry_revision && device.received_before) {
        query.set("revision", device.registry_revision); query.set("received_before", device.received_before);
      }
      const base = `/api/registry/devices/${encodeURIComponent(id)}`;
      const [bindings, assignments, maps, link] = await Promise.all([
        api.get(`${base}/bindings?${query}`), api.get(`${base}/point-map-assignments?${query}`), api.get(`${base}/point-maps?${query}`),
        permissions.has("acquisition.read") ? api.get(`${base}/link-status${device.registry_revision ? `?${new URLSearchParams({revision: device.registry_revision, received_before: device.received_before})}` : ""}`) : null,
      ]);
      if (!valid()) return;
      selected = {device, bindings, assignments, maps};
      const binding = bindings.items.find(item => item.valid_to === null);
      const assignment = assignments.items.find(item => item.valid_to === null);
      const mutable = device.status !== "retired";
      const action = (permission, name, label) => mutable && permissions.has(permission) ? button(name, label) : "";
      detail.innerHTML = `<header><div><p class="eyebrow">REGISTERED IDENTITY</p><h2>${esc(device.name)}</h2><p>${esc(device.gateway_id)} / ${esc(device.device_id)}</p></div><span>版本 ${esc(device.version)}</span></header>
        <div class="panel-body"><div class="registry-status-grid">
          <article><small>登记状态</small><strong>${esc(STATES[device.status])}</strong></article>
          <article><small>接收链路</small><strong>${link ? esc(LINKS[link.link_status] || "状态未知") : "无链路查询权限"}</strong>${link?.last_received_at ? `<time title="${esc(link.last_received_at)}">${esc(registryTime(link.last_received_at))}</time>` : ""}</article>
          <article><small>平台期望点表</small><strong>${assignment ? `版本 ${esc(assignment.map_version)}` : "尚未关联"}</strong><span>仅平台定义，不代表终端已采用</span></article>
          <article><small>终端实际版本</small><strong>终端实际版本未确认</strong></article>
        </div><dl class="registry-metadata"><div><dt>生产井</dt><dd>${esc(binding?.well_id || "未绑定")}</dd></div>
          <div><dt>用途 / 协议</dt><dd>${device.usage === "production" ? "生产用途" : "调试用途"} / ${esc(TRANSPORTS[device.transport])}</dd></div>
          ${[["manufacturer", "厂家"], ["model", "型号"], ["serial_number", "序列号"]].map(([key, label]) => `<div><dt>${label}</dt><dd>${esc(device[key] || "未登记")}</dd></div>`).join("")}
          <div><dt>最近登记变更（北京时间）</dt><dd><time title="${esc(device.updated_at)}">${esc(registryTime(device.updated_at))}</time></dd></div></dl>
        <div class="registry-actions">${action("registry.devices.manage", "edit-device", "编辑设备")}${action("registry.bindings.manage", "bind-device", "绑定 / 迁井")}
          ${binding ? action("registry.bindings.manage", "unbind-device", "解除绑定") : ""}${action("registry.maps.manage", "create-map", "保存新点表")}
          ${device.status !== "active" ? action("registry.review", "enable-device", "核查后启用") : ""}
          ${device.status === "active" && (permissions.has("registry.devices.manage") || permissions.has("registry.review")) ? button("suspend-device", "停用台账") : ""}
          ${action("registry.review", "retire-device", "退役台账")}</div><p class="registry-boundary">启用、停用和退役只改变平台登记状态，不代表现场电机运行或停止。</p>
        <div data-registry-history></div></div>`;
      root.dispatchEvent(new CustomEvent("registry:selection", {detail: {...selected, generation}}));
      history = mountRegistryHistory({root: detail.querySelector("[data-registry-history]"), api, permissions, device,
        params: {}, isCurrent: valid});
    } catch (error) {
      if (valid()) detail.innerHTML = `<div class="panel-body"><p class="inline-error" role="alert">${esc(registryError(error))}</p>${button("refresh-device", "重新读取", `data-device-id="${esc(id)}"`)}</div>`;
    }
  }
  root.addEventListener("submit", event => {
    const form = event.target.closest("[data-registry-filter]");
    if (!form) return;
    event.preventDefault();
    const key = form.dataset.registryFilter;
    state[key].filters = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]).filter(([, value]) => value));
    loadList(key);
  });
  root.addEventListener("click", event => {
    const device = event.target.closest("[data-registry-device]");
    if (device) { selectDevice(device.dataset.registryDevice); return; }
    const refresh = event.target.closest("[data-registry-refresh]");
    if (refresh) { loadList(refresh.dataset.registryRefresh); return; }
    const pagination = event.target.closest("[data-registry-page]");
    if (pagination && !pagination.disabled && state[pagination.dataset.registryPage]) {
      loadList(pagination.dataset.registryPage, Number(pagination.dataset.pageNumber)); return;
    }
    const action = event.target.closest("[data-action]");
    if (action?.dataset.action === "refresh-device") { selectDevice(action.dataset.deviceId); return; }
    if (action) root.dispatchEvent(new CustomEvent("registry:action", {detail: {
      action: action.dataset.action, ...selected, well: state.wells.result?.items.find(well => well.id === action.dataset.wellId),
      generation: selection,
    }}));
  });
  controller.client = client;
  controller.root = root;
  controller.isCurrent = current;
  controller.selectionIsCurrent = generation => current() && generation === selection;
  controller.onChanged = async ({deviceId, wellId} = {}) => {
    if (!current()) return;
    const tasks = [loadList("devices")];
    if (wellId) tasks.push(loadList("wells"));
    await Promise.all(tasks);
    if (current() && deviceId) await selectDevice(deviceId);
  };
  forms = mountRegistryForms({root, api, client, components, permissions, onChanged: controller.onChanged, isCurrent: current});
  if (permissions.has("telemetry.read")) provenance = mountRegistryHistory({root: root.querySelector("[data-registry-provenance]"),
    api, permissions, device: null, params: context.params || {}, isCurrent: current});
  await Promise.all([loadList("wells"), loadList("devices")]);
  return current();
}
