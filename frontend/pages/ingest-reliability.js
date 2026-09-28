import {INGEST_BASE, CASE_STATES, DELIVERY_STATES, REASONS, createIngestClient, ingestError, ingestTime,
  node, control, metadata, pagination} from "../ingest/client.js";
import {mountIngestDetail} from "../ingest/detail.js";
import {mountIngestActions} from "../ingest/actions.js";

const mounted = new WeakMap();
const HEALTH_REASONS = {"publisher.missing": "尚无发送器心跳", "publisher.stale": "发送器心跳过期",
  "publisher.failed": "发送器报告失败", "publisher.unverified": "发送器尚无近期成功证据", "publisher.clock_invalid": "发送器时间异常",
  "capacity.exhausted": "原文容量已达限额", "capacity.clock_missing": "容量时钟未初始化", "capacity.clock_invalid": "容量时钟异常",
  "metadata.clock_invalid": "记录时间异常"};

function field(parent, name, caption, options = null, type = "text") {
  const label = node("label", caption), input = node(options ? "select" : "input"); input.name = name;
  if (options) {input.append(new Option("全部", "")); for (const [key, text] of Object.entries(options)) input.append(new Option(text, key));}
  else {input.type = type; if (type === "text") input.maxLength = 128;}
  label.append(input); parent.append(label); return input;
}

export async function renderIngestReliability(context) {
  const {workspace, api, permissions, pageHeader, page, isCurrent} = context;
  mounted.get(workspace)?.dispose();
  workspace.innerHTML = pageHeader(page);
  const root = node("section", undefined, "ingest-reliability"); workspace.append(root);
  if (!permissions.has("ingest.read")) {root.textContent = "没有接收异常查看权限。"; return false;}
  if (context.capabilities?.ingest_reliability?.enabled !== true) {
    root.textContent = "接收异常与回执模块未配置，请联系管理员启用独立连接；其他业务功能不受影响。"; return false;
  }
  let disposed = false, selection = 0, selected = null, detailController = null, actionController = null, healthGeneration = 0;
  const current = () => !disposed && isCurrent() && workspace.contains(root);
  const client = createIngestClient(api, {isCurrent: current});
  const state = Object.fromEntries(["cases", "receipts"].map(kind => [kind, {filters: {}, generation: 0, result: null}]));
  mounted.set(workspace, {dispose() {disposed = true; ++selection; ++healthGeneration; client.dispose(); detailController?.dispose(); actionController?.dispose();}});
  root.append(node("p", "接收链路事实与人工处理独立保存。这里不下发设备控制，不修改报文、不自动重放隔离数据。", "ingest-boundary"));
  const healthPanel = node("section", undefined, "panel");
  const healthHeader = node("header"); healthHeader.append(node("h2", "链路与容量健康"));
  control(healthHeader, "刷新健康", "data-ingest-refresh", "health", loadHealth);
  const healthBody = node("div", undefined, "panel-body"); healthBody.dataset.ingestHealth = "";
  healthPanel.append(healthHeader, healthBody); root.append(healthPanel);
  const directories = node("div", undefined, "ingest-directories"); root.append(directories);
  const results = {};
  for (const kind of ["cases", "receipts"]) {
    const panel = node("section", undefined, "panel"), header = node("header");
    header.append(node("h2", kind === "cases" ? "隔离案件" : "应用回执"));
    control(header, "刷新列表", "data-ingest-refresh", kind, () => {clearSelection(); loadList(kind);}); panel.append(header);
    const body = node("div", undefined, "panel-body"), form = node("form", undefined, "ingest-filters"); form.dataset.ingestFilter = kind;
    for (const [name, caption] of [["gateway_id", "网关编号"], ["device_id", "终端编号"], ["message_id", "报文编号"]]) field(form, name, caption);
    field(form, "reason_code", "接收原因", Object.fromEntries(Object.entries(REASONS).filter(([key]) => kind === "receipts" || key !== "accepted")));
    if (kind === "cases") field(form, "state", "人工处置状态", CASE_STATES);
    else {field(form, "status", "固定结果", {stored: "已持久保存", quarantined: "已隔离"}); field(form, "delivery_state", "发送状态", DELIVERY_STATES);}
    field(form, "from_time", "开始时间（北京时间）", null, "datetime-local"); field(form, "to_time", "结束时间（北京时间）", null, "datetime-local");
    const submit = node("button", "查询", "button primary"); submit.type = "submit"; form.append(submit);
    const validation = node("p", undefined, "inline-error"); validation.setAttribute("role", "alert");
    form.addEventListener("submit", event => {
      event.preventDefault(); validation.textContent = "";
      const values = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, value.trim()]).filter(([, value]) => value));
      for (const key of ["from_time", "to_time"]) if (values[key]) values[key] += (values[key].length === 16 ? ":00" : "") + "+08:00";
      if (values.from_time && values.to_time && (new Date(values.from_time) > new Date(values.to_time) || new Date(values.to_time) - new Date(values.from_time) > 31 * 86400000)) {
        validation.textContent = "开始时间不能晚于结束时间，每次查询范围最多 31 天。"; return;
      }
      state[kind].filters = values; clearSelection(); loadList(kind);
    });
    body.append(form, validation, node("p", "未指定时间时查询最近 24 小时；每次最多 31 天。分页固定本次查询时点。", "ingest-muted"));
    results[kind] = node("div"); results[kind].dataset.ingestResults = kind; body.append(results[kind]); panel.append(body); directories.append(panel);
  }
  const detail = node("section", undefined, "panel ingest-detail"); detail.dataset.ingestDetail = "";
  const actionBox = node("section", undefined, "panel ingest-action-panel"); actionBox.dataset.ingestActions = "";
  root.append(detail, actionBox); clearSelection();
  function clearSelection() {
    ++selection; selected = null; detailController?.dispose(); detailController = null;
    root.dispatchEvent(new CustomEvent("ingest:selection-start"));
    detail.replaceChildren(node("p", "选择一条案件或回执，查看事实、原文与处置记录。", "panel-body"));
  }
  async function loadHealth() {
    const generation = ++healthGeneration; healthBody.textContent = "正在读取健康信息…";
    try {
      const value = await client.read(`${INGEST_BASE}/health`);
      if (!value || !current() || generation !== healthGeneration) return;
      healthBody.replaceChildren(node("p", value.healthy ? "中心发送与容量检查正常（不代表现场接收成功）" : "链路需要关注", value.healthy ? "ingest-health-ok" : "ingest-health-warning"));
      metadata(healthBody, [["隔离案件总数", value.quarantined_cases], ["最近 24 小时新增", value.new_cases_24h], ["未关闭案件", value.open_cases],
        ["最老未处置年龄", value.oldest_open_age_seconds === null ? "无" : `${value.oldest_open_age_seconds} 秒`], ["待发回执", value.pending_receipts],
        ["最老待发年龄", value.oldest_pending_age_seconds === null ? "无" : `${value.oldest_pending_age_seconds} 秒`],
        ["发送器最近心跳", ingestTime(value.publisher_last_seen)], ["近期活动 / 总发送器", `${value.publisher_workers.active} / ${value.publisher_workers.workers}`],
        ["最近发送失败代码", value.publisher_error_code || "无"], ["健康检查时间", ingestTime(value.queried_at)]]);
      for (const [label, used, limit] of [["全部原文逻辑容量", value.used_total_bytes, value.total_bytes], ["隔离原文逻辑容量", value.used_quarantine_bytes, value.quarantine_bytes]]) {
        const text = node("label", `${label}：${used} / ${limit} 字节`), meter = node("progress");
        meter.max = 10000; meter.value = Number(BigInt(limit) > 0n ? (BigInt(used) * 10000n / BigInt(limit) > 10000n ? 10000n : BigInt(used) * 10000n / BigInt(limit)) : 10000n);
        text.append(meter); healthBody.append(text);
      }
      for (const reason of value.reasons) healthBody.append(node("p", `${HEALTH_REASONS[reason] || "运行异常"} · ${reason}`, "ingest-health-warning"));
      healthBody.append(node("p", "逻辑原文字节不包含索引、日志和备份；服务器磁盘余量须另行监控。", "ingest-muted"));
    } catch (error) {if (current() && generation === healthGeneration) healthBody.replaceChildren(node("p", ingestError(error), "inline-error"));}
  }
  function table(kind, result, target) {
    target.replaceChildren(node("p", `查询范围：${ingestTime(result.from_time)} 至 ${ingestTime(result.to_time)}`, "ingest-muted"));
    if (!result.items.length) {target.append(node("p", "暂无符合条件的记录。", "ingest-empty")); return;}
    const wrapper = node("div", undefined, "ingest-table-wrap"), table = node("table");
    const thead = node("thead"), tr = node("tr");
    const headers = kind === "cases" ? ["终端 / 网关", "接收原因", "人工状态", "重复次数", "版本"] : ["终端 / 网关", "固定结果", "发送状态", "尝试次数", "版本"];
    for (const text of headers) tr.append(node("th", text)); thead.append(tr); table.append(thead);
    const tbody = node("tbody"); table.append(tbody);
    for (const row of result.items) {
      const tr = node("tr"), identity = node("td");
      const select = control(identity, row.device_id || "身份未识别", "data-ingest-select", kind, () => selectRecord(kind, row.id)); select.dataset.id = row.id;
      identity.append(node("small", row.gateway_id || "网关未识别")); tr.append(identity);
      const values = kind === "cases" ? [REASONS[row.reason_code] || row.reason_code, CASE_STATES[row.state] || row.state, row.duplicate_count, row.version] :
        [row.status, DELIVERY_STATES[row.delivery_state] || row.delivery_state, row.attempts, row.version];
      for (const text of values) tr.append(node("td", text)); tbody.append(tr);
    }
    wrapper.append(table); target.append(wrapper);
    pagination(target, kind, result, page => {clearSelection(); loadList(kind, page);});
  }
  async function loadList(kind, page = "1", preserve = false) {
    const data = state[kind], generation = ++data.generation, target = results[kind];
    const query = new URLSearchParams({...data.filters, page: String(page), page_size: "20"});
    if (String(page) !== "1" && data.result) {query.set("from_time", data.result.from_time); query.set("to_time", data.result.to_time);}
    const notice = node("p", "正在读取接收记录…"); notice.setAttribute("role", "status");
    if (preserve && data.result) target.prepend(notice); else target.replaceChildren(notice);
    target.setAttribute("aria-busy", "true");
    try {
      const result = await client.read(`${INGEST_BASE}/${kind}?${query}`);
      if (!result || !current() || generation !== data.generation) return;
      data.result = result; table(kind, result, target);
    } catch (error) {
      if (!current() || generation !== data.generation) return;
      data.result = null; target.replaceChildren(node("p", ingestError(error), "inline-error"));
    } finally {if (current() && generation === data.generation) target.setAttribute("aria-busy", "false");}
  }
  function renderDetail(entity, generation) {
    detailController?.dispose();
    detailController = mountIngestDetail({root: detail, api, permissions, fact: entity, isCurrent: () => current() && generation === selection});
  }
  async function selectRecord(kind, id) {
    clearSelection(); const generation = selection;
    detail.textContent = "正在读取接收事实…";
    try {
      const result = await client.read(`${INGEST_BASE}/${kind}/${encodeURIComponent(id)}`);
      if (!result || !current() || generation !== selection) return;
      selected = {kind, entity: result.entity, generation}; renderDetail(result.entity, generation);
      root.dispatchEvent(new CustomEvent("ingest:selection", {detail: selected}));
    } catch (error) {if (current() && generation === selection) {
      detail.replaceChildren(node("p", ingestError(error), "inline-error"));
      control(detail, "重新读取详情", "data-ingest-retry-detail", "", () => selectRecord(kind, id));
    }}
  }
  actionController = mountIngestActions({root, client, permissions, isCurrent: current, onChanged: async ({kind, id, generation}) => {
    const valid = () => current() && generation === selection && selected?.kind === kind && selected.entity.id === id;
    if (!valid()) return null;
    await Promise.all([loadList(kind, "1", true), loadHealth()]);
    if (!valid()) return null;
    try {
      const result = await client.read(`${INGEST_BASE}/${kind}/${encodeURIComponent(id)}`);
      if (!result || !valid()) return null;
      selected = {kind, entity: result.entity, generation}; renderDetail(result.entity, generation);
      return result.entity;
    } catch (_error) {return null;}
  }});
  await Promise.all([loadHealth(), loadList("cases"), loadList("receipts")]);
  return current();
}
