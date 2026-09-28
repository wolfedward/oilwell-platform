import {INGEST_BASE, ingestError, node, control} from "./client.js";

const LABELS = {ack: "确认异常", close: "关闭案件", reopen: "重新打开", note: "补充说明", resend: "重新请求发送回执"};
const allowed = (selection, action) => selection?.kind === "receipts" ? action === "resend" :
  ({open: ["ack", "close", "note"], acknowledged: ["close", "note"], closed: ["reopen", "note"]}[selection?.entity?.state] || []).includes(action);

export function mountIngestActions({root, client, permissions, onChanged, isCurrent}) {
  const target = root.querySelector("[data-ingest-actions]");
  let disposed = false, revision = 0, selection = null;
  // Pending commands belong to this mounted workspace, not to a disposable
  // selected-row form. No reasons or credentials are written to browser storage.
  const pending = new Map();
  const identity = value => `${value.kind}/${value.entity.id}`;
  const current = () => !disposed && isCurrent() && root.contains(target);
  function clear() { ++revision; selection = null; target.replaceChildren(); }
  function actions(message = "") {
    ++revision; target.replaceChildren();
    if (!selection || !permissions.has("ingest.read") || !permissions.has("ingest.manage")) return;
    const retained = pending.get(identity(selection));
    if (retained) { open(retained.action, retained); return; }
    target.append(node("h3", selection.kind === "cases" ? "人工处置" : "回执发送操作"));
    if (message) { const notice = node("p", message, "ingest-notice"); notice.setAttribute("role", "status"); target.append(notice); }
    target.append(node("p", selection.kind === "cases" ? "关闭仅改变人工处理状态；不删除原文、不转为测点、不改变隔离结论。" :
      "仅重新安排既有回执发送，不修改回执内容，不代表现场已收到。", "ingest-boundary"));
    const bar = node("div", undefined, "ingest-actions-bar"); target.append(bar);
    for (const [action, label] of Object.entries(LABELS)) if (allowed(selection, action)) {
      control(bar, label, "data-ingest-action", action, () => open(action));
    }
  }
  function open(action, retained = null) {
    if (!current() || !selection || (!retained && !allowed(selection, action)) || !permissions.has("ingest.manage")) return;
    const token = ++revision, captured = {...selection, entity: {...selection.entity}};
    if (retained) captured.entity.version = retained.version;
    const key = identity(captured);
    const valid = () => current() && revision === token;
    target.replaceChildren(node("h3", LABELS[action]));
    const form = node("form"); form.dataset.ingestActionForm = "";
    const label = node("label", "处置原因（2—500 个字符）");
    const reason = node("textarea"); reason.name = "reason"; reason.rows = 3; reason.required = true;
    if (retained) reason.value = retained.reason;
    label.append(reason); form.append(label);
    const version = node("p", `当前版本 ${captured.entity.version}`, "ingest-muted"); form.append(version);
    const notice = node("p"); notice.setAttribute("role", "status"); notice.setAttribute("aria-live", "polite"); form.append(notice);
    const submit = node("button", "确认提交", "button primary"); submit.type = "submit"; form.append(submit);
    const cancel = control(form, "取消", "data-ingest-cancel", "", () => {if (valid()) actions();});
    target.append(form); reason.focus();
    let entry = retained, operation = retained?.operation || null, sending = false, terminal = false, retry = null, needsRefresh = false;
    function lock(value) { reason.disabled = value; submit.disabled = value; cancel.disabled = value; }
    function uncertain(error) {
      lock(true);
      notice.textContent = ingestError(error, true) + ` 原操作标识：${operation.key}。本工作区保留请求，切换记录后仍可重试；内容不可更改。`;
      if (!retry) retry = control(form, "按原操作重试", "data-ingest-retry", "", () => send());
      retry.disabled = false;
    }
    async function refresh(conflict) {
      const entity = await onChanged({kind: captured.kind, id: captured.entity.id, generation: captured.generation,
        conflict, reason: reason.value});
      if (!valid() || !entity) return null;
      captured.entity = {...entity}; selection = {...captured}; version.textContent = `当前版本 ${entity.version}`;
      return entity;
    }
    async function send(observedPromise = null) {
      if (!valid() || sending || terminal) return;
      if (!operation) {
        const text = reason.value;
        if ([...text.trim()].length < 2 || [...text].length > 500 || /[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(text)) {
          notice.textContent = "原因须为 2—500 个字符，不能包含换行或控制字符。"; return;
        }
        if (!allowed(captured, action)) {notice.textContent = "当前状态不再支持此操作，请取消后重新选择。"; return;}
        operation = client.prepareChange(`${INGEST_BASE}/${captured.kind}/${encodeURIComponent(captured.entity.id)}/${action}`,
          {reason: text, expected_version: captured.entity.version});
        entry = {action, operation, reason: text, version: captured.entity.version};
        pending.set(key, entry);
      }
      sending = true; lock(true); if (retry) retry.disabled = true;
      notice.textContent = `正在确认操作 ${operation.key}…`;
      if (!observedPromise) {
        const attempt = entry;
        attempt.phase = "sending";
        const forget = () => { if (pending.get(key) === attempt) pending.delete(key); };
        attempt.promise = operation.run().then(result => {
          attempt.phase = "succeeded"; forget(); return result;
        }, error => {
          attempt.error = error;
          attempt.phase = !error?.status || error.status >= 500 && error.status <= 599 ? "uncertain" : "rejected";
          if (attempt.phase === "rejected") forget();
          throw error;
        });
        observedPromise = attempt.promise;
      }
      try {
        const result = await observedPromise;
        if (!valid()) return;
        let entity = null;
        try {entity = await refresh(false);} catch (_error) { /* The write was confirmed; only its follow-up read failed. */ }
        if (!valid()) return;
        if (!entity) {
          terminal = true; notice.textContent = "操作已提交，但最新详情读取失败。请刷新核查；不要另建相同操作。";
          cancel.disabled = false; return;
        }
        actions(result.replayed ? "原操作已重试确认，未重复执行。" : "操作已提交，最新状态已读取。");
      } catch (error) {
        if (!valid()) return;
        notice.textContent = ingestError(error, true);
        if (error?.status && !(error.status >= 500 && error.status <= 599)) { retry?.remove(); retry = null; }
        if (error?.status === 409) {
          operation = null;
          try {
            const entity = await refresh(true);
            if (!valid()) return;
            needsRefresh = !entity;
          } catch (_error) { if (!valid()) return; needsRefresh = true; }
          if (!valid()) return;
          reason.disabled = false; cancel.disabled = false; submit.disabled = needsRefresh;
          notice.textContent = needsRefresh ? "记录已变化，最新版本读取失败。原因已保留，请重新读取后核查。" : "记录已变化，已读取最新版本；原因已保留，请核查后再次确认提交。";
          if (needsRefresh) control(form, "重新读取版本", "data-ingest-refresh-version", "", async event => {
            if (!valid()) return;
            event.currentTarget.disabled = true;
            try { if (await refresh(true)) {needsRefresh = false; submit.disabled = false; notice.textContent = "最新版本已读取，请核查后再次确认提交。";} }
            catch (_error) {if (valid()) notice.textContent = "最新版本仍不可用，原因已保留。";}
            finally {if (valid()) event.target.disabled = false;}
          });
        } else if (!error?.status || error.status >= 500 && error.status <= 599) {
          uncertain(error);
        } else if (error.status === 422) {
          operation = null; lock(false);
        } else {
          terminal = true; cancel.disabled = false;
        }
      } finally {sending = false;}
    }
    form.addEventListener("submit", event => {event.preventDefault(); if (!needsRefresh) send();});
    if (retained?.phase === "sending") send(retained.promise);
    else if (retained?.phase === "uncertain") uncertain(retained.error);
  }
  function selected(event) { if (!current()) return; selection = event.detail; actions(); }
  root.addEventListener("ingest:selection-start", clear);
  root.addEventListener("ingest:selection", selected);
  return {dispose() {disposed = true; ++revision; pending.clear(); root.removeEventListener("ingest:selection-start", clear); root.removeEventListener("ingest:selection", selected);}};
}
