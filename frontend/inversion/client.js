export const INVERSION_BASE = '/api/production-inversion';

const sessions = new WeakMap();
const clone = value => structuredClone(value);
const freeze = value => {
  if (value && typeof value === 'object') {Object.values(value).forEach(freeze); Object.freeze(value);}
  return value;
};
const deniedError = () => Object.assign(new Error('当前登录会话无权继续此操作'), {status: 403, code: 'session_denied'});
// These codes are raised before the transaction commits. A post-commit read
// can also return 409 (notably export_manifest_corrupt), so status alone is
// insufficient evidence that a command is safe to replace.
const DEFINITE_PRECOMMIT_REJECTIONS = new Set([
  'inversion.self_review', 'inversion.version_conflict', 'inversion.parent_conflict',
  'inversion.review_transition',
  'inversion.attachment_mismatch',
]);

export function createInversionClient(ctx, {request = ctx.api.request.bind(ctx.api)} = {}) {
  const token = ctx.authSession && typeof ctx.authSession === 'object' ? ctx.authSession : ctx;
  let actors = sessions.get(token);
  if (!actors) {actors = new Map(); sessions.set(token, actors);}
  const actor = String(ctx.actorId ?? '');
  if (!actors.has(actor)) actors.set(actor, {denied: false, pending: new Map(), uncertain: new Set()});
  const session = actors.get(actor);
  const controllers = new Set();
  let disposed = false;
  const current = () => !disposed && (ctx.isCurrent?.() ?? true);
  const can = permission => !permission || ctx.permissions?.has(permission);
  async function call(path, method, body, permission, {absolute = false, filename} = {}) {
    if (!current()) return null;
    if (session.denied || !can(permission)) throw deniedError();
    const controller = new AbortController(); controllers.add(controller);
    try {
      const target = absolute || path.startsWith(INVERSION_BASE) ? path : INVERSION_BASE + path;
      const value = filename === undefined
        ? await request(target, {method, ...(body === undefined ? {} : {body}), signal: controller.signal})
        : await ctx.api.download(target, filename, {signal: controller.signal});
      return current() ? value : null;
    } catch (error) {
      if (error?.status === 401 || error?.status === 403) session.denied = true;
      if (!current()) return null;
      throw error;
    } finally {controllers.delete(controller);}
  }
  function prepare({path, action, body, expected, reason, permission, slot = action + ':' + path, intent}) {
    if (typeof reason !== 'string' || !reason.trim() || reason.length > 512) throw Error('请填写不超过 512 字的操作理由');
    if (!expected || (expected.kind !== 'absent' || Object.keys(expected).length !== 1) &&
      (expected.kind !== 'match' || !Number.isSafeInteger(expected.version) || expected.version < 1 || Object.keys(expected).length !== 2))
      throw Error('请先读取并核对当前版本');
    if (intent !== undefined && (action !== 'exports.create' || !['quantitative','evidence'].includes(intent))) throw Error('无效导出用途');
    const command = {key: crypto.randomUUID(), expected: clone(expected), reason: reason.trim()};
    return freeze({path, action, permission, slot, intent, envelope: body === undefined ? {command} : {command, body: clone(body)}});
  }
  async function recoverPending(slot) {
    const saved = session.pending.get(slot);
    if (!saved) return null;
    const query = new URLSearchParams({action: saved.action});
    if (saved.intent) query.set('intent', saved.intent);
    const value = await call(`/requests/${saved.envelope.command.key}?${query}`, 'GET', undefined, saved.permission);
    if (value?.state === 'committed') {
      session.pending.delete(slot);
      session.uncertain.delete(slot);
    }
    return value;
  }
  async function submit(input) {
    const slot = input.slot || input.action + ':' + input.path;
    let saved = session.pending.get(slot);
    if (saved) {
      // Recovery is mandatory before a saved command can be issued again.
      const found = await recoverPending(slot);
      if (!found || found.state !== 'unknown') return found;
      if (!input.replay) return found;
    } else {
      saved = prepare({...input, slot});
      session.pending.set(slot, saved);
    }
    try {
      const value = await call(saved.path, 'POST', saved.envelope, saved.permission);
      if (value) {
        session.pending.delete(slot);
        session.uncertain.delete(slot);
      } else session.uncertain.add(slot);
      return value;
    } catch (error) {
      const definite = error?.status === 400 && error?.code === 'inversion.contract_invalid'
        || error?.status === 409 && DEFINITE_PRECOMMIT_REJECTIONS.has(error?.code)
          && error?.details?.commit_outcome === 'not_committed' && error.details.action === saved.action;
      if (definite && !session.uncertain.has(slot)) session.pending.delete(slot);
      else session.uncertain.add(slot);
      throw error;
    }
  }
  return {
    get(path, permission = 'inversion.read') {return call(path, 'GET', undefined, permission);},
    preflight(body) {
      if (!['inversion.run','waveform.read','waveform.raw.read','waveform.export'].every(can)) return Promise.reject(deniedError());
      return call('/runs/preflight', 'POST', {body:clone(body)}, 'inversion.run');
    },
    waveformGet(path) {
      // Metadata-only finite routes. Never accepts origin, raw, export or files URLs.
      const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
      if (typeof path !== 'string' || !new RegExp(`^/(?:datasets(?:/${uuid})?|pairs/${uuid})(?:\\?[^#]*)?$`).test(path)) throw Error('无效冻结成员读取路径');
      const query = new URLSearchParams(path.split('?')[1] || '');
      const allowed = new Set(['state','page','page_size','snapshot_revision','received_before','received_after','view','version_id']);
      if ([...query.keys()].some(key=>!allowed.has(key)) || query.has('view') && query.get('view') !== 'members') throw Error('仅支持冻结成员元数据');
      return call('/api/waveforms'+path, 'GET', undefined, 'waveform.read', {absolute:true});
    },
    downloadExport(id, filename) {
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id)) throw Error('无效导出标识');
      if (!['inversion.read','inversion.export','waveform.read','waveform.raw.read','waveform.export'].every(can)) return Promise.reject(deniedError());
      return call('/exports/'+id, 'GET', undefined, 'inversion.export', {filename});
    },
    submit, recoverPending, prepare,
    pending: session.pending,
    get denied() {return session.denied;},
    dispose() {disposed = true; controllers.forEach(controller => controller.abort()); controllers.clear();},
  };
}
