import {INGEST_BASE, CASE_STATES, DELIVERY_STATES, REASONS, ingestError, ingestTime, node, control, metadata, pagination} from "./client.js";

export function mountIngestDetail({root, api, permissions, fact, isCurrent}) {
  let disposed = false, previewGeneration = 0, auditGeneration = 0;
  const current = () => !disposed && isCurrent();
  const isCase = Object.hasOwn(fact, "state"), id = fact.fact_id;
  root.replaceChildren();
  const head = node("header"); head.append(node("h2", `${fact.gateway_id || "身份未识别"} / ${fact.device_id || "终端未识别"}`));
  root.append(head);
  const body = node("div", undefined, "panel-body"); root.append(body);
  body.append(node("h3", "原始事实（不可变）"));
  metadata(body, [["事实编号", id], ["持久结果", isCase || fact.status === "quarantined" ? "quarantined · 已隔离，未转为测点" : "stored · 已持久保存"],
    ["原因", `${REASONS[fact.reason_code] || fact.reason_code} (${fact.reason_code})`], [isCase ? "原始主题" : "回执主题", fact.topic],
    ["报文编号", fact.message_id || "不能可靠关联"], ["SHA-256", fact.payload_sha256],
    ...(isCase ? [["原文字节数", fact.payload_bytes], ["验证器版本", fact.validator_version], ["首次接收（北京时间）", ingestTime(fact.received_at)],
      ["首次出现", ingestTime(fact.first_seen)], ["最近出现", ingestTime(fact.last_seen)], ["重复次数", fact.duplicate_count],
      ["传输异常次数", fact.transport_anomaly_count], ["计数饱和", fact.counters_saturated ? "已达计数上限" : "未达上限"],
      ["首次 MQTT 标志", `QoS ${fact.qos} · retain=${fact.retain} · dup=${fact.dup}`]] : [["结果记录时间", ingestTime(fact.recorded_at)]])]);
  if (isCase) {
    body.append(node("h3", "人工处置（独立状态）"));
    metadata(body, [["案件编号", fact.id], ["状态", CASE_STATES[fact.state] || fact.state], ["版本", fact.version], ["更新时间", ingestTime(fact.updated_at)]]);
    body.append(node("p", "人工关闭不改变隔离事实，不生成新的有效测点；设备纠正后应使用新报文编号。", "ingest-boundary"));
  }
  const receiptBox = node("section", undefined, "ingest-receipt-summary"); body.append(receiptBox);
  function renderReceipt(receipt) {
    receiptBox.replaceChildren(node("h3", "回执发送状态"));
    metadata(receiptBox, [["回执编号", receipt.id], ["固定回执结果", receipt.status],
      ["投递状态", DELIVERY_STATES[receipt.delivery_state] || receipt.delivery_state], ["发送版本", receipt.version],
      ["请求 / 已发代次", `${receipt.requested_generation} / ${receipt.sent_generation}`], ["发送尝试次数", receipt.attempts],
      ["最近安全错误", receipt.last_error_code || "无"], ["Broker 确认时间", ingestTime(receipt.broker_acked_at)],
      ["下一次尝试", ingestTime(receipt.next_attempt_at)], ["租约到期", ingestTime(receipt.lease_until)]]);
    receiptBox.append(node("p", "Broker 确认仅证明消息服务器接收；现场是否完成本地确认，须核查网关本地记录。", "ingest-boundary"));
  }
  if (!isCase) renderReceipt(fact);
  else if (fact.receipt_id) {
    receiptBox.textContent = "正在读取关联回执…";
    api.get(`${INGEST_BASE}/receipts/${encodeURIComponent(fact.receipt_id)}`).then(result => {
      if (current()) renderReceipt(result.entity);
    }, error => {if (current()) receiptBox.textContent = ingestError(error);});
  } else receiptBox.append(node("h3", "回执发送状态"), node("p", "未生成应用回执：可能未启用或身份不能可靠关联，不能据此推定现场已收到结果。"));

  if (permissions.has("ingest.read") && permissions.has("ingest.raw.read")) {
    const raw = node("section", undefined, "ingest-raw"); body.append(raw);
    raw.append(node("h3", "授权原文访问"), node("p", "每次预览与下载均先记录访问审计。预览以文本显示，下载保留精确字节。"));
    const buttons = node("div", undefined, "ingest-actions-bar"), result = node("div"); raw.append(buttons, result);
    const preview = control(buttons, "预览原文（最多 4 KiB）", "data-ingest-raw", "preview", async () => {
      const generation = ++previewGeneration; preview.disabled = true; result.textContent = "正在记录访问审计并读取原文…";
      try {
        const value = await api.get(`${INGEST_BASE}/facts/${encodeURIComponent(id)}/preview`);
        if (!current() || generation !== previewGeneration) return;
        result.replaceChildren(node("p", value.truncated ? `仅显示前 4 KiB，原文 ${value.payload_bytes} 字节，已截断；完整字节请下载。` : `原文 ${value.payload_bytes} 字节，未截断。`));
        const pre = node("pre", value.text, "ingest-raw-text"); pre.dataset.ingestRawText = ""; result.append(pre);
        result.append(node("p", "无效 UTF-8 在预览中用替代字符显示；校验 SHA-256 应使用下载的原始字节。", "ingest-muted"));
      } catch (error) {if (current() && generation === previewGeneration) result.textContent = ingestError(error);}
      finally {if (current() && generation === previewGeneration) preview.disabled = false;}
    });
    const downloadNotice = node("p"); raw.append(downloadNotice);
    const download = control(buttons, "下载原始字节", "data-ingest-raw", "download", async () => {
      download.disabled = true; downloadNotice.textContent = "正在请求授权下载…";
      try {
        await api.download(`${INGEST_BASE}/facts/${encodeURIComponent(id)}/raw`, `ingest-${id}.bin`);
        if (current()) downloadNotice.textContent = "已向浏览器交付下载，请自行核对文件；访问审计不代表完整文件已保存。";
      } catch (error) {if (current()) downloadNotice.textContent = ingestError(error);}
      finally {if (current()) download.disabled = false;}
    });
  }
  if (permissions.has("ingest.read") && permissions.has("audit.read")) {
    const audit = node("section", undefined, "ingest-audit"); body.append(audit);
    audit.append(node("h3", "处置与访问审计"));
    const filter = node("form", undefined, "ingest-filters"); filter.dataset.ingestAuditFilter = "";
    for (const [key, caption] of [["from_time", "开始时间（北京时间）"], ["to_time", "结束时间（北京时间）"]]) {
      const label = node("label", caption), input = node("input"); input.type = "datetime-local"; input.name = key;
      label.append(input); filter.append(label);
    }
    const submit = node("button", "按时间查询审计", "button secondary"); submit.type = "submit"; filter.append(submit); audit.append(filter);
    const result = node("div");
    control(audit, "读取审计记录（最近 24 小时）", "data-ingest-audit", "", () => {filters = {}; loadAudit("1");});
    audit.append(result); let window = null, filters = {};
    filter.addEventListener("submit", event => {
      event.preventDefault();
      filters = Object.fromEntries([...new FormData(filter)].filter(([, value]) => value).map(([key, value]) => [key, value + (value.length === 16 ? ":00" : "") + "+08:00"]));
      if (filters.from_time && filters.to_time && (new Date(filters.from_time) > new Date(filters.to_time) || new Date(filters.to_time) - new Date(filters.from_time) > 31 * 86400000)) {
        ++auditGeneration; result.textContent = "审计开始时间不能晚于结束时间，每次范围最多 31 天。"; return;
      }
      loadAudit("1");
    });
    async function loadAudit(page) {
      const generation = ++auditGeneration; result.textContent = "正在读取审计…";
      const query = new URLSearchParams({fact_id: id, page, page_size: "20", ...filters, ...(page !== "1" && window || {})});
      try {
        const value = await api.get(`${INGEST_BASE}/audit?${query}`);
        if (!current() || generation !== auditGeneration) return;
        window = {from_time: value.from_time, to_time: value.to_time};
        result.replaceChildren(node("p", `范围 ${ingestTime(value.from_time)} 至 ${ingestTime(value.to_time)}`));
        if (!value.items.length) result.append(node("p", "当前时间范围内暂无审计记录。"));
        for (const event of value.items) {
          const article = node("article", undefined, "ingest-audit-event");
          metadata(article, [["时间", ingestTime(event.recorded_at)], ["操作", event.action],
            ["操作人", event.actor?.display_name || event.actor?.username || event.actor?.user_id], ["原因", event.reason]]);
          article.append(node("pre", JSON.stringify({before: event.before_data, after: event.after_data}, null, 2))); result.append(article);
        }
        pagination(result, "audit", value, loadAudit);
      } catch (error) {if (current() && generation === auditGeneration) result.textContent = ingestError(error);}
    }
  }
  return {dispose() {disposed = true; ++previewGeneration; ++auditGeneration;}};
}
