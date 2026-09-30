import {node, button, field, metadata, errorText, createPager, pages, mountCommand} from './client.js';
import {mountWaveformChart} from './charts.js';

export function mountPairs({root, client, permissions, isCurrent}) {
  let disposed = false, selection = 0, plotGeneration = 0, chart = null; const actions = [];
  const current = () => !disposed && isCurrent(); root.replaceChildren(node('h2', '配对复核'));
  root.append(node('p', '核对同一物理周期的井归属、时间依据、来源与校准门禁。生产反演：待模型工作包接入。', 'wave-notice'));
  if (permissions.has('waveform.pair.manage')) {
    const editor = node('details'); editor.append(node('summary', '新建固定选择的配对候选'));
    const input = field(editor, '固定选择 JSON（真实录波、通道、周期及证据版本）', 'selection', {multiline: true, maxLength: 60000});
    actions.push(mountCommand({root: editor, client, permissions, permission: 'waveform.pair.manage', path: '/pairs', version: '0', label: '创建配对',
      body: () => ({selection: JSON.parse(input.value), method_version: 'linear_adjacent/1'}), isCurrent: current, onChanged: () => load()})); root.append(editor);
  }
  const list = node('div'), detail = node('section', undefined, 'wave-subpanel'); root.append(list, detail); let pager = createPager(client, '/pairs'), listGeneration = 0;
  const state = field(root, '复核状态', 'state', {choices: [['', '全部'], ['proposed', '待复核'], ['approve', '已批准'], ['reject', '已拒绝'], ['revoke', '已撤销']]});
  button(root, '查询配对', 'data-wave-refresh', 'pairs', () => {pager = createPager(client, '/pairs', state.value ? {state: state.value} : {}); load();});
  async function load(page = 1) {
    const n = ++listGeneration;
    try {
      const result = await pager.load(page); if (!result || !current() || n !== listGeneration) return; list.replaceChildren();
      for (const row of result.items) button(list, `${row.pair_version_id} · ${row.state} · v${row.version}`, 'data-wave-pair', row.pair_version_id, () => select(row.pair_version_id));
      if (!result.items.length) list.append(node('p', '暂无配对记录。')); pages(list, result, load);
    } catch (error) {if (current() && n === listGeneration) list.textContent = errorText(error);}
  }
  async function select(id) {
    const n = ++selection; ++plotGeneration; chart?.dispose(); detail.replaceChildren(node('p', '正在读取配对…'));
    try {
      const pair = await client.read(`/pairs/${id}`); if (!pair || !current() || n !== selection) return;
      detail.replaceChildren(node('h3', '配对固定选择与门禁')); metadata(detail, [['版本', pair.version], ['复核状态', pair.review_state], ['创建者', pair.creator], ['固定选择', pair.body?.selection], ['内容摘要', pair.content_sha256]]);
      const gates = node('div'), plot = node('div'); detail.append(gates, plot);
      async function inspect() {
        try {const value = await client.read(`/pairs/${id}/preview`); if (value && current() && n === selection) {gates.replaceChildren(); metadata(gates, [['历史质量', value.historical_status], ['当前质量', value.current_status], ['当前原因', value.current_reasons], ['用途限制', value.current_restrictions], ['依赖仍有效', value.dependencies_current]]);}}
        catch (error) {if (current() && n === selection) gates.textContent = errorText(error);}
      }
      button(detail, '复核当前门禁', 'data-wave-inspect', '', inspect); inspect();
      const reference = pair.body?.selection?.channels?.find(c => c.role === 'displacement');
      async function windowRead(request) {
        const p = ++plotGeneration; chart?.dispose(); plot.textContent = '正在读取传感器 XY…';
        try {const value = await client.read(`/pairs/${id}/preview/window?${new URLSearchParams(request)}`); if (value && current() && n === selection && p === plotGeneration) chart = mountWaveformChart(plot, {...value, start: request.start, count: Number(request.count)}, {onWindowRequest: windowRead});}
        catch (error) {if (current() && n === selection && p === plotGeneration) plot.textContent = errorText(error);}
      }
      if (reference) button(detail, '查看传感器载荷—位移', 'data-wave-xy', '', () => windowRead({start: reference.first_index, count: String(BigInt(reference.last_index) - BigInt(reference.first_index) + 1n), mode: 'summary', budget: '2000'}));
      else plot.textContent = '没有可用的位移参考通道，无法绘制传感器 XY。待模型工作包接入。';
      for (const decision of ['approve', 'reject', 'revoke']) actions.push(mountCommand({root: detail, client, permissions, permission: 'waveform.review', path: `/pairs/${id}/review`,
        version: pair.version, label: {approve: '批准配对', reject: '拒绝配对', revoke: '撤销配对'}[decision], body: {decision}, isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
    } catch (error) {if (current() && n === selection) detail.textContent = errorText(error);}
  }
  const ready = load(); return {ready, dispose() {disposed = true; ++selection; ++listGeneration; ++plotGeneration; chart?.dispose(); actions.forEach(a => a.dispose());}};
}
