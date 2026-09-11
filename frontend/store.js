const listeners = new Set();

function clone(value) {
  return globalThis.structuredClone
    ? globalThis.structuredClone(value)
    : JSON.parse(JSON.stringify(value));
}

let state = Object.freeze({
  user: null,
  activeModule: "overview",
  activePage: "dashboard",
  tabs: ["dashboard"],
  tabParams: { dashboard: {} },
  sidebarCollapsed: false,
  loading: false,
});

function snapshot() {
  return clone(state);
}

export const store = {
  getState() {
    return snapshot();
  },

  setState(patch) {
    const update = typeof patch === "function" ? patch(snapshot()) : patch;
    if (!update || typeof update !== "object") return snapshot();
    state = Object.freeze({ ...clone(state), ...clone(update) });
    const current = snapshot();
    listeners.forEach((listener) => listener(current));
    return current;
  },

  subscribe(listener) {
    if (typeof listener !== "function") throw new TypeError("listener must be a function");
    listeners.add(listener);
    return () => listeners.delete(listener);
  },

  reset() {
    return this.setState({
      user: null,
      activeModule: "overview",
      activePage: "dashboard",
      tabs: ["dashboard"],
      tabParams: { dashboard: {} },
      sidebarCollapsed: false,
      loading: false,
    });
  },
};
