import {node, button, field, metadata, errorText, createPager, pages, mountCommand} from './client.js';
const purposes = [['raw_archive', '原文归档'], ['software_pair_test', '软件配对测试'], ['field_pair_input', '现场配对输入']];

export function mountDatasets({root, client, permissions, isCurrent}) {
  let disposed = false, selection = 0, listGeneration = 0; const actions = [];
  const current = () => !disposed && isCurrent(); root.replaceChildren(node('h2', '数据集'));
  root.append(node('p', '冻结用途与本次导出意图分别核验。证据下载不能证明训练资格；同一物理周期及其派生变体不得跨分区。', 'wave-notice'));
  if (permissions.has('waveform.dataset.manage')) {
    const editor = node('details'); editor.append(node('summary', '新建数据集草稿'));
    const title = field(editor, '数据集名称', 'title', {maxLength: 256}), purpose = field(editor, '冻结用途', 'purpose', {choices: purposes});
    const split = field(editor, '分区计划', 'split', {choices: [['none', '无独立分区'], ['well_holdout', '按井留出'], ['time_holdout', '按时间留出']]});
    const intervals = field(editor, '时间留出计划 JSON（仅按时间留出时）', 'intervals', {multiline: true});
    actions.push(mountCommand({root: editor, client, permissions, permission: 'waveform.dataset.manage', path: '/datasets', version: '0', label: '创建数据集',
      body: () => ({title: title.value, purpose: purpose.value, split_plan: split.value === 'time_holdout' ? JSON.parse(intervals.value) : {kind: split.value}}), isCurrent: current, onChanged: () => load()})); root.append(editor);
  }
  const list = node('div'), detail = node('section', undefined, 'wave-subpanel'); root.append(list, detail); let pager = createPager(client, '/datasets');
  const state = field(root, '状态', 'state', {choices: [['', '全部'], ['draft', '草稿'], ['submitted', '待冻结'], ['frozen', '已冻结']]});
  button(root, '查询数据集', 'data-wave-refresh', 'datasets', () => {pager = createPager(client, '/datasets', state.value ? {state: state.value} : {}); load();});
  async function load(page = 1) {
    const n = ++listGeneration;
    try {
      const result = await pager.load(page); if (!result || !current() || n !== listGeneration) return; list.replaceChildren();
      for (const row of result.items) button(list, `${row.title} · ${row.state} · ${row.purpose} · ${row.member_count} 成员`, 'data-wave-dataset', row.dataset_id, () => select(row.dataset_id));
      if (!result.items.length) list.append(node('p', '暂无数据集记录。')); pages(list, result, load);
    } catch (error) {if (current() && n === listGeneration) list.textContent = errorText(error);}
  }
  async function select(id) {
    const n = ++selection; detail.textContent = '正在读取数据集…';
    try {
      const result = await client.read(`/datasets/${id}`); if (!result || !current() || n !== selection) return;
      const {draft, version} = result; detail.replaceChildren(node('h3', draft.title));
      metadata(detail, [['草稿版本', draft.revision], ['草稿状态', draft.state], ['冻结用途', version?.purpose || draft.purpose], ['分区计划', draft.split_plan],
        ['冻结版本', version?.id], ['冻结摘要', version?.content_sha256], ['当前限制', version?.restriction_reasons], ['独立分区', version?.independent_split]]);
      const members = node('div'), files = node('div'), exports = node('div'); detail.append(members, files, exports);
      async function collection(view, target) {
        const filters = {view, ...(version ? {version_id: version.id} : {revision: draft.revision})}, pageset = createPager(client, `/datasets/${id}`, filters, 50);
        async function read(page = 1) {
          try {const value = await pageset.load(page); if (!value || !current() || n !== selection) return; target.replaceChildren(node('h4', view === 'members' ? '版本成员' : '冻结文件'));
            for (const row of value.items) metadata(target, Object.entries(row)); if (!value.items.length) target.append(node('p', '暂无记录。')); pages(target, value, read);
          } catch (error) {if (current() && n === selection) target.textContent = errorText(error);}
        }
        await read();
      }
      button(detail, '读取版本成员', 'data-wave-members', '', () => collection('members', members));
      if (version) button(detail, '读取冻结文件', 'data-wave-files', '', () => collection('files', files));
      if (!version && permissions.has('waveform.dataset.manage')) {
        const change = field(detail, '成员变更（一次最多 100 项；累计最多 1000）', 'changes', {multiline: true, maxLength: 60000});
        detail.append(node('p', '变更格式：upsert_members 或 remove_members；每项填写实际配对 / 录波 ID、质量记录和分区。', 'wave-muted'));
        actions.push(mountCommand({root: detail, client, permissions, permission: 'waveform.dataset.manage', path: `/datasets/${id}/update`, version: draft.revision,
          label: '保存变更', body: () => ({changes: JSON.parse(change.value)}), isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
        actions.push(mountCommand({root: detail, client, permissions, permission: 'waveform.dataset.manage', path: `/datasets/${id}/submit`, version: draft.revision,
          label: '提交冻结复核', body: {}, isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
      }
      if (!version) actions.push(mountCommand({root: detail, client, permissions, permission: 'waveform.review', path: `/datasets/${id}/freeze`, version: draft.revision,
        label: '独立复核并冻结', body: {}, isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
      if (version && permissions.has('waveform.raw.read') && permissions.has('waveform.export')) {
        const intent = field(exports, '本次导出意图（独立于冻结用途）', 'purpose', {choices: [['evidence', '证据'], ['research', '研究'], ['training', '训练']]});
        actions.push(mountCommand({root: exports, client, permissions, permission: 'waveform.export', path: `/dataset-versions/${version.id}/exports`, version: '0', label: '创建受控导出',
          body: () => ({purpose: intent.value}), isCurrent: () => current() && n === selection, onChanged: value => {
            metadata(exports, [['本次限制', value.description], ['导出', value.export]]);
            const exportId = value.export.id || value.export.export_id;
            button(exports, '下载冻结清单', 'data-wave-export-manifest', '', () => client.download(`/exports/${exportId}/manifest`, 'dataset.json', ['waveform.raw.read', 'waveform.export']).catch(error => {if (current() && n === selection) exports.append(node('p', errorText(error)));}));
            const capture = field(exports, '文件所属中心录波 ID（从冻结文件列表选择）', 'capture_pk');
            button(exports, '下载所选原文件', 'data-wave-export-file', '', () => {
              if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(capture.value)) return;
              client.download(`/exports/${exportId}/captures/${capture.value}/file`, `capture-${capture.value}.owpwave`, ['waveform.raw.read', 'waveform.export']).catch(error => {if (current() && n === selection) exports.append(node('p', errorText(error)));});
            });
          }}));
      }
    } catch (error) {if (current() && n === selection) detail.textContent = errorText(error);}
  }
  const ready = load(); return {ready, dispose() {disposed = true; ++selection; ++listGeneration; actions.forEach(a => a.dispose());}};
}
