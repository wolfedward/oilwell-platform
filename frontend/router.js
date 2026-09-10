const loaders = new Map();
const listeners = new Set();
let current = null;
let activationSequence = 0;

function encodeRoute(pageId, params) {
  const query = new URLSearchParams(params || {}).toString();
  return `#/${encodeURIComponent(pageId)}${query ? `?${query}` : ""}`;
}

function decodeRoute(hash = window.location.hash) {
  try {
    const raw = hash.replace(/^#\/?/, "");
    const [encodedPage, query = ""] = raw.split("?", 2);
    return {
      pageId: decodeURIComponent(encodedPage || "dashboard"),
      params: Object.fromEntries(new URLSearchParams(query)),
    };
  } catch (error) {
    if (!(error instanceof URIError)) throw error;
    return { pageId: "dashboard", params: {} };
  }
}

async function activate(pageId, params, historyMode) {
  const loader = loaders.get(pageId);
  if (!loader) throw new Error(`Unknown page: ${pageId}`);
  const activationId = ++activationSequence;
  const result = await loader({ ...params });
  if (activationId !== activationSequence) return result;
  if (historyMode === "push") history.pushState(null, "", encodeRoute(pageId, params));
  if (historyMode === "replace") history.replaceState(null, "", encodeRoute(pageId, params));
  current = { pageId, params: { ...params } };
  listeners.forEach((listener) => listener({ ...current }));
  return result;
}

export const router = {
  register(pageId, loader) {
    if (!pageId || typeof loader !== "function") throw new TypeError("invalid route registration");
    loaders.set(pageId, loader);
    return this;
  },

  open(pageId, params = {}) {
    return activate(pageId, params, "push");
  },

  restore() {
    const route = decodeRoute();
    const pageId = loaders.has(route.pageId) ? route.pageId : "dashboard";
    return activate(pageId, pageId === route.pageId ? route.params : {}, "replace");
  },

  refresh() {
    const route = current || decodeRoute();
    return activate(route.pageId, route.params, null);
  },

  subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },

  current() {
    return current ? { ...current, params: { ...current.params } } : null;
  },
};

window.addEventListener("popstate", () => router.restore());
