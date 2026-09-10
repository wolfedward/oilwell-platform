const DEFAULT_TIMEOUT_MS = 15_000;

export class ApiError extends Error {
  constructor(message, { status = 0, code = "request_failed", details = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function parseResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (response.status === 204) return null;
  if (contentType.includes("application/json")) return response.json();
  return response.text();
}

function errorFrom(response, payload) {
  const envelope = payload && typeof payload === "object" ? payload.error : null;
  return new ApiError(
    envelope?.message || `请求失败（HTTP ${response.status}）`,
    {
      status: response.status,
      code: envelope?.code || "http_error",
      details: envelope?.details || null,
    },
  );
}

export const api = {
  async request(path, options = {}) {
    const controller = new AbortController();
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const callerSignal = options.signal;
    let signal = controller.signal;
    if (callerSignal && typeof AbortSignal.any === "function") {
      signal = AbortSignal.any([controller.signal, callerSignal]);
    } else if (callerSignal) {
      if (callerSignal.aborted) controller.abort();
      else callerSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    let body = options.body;
    if (body !== undefined && body !== null && !(body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, {
        ...options,
        body,
        headers,
        credentials: "same-origin",
        signal,
      });
      const payload = await parseResponse(response);
      if (!response.ok) {
        const error = errorFrom(response, payload);
        if (response.status === 401) {
          window.dispatchEvent(new CustomEvent("platform:unauthenticated"));
        }
        throw error;
      }
      return payload?.data ?? payload;
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new ApiError(
          timedOut ? "请求超时，请检查本机服务状态" : "请求已取消",
          { code: timedOut ? "timeout" : "cancelled" },
        );
      }
      if (error instanceof ApiError) throw error;
      throw new ApiError("无法连接本机服务", {
        code: "network_error",
        details: error?.message || null,
      });
    } finally {
      window.clearTimeout(timer);
    }
  },

  get(path, options) {
    return this.request(path, { ...options, method: "GET" });
  },

  post(path, body, options) {
    return this.request(path, { ...options, method: "POST", body });
  },

  patch(path, body, options) {
    return this.request(path, { ...options, method: "PATCH", body });
  },

  async download(path, filename) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, DEFAULT_TIMEOUT_MS);
    try {
      const response = await fetch(path, {
        credentials: "same-origin",
        signal: controller.signal,
      });
      if (!response.ok) {
        const error = errorFrom(response, await parseResponse(response));
        if (response.status === 401) {
          window.dispatchEvent(new CustomEvent("platform:unauthenticated"));
        }
        throw error;
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new ApiError(
          timedOut ? "下载超时，请检查本机服务状态" : "下载已取消",
          { code: timedOut ? "timeout" : "cancelled" },
        );
      }
      if (error instanceof ApiError) throw error;
      throw new ApiError("无法连接本机服务", {
        code: "network_error",
        details: error?.message || null,
      });
    } finally {
      window.clearTimeout(timer);
    }
  },
};
