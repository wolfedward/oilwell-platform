import {node, button, field, metadata} from './client.js';

function fraction(value) {const [n, d = '1'] = String(value).split('/'); return [BigInt(n), BigInt(d)];}
function relativeSeconds(value, reference) {
  const [a, b] = fraction(value), [c, d] = fraction(reference);
  // Only this exact difference is converted, never an absolute index/time.
  return Number(a * d - c * b) / Number(b * d) / 1e9;
}
const svgNode = (name, attributes = {}) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [k, v] of Object.entries(attributes)) n.setAttribute(k, v); return n;
};
function provenance(root, title, source) {
  if (!source) return;
  metadata(root, [[title, source.source_kind], ['实际源设备', source.source], ['所属录波', source.key], ['原文摘要', source.manifest_sha256]]);
}
export function mountWaveformChart(root, result, {onWindowRequest = () => {}} = {}) {
  let disposed = false; const removers = []; root.replaceChildren();
  const xy = !Array.isArray(result.channels);
  root.append(node('p', result.mode === 'summary' ? '显示摘要 · 分桶首末 / 极值；悬停仅定位保留的真实样本。缩小窗口后读取原始窗口。' : '有界原始窗口 · 工程值；原码需另行授权下载。', 'wave-notice'));
  if (xy) {
    provenance(root, '位移来源', result.displacement_source); provenance(root, '载荷来源', result.load_source);
    root.append(node('p', '传感器载荷—位移展示；quantitative_use_allowed: false · closed: false。待模型工作包接入。', 'wave-notice'));
  } else provenance(root, '来源声明', result);
  root.append(node('p', `完整性核查：${result.verification || 'selected_chunks'}；用途限制：${(result.restrictions || []).join('、') || '以当前服务端质量复核为准'}`, 'wave-muted'));
  const channels = xy ? [{...result, metadata: {channel_id: 'sensor-xy', quantity: '传感器载荷—位移', unit: result.load_source?.unit || ''}, time_basis: result.reference?.time_basis}] : result.channels;
  for (const channel of channels) {
    const section = node('section', undefined, 'wave-chart'); section.dataset.waveChannel = channel.metadata.channel_id; root.append(section);
    section.append(node('h3', `${channel.metadata.quantity} · ${channel.metadata.unit}`));
    metadata(section, [['通道', channel.metadata.channel_id], ['状态', channel.status], ['用途资格限制', channel.restrictions || []],
      ['时间依据', channel.time_basis], ['报告配置标签', channel.metadata.reported_config_id],
      ['中心配置版本 / 摘要', channel.metadata.sampling_config], ['校准版本 / 摘要', channel.metadata.calibration || channel.calibration_id]]);
    const points = channel.points || [], gaps = channel.gaps || [];
    if (channel.status !== 'ok') section.append(node('p', `无法绘制：${channel.status}；请缩小窗口或复核来源 / 校准。`, 'wave-notice'));
    else if (!points.length) section.append(node('p', '没有可绘制的有效样本；缺失值不作零值。'));
    else {
      const reference = points[0].relative_ns;
      const coords = points.map(p => ({point: p, x: xy ? p.displacement : relativeSeconds(p.relative_ns, reference), y: xy ? p.load : p.value}));
      const valid = coords.filter(p => Number.isFinite(p.x) && Number.isFinite(p.y) && (xy || p.point.quality === 0));
      if (valid.length) {
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const p of valid) {minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y);}
        const sx = x => 50 + (x - minX) / (maxX - minX || 1) * 800, sy = y => 240 - (y - minY) / (maxY - minY || 1) * 205;
        const svg = svgNode('svg', {viewBox: '0 0 900 280', role: 'img', 'aria-label': `${channel.metadata.quantity}，缺口不连线`});
        svg.append(svgNode('path', {d: 'M50 25V240H850', class: 'wave-axis'}));
        const xAxis = svgNode('g', {'data-wave-axis': 'x', class: 'wave-axis-text'}), yAxis = svgNode('g', {'data-wave-axis': 'y', class: 'wave-axis-text'});
        for (let tick = 0; tick <= 4; tick++) {
          const x = minX + (maxX - minX || 1) * tick / 4, y = minY + (maxY - minY || 1) * tick / 4;
          const tx = svgNode('text', {x: 50 + tick * 200, y: 258, 'text-anchor': 'middle', 'data-wave-tick': ''}); tx.textContent = x.toFixed(2);
          const ty = svgNode('text', {x: 42, y: 244 - tick * 51.25, 'text-anchor': 'end', 'data-wave-tick': ''}); ty.textContent = y.toFixed(2);
          xAxis.append(svgNode('path', {d: `M${50 + tick * 200} 240v5`, class: 'wave-axis'}), tx);
          yAxis.append(svgNode('path', {d: `M45 ${240 - tick * 51.25}h5`, class: 'wave-axis'}), ty);
        }
        const xTitle = svgNode('text', {x: 450, y: 277, 'text-anchor': 'middle'});
        xTitle.textContent = xy ? `位移（${result.displacement_source?.unit || '单位未提供'}）` : '距首个保留样本的时间（s）';
        const yTitle = svgNode('text', {x: 50, y: 16}); yTitle.textContent = xy ? `载荷（${channel.metadata.unit}）` : `${channel.metadata.quantity}（${channel.metadata.unit}）`;
        xAxis.append(xTitle); yAxis.append(yTitle); svg.append(xAxis, yAxis);
        const orderedGaps = gaps.map(g => ({lo: BigInt(g.first_index), hi: BigInt(g.last_index)}));
        let path = '', previous = null, gapPosition = 0;
        const flush = () => {if (path) svg.append(svgNode('path', {d: path, 'data-wave-path': '', class: 'wave-line'})); path = '';};
        for (const p of coords) {
          const index = BigInt(p.point.index);
          if (!Number.isFinite(p.x) || !Number.isFinite(p.y) || !xy && p.point.quality !== 0) {flush(); previous = null; continue;}
          if (previous !== null) {
            while (gapPosition < orderedGaps.length && orderedGaps[gapPosition].hi < previous) gapPosition++;
            if (gapPosition < orderedGaps.length && orderedGaps[gapPosition].lo <= index) flush();
          }
          path += `${path ? 'L' : 'M'}${sx(p.x).toFixed(3)} ${sy(p.y).toFixed(3)} `; previous = index;
        }
        flush();
        const dot = svgNode('circle', {r: '4', class: 'wave-hover-dot', hidden: ''});
        const hit = svgNode('rect', {x: 45, y: 20, width: 815, height: 225, fill: 'transparent', tabindex: '0', 'data-wave-point': '', 'aria-label': '移动指针或左右方向键查看样本'});
        const tip = node('div', undefined, 'wave-tooltip'); tip.dataset.waveTooltip = ''; tip.hidden = true; tip.setAttribute('role', 'status');
        let selected = 0;
        function show(p) {
          if (disposed) return; dot.removeAttribute('hidden'); dot.setAttribute('cx', sx(p.x)); dot.setAttribute('cy', sy(p.y)); tip.hidden = false;
          tip.textContent = `横坐标 ${p.x.toFixed(2)} ${xy ? result.displacement_source?.unit || '' : 's（窗口相对）'} · 纵坐标 ${p.y.toFixed(2)} ${channel.metadata.unit} · 样本序号 ${p.point.index} · 精确时间 ${p.point.relative_ns} ns · 原始位置 ${p.point.raw_index || p.point.index}`;
          if (xy) tip.textContent += ` · 位移原索引 ${p.point.displacement_indices.join(',')} / 载荷原索引 ${p.point.load_indices.join(',')} · 插值 ${p.point.displacement_interpolated}/${p.point.load_interpolated}`;
        }
        function move(event) {
          const box = svg.getBoundingClientRect(), x = event.clientX ? (event.clientX - box.left) / box.width * 900 : sx(valid[0].x), y = event.clientY ? (event.clientY - box.top) / box.height * 280 : sy(valid[0].y);
          let best = Infinity;
          valid.forEach((p, i) => {const distance = (sx(p.x) - x) ** 2 + (xy ? (sy(p.y) - y) ** 2 : 0); if (distance < best) {selected = i; best = distance;}}); show(valid[selected]);
        }
        function hide() {dot.setAttribute('hidden', ''); tip.hidden = true;}
        function key(event) {if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {event.preventDefault(); selected = Math.max(0, Math.min(valid.length - 1, selected + (event.key === 'ArrowRight' ? 1 : -1))); show(valid[selected]);}}
        for (const [type, listener] of [['pointermove', move], ['pointerleave', hide], ['focus', move], ['blur', hide], ['keydown', key]]) {
          hit.addEventListener(type, listener); removers.push(() => hit.removeEventListener(type, listener));
        }
        removers.push(hide); svg.append(dot, hit); section.append(svg, tip);
        section.append(node('p', `${xy ? '位移' : '距首个保留样本的时间（s）'}：${minX.toFixed(2)} — ${maxX.toFixed(2)}；${channel.metadata.unit}：${minY.toFixed(2)} — ${maxY.toFixed(2)}`, 'wave-muted'));
      }
    }
    if (gaps.length) metadata(section, gaps.map(g => ['缺口（含端点）', `${g.first_index} — ${g.last_index} · ${(g.reasons || []).join('、')}`]));
  }
  const controls = node('div', undefined, 'wave-controls'); root.append(controls);
  const first = result.start ?? channels[0]?.points?.[0]?.index;
  if (first !== undefined) {
    const start = field(controls, '窗口起始索引（精确整数）', 'start', {value: first});
    const suggested = channels.find(c => c.suggested_count)?.suggested_count;
    const count = field(controls, '原始窗口帧数（最多 20000）', 'count', {value: String(Math.max(1, Math.min(20000, suggested || Math.ceil((result.count || result.total_frames || 1) / 2))))});
    const notice = node('p', undefined, 'inline-error');
    button(controls, '读取更窄原始窗口', 'data-wave-narrow', '', () => {
      if (disposed) return;
      if (!/^(?:0|-?[1-9][0-9]*)$/.test(start.value) || !/^[1-9][0-9]*$/.test(count.value) || BigInt(count.value) > 20000n || BigInt(start.value) < -9223372036854775808n || BigInt(start.value) > 9223372036854775807n) {notice.textContent = '请填写有界精确整数；服务端复验所属流范围。'; return;}
      onWindowRequest({start: start.value, count: count.value, mode: 'raw'});
    }); controls.append(notice);
  }
  return {dispose() {disposed = true; removers.forEach(remove => remove());}};
}
