import {node, button, field, metadata, errorText, createPager, pages, mountCommand} from './client.js';
import {mountWaveformChart} from './charts.js';

export function mountPairs({root, client, permissions, isCurrent}) {
  let disposed = false, selection = 0, plotGeneration = 0, chart = null, selectedClient = null, plotClient = null, inspectionClient = null, listClient = null; const actions = [], selectedActions = [];
  const current = () => !disposed && isCurrent(); root.replaceChildren(node('h2', '配对复核'));
  root.append(node('p', '核对同一物理周期的井归属、时间依据、来源与校准门禁。生产反演：待模型工作包接入。', 'wave-notice'));
  if (permissions.has('waveform.pair.manage')) {
    const editor = node('details'); editor.append(node('summary', '新建固定选择的配对候选'));
    const input = field(editor, '固定选择 JSON（真实录波、通道、周期及证据版本）', 'selection', {multiline: true, maxLength: 60000});
    actions.push(mountCommand({root: editor, client, permissions, permission: 'waveform.pair.manage', path: '/pairs', version: '0', label: '创建配对',
      body: () => ({selection: JSON.parse(input.value), method_version: 'linear_adjacent/1'}), isCurrent: current, onChanged: () => load()})); root.append(editor);
  }
  const list = node('div'), detail = node('section', undefined, 'wave-subpanel'); detail.dataset.wavePairDetail = ''; root.append(list, detail);
  const listReader = {read: path => listClient.read(path)}; let pager = createPager(listReader, '/pairs'), listGeneration = 0;
  const state = field(root, '复核状态', 'state', {choices: [['', '全部'], ['proposed', '待复核'], ['approve', '已批准'], ['reject', '已拒绝'], ['revoke', '已撤销']]});
  button(root, '查询配对', 'data-wave-refresh', 'pairs', () => {pager = createPager(listReader, '/pairs', state.value ? {state: state.value} : {}); load();});
  async function load(page = 1) {
    const n = ++listGeneration;
    listClient?.dispose(); listClient = client.scope(() => current() && n === listGeneration);
    try {
      const result = await pager.load(page); if (!result || !current() || n !== listGeneration) return; list.replaceChildren();
      for (const row of result.items) button(list, `${row.pair_version_id} · ${row.state} · v${row.version}`, 'data-wave-pair', row.pair_version_id, () => select(row.pair_version_id));
      if (!result.items.length) list.append(node('p', '暂无配对记录。')); pages(list, result, load);
    } catch (error) {if (current() && n === listGeneration) list.textContent = errorText(error);}
  }
  async function select(id) {
    const n = ++selection; ++plotGeneration; chart?.dispose(); detail.replaceChildren(node('p', '正在读取配对…'));
    selectedClient?.dispose(); selectedActions.splice(0).forEach(a => a.dispose());
    const reading = selectedClient = client.scope(() => current() && n === selection);
    try {
      const pair = await reading.read(`/pairs/${id}`); if (!pair || !current() || n !== selection) return;
      detail.replaceChildren(node('h3', '配对固定选择与门禁')); metadata(detail, [['版本', pair.version], ['复核状态', pair.review_state], ['创建者', pair.creator], ['固定选择', pair.body?.selection], ['内容摘要', pair.content_sha256]]);
      const historical = node('section'); detail.append(historical);
      historical.append(node('h3', '固定评估历史与测量声明'), node('p', '历史归属和联合校正区间不代表当前绑定、当前用途资格或现场真实性。测量声明按录波独立读取；不补造每侧校正时间。', 'wave-notice'));
      const sides = pair.historical_evaluations;
      const validSides = Array.isArray(sides) && sides.length === 2 && ['left', 'right'].every((side, i) => sides[i].side === side && sides[i].quality_run_id === pair[`${side}_quality_run_id`]);
      if (!validSides) historical.append(node('p', '固定质量引用历史不可用或不一致；井归属、联合时间区间未确认。'));
      else {
        if (pair.left_quality_run_id === pair.right_quality_run_id) historical.append(node('p', '两侧引用同一联合评估，不代表两次独立测量。'));
        const discoveries = new Map();
        for (const side of sides) {
          const panel = node('section', undefined, 'wave-subpanel'); panel.dataset.wavePairHistory = side.side; historical.append(panel);
          panel.append(node('h4', side.side === 'left' ? '左侧固定评估' : '右侧固定评估'));
          const history = side.historical_registry, confirmed = side.status === 'eligible' && history?.status === 'resolved' && history.well;
          metadata(panel, [['质量记录 ID', side.quality_run_id], ['评估录波记录', side.capture_pk], ['评估源身份', side.source_key], ['清单摘要', side.manifest_sha256], ['用途 / 评估状态', `${side.purpose} / ${side.status}`], ['评估记录时刻', side.recorded_at],
            ['评估时历史归属', confirmed ? `${history.well.area_code} / ${history.well.well_code}（${history.well.id}）` : `归属未确认（${side.status} / ${history?.status || '无历史快照'}）`], ['历史归属原因', history?.resolution_reasons], ['历史目录版本', history?.revision], ['历史 UTC 锚点', history?.utc_start], ['历史时基', history?.time_basis],
            ['联合校正区间（含不确定度；相对历史 UTC 锚点，ns）', history ? `${history.lower_ns ?? '未提供'} — ${history.upper_ns ?? '未提供'}` : '未提供；不推算']]);
          const declared = node('div'); panel.append(declared);
          if (!side.capture_pk) {declared.textContent = '测量声明锚点 / 不确定度未提供：评估未绑定可读取录波。'; continue;}
          if (!discoveries.has(side.capture_pk)) discoveries.set(side.capture_pk, reading.read(`/captures/${side.capture_pk}`).then(value => ({value}), error => ({error})));
          declared.textContent = '正在读取测量声明锚点与实际源…';
          discoveries.get(side.capture_pk).then(({value, error}) => {
            if (!reading.current()) return; declared.replaceChildren();
            if (error || !value) {declared.textContent = `测量声明锚点 / 不确定度不可用：${error ? errorText(error) : '无读取结果'}`; return;}
            metadata(declared, [['测量声明锚点（独立录波读取）', value.utc_start], ['声明 UTC 不确定度（ns）', value.utc_uncertainty_ns], ['声明来源类别', value.source_kind], ['实际通路源', value.streams?.map(s => s.source)], ['录波声明周期', value.cycle]]);
          });
        }
      }
      metadata(historical, [['历史同周期质量状态', pair.body?.joint_result?.status], ['历史质量原因', pair.body?.joint_result?.reasons], ['历史周期指标', pair.body?.joint_result?.cycle_summary], ['固定选择周期', pair.body?.selection?.cycle], ['历史物理周期证明', pair.body?.physical_cycles]]);
      const gates = node('div'), plot = node('div'); detail.append(gates, plot);
      async function inspect() {
        inspectionClient?.dispose(); const inspecting = inspectionClient = reading.scope();
        try {const value = await inspecting.read(`/pairs/${id}/preview`); if (value && current() && n === selection) {gates.replaceChildren(); metadata(gates, [['历史质量', value.historical_status], ['当前质量（独立门禁读取）', value.current_status], ['当前原因', value.current_reasons], ['用途限制', value.current_restrictions], ['依赖仍有效', value.dependencies_current]]);}}
        catch (error) {if (current() && n === selection) gates.textContent = errorText(error);}
      }
      button(detail, '复核当前门禁', 'data-wave-inspect', '', inspect); inspect();
      const reference = pair.body?.selection?.channels?.find(c => c.role === 'displacement');
      async function windowRead(request) {
        const p = ++plotGeneration; chart?.dispose(); plot.textContent = '正在读取传感器 XY…';
        plotClient?.dispose(); const plotting = plotClient = reading.scope(() => p === plotGeneration);
        try {const value = await plotting.read(`/pairs/${id}/preview/window?${new URLSearchParams(request)}`); if (value && current() && n === selection && p === plotGeneration) chart = mountWaveformChart(plot, {...value, start: request.start, count: Number(request.count)}, {onWindowRequest: windowRead});}
        catch (error) {if (current() && n === selection && p === plotGeneration) plot.textContent = errorText(error);}
      }
      if (reference) button(detail, '查看传感器载荷—位移', 'data-wave-xy', '', () => windowRead({start: reference.first_index, count: String(BigInt(reference.last_index) - BigInt(reference.first_index) + 1n), mode: 'summary', budget: '2000'}));
      else plot.textContent = '没有可用的位移参考通道，无法绘制传感器 XY。待模型工作包接入。';
      for (const decision of ['approve', 'reject', 'revoke']) selectedActions.push(mountCommand({root: detail, client: reading, permissions, permission: 'waveform.review', path: `/pairs/${id}/review`,
        version: pair.version, intent: decision, label: {approve: '批准配对', reject: '拒绝配对', revoke: '撤销配对'}[decision], body: {decision}, isCurrent: () => current() && n === selection, onChanged: () => select(id)}));
    } catch (error) {if (current() && n === selection) detail.textContent = errorText(error);}
  }
  const ready = load(); return {ready, dispose() {disposed = true; ++selection; ++listGeneration; ++plotGeneration; selectedClient?.dispose(); listClient?.dispose(); chart?.dispose(); [...actions, ...selectedActions].forEach(a => a.dispose());}};
}
