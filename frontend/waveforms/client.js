export const BASE = '/api/waveforms';
export const node = (tag, text, className) => {
  const value = document.createElement(tag); if (text !== undefined && text !== null) value.textContent = String(text);
  if (className) value.className = className; return value;
};
export function button(root, text, key, value, action) {
  const b = node('button', text, 'button'); b.type = 'button'; if (key) b.setAttribute(key, value);
  b.addEventListener('click', action); root.append(b); return b;
}
export function field(root, label, name, options = {}) {
  const wrap = node('label', label), input = node(options.choices ? 'select' : options.multiline ? 'textarea' : 'input');
  input.name = name; if (options.choices) for (const [v, t] of options.choices) input.append(new Option(t, v));
  else {if (!options.multiline) input.type = options.type || 'text'; input.maxLength = options.maxLength || 4096;}
  if (options.value !== undefined) input.value = options.value;
  if (options.required) input.required = true; wrap.append(input); root.append(wrap); return input;
}
export function metadata(root, entries) {
  const dl = node('dl', undefined, 'wave-metadata');
  for (const [label, value] of entries) {dl.append(node('dt', label), node('dd', value === null || value === undefined ? '未提供' : typeof value === 'object' ? JSON.stringify(value) : value));}
  root.append(dl); return dl;
}
export function errorText(error) {
  if (error?.status === 401) return '登录会话已失效，请重新登录后使用原操作恢复。';
  if (error?.status === 403) return '权限不足或不允许自审；本次操作已停止。';
  if (error?.status === 503) return '波形模块暂时不可用，请检查独立服务配置。';
  if (error?.status === 409) return '记录或依赖已变化，请重新读取后核对。';
  if (error?.code === 'cancelled') return '请求已暂停，可使用原文件恢复。';
  if (error?.status === 422) return '输入不符合接口要求，请核对字段与版本。';
  return '请求未能确认结果；可用同一操作重试。';
}
const deepFreeze = value => {if (value && typeof value === 'object') {Object.values(value).forEach(deepFreeze); Object.freeze(value);} return value;};
export function createWaveformClient(api, {isCurrent = () => true, can = () => true, pending = new Map()} = {}) {
  let disposed = false, denied = false; const controllers = new Set();
  const current = () => !disposed && isCurrent();
  async function run(callback, permission) {
    if (!current()) return null;
    if (denied || permission && !can(permission)) throw Object.assign(Error('forbidden'), {status: 403});
    const controller = new AbortController(); controllers.add(controller);
    try {const value = await callback(controller.signal); return current() ? value : null;}
    catch (error) {if (error.status === 401 || error.status === 403) denied = true; if (!current()) return null; throw error;}
    finally {controllers.delete(controller);}
  }
  return {
    current, pending,
    read(path) {return run(signal => api.get(path.startsWith(BASE) ? path : BASE + path, {signal}), 'waveform.read');},
    prepare(path, version, reason, body, permission, key = crypto.randomUUID()) {
      if (!/^(?:0|[1-9][0-9]*)$/.test(version) || !reason.trim()) throw Error('操作需要明确理由和版本');
      return deepFreeze({path: path.startsWith(BASE) ? path : BASE + path, permission,
        envelope: {idempotency_key: key, expected_version: version, reason, body: structuredClone(body)}});
    },
    send(command) {return run(signal => api.post(command.path, command.envelope, {signal}), command.permission);},
    raw(command, bytes, contentType = 'application/octet-stream', extra = {}) {
      return run(signal => api.raw(command.path, bytes, {signal, headers: {'Content-Type': contentType,
        'Idempotency-Key': command.envelope.idempotency_key, 'If-Match': command.envelope.expected_version,
        'X-Waveform-Reason': encodeURIComponent(command.envelope.reason), ...extra}}), command.permission);
    },
    download(path, filename, permissions = ['waveform.raw.read']) {
      if (!current() || denied || permissions.some(p => !can(p))) return Promise.reject(Object.assign(Error('forbidden'), {status: 403}));
      return run(signal => api.download(BASE + path, filename, {signal}), permissions[0]);
    },
    cancelPending() {controllers.forEach(c => c.abort());},
    dispose() {disposed = true; controllers.forEach(c => c.abort()); controllers.clear();},
  };
}

// A list's opaque token and effective time bounds belong to its exact query.
export function createPager(client, path, filters = {}, pageSize = 20) {
  let previous = null;
  return {async load(page = 1) {
    const query = new URLSearchParams({...filters, page: String(page), page_size: String(pageSize)});
    if (page > 1 && previous) for (const key of ['snapshot_revision', 'received_before', 'received_after']) if (previous[key] !== undefined) query.set(key, previous[key]);
    try {const result = await client.read(`${path}?${query}`); if (result) previous = result; return result;}
    catch (error) {
      if (page > 1 && error.status === 409 && error.code === 'waveform.refresh_required') {
        previous = null; const result = await this.load(1); return result && {...result, refreshed: true};
      }
      throw error;
    }
  }};
}
export function pages(root, result, load) {
  const bar = node('div', undefined, 'wave-controls');
  if (result.page > 1) button(bar, '上一页', 'data-wave-page', 'previous', () => load(result.page - 1));
  bar.append(node('span', `第 ${result.page || 1} 页`));
  if (result.has_more) button(bar, '下一页', 'data-wave-page', 'next', () => load((result.page || 1) + 1)); root.append(bar);
}

// Uncertain writes retain their real target and immutable envelope across remounts.
export function mountCommand({root, client, permissions, permission, path, version, label, body, isCurrent, onChanged}) {
  if (!permissions.has(permission)) return {dispose() {}};
  let disposed = false, busy = false; const current = () => !disposed && isCurrent();
  const form = node('form', undefined, 'wave-command'); form.dataset.waveAction = path;
  const reason = field(form, `${label}理由`, 'reason', {required: true, maxLength: 512});
  const submit = node('button', label, 'button primary'); submit.type = 'submit'; form.append(submit);
  const status = node('p', undefined, 'inline-error'); status.setAttribute('role', 'status'); form.append(status); root.append(form);
  const pending = () => client.pending.get(path);
  if (pending()) {reason.value = pending().envelope.reason; reason.disabled = true; submit.textContent = `重试${label}（原操作）`;}
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (!current() || busy) return; busy = true; submit.disabled = true;
    try {
      let command = pending();
      if (!command) {command = client.prepare(path, String(version), reason.value, typeof body === 'function' ? body() : body, permission); client.pending.set(path, command);}
      reason.value = command.envelope.reason; reason.disabled = true;
      const result = await client.send(command);
      if (result) client.pending.delete(path);
      if (!result || !current()) return;
      status.textContent = '操作已保存。'; await onChanged?.(result);
    } catch (error) {
      if (!current()) return; status.textContent = errorText(error);
      if ([409, 422].includes(error.status)) client.pending.delete(path);
      if ([401, 403, 409, 422].includes(error.status)) {submit.disabled = true; return;}
      submit.textContent = `重试${label}（原操作）`;
    } finally {busy = false; if (current() && !/权限|失效|重新读取|输入不符合/.test(status.textContent)) submit.disabled = false;}
  });
  return {dispose() {disposed = true;}};
}
