import {node, button, metadata, errorText, createPager, pages, mountCommand, field} from './client.js';
import {mountWaveformChart} from './charts.js';
import {mountEvidence} from './evidence.js';

export function mountDetail({root, client, capture, permissions, isCurrent, navigate}) {
  let disposed = false, generation = 0, chart = null, evidence = null, windowClient = null, historyClient = null; const commands = [];
  client = client.scope(() => !disposed && isCurrent());
  const current = () => !disposed && isCurrent(); root.replaceChildren(node('h2', `录波详情 · ${capture.device_id}`));
  metadata(root, [['录波身份', capture.capture_id], ['中心记录', capture.capture_pk], ['收齐状态', capture.state], ['完整性', capture.completion],
    ['用途资格', '收齐不等于质量合格；以独立质量复核为准'], ['来源声明', capture.source_kind], ['缺片', capture.missing_chunks],
    ['时间锚点', capture.utc_start], ['UTC 不确定度（ns）', capture.utc_uncertainty_ns], ['周期声明', capture.cycle],
    ['冲程 / 冲次', '接口未提供经复核的冲程 / 冲次指标；不由缺失数据补算'], ['限制', capture.restrictions]]);
  if (permissions.has('registry.read')) button(root, '查看生产台账', 'data-wave-registry', '', () => navigate?.('field-registry', {gateway_id: capture.gateway_id, device_id: capture.device_id}));
  const streamBar = node('div', undefined, 'wave-controls'), chartRoot = node('div'); root.append(streamBar, chartRoot);
  const streams = capture.streams || [];
  if (!streams.length) chartRoot.append(node('p', '没有可用通道；尚无已接受清单或通道资料。不会补画参考曲线。'));
  async function windowRead(stream, request) {
    const n = ++generation; chart?.dispose(); chartRoot.textContent = '正在读取精确窗口…';
    windowClient?.dispose(); const reading = windowClient = client.scope(() => n === generation);
    const query = new URLSearchParams(request);
    try {
      const result = await reading.read(`/captures/${capture.capture_pk}/streams/${stream.stream_id}/window?${query}`);
      if (!result || !current() || n !== generation) return;
      chart = mountWaveformChart(chartRoot, result, {onWindowRequest: next => windowRead(stream, next)});
    } catch (error) {if (current() && n === generation) {chartRoot.textContent = errorText(error); button(chartRoot, '重新读取窗口', null, '', () => windowRead(stream, request));}}
  }
  for (const stream of streams) {
    button(streamBar, `${stream.stream_id} · ${stream.rate.num}/${stream.rate.den} S/s`, 'data-wave-stream', stream.stream_id,
      () => windowRead(stream, {start: stream.first_sample_index, count: String(stream.frame_count), mode: 'summary', budget: '2000'}));
    metadata(root, [['实际流来源', stream.source], ['实际通道 / 单位', stream.channels.map(c => `${c.channel_id} / ${c.quantity} / ${c.unit}`)], ['采样率', stream.rate], ['流起始索引', stream.first_sample_index], ['声明帧数', stream.frame_count]]);
  }
  const observations = node('section', undefined, 'wave-subpanel'); observations.append(node('h3', '通路、原文与用途复核')); root.append(observations);
  const history = node('div'); observations.append(history); let historyGeneration = 0;
  async function showHistory(view) {
    const n = ++historyGeneration, path = view === 'evaluations' ? `/captures/${capture.capture_pk}/evaluations` : `/captures/${capture.capture_pk}`;
    historyClient?.dispose(); const historyScope = historyClient = client.scope(() => n === historyGeneration); let pageClient;
    const pager = createPager({read: path => pageClient.read(path)}, path, view === 'evaluations' ? {} : {view});
    async function load(page = 1) {
      history.textContent = '正在读取…';
      pageClient?.dispose(); pageClient = historyScope.scope();
      try {
        const result = await pager.load(page); if (!result || !current() || n !== historyGeneration) return; history.replaceChildren();
        if (!result.items.length) history.append(node('p', '暂无此类记录。'));
        for (const row of result.items) {
          metadata(history, Object.entries(row));
          if (view === 'facts' && permissions.has('waveform.raw.read')) button(history, '下载原始报文', 'data-wave-raw', row.fact_id, () => {
            historyScope.download(`/facts/${row.fact_id}/raw`, `waveform-${row.fact_id}.bin`).catch(error => {if (current() && n === historyGeneration) history.append(node('p', errorText(error)));});
          });
        }
        pages(history, result, load);
      } catch (error) {if (current() && n === historyGeneration) history.textContent = errorText(error);}
    }
    await load();
  }
  for (const [view, label] of [['provenance', '接收通路'], ['facts', '原始事实'], ['outcomes', '接收结果'], ['evaluations', '质量与用途资格']]) button(observations, label, 'data-wave-history', view, () => showHistory(view));
  if (permissions.has('waveform.pair.manage')) {
    const editor = node('details'); editor.append(node('summary', '记录质量评估（固定选择）'));
    const selection = field(editor, '质量选择 JSON（须含实际通道及证据版本）', 'selection', {multiline: true, maxLength: 60000});
    commands.push(mountCommand({root: editor, client, permissions, permission: 'waveform.pair.manage', path: `/captures/${capture.capture_pk}/evaluations`, version: '0',
      label: '记录评估', body: () => JSON.parse(selection.value), isCurrent: current, onChanged: () => showHistory('evaluations')})); root.append(editor);
  }
  const evidenceRoot = node('section', undefined, 'wave-subpanel'); root.append(evidenceRoot);
  evidence = mountEvidence({root: evidenceRoot, client, permissions, capture, isCurrent: current});
  if (streams.length) windowRead(streams[0], {start: streams[0].first_sample_index, count: String(streams[0].frame_count), mode: 'summary', budget: '2000'});
  showHistory('provenance');
  return {dispose() {disposed = true; ++generation; ++historyGeneration; client.dispose(); chart?.dispose(); evidence?.dispose(); commands.forEach(c => c.dispose());}};
}
