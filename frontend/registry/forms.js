import {registryError} from "./client.js";

const RIGHTS = {
  "create-well": "registry.wells.manage", "edit-well": "registry.wells.manage", "archive-well": "registry.wells.manage",
  "create-device": "registry.devices.manage", "edit-device": "registry.devices.manage",
  "bind-device": "registry.bindings.manage", "unbind-device": "registry.bindings.manage",
  "create-map": "registry.maps.manage", "assign-map": "registry.maps.manage", "review-map": "registry.review",
  "enable-device": "registry.review", "retire-device": "registry.review", "suspend-device": ["registry.review", "registry.devices.manage"],
};
const TITLES = {"create-well": "登记生产井", "edit-well": "修改井名称", "archive-well": "归档生产井",
  "create-device": "登记生产终端", "edit-device": "修改设备资料", "bind-device": "绑定 / 迁移生产井",
  "unbind-device": "解除井绑定", "create-map": "保存新点表版本", "review-map": "核对并审核点表",
  "assign-map": "关联平台期望点表", "enable-device": "人工核查后启用台账", "suspend-device": "停用台账", "retire-device": "退役台账", "view-map": "点表不可变快照"};
const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function field(parent, label, name, value = "", choices = null) {
  const wrapper = node("label", undefined, "registry-field");
  wrapper.append(node("span", label));
  const input = node(choices ? "select" : "input");
  input.name = name;
  if (choices) for (const [key, caption] of choices) {
    const option = node("option", caption); option.value = key; input.append(option);
  }
  input.value = String(value ?? "");
  wrapper.append(input); parent.append(wrapper);
  return input;
}
function control(parent, action, label, handler) {
  const button = node("button", label, "button secondary");
  button.type = "button"; button.dataset.action = action;
  button.addEventListener("click", handler); parent.append(button);
  return button;
}
function snapshot(parent, map) {
  parent.append(node("p", `点表版本 ${map.version} · 父版 ${map.parent_version ?? "无"}`));
  parent.append(node("p", `SHA-256：${map.sha256}`, "registry-digest"));
  parent.append(node("p", "终端实际版本未确认；本快照只表示平台保存的定义。"));
  parent.append(node("pre", JSON.stringify(map.content, null, 2), "registry-snapshot"));
}
function textValue(form, key, min = 0, max = 120) {
  const value = form.querySelector(`[name="${key}"]`)?.value ?? "";
  if ([...value].length > max || [...value.trim()].length < min || /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(value)) {
    throw new Error(`${key} 须为 ${min}—${max} 个有效字符，不能含控制字符。`);
  }
  return value;
}
function identifier(form, key) {
  const value = textValue(form, key, 1, 128);
  if (!ID_PATTERN.test(value)) throw new Error(`${key} 须使用 ASCII 字母、数字及 . _ : -。`);
  return value;
}
function numeric(row, key, optional = false) {
  const text = row.querySelector(`[name="${key}"]`).value.trim();
  if (!text && optional) return null;
  const value = Number(text);
  if (!text || !/^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i.test(text) || !Number.isFinite(value)
      || (value === 0 && /[1-9]/.test(text.split(/e/i)[0]))) throw new Error(`${key} 必须为可表示的有限数值。`);
  return value;
}

function pointEditor(form, device, previous) {
  form.append(node("p", device.transport === "mqtt_direct" ? "MQTT：仅定义测点语义，不提交寄存器参数。" : "Modbus：只读 FC03 / FC04；寄存器地址为零基地址。"));
  const rows = node("div", undefined, "registry-point-rows"); form.append(rows);
  let add;
  function append(point = {}) {
    if (rows.children.length >= 256) return;
    const row = node("fieldset", undefined, "registry-point-row"); row.dataset.pointRow = "";
    row.append(node("legend", "采集测点"));
    field(row, "测点编码", "point_code", point.point_code);
    field(row, "工程单位（可空）", "unit", point.unit);
    field(row, "工程下界（可空）", "engineering_min", point.engineering_min);
    field(row, "工程上界（可空）", "engineering_max", point.engineering_max);
    if (device.transport !== "mqtt_direct") {
      field(row, "从站地址（1—247）", "unit_id", point.unit_id ?? 1);
      field(row, "只读功能码", "function_code", point.function_code ?? 3, [["3", "FC03 保持寄存器"], ["4", "FC04 输入寄存器"]]);
      field(row, "寄存器零基地址", "address", point.address ?? 0);
      field(row, "数据类型", "data_type", point.data_type ?? "float32", ["uint16", "int16", "uint32", "int32", "float32", "float64"].map(x => [x, x]));
      for (const [key, label] of [["byte_order", "字节序"], ["word_order", "字序"]]) field(row, label, key, point[key] ?? "big", [["big", "大端"], ["little", "小端"]]);
      field(row, "比例系数", "scale", point.scale ?? 1); field(row, "偏移量", "offset", point.offset ?? 0);
      field(row, "轮询分组", "poll_group", point.poll_group ?? "normal");
    }
    control(row, "remove-point", "删除测点", () => { row.remove(); add.disabled = false; });
    rows.append(row); if (add) add.disabled = rows.children.length >= 256;
  }
  add = control(form, "add-point", "添加测点（最多 256 个）", () => append());
  const initial = previous?.content?.points;
  if (initial?.length) initial.forEach(append); else append();
  return () => {
    const definitions = [...rows.querySelectorAll("[data-point-row]")];
    if (!definitions.length || definitions.length > 256) throw new Error("每版点表须包含 1—256 个测点。");
    const codes = new Set();
    return definitions.map(row => {
      const point = {point_code: identifier(row, "point_code"), unit: textValue(row, "unit", 0, 32) || null,
        engineering_min: numeric(row, "engineering_min", true), engineering_max: numeric(row, "engineering_max", true)};
      if (codes.has(point.point_code)) throw new Error("测点编码不能重复。"); codes.add(point.point_code);
      if (point.engineering_min !== null && point.engineering_max !== null && point.engineering_min > point.engineering_max) throw new Error("工程下界不能大于上界。");
      if (device.transport !== "mqtt_direct") {
        for (const key of ["unit_id", "function_code", "address", "scale", "offset"]) point[key] = numeric(row, key);
        for (const key of ["data_type", "byte_order", "word_order"]) point[key] = row.querySelector(`[name="${key}"]`).value;
        const width = {uint16: 1, int16: 1, uint32: 2, int32: 2, float32: 2, float64: 4}[point.data_type];
        if (!Number.isInteger(point.unit_id) || point.unit_id < 1 || point.unit_id > 247 || ![3, 4].includes(point.function_code)
            || !Number.isInteger(point.address) || point.address < 0 || point.address + width > 65536) throw new Error("只读 Modbus 地址或功能码超出范围。");
        point.poll_group = identifier(row, "poll_group"); point.writable = false;
      }
      return point;
    });
  };
}

async function wellPicker(form, api, valid) {
  const group = node("section"); form.append(group);
  const area = field(group, "筛选区块", "picker_area_code");
  const code = field(group, "筛选井编号", "picker_well_code");
  const select = field(group, "目标在用井", "well_id", "", []);
  const notice = node("p"); group.append(notice);
  let result = null, generation = 0;
  const previous = control(group, "picker-previous", "上一页井", () => load((result?.page || 1) - 1));
  const next = control(group, "picker-next", "下一页井", () => load((result?.page || 1) + 1));
  async function load(page = 1) {
    const request = ++generation;
    const query = new URLSearchParams({status: "active", page: String(page), page_size: "20"});
    if (area.value.trim()) query.set("area_code", area.value.trim());
    if (code.value.trim()) query.set("well_code", code.value.trim());
    if (page > 1 && result) { query.set("revision", result.registry_revision); query.set("received_before", result.received_before); }
    select.replaceChildren(); select.disabled = true; previous.disabled = true; next.disabled = true;
    notice.textContent = "正在读取在用井…";
    try {
      const data = await api.get(`/api/registry/wells?${query}`);
      if (!valid() || request !== generation) return;
      result = data; select.append(new Option("请选择目标井", ""));
      data.items.filter(well => well.status === "active").forEach(well => select.append(new Option(`${well.area_code} / ${well.well_code} · ${well.name}`, well.id)));
      select.disabled = false; previous.disabled = data.page <= 1; next.disabled = data.page * data.page_size >= data.total;
      notice.textContent = data.items.length ? `共 ${data.total} 个在用井 · 第 ${data.page} 页` : "没有符合条件的在用井，请先登记。";
    } catch (error) { if (valid() && request === generation) { result = null; notice.textContent = registryError(error); } }
  }
  control(group, "picker-search", "查询在用井", () => load());
  await load();
  return () => {
    if (select.disabled || !select.value) throw new Error("请选择当前页内已核实的在用井。");
    return select.value;
  };
}

export function mountRegistryForms({root, api, client, components, permissions, onChanged, isCurrent}) {
  let disposed = false, generation = 0, active = null;
  const current = () => !disposed && isCurrent();
  const selectionChanged = () => { ++generation; active?.closeForNavigation(); };
  async function open(event) {
    const data = event.detail || {}, {action, map, maps} = data;
    // Directory actions must not inherit the currently selected terminal.
    const wellAction = ["create-well", "edit-well", "archive-well"].includes(action);
    const device = wellAction || action === "create-device" ? null : data.device;
    const well = wellAction && action !== "create-well" ? data.well : null;
    if (!current() || !permissions.has("registry.read") || !(action in TITLES)) return;
    const rights = RIGHTS[action];
    if (rights && !(Array.isArray(rights) ? rights : [rights]).some(right => permissions.has(right))) return;
    if (device?.status === "retired" && action !== "view-map") return;
    if (active && !active.requestClose()) return;
    const stamp = ++generation;
    const form = node("form", undefined, "field-registry registry-form"); form.noValidate = true;
    // A picker may still be loading; Enter must never perform native navigation.
    form.addEventListener("submit", event => event.preventDefault());
    const identity = device ? `${device.gateway_id} / ${device.device_id}` : well ? `${well.area_code} / ${well.well_code}` : "新登记对象";
    form.append(node("p", `对象：${identity} · 当前版本：${device?.version || well?.version || "新建"}`, "registry-identity"));
    form.append(node("p", "此操作只改变平台台账，不向现场发送指令。"));
    const errorBox = node("p", undefined, "inline-error"); errorBox.setAttribute("role", "alert");
    let operation = null, read = () => ({}), path = "", modal, closed = false;
    const valid = () => current() && stamp === generation && !closed;
    const unresolved = () => operation && ["sending", "uncertain"].includes(operation.phase);
    const warning = "操作结果未确认，可能已提交。离开后不会跨浏览器重启保留重试内容，请先核查台账和审计。";
    const beforeUnload = event => { if (unresolved()) { event.preventDefault(); event.returnValue = ""; } };
    const close = () => {
      if (closed) return;
      closed = true; window.removeEventListener("beforeunload", beforeUnload); observer.disconnect(); modal.close();
      if (active?.stamp === stamp) active = null;
    };
    const requestClose = () => { if (unresolved() && !window.confirm(warning)) return false; close(); return true; };
    const closeForNavigation = () => { if (unresolved()) components.toast(warning, "error"); close(); };
    modal = components.modal({title: TITLES[action], content: form});
    const observer = new MutationObserver(() => { if (!modal.element.isConnected || !current()) closeForNavigation(); });
    observer.observe(document.body, {childList: true, subtree: true});
    window.addEventListener("beforeunload", beforeUnload);
    active = {stamp, requestClose, closeForNavigation};
    modal.element.addEventListener("click", event => {
      if (event.target.closest(".icon-close")) { event.preventDefault(); event.stopImmediatePropagation(); requestClose(); }
    }, true);
    modal.element.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); requestClose(); }
    }, true);
    try {
      const base = device ? `/api/registry/devices/${encodeURIComponent(device.id)}` : well ? `/api/registry/wells/${encodeURIComponent(well.id)}` : "";
      if (action === "view-map") {
        snapshot(form, map); control(form, "cancel-registry", "关闭", requestClose); return;
      }
      if (action === "create-well" || action === "edit-well") {
        if (!well) { field(form, "区块编号", "area_code"); field(form, "井编号", "well_code"); }
        field(form, "井名称", "name", well?.name);
        path = well ? `${base}/changes` : "/api/registry/wells";
        read = () => ({name: textValue(form, "name", 1), ...(!well ? {area_code: identifier(form, "area_code"), well_code: identifier(form, "well_code")} : {})});
      } else if (action === "create-device" || action === "edit-device") {
        if (!device) {
          field(form, "网关编号", "gateway_id"); field(form, "终端编号", "device_id");
          field(form, "接入方式（登记后不可改）", "transport", "modbus_tcp", [["modbus_tcp", "Modbus TCP"], ["modbus_rtu", "Modbus RTU"], ["mqtt_direct", "MQTT 直接上行"]]);
        }
        for (const [key, label] of [["name", "名称"], ["manufacturer", "厂家"], ["model", "型号"], ["serial_number", "序列号"]]) field(form, label, key, device?.[key]);
        const canSetUsage = !device || (device.status === "pending" && !device.usage_locked);
        if (canSetUsage) field(form, "用途（首次启用后锁定）", "usage", device?.usage || "commissioning", [["commissioning", "调试用途"], ["production", "生产用途"]]);
        if (device) field(form, "资料核对说明", "check_note", device.check_note);
        path = device ? `${base}/changes` : "/api/registry/devices";
        read = () => ({name: textValue(form, "name", 1), manufacturer: textValue(form, "manufacturer"), model: textValue(form, "model"), serial_number: textValue(form, "serial_number", 0, 128),
          ...(canSetUsage ? {usage: form.elements.usage.value} : {}), ...(device ? {check_note: textValue(form, "check_note", 0, 1000)} : {
            gateway_id: identifier(form, "gateway_id"), device_id: identifier(form, "device_id"), transport: form.elements.transport.value})});
      } else if (action === "bind-device") {
        path = `${base}/bindings`;
        form.append(node("p", "迁井将结束原井关系并清除期望点表；已启用台账转为停用待复核。"));
        const chosen = await wellPicker(form, api, valid); read = () => ({well_id: chosen()});
      } else if (action === "create-map") {
        const latest = maps?.items?.[0] || null;
        form.append(node("p", `基于最新点表版本：${latest?.version || "无"}；保存将新增版本，不能覆盖原快照。`));
        const points = pointEditor(form, device, latest); path = `${base}/point-maps`;
        read = () => ({parent_version: latest?.version ?? null, points: points()});
      } else if (action === "review-map" || action === "assign-map") {
        snapshot(form, map);
        if (action === "review-map") {
          field(form, "审核结论", "decision", "approved", [["approved", "批准"], ["rejected", "驳回"]]);
          path = `${base}/point-maps/${encodeURIComponent(map.version)}/review`;
          read = () => ({sha256: map.sha256, decision: form.elements.decision.value});
        } else {
          path = `${base}/point-map-assignment`; read = () => ({map_version: map.version});
          form.append(node("p", "设置平台期望定义，不下发到设备；已启用台账会停用待复核。"));
        }
      } else if (action === "archive-well") { path = `${base}/changes`; read = () => ({status: "archived"});
      } else if (action === "unbind-device") { path = `${base}/unbind`;
      } else {
        const status = {"enable-device": "active", "suspend-device": "suspended", "retire-device": "retired"}[action];
        path = `${base}/state`;
        if (status === "active") field(form, "本次人工核查依据（必填）", "check_note");
        read = () => ({status, ...(status === "active" ? {check_note: textValue(form, "check_note", 2, 1000)} : {})});
      }
      if (!valid()) return;
      const dangerous = ["archive-well", "unbind-device", "assign-map", "enable-device", "suspend-device", "retire-device"].includes(action);
      if (dangerous) {
        const confirm = field(form, action === "retire-device" ? "我确认台账退役不可恢复，且不代表物理停机" : "我已核对对象，确认仅修改平台台账关系或状态", "confirm");
        confirm.type = "checkbox";
      }
      field(form, "操作原因（2—500 字符）", "reason");
      form.append(errorBox);
      const buttons = node("div", undefined, "registry-actions"); form.append(buttons);
      const submit = control(buttons, "submit-registry", "确认提交", () => form.requestSubmit());
      control(buttons, "cancel-registry", "取消", requestClose);
      form.addEventListener("submit", async event => {
        event.preventDefault(); if (!valid() || operation?.phase === "sending") return;
        try {
          if (!operation || operation.phase === "rejected") {
            const body = {...read(), reason: textValue(form, "reason", 2, 500)};
            if (dangerous && !form.elements.confirm.checked) throw new Error("请先勾选确认，并核实台账与现场状态的区别。");
            if (device || well) body.expected_version = (device || well).version;
            const candidate = client.beginMutation(path, body);
            if (new TextEncoder().encode(JSON.stringify(candidate.body)).length > 65536) throw new Error("请求超过 64 KiB，请缩减本版点表内容。");
            operation = candidate;
          }
          submit.disabled = true; errorBox.textContent = "正在提交，请勿重复操作。";
          const result = await client.submitMutation(operation);
          if (!valid()) return;
          close();
          // Receipt versions can be historical. Only the identity selects a fresh GET.
          await onChanged({deviceId: path.startsWith("/api/registry/devices") ? result.entity.id : null,
            wellId: path.startsWith("/api/registry/wells") ? result.entity.id : null});
        } catch (error) {
          if (!valid()) return;
          errorBox.textContent = error.status || error.code ? registryError(error) : error.message;
          const uncertain = operation?.phase === "uncertain";
          form.querySelectorAll("input,select,textarea,button[data-action='add-point'],button[data-action='remove-point']").forEach(input => { input.disabled = uncertain; });
          submit.textContent = uncertain ? "重试原操作（相同正文和标识）" : "核对后重新提交";
          if (uncertain) errorBox.textContent += ` ${warning} 操作标识：${operation.key}`;
          submit.disabled = [401, 403].includes(error.status);
        }
      });
    } catch (error) {
      if (valid()) { errorBox.textContent = registryError(error); form.append(errorBox); control(form, "cancel-registry", "关闭", requestClose); }
    }
  }
  root.addEventListener("registry:action", open);
  root.addEventListener("registry:selection-start", selectionChanged);
  return {dispose() { disposed = true; ++generation; active?.closeForNavigation(); root.removeEventListener("registry:action", open); root.removeEventListener("registry:selection-start", selectionChanged); }};
}
