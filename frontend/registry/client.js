function freeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

export function registryError(error) {
  if (error?.code === "registry.disabled") return "生产台账未配置，请联系管理员启用独立连接。";
  return ({401: "登录已失效，请重新登录后核查提交结果。", 403: "没有此操作权限，请联系管理员。",
    409: "台账已变更，请刷新核查结果后重新操作。", 422: "输入不符合要求，请检查字段和时间范围。",
    503: "生产台账暂不可用。写入结果可能尚未确认，请保留原操作重试。"})[error?.status]
    || "连接中断或请求超时，结果尚未确认，请保留原操作重试。";
}

export function createRegistryClient(api, {isCurrent}) {
  let selection = 0, disposed = false;
  const operations = new WeakMap();
  return {
    async selectDevice(id) {
      const generation = ++selection;
      try {
        const result = await api.get(`/api/registry/devices/${encodeURIComponent(id)}`);
        return !disposed && isCurrent() && generation === selection ? result : null;
      } catch (error) {
        if (!disposed && isCurrent() && generation === selection) throw error;
        return null;
      }
    },
    beginMutation(path, body) {
      if (disposed || !isCurrent()) throw new Error("当前页面已关闭");
      const key = crypto.randomUUID();
      const operation = {phase: "new"};
      Object.defineProperties(operation, {
        path: {value: path, enumerable: true}, key: {value: key, enumerable: true},
        body: {value: freeze(structuredClone({...body, idempotency_key: key})), enumerable: true},
      });
      operations.set(operation, {});
      return operation;
    },
    submitMutation(operation) {
      const state = operations.get(operation);
      if (!state || disposed || !isCurrent()) return Promise.reject(new Error("当前操作已关闭"));
      if (operation.phase === "sending") return state.promise;
      if (operation.phase === "succeeded") return Promise.resolve(state.result);
      if (operation.phase === "rejected") return Promise.reject(state.error);
      operation.phase = "sending";
      // Invoke immediately so a second click shares precisely this in-flight operation.
      try {
        state.promise = Promise.resolve(api.post(operation.path, operation.body));
      } catch (error) { state.promise = Promise.reject(error); }
      state.promise = state.promise.then(result => {
        operation.phase = "succeeded";
        state.result = result;
        return result;
      }, error => {
        operation.phase = !error.status || error.status === 503 ? "uncertain" : "rejected";
        state.error = error;
        throw error;
      });
      return state.promise;
    },
    dispose() { disposed = true; ++selection; },
  };
}
