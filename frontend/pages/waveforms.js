import {node, button, field, metadata, errorText, createWaveformClient, createPager, pages} from '../waveforms/client.js';
import {mountDetail} from '../waveforms/detail.js';
import {mountPairs} from '../waveforms/pairs.js';
import {mountDatasets} from '../waveforms/datasets.js';
import {mountImports} from '../waveforms/imports.js';

const mounted = new WeakMap();
const authenticatedSessions = new WeakMap();
export async function renderWaveforms(context) {
  const {workspace, permissions, api, isCurrent} = context;
  mounted.get(workspace)?.dispose(); workspace.innerHTML = context.pageHeader(context.page);
  const root = node('section', undefined, 'waveforms'); workspace.append(root);
  if (!permissions.has('waveform.read')) {root.textContent = '没有同步波形查看权限。'; root.dataset.waveState = 'forbidden'; return false;}
  const capability = context.capabilities?.waveforms;
  if (capability?.enabled !== true) {root.textContent = '同步波形与数据集模块未配置；请联系管理员。'; root.dataset.waveState = 'disabled'; return false;}
  if (capability.status !== 'ready') {root.textContent = '同步波形与数据集模块暂时不可用，请检查独立配置与数据库。'; root.dataset.waveState = 'unavailable'; return false;}
  let disposed = false, generation = 0, activeMount = null, client = null;
  // The shell supplies a token replaced only after successful authentication.
  // Component remounts and newly-created permission Sets cannot reset denial.
  const authToken = context.authSession || permissions;
  let actors = authenticatedSessions.get(authToken);
  if (!actors) {actors = new Map(); authenticatedSessions.set(authToken, actors);}
  const actor = String(context.actorId ?? '');
  if (!actors.has(actor)) actors.set(actor, {denied: false, pending: new Map()});
  const session = actors.get(actor), pendingCommands = session.pending;
  const current = () => !disposed && isCurrent() && workspace.contains(root);
  const controller = {dispose() {disposed = true; ++generation; activeMount?.dispose(); client?.dispose();}};
  mounted.set(workspace, controller); context.registerDispose?.(() => controller.dispose());
  root.append(node('p', '同步采样原文、来源证据与用途资格分别核验。当前软件向量保留 synthetic 来源；普通轮询不视为同步 ADC。', 'wave-notice'));
  const tabs = node('nav', undefined, 'wave-tabs'); tabs.setAttribute('aria-label', '波形工作视图'); const body = node('div', undefined, 'panel wave-body'); root.append(tabs, body);
  for (const [key, label] of [['captures', '接收清单'], ['pairs', '配对复核'], ['datasets', '数据集'], ['imports', '文件导入 / 恢复']]) button(tabs, label, 'data-wave-tab', key, () => show(key));
  async function show(view) {
    const n = ++generation; activeMount?.dispose(); client?.dispose(); body.replaceChildren();
    const valid = () => current() && n === generation;
    client = createWaveformClient(api, {isCurrent: valid, can: permission => permissions.has(permission), pending: pendingCommands, session});
    for (const tab of tabs.children) {tab.classList.toggle('active', tab.dataset.waveTab === view); tab.setAttribute('aria-current', tab.dataset.waveTab === view ? 'page' : 'false');}
    const args = {root: body, client, permissions, actorId: context.actorId, isCurrent: valid};
    if (view === 'captures') activeMount = mountCaptures({...args, navigate: context.navigate});
    if (view === 'pairs') activeMount = mountPairs(args);
    if (view === 'datasets') activeMount = mountDatasets(args);
    if (view === 'imports') activeMount = mountImports(args);
    await activeMount?.ready;
  }
  const initial = ['captures', 'pairs', 'datasets', 'imports'].includes(context.params?.view) ? context.params.view : 'captures';
  await show(initial); return current();
}

function mountCaptures({root, client, permissions, isCurrent, navigate}) {
  let disposed = false, selection = 0, listGeneration = 0, detailMount = null, detailClient = null, listClient = null;
  const current = () => !disposed && isCurrent(); root.append(node('h2', '接收清单'));
  const filters = node('form', undefined, 'wave-filters');
  field(filters, '网关', 'gateway_id'); field(filters, '源设备', 'device_id'); field(filters, '井号目录 ID', 'well_id');
  field(filters, '收齐状态', 'state', {choices: [['', '全部'], ['collecting', '接收中'], ['complete', '已收齐'], ['invalid', '完整性失败']]});
  field(filters, '问题筛选', 'stalled', {choices: [['', '全部'], ['true', '超过 24 小时无新有效片'], ['false', '未停滞']]});
  const submit = node('button', '查询', 'button primary'); submit.type = 'submit'; filters.append(submit);
  const list = node('div'), detail = node('section', undefined, 'wave-subpanel'); detail.dataset.waveDetail = ''; root.append(filters, list, detail);
  const listReader = {read: path => listClient.read(path)};
  let pager = createPager(listReader, '/captures');
  function clear() {++selection; detailClient?.dispose(); detailMount?.dispose(); detailMount = null; detail.replaceChildren(node('p', '选择录波查看实际通道、接收通路、缺片、来源与资料。'));}
  filters.addEventListener('submit', event => {event.preventDefault(); const values = Object.fromEntries([...new FormData(filters)].filter(([, v]) => v)); pager = createPager(listReader, '/captures', values); clear(); load();});
  async function select(id) {
    clear(); const n = selection, selectedClient = detailClient = client.scope(() => current() && n === selection); detail.textContent = '正在读取录波…';
    try {
      const result = await selectedClient.read(`/captures/${id}`); if (!result || !current() || n !== selection) return;
      detailMount = mountDetail({root: detail, client: selectedClient, capture: result, permissions, navigate, isCurrent: () => current() && n === selection});
    } catch (error) {if (current() && n === selection) {detail.textContent = errorText(error); button(detail, '重新读取详情', 'data-wave-retry', '', () => select(id));}}
  }
  async function load(page = 1) {
    const n = ++listGeneration; list.textContent = '正在读取接收清单…';
    listClient?.dispose(); const reading = listClient = client.scope(() => current() && n === listGeneration);
    try {
      const result = await pager.load(page); if (!result || !current() || n !== listGeneration) return; list.replaceChildren();
      if (result.refreshed) {clear(); list.append(node('p', '列表已变化，已从第一页重新读取。'));}
      if (!result.items.length) {list.append(node('p', '暂无符合条件的录波记录。')); root.dataset.waveState = 'empty';}
      else {
        root.dataset.waveState = 'ready'; const wrap = node('div', undefined, 'wave-table-wrap'), table = node('table'), head = node('tr');
        for (const label of ['设备 / 网关', '收齐状态', '完整性 / 用途', '时间依据', '通路 / 缺片 / 井归属']) head.append(node('th', label));
        const thead = node('thead'); thead.append(head); table.append(thead);
        const enrichments = [];
        for (const row of result.items) {
          const tr = node('tr'), identity = node('td'); button(identity, row.device_id, 'data-wave-select', row.capture_pk, () => select(row.capture_pk)); identity.append(node('small', row.gateway_id)); tr.append(identity);
          const stateCell = node('td', `${row.state || '未知'}${row.stalled ? ' · stalled' : ''}`), quality = node('td', `${row.has_conflicts ? '存在原文冲突' : '完整性待查询'} / 用途待查询`),
            time = node('td', `${row.first_seen || '未提供'}（中心接收时刻）`), provenance = node('td', '正在查询通路、缺片和实际源身份…');
          tr.append(stateCell, quality, time, provenance);
          enrichments.push({row, stateCell, quality, time, provenance});
          table.append(tr);
        }
        wrap.append(table); list.append(wrap);
        // At most two rows / six read requests concurrently, only on this page.
        let index = 0;
        const enrich = async () => {
          while (current() && n === listGeneration && index < enrichments.length) {
            const {row, stateCell, quality, time, provenance} = enrichments[index++];
            const responses = await Promise.allSettled([
              reading.read(`/captures/${row.capture_pk}?view=summary`),
              reading.read(`/captures/${row.capture_pk}?view=provenance&page=1&page_size=5`),
              reading.read(`/captures/${row.capture_pk}/evaluations?page=1&page_size=5`),
            ]);
            if (!current() || n !== listGeneration) return;
            const [summary, paths, evaluations] = responses.map(r => r.status === 'fulfilled' ? r.value : null);
            provenance.replaceChildren();
            if (summary) {
              stateCell.textContent = `${summary.state || row.state} · 详情版本 ${summary.version ?? '未提供'}${row.stalled ? ' · 列表时点 stalled' : ''}`;
              const missing = Object.values(summary.missing_chunks || {}).reduce((count, list) => count + list.length, 0);
              provenance.append(node('p', summary.streams?.length ? `缺 ${missing} 片` : '缺片数未知（无已接受清单）'));
              provenance.append(node('p', `来源 ${summary.source_kind || '未提供'} · ${(summary.streams || []).map(s => `${s.source.gateway_id} / ${s.source.device_id}`).join('；') || '实际源身份未提供'}`));
              time.append(node('p', `测量锚点 ${summary.utc_start || '未知'} · UTC 不确定度 ${summary.utc_uncertainty_ns ?? '未知'} ns`));
              quality.textContent = `完整性 ${summary.completion?.status || '尚未封存核验'}`;
            } else {provenance.append(node('p', '缺片 / 来源查询不可用')); quality.textContent = '完整性查询不可用';}
            provenance.append(node('p', paths ? `已读取通路：${[...new Set(paths.items.map(p => p.transport))].join(' / ') || '无记录'}${paths.has_more ? '（尚有更多通路事实，详见详情）' : ''}` : '通路查询不可用'));
            if (!evaluations?.items.length) provenance.append(node('p', '井归属未提供：暂无评估时历史快照（可到台账核对时效）'));
            for (const evaluation of evaluations?.items || []) {
              const history = evaluation.historical_registry;
              const confirmed = evaluation.status === 'eligible' && history?.status === 'resolved' && history.well;
              const association = confirmed ? `${history.well.area_code} / ${history.well.well_code}（${history.well.id}）` : `归属未确认（${evaluation.status} / ${history?.status || '无快照'}；${(history?.resolution_reasons || []).join('、')}）`;
              provenance.append(node('p', `评估时历史归属 · ${evaluation.purpose} · ${evaluation.quality_run_id} · ${evaluation.recorded_at || '时间未提供'}：${association}`));
              if (evaluation.source_key) provenance.append(node('small', `评估源 ${JSON.stringify(evaluation.source_key)}`));
              if (history) provenance.append(node('small', `历史目录版本 ${history.revision ?? '未知'} · 时基 ${JSON.stringify(history.time_basis)} · ${history.lower_ns ?? '未知'} — ${history.upper_ns ?? '未知'} ns`));
            }
            if (permissions.has('registry.read')) button(provenance, '查看台账', 'data-wave-registry', '', () => navigate?.('field-registry', {gateway_id: row.gateway_id, device_id: row.device_id}));
            quality.append(node('p', evaluations ? `已记录用途评估：${evaluations.items.map(e => `${e.purpose} / ${e.status}`).join('；') || '暂无评估'}${evaluations.has_more ? '（更多见详情）' : ''}` : '用途资格查询不可用'));
            quality.append(node('small', '各项为独立读取时点；历史评估不替代当前使用授权。'));
          }
        };
        // Scoped completion updates only this still-current list. Errors stay local.
        Promise.all([enrich(), enrich()]).catch(() => {});
      }
      pages(list, result, p => {clear(); load(p);});
    } catch (error) {if (current() && n === listGeneration) {root.dataset.waveState = error.status === 403 ? 'forbidden' : 'unavailable'; list.textContent = errorText(error);}}
  }
  clear(); const ready = load(); return {ready, dispose() {disposed = true; ++selection; ++listGeneration; listClient?.dispose(); detailClient?.dispose(); detailMount?.dispose();}};
}
