import {ApiError} from '../api.js';

const BASE = '/api/production-studies';
const uncertain = error => ['timeout', 'network_error', 'cancelled'].includes(error?.code) || error?.status === 503;
const clone = value => JSON.parse(JSON.stringify(value));

export function createStudiesClient(api, actorId, isCurrent = () => true) {
  const prefix = `production-studies:pending:${actorId}:`;
  const pending = new Map();
  let disposed = false;
  const controller = new AbortController();
  const current = () => !disposed && isCurrent();
  const checkCurrent = () => {if(!current())throw new ApiError('页面已关闭', {code:'cancelled'});};
  const storage = (() => {try {return window.sessionStorage;} catch {return null;}})();
  const key = scope => `${prefix}${scope}`;
  function getPending(scope) {
    if (pending.has(scope)) return pending.get(scope);
    try {const saved = storage?.getItem(key(scope));if(saved){const value=JSON.parse(saved);pending.set(scope,value);return value;}} catch { /* private mode */ }
    return null;
  }
  function save(scope, item) {pending.set(scope,item);try {storage?.setItem(key(scope),JSON.stringify(item));} catch { /* private mode */ }}
  function clear(scope) {pending.delete(scope);try {storage?.removeItem(key(scope));} catch { /* private mode */ }}
  async function mutate(scope, path, build, metadata = {}) {
    checkCurrent();
    const saved = getPending(scope);
    const item = saved || {path, body: clone(build()), ...metadata};
    if (!saved) save(scope,item);
    try {
      if (saved && scope === 'create') {
        try {const receipt = await api.get(`${BASE}/requests/${item.body.request_id}`, {signal:controller.signal});checkCurrent();clear(scope);return receipt;}
        catch (error) {checkCurrent();if (error?.status !== 404) throw error;}
      }
      checkCurrent();
      const response = await api.post(item.path, item.body, {timeoutMs: 120000, signal:controller.signal});
      checkCurrent();
      clear(scope);
      return response;
    } catch (error) {
      if (!uncertain(error)) clear(scope);
      throw error;
    }
  }
  return {
    current, dispose() {disposed = true;controller.abort();}, pending: getPending,
    async list(kind, offset=0) {checkCurrent();const data=await api.get(`${BASE}?${kind ? `kind=${encodeURIComponent(kind)}&` : ''}limit=50&offset=${offset}`, {signal:controller.signal});checkCurrent();return data;},
    async get(id) {checkCurrent();const data=await api.get(`${BASE}/${encodeURIComponent(id)}`, {signal:controller.signal});checkCurrent();return data;},
    create: body => mutate('create', BASE, () => body),
    review: (id, body) => mutate(`review:${id}`, `${BASE}/${encodeURIComponent(id)}/reviews`, () => body),
    select: (kind, body, targetId) => mutate(`select:${kind}`, `${BASE}/selections/${encodeURIComponent(kind)}`, () => body, {target_id:targetId}),
    async export(id) {checkCurrent();await api.download(`${BASE}/${encodeURIComponent(id)}/export`, `study-${id}.json`, {signal:controller.signal});checkCurrent();},
  };
}
