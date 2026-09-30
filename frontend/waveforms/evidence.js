import {node, button, field, metadata, errorText, createPager, pages, mountCommand} from './client.js';
import {digest} from './file-scan.js';

export function mountEvidence({root, client, permissions, capture, isCurrent}) {
  let disposed = false, selection = 0; const actions = [], persistentActions = []; const current = () => !disposed && isCurrent();
  root.append(node('h3', '校准与来源资料'));
  root.append(node('p', '资料是独立不可变版本。报告配置标签与中心配置 UUID / 摘要分开记录；历史展示不授予定量使用资格。', 'wave-muted'));
  const linked = new Set();
  for (const stream of capture.streams || []) for (const c of stream.channels) if (c.calibration_id) linked.add(c.calibration_id);
  const resultRoot = node('div');
  async function select(id) {
    const n = ++selection; actions.splice(0).forEach(a => a.dispose()); resultRoot.textContent = '正在读取资料…';
    try {
      const result = await client.read(`/evidence/${id}`); if (!result || !current() || n !== selection) return;
      resultRoot.replaceChildren(); metadata(resultRoot, [['资料 ID', result.id], ['类型', result.kind], ['版本', result.version], ['审核状态', result.review_state], ['内容摘要', result.content_sha256], ['资料正文', result.body], ['创建者', result.creator]]);
      for (const attachment of result.body?.attachment_ids || []) if (permissions.has('waveform.raw.read')) button(resultRoot, '下载附件（不在线执行）', 'data-wave-attachment', attachment, () => {
        client.download(`/attachments/${attachment}/raw`, `attachment-${attachment}`).catch(error => {if (current() && n === selection) resultRoot.append(node('p', errorText(error)));});
      });
      for (const decision of ['approve', 'reject', 'revoke']) actions.push(mountCommand({root: resultRoot, client, permissions, permission: 'waveform.review',
        path: `/evidence/${id}/${decision === 'revoke' ? 'revoke' : 'review'}`, version: result.version, label: {approve: '批准资料', reject: '拒绝资料', revoke: '撤销资料'}[decision],
        body: decision === 'revoke' ? {} : {decision}, isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
    } catch (error) {if (current() && n === selection) resultRoot.textContent = errorText(error);}
  }
  for (const id of linked) button(root, `查看校准 ${id}`, 'data-wave-evidence', id, () => select(id));
  const filter = field(root, '资料类型', 'kind', {choices: [['', '全部'], ['sampling_config', '采样配置'], ['calibration', '校准'], ['origin', '来源'], ['sync', '同步依据'], ['profile', '质量规则']]});
  const list = node('div'); root.append(list, resultRoot); let listGeneration = 0;
  async function listEvidence() {
    const n = ++listGeneration, pager = createPager(client, '/evidence', filter.value ? {kind: filter.value} : {});
    async function load(page = 1) {
      try {
        const result = await pager.load(page); if (!result || !current() || n !== listGeneration) return; list.replaceChildren();
        for (const row of result.items) button(list, `${row.kind} · ${row.state} · ${row.evidence_id}`, 'data-wave-evidence', row.evidence_id, () => select(row.evidence_id));
        if (!result.items.length) list.append(node('p', '暂无资料记录。')); pages(list, result, load);
      } catch (error) {if (current() && n === listGeneration) list.textContent = errorText(error);}
    }
    await load();
  }
  button(root, '查询资料库', 'data-wave-evidence-list', '', listEvidence);
  if (permissions.has('waveform.evidence.manage')) {
    const editor = node('details'); editor.append(node('summary', '新建不可变资料版本'));
    const kind = field(editor, '类型', 'kind', {choices: [['calibration', '校准'], ['sampling_config', '采样配置'], ['origin', '来源'], ['sync', '同步依据'], ['profile', '质量规则']]});
    const body = field(editor, '资料正文 JSON（未知值保留 null，不自动补齐）', 'body', {multiline: true, maxLength: 60000});
    const creation = mountCommand({root: editor, client, permissions, permission: 'waveform.evidence.manage', path: '/evidence', version: '0', label: '保存资料',
      body: () => ({kind: kind.value, body: JSON.parse(body.value)}), isCurrent: current, onChanged: result => select(result.id)});
    root.append(editor);
    const upload = node('div', undefined, 'wave-controls'), file = field(upload, '资料附件（PDF / PNG / JPEG，最多 4 MiB）', 'attachment', {type: 'file'});
    file.accept = '.pdf,.png,.jpg,.jpeg'; const reason = field(upload, '附件上传理由', 'reason', {maxLength: 512}); const notice = node('p');
    let busy = false;
    button(upload, '上传附件 / 重试原附件', 'data-wave-upload', '', async () => {
      if (!current() || busy) return; busy = true;
      try {
        const chosen = file.files[0]; if (!chosen || chosen.size > 4194304 || !chosen.size || !['application/pdf', 'image/png', 'image/jpeg'].includes(chosen.type)) throw Error('请选择不超过 4 MiB 的 PDF / PNG / JPEG');
        const bytes = new Uint8Array(await chosen.arrayBuffer()), hash = await digest(bytes);
        let pending = client.pending.get('/attachments');
        if (pending && (pending.hash !== hash || pending.type !== chosen.type)) throw Error('请重选原附件以恢复原操作');
        if (!pending) {pending = {hash, type: chosen.type, command: client.prepare('/attachments', '0', reason.value, {}, 'waveform.evidence.manage')}; client.pending.set('/attachments', pending);}
        const result = await client.raw(pending.command, bytes, chosen.type, {'X-Content-SHA256': hash});
        if (result) client.pending.delete('/attachments');
        if (result && current()) notice.textContent = `附件已保存：${result.id || result.attachment_id}。请将附件 ID 关联到资料（每件最多 8 个）。`;
      } catch (error) {if (current()) notice.textContent = error.status || error.code ? errorText(error) : error.message;}
      finally {busy = false;}
    }); upload.append(notice); root.append(upload); persistentActions.push(creation);
  }
  return {dispose() {disposed = true; ++selection; ++listGeneration; [...actions, ...persistentActions].forEach(a => a.dispose());}};
}
