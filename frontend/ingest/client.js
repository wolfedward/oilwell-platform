export const INGEST_BASE = "/api/ingest-reliability";
export const CASE_STATES = {open: "待处置", acknowledged: "已确认待处理", closed: "人工已关闭"};
export const DELIVERY_STATES = {pending: "等待发送", inflight: "正在发送", broker_confirmed: "回执已交 Broker"};
export const REASONS = {accepted: "契约校验通过", topic_invalid: "主题格式错误", payload_too_large: "报文超限",
  utf8_invalid: "UTF-8 编码错误", json_invalid: "JSON 格式错误", json_duplicate_key: "JSON 字段重复",
  contract_invalid: "遥测契约不合格", identity_mismatch: "主题与报文身份不符", idempotency_conflict: "同编号内容冲突",
  qos_not_supported: "不支持的 QoS", retained_publish: "不接受保留报文"};

export function ingestError(error, writing = false) {
  const known = {401: "登录已失效，请重新登录后核查原操作结果。", 403: "没有此项权限，请联系管理员。",
    404: "未找到指定记录，请刷新列表。", 409: "记录已变化，请重新核查版本和状态。",
    422: "输入不符合要求，请检查字段、原因和时间范围。"};
  return known[error?.status] || (writing ? "处置结果尚未确认；请保留原操作标识和内容重试。" : "接收异常服务暂不可用，请稍后重新读取。");
}

export function ingestTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return String(value);
  const stamp = new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).format(date);
  const fraction = String(value).match(/\.(\d{1,6})/)?.[1]?.padEnd(6, "0") || "000000";
  return `${stamp}.${fraction} +08:00`;
}

export function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text ?? "—");
  if (className) element.className = className;
  return element;
}

export function control(parent, label, attribute, value, handler) {
  const element = node("button", label, "button secondary");
  element.type = "button"; element.setAttribute(attribute, value);
  if (handler) element.addEventListener("click", handler);
  parent.append(element);
  return element;
}

export function pagination(parent, kind, result, load) {
  const total = BigInt(result.total), size = BigInt(result.page_size), page = BigInt(result.page);
  const pages = total ? (total + size - 1n) / size : 1n;
  const nav = node("nav", undefined, "pagination"); nav.setAttribute("aria-label", "接收记录分页");
  nav.append(node("span", `共 ${total} 条 · 第 ${page} / ${pages} 页`));
  for (const [direction, label, next, disabled] of [["previous", "上一页", page - 1n, page <= 1n],
    ["next", "下一页", page + 1n, page >= pages || page * size > 100000n]]) {
    const button = control(nav, label, "data-ingest-page", kind, () => {if (!button.disabled) load(String(next));});
    button.dataset.direction = direction; button.disabled = disabled;
  }
  if (page < pages && page * size > 100000n) nav.append(node("span", "已达分页上限，请缩小时间或身份范围。"));
  parent.append(nav);
}

export function metadata(parent, entries) {
  const list = node("dl", undefined, "ingest-metadata");
  for (const [label, value] of entries) {
    const row = node("div"); row.append(node("dt", label), node("dd", value)); list.append(row);
  }
  parent.append(list);
}

export function createIngestClient(api, {isCurrent}) {
  let disposed = false;
  const current = () => !disposed && isCurrent();
  return {
    async read(path) {
      if (!current()) return null;
      try {
        const result = await api.get(path);
        return current() ? result : null;
      } catch (error) { if (current()) throw error; return null; }
    },
    prepareChange(path, fields) {
      if (!current()) throw new Error("当前工作区已关闭");
      const key = crypto.randomUUID();
      // The command contract contains only strings; a private immutable copy
      // survives edits to the caller's form and uncertain network responses.
      const body = Object.freeze({reason: String(fields.reason), expected_version: String(fields.expected_version), idempotency_key: key});
      let phase = "new", pending, result, failure;
      return Object.freeze({key, run() {
        if (!current()) return Promise.reject(new Error("当前操作已关闭"));
        if (phase === "sending") return pending;
        if (phase === "succeeded") return Promise.resolve(result);
        if (phase === "rejected") return Promise.reject(failure);
        phase = "sending";
        try { pending = Promise.resolve(api.post(path, body)); }
        catch (error) { pending = Promise.reject(error); }
        pending = pending.then(value => {phase = "succeeded"; result = value; return value;}, error => {
          phase = !error?.status || error.status >= 500 && error.status <= 599 ? "uncertain" : "rejected";
          failure = error; throw error;
        });
        return pending;
      }});
    },
    dispose() { disposed = true; },
  };
}
