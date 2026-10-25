/** 轻量 HTTP 层：统一 401/超时/网络错误语义，并给写操作做在失去重。
 *
 * 设计取舍（来自旧面板的血泪）：
 *  · 令牌走 header `X-Panel-Token`，不是 Cookie；只存 sessionStorage。
 *  · 任何非 2xx 都抛 ApiError，调用方好按 status/kind 分流。
 *  · 写操作按「方法+路径+body」去重：双击/连点不会打两次。
 */

export type ApiErrorKind = "unauthorized" | "timeout" | "network" | "bad_response" | "server";

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;
  readonly detail?: string;

  constructor(message: string, opts: { status?: number; kind?: ApiErrorKind; detail?: string } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status ?? 0;
    this.kind = opts.kind ?? "server";
    this.detail = opts.detail;
  }
}

export const TOKEN_KEY = "hub-panel-token";

/** 每家引擎有**自己的**面板令牌：同一令牌打另一家会 401。 */
export type EngineId = "wb" | "qd" | "trae";
export function tokenKey(engine?: EngineId): string {
  return engine ? TOKEN_KEY + "::" + engine : TOKEN_KEY;
}
const DEFAULT_TIMEOUT_MS = 20_000;

let baseUrl = "";

/** 引擎令牌刷新器：由 api.ts 注入（避免 http ↔ api 循环依赖）。
 *  401 时用它拿一张新令牌再重试一次 —— 服务重启/换密钥/槽位丢失都能自愈。 */
type Refresher = (engine: EngineId) => Promise<string | null>;
let engineTokenRefresher: Refresher | null = null;
export function setEngineTokenRefresher(fn: Refresher | null): void {
  engineTokenRefresher = fn;
}

/** 测试与多实例联调用：/?backend=http://host:port 可覆盖。 */
export function setBase(url: string): void {
  baseUrl = url.replace(/\/+$/, "");
}

export function getBase(): string {
  return baseUrl;
}

export function readToken(engine?: EngineId): string {
  try {
    const scoped = engine ? sessionStorage.getItem(tokenKey(engine)) || "" : "";
    return scoped || sessionStorage.getItem(TOKEN_KEY) || "";
  } catch { return ""; }
}

export function writeToken(token: string, engine?: EngineId): void {
  try {
    const key = tokenKey(engine);
    if (token) sessionStorage.setItem(key, token);
    else sessionStorage.removeItem(key);
  } catch { /* 隐私模式忽略 */ }
}

/** 从请求路径推断引擎：/api/wb/... → wb。取不到返回 undefined（用主槽）。 */
export function engineOfPath(path: string): EngineId | undefined {
  const m = /^\/api\/(wb|qd|trae)(\/|$)/.exec(path);
  return m ? (m[1] as EngineId) : undefined;
}

/** 在失去重表：key = METHOD path body。 */
const inflight = new Map<string, Promise<unknown>>();

export type RequestOpts = {
  method?: string;
  body?: unknown;
  query?: Record<string, string | number | undefined>;
  timeoutMs?: number;
  /** 写操作默认去重；读操作不去重（允许并发刷新）。 */
  dedupe?: boolean;
  /** 按次覆盖令牌（hub 保存要用「面板口令」而不是引擎令牌）。 */
  token?: string;
  signal?: AbortSignal;
};

function buildUrl(path: string, query?: RequestOpts["query"]): string {
  const url = baseUrl + path;
  if (!query) return url;
  const qs = Object.entries(query)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(String(v)))
    .join("&");
  return qs ? url + (url.includes("?") ? "&" : "?") + qs : url;
}

export async function request<T = unknown>(path: string, opts: RequestOpts = {}): Promise<T> {
  const method = (opts.method || "GET").toUpperCase();
  const isWrite = method !== "GET" && method !== "HEAD";
  const url = buildUrl(path, opts.query);
  const key = method + " " + url + " " + (opts.body === undefined ? "" : JSON.stringify(opts.body));
  const shouldDedupe = opts.dedupe ?? isWrite;

  if (shouldDedupe) {
    const running = inflight.get(key);
    if (running) return running as Promise<T>;
  }

  const task = (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? DEFAULT_TIMEOUT_MS);
    const onAbort = () => controller.abort();
    opts.signal?.addEventListener("abort", onAbort);

    try {
      const engine = engineOfPath(path);
      const scoped = engine ? (() => { try { return sessionStorage.getItem(tokenKey(engine)) || ""; } catch { return ""; } })() : "";
      const main = readToken();
      const headers: Record<string, string> = { Accept: "application/json" };
      const token = opts.token !== undefined ? opts.token : readToken(engine);
      if (token) headers["X-Panel-Token"] = token;
      if (opts.body !== undefined) headers["Content-Type"] = "application/json";

      const res = await fetch(url, {
        method,
        headers,
        credentials: "same-origin",
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        signal: controller.signal,
      });

      // 引擎槽里可能存着「上一轮进程」的旧令牌（服务重启/密钥轮换后失效）。
      // 命中 401 且用的是引擎槽 → 清掉坏槽、用主令牌重试一次：面板自愈，用户不用手动清缓存。
      if (res.status === 401 && opts.token === undefined && engine && scoped && main && scoped !== main) {
        writeToken("", engine);
        opts.signal?.removeEventListener("abort", onAbort);
        clearTimeout(timer);
        inflight.delete(key);
        return await request<T>(path, { ...opts, token: main, dedupe: false });
      }

      // 第一次 401：让 api 层用内存口令给这家重登一次，拿到新令牌就重试（只试一次）。
      const isLoginPath = /\/panel\/login$/.test(path);
      if (res.status === 401 && !isLoginPath && opts.token === undefined && engine && engineTokenRefresher && !(opts as { _retried?: boolean })._retried) {
        const fresh = await engineTokenRefresher(engine);
        if (fresh) {
          writeToken(fresh, engine);
          opts.signal?.removeEventListener("abort", onAbort);
          clearTimeout(timer);
          inflight.delete(key);
          return await request<T>(path, { ...opts, token: fresh, dedupe: false, _retried: true } as RequestOpts);
        }
      }

      if (res.status === 401) {
        // 令牌轮换（服务重启/换密钥）后会全站 401：清掉所有槽并广播，
        // 由登录门接住并重新要密码 —— 比在视图里显示"某家没数据"诚实得多。
        if (opts.token === undefined && (!engine || token === main || !scoped)) {
          try {
            for (const k of Object.keys(sessionStorage)) {
              if (k.indexOf(TOKEN_KEY) === 0) sessionStorage.removeItem(k);
            }
          } catch { /* 隐私模式 */ }
          try { window.dispatchEvent(new CustomEvent("hub-unauthorized")); } catch { /* SSR/测试 */ }
        }
        throw new ApiError("unauthorized", { status: 401, kind: "unauthorized" });
      }

      const text = await res.text();
      let data: unknown = null;
      if (text) {
        try { data = JSON.parse(text); } catch { data = { raw: text.slice(0, 400) }; }
      }

      if (!res.ok) {
        const body = data as { error?: unknown } | null;
        const detail =
          typeof body?.error === "string" ? body.error
          : (body?.error as { message?: string } | undefined)?.message;
        throw new ApiError(detail || "HTTP " + res.status, {
          status: res.status,
          kind: "server",
          detail,
        });
      }

      return data as T;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if ((err as Error)?.name === "AbortError") {
        throw new ApiError("timeout", { kind: "timeout" });
      }
      throw new ApiError((err as Error)?.message || "network", { kind: "network" });
    } finally {
      clearTimeout(timer);
      opts.signal?.removeEventListener("abort", onAbort);
      if (shouldDedupe) inflight.delete(key);
    }
  })();

  if (shouldDedupe) inflight.set(key, task);
  return task;
}

export const get = <T = unknown>(path: string, opts: Omit<RequestOpts, "method" | "body"> = {}) =>
  request<T>(path, { ...opts, method: "GET" });

export const post = <T = unknown>(path: string, body?: unknown, opts: Omit<RequestOpts, "method" | "body"> = {}) =>
  request<T>(path, { ...opts, method: "POST", body });

/** 仅供测试：清空在失去重表 */
export function __resetInflight(): void {
  inflight.clear();
}
