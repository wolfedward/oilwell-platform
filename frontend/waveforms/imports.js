import {scanWaveformFile, digest} from './file-scan.js';
import {node, button, field, metadata, errorText} from './client.js';
export {scanWaveformFile} from './file-scan.js';
const metadataKeys = ['file_sha256', 'bytes', 'line_count', 'final_lf', 'gateway_id', 'device_id', 'capture_id'];
const definitiveImportCodes = new Set([
  'waveform.import_request_hash', 'waveform.import_idempotency_conflict', 'waveform.import_not_found',
  'waveform.import_gateway', 'waveform.import_version', 'waveform.import_terminal',
  'waveform.import_session_limit', 'waveform.import_source_limit', 'waveform.import_line',
  'waveform.import_identity', 'waveform.import_line_conflict', 'waveform.import_file_bytes',
  'waveform.import_line_rejected', 'waveform.import_incomplete', 'waveform.import_file_digest',
]);

export function createImportSession({client, actorId, origin = location.origin, storage = localStorage, onProgress = () => {}}) {
  if (actorId === undefined || actorId === null || actorId === '') throw Error('需要已认证账号身份');
  const key = `waveform-import-v1:${encodeURIComponent(origin)}:${encodeURIComponent(String(actorId))}`;
  let disposed = false, busy = false;
  function saved() {
    const raw = storage.getItem(key); if (!raw) return null;
    const value = JSON.parse(raw);
    if (value.actor !== String(actorId) || value.origin !== origin || value.format !== 1) throw Error('导入恢复记录不匹配');
    return value;
  }
  function save(value) {storage.setItem(key, JSON.stringify(value));}
  function active() {return !disposed && client.current();}
  function definitiveImportRejection(error) {
    return error?.status === 409 && definitiveImportCodes.has(error.code);
  }
  async function submit(state, command, bytes) {
    state.pending = command; save(state); // Persist identity before even starting a request.
    let result;
    try {result = bytes ? await client.raw(command, bytes) : await client.send(command);}
    catch (error) {
      if (active() && definitiveImportRejection(error)) {
        state.pending = null; state.pendingLine = null;
        if (!state.session) state.rejectedCreate = true;
        save(state);
        if (state.session) {
          try {
            const info = await client.read(`/imports/${state.session.import_id}`);
            if (info && active()) {state.session = info; save(state); onProgress(info);}
          } catch { /* A later resume or cancel rereads the actual session. */ }
        }
      }
      throw error;
    }
    if (!result || !active()) return null;
    state.session = result; state.pending = null; state.rejectedCreate = false; save(state); onProgress(result); return result;
  }
  async function continueUpload(file, descriptor, state) {
    if (descriptor.file_sha256 !== state.metadata.file_sha256 || descriptor.bytes !== state.metadata.bytes || descriptor.line_count !== state.metadata.line_count) throw Error('请选择摘要一致的原文件');
    if (state.pending) {
      let bytes;
      if (state.pendingLine !== undefined && state.pendingLine !== null) {
        const line = descriptor.lines[state.pendingLine]; bytes = new Uint8Array(await file.slice(line.offset, line.offset + line.length).arrayBuffer());
        if (await digest(bytes) !== line.sha256) throw Error('原文件已变化');
      }
      if (!await submit(state, state.pending, bytes)) return null;
    }
    if (!state.session) throw Error('恢复记录缺少创建命令');
    let info = await client.read(`/imports/${state.session.import_id}`);
    if (!info || !active()) return null;
    state.session = info; save(state);
    if (['sealed', 'cancelled'].includes(info.state)) {onProgress(info); return info;}
    for (const index of [...info.missing_lines]) {
      if (!active()) return null;
      const line = descriptor.lines[index]; if (!line) throw Error('服务端行号不匹配');
      const bytes = new Uint8Array(await file.slice(line.offset, line.offset + line.length).arrayBuffer());
      if (await digest(bytes) !== line.sha256) throw Error('原文件已变化');
      state.pendingLine = index;
      const command = client.prepare(`/imports/${info.import_id}/lines/${index}`, info.version, state.reason, {}, 'waveform.import');
      info = await submit(state, command, bytes); if (!info) return null;
    }
    if (!active()) return null;
    state.pendingLine = null;
    return submit(state, client.prepare(`/imports/${info.import_id}/seal`, info.version, state.reason, {}, 'waveform.import'));
  }
  async function exclusive(callback) {
    if (!active() || busy) return null; busy = true;
    try {return await callback();} finally {busy = false;}
  }
  return {
    saved,
    start(file, descriptor, reason) {return exclusive(async () => {
      const old = saved(); if (old && !old.rejectedCreate && !['sealed', 'cancelled'].includes(old.session?.state)) throw Error('已有未完成导入，请用原文件恢复');
      const metadata = Object.fromEntries(metadataKeys.map(k => [k, descriptor[k]]));
      const state = {format: 1, actor: String(actorId), origin, metadata, reason, session: null, pendingLine: null,
        pending: client.prepare('/imports', '0', reason, metadata, 'waveform.import')};
      save(state); return continueUpload(file, descriptor, state);
    });},
    resume(file, descriptor) {return exclusive(() => {const state = saved(); if (!state) throw Error('当前账号没有已保存的导入'); return continueUpload(file, descriptor, state);});},
    cancel(reason) {return exclusive(async () => {
      const state = saved(); if (!state?.session || state.pending) throw Error('请先恢复并确认原操作，再取消会话');
      const info = await client.read(`/imports/${state.session.import_id}`);
      if (!info || !active()) return null;
      state.session = info; save(state);
      state.pendingLine = null;
      return submit(state, client.prepare(`/imports/${info.import_id}/cancel`, info.version, reason, {}, 'waveform.import'));
    });},
    dispose() {disposed = true; client.cancelPending();},
  };
}

export function mountImports({root, client, permissions, actorId, isCurrent}) {
  let disposed = false, generation = 0, file = null, descriptor = null;
  const current = () => !disposed && isCurrent();
  root.replaceChildren(node('h2', '文件导入与恢复'));
  root.append(node('p', '仅显示本浏览器、同源、当前账号保存的导入会话。离开页面会暂停传输，服务器会话继续保留。来源声明不等于真实性认证。', 'wave-muted'));
  if (!permissions.has('waveform.import')) {root.append(node('p', '没有导入权限。')); return {dispose() {disposed = true;}};}
  const status = node('p', undefined, 'wave-status'); status.setAttribute('role', 'status');
  const details = node('div'), controls = node('div', undefined, 'wave-controls');
  const picker = field(root, '原始 .owpwave 文件', 'wavefile', {type: 'file'}); picker.accept = '.owpwave';
  const reason = field(root, '导入 / 取消理由', 'reason', {value: '人工导入原始录波', maxLength: 512});
  const session = createImportSession({client, actorId, onProgress: info => {if (current()) status.textContent = `会话 ${info.import_id} · ${info.state} · 缺 ${info.missing_lines.length} 行 · 版本 ${info.version}`;}});
  const old = session.saved(); if (old) metadata(details, [['保存的文件摘要', old.metadata.file_sha256], ['会话', old.session?.import_id || (old.rejectedCreate ? '创建被拒绝' : '创建结果待确认')], ['状态', old.session?.state || (old.rejectedCreate ? '可重新开始导入' : '需重试原创建操作')]]);
  async function action(kind) {
    if (!current()) return; for (const b of controls.querySelectorAll('button')) b.disabled = true;
    try {
      if (kind !== 'cancel' && (!file || !descriptor)) throw Error('请先选择并预检原文件');
      const result = kind === 'start' ? await session.start(file, descriptor, reason.value) : kind === 'resume' ? await session.resume(file, descriptor) : await session.cancel(reason.value);
      if (result && current()) status.textContent = `${result.state} · 会话 ${result.import_id} · 服务端始终复验原文`;
    } catch (error) {if (current()) status.textContent = error.status || error.code ? errorText(error) : error.message;}
    finally {if (current()) for (const b of controls.querySelectorAll('button')) b.disabled = false;}
  }
  button(controls, '开始导入', 'data-wave-import', 'start', () => action('start'));
  button(controls, '使用原文件恢复', 'data-wave-import', 'resume', () => action('resume'));
  button(controls, '取消服务器会话', 'data-wave-import', 'cancel', () => action('cancel'));
  root.append(details, controls, status);
  picker.addEventListener('change', async () => {
    const n = ++generation; descriptor = null; file = picker.files[0]; if (!file) return;
    status.textContent = '正在校验原始字节、摘要与布局…';
    try {
      const result = await scanWaveformFile(file); if (!current() || n !== generation) return; descriptor = result; details.replaceChildren();
      metadata(details, [['文件 SHA256', result.file_sha256], ['字节 / 行数', `${result.bytes} / ${result.line_count}`], ['来源声明', result.source_kind], ['设备', result.device_id], ['末行 LF', result.final_lf]]);
      status.textContent = '浏览器预检通过；服务器仍将独立复验。';
    } catch (error) {if (current() && n === generation) status.textContent = `文件预检失败：${error.message}`;}
  });
  return {dispose() {disposed = true; ++generation; session.dispose();}};
}
