/** 业务 API：面板鉴权 + 三家引擎 + 聚合层。
 *
 * 端点清单来自 docs/merge/02-接口实测样本.md（实测，不靠记忆）。
 * 合并态（/api/hub/*）与引擎态（/api/<engine>/*）并存：
 *  · 引擎态：单家读写（账号、设置、定价…）
 *  * 聚合态：跨三家的只读视图（usage/keys/health）
 */

import { get, post, ApiError, readToken, writeToken, setBase, type EngineId , setEngineTokenRefresher } from "./http";

/** 门户的接口全在 /api 下（静态资源在根）。所有请求都必须带这个前缀。 */
export const API = "/api";

export const ENGINES = ["wb", "qd", "trae"] as const;
export type Engine = (typeof ENGINES)[number];

export const ENGINE_LABELS: Record<Engine, string> = {
  wb: "WorkBuddy",
  qd: "Qoder",
  trae: "Trae",
};

export function isEngine(v: string): v is Engine {
  return (ENGINES as readonly string[]).includes(v);
}

// ---------------------------------------------------------------- 鉴权

export type LoginResult =
  | { ok: true; token: string; usingDefaultPassword: boolean; degraded?: Engine }
  | { ok: false; error: string };

/** 登录：默认打「当前引擎」，失败则挨家试 —— 某一家挂了不该让整站登不上。 */
export async function login(password: string, engine: Engine = "wb"): Promise<LoginResult> {
  const attempt = async (eng: Engine) => {
    const data = await post<{ token?: string; panel_token?: string; ok?: boolean; using_default_password?: boolean }>(
      API + "/" + eng + "/panel/login",
      { password },
      { dedupe: true },
    );
    const token = data?.token || data?.panel_token || "";
    if (!token) throw new ApiError("login failed", { status: 502, kind: "bad_response" });
    return { token, usingDefaultPassword: !!data?.using_default_password };
  };

  const order: Engine[] = [engine, ...ENGINES.filter((e) => e !== engine)];
  let lastErr = "login failed";
  for (const eng of order) {
    try {
      panelPassword = password;
      const r = await attempt(eng);
      writeToken(r.token);
      writeToken(r.token, eng);
      // 同一密码去登其余引擎（各家令牌独立）；失败静默，切到那家时再单独登。
      await fillEngineSlots(password, eng);
      return {
        ok: true,
        token: r.token,
        usingDefaultPassword: r.usingDefaultPassword,
        degraded: eng === engine ? undefined : eng,
      };
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return { ok: false, error: "unauthorized" };
      lastErr = (err as Error)?.message || "login failed";
    }
  }
  return { ok: false, error: lastErr };
}

/** 逐引擎补登：把同一密码登进其余两家，各自存各自的令牌槽。 */
async function fillEngineSlots(password: string, skip: Engine): Promise<void> {
  await Promise.all(
    ENGINES.filter((e) => e !== skip).map(async (eng) => {
      try {
        const r = await attemptLogin(eng, password);
        writeToken(r.token, eng);
      } catch { /* 那家密码不同或不可用：忽略 */ }
    }),
  );
}

/** 内存里的面板口令：仅在本次会话内保存，用于 401 时自动补登（不落 storage）。 */
let panelPassword = "";

/** 注入给 http 层：某家 401 → 用同一口令重登该家，返回新令牌。 */
setEngineTokenRefresher(async function (eng: EngineId): Promise<string | null> {
  if (!panelPassword) return null;
  try {
    const r = await attemptLogin(eng as Engine, panelPassword);
    return r.token;
  } catch {
    return null;
  }
});

async function attemptLogin(eng: Engine, password: string): Promise<{ token: string }> {
  const data = await post<{ token?: string; panel_token?: string }>(
    API + "/" + eng + "/panel/login",
    { password },
    { dedupe: true, timeoutMs: 8000 },
  );
  const token = data?.token || data?.panel_token || "";
  if (!token) throw new ApiError("login failed", { status: 502, kind: "bad_response" });
  return { token };
}

export async function logout(engine: Engine = "wb"): Promise<void> {
  try { await post(API + "/" + engine + "/panel/logout", {}); } catch { /* 忽略 */ }
  writeToken("");
}

export function hasToken(): boolean {
  return !!readToken();
}

// ------------------------------------------------------------ 引擎态读

export type Realm = "cn" | "intl";

export type HealthPayload = {
  ok?: boolean;
  service?: string;
  version?: string;
  realm?: string;
  accounts?: number;
  accounts_ready?: number;
  api_key_required?: boolean;
  configured?: boolean;
  [k: string]: unknown;
};

export const engineHealth = (eng: Engine) => get<HealthPayload>(API + "/" + eng + "/health");

export type AccountsPayload = {
  accounts?: Account[];
  storage?: string;
  usable?: number;
  [k: string]: unknown;
};

export type Account = {
  tokenExpired?: boolean; needRelogin?: boolean; hasRefreshToken?: boolean;
  lastErrorDetail?: unknown; expiresAt?: number; expiresIn?: string;
  inCooldown?: boolean; cooldownFor?: string | null; restrictedFor?: string | null;
  fails?: number; source?: string; disabledModels?: string[];
  uid?: string;
  id?: string;
  nickname?: string;
  email?: string;
  realm?: string;
  enabled?: boolean;
  platform?: string;
  product?: string;
  note?: string;
  weight?: number;
  lastError?: string;
  last_error?: string;
  [k: string]: unknown;
};

export const engineAccounts = (eng: Engine, realm?: Realm) =>
  get<AccountsPayload>(API + "/" + eng + "/accounts", { query: realm ? { realm } : undefined });

export type ModelsPayload = { object?: string; data?: ModelRow[]; realm?: string };
export type ModelRow = {
  id?: string;
  object?: string;
  owned_by?: string;
  context_window?: number;
  max_output_tokens?: number;
  [k: string]: unknown;
};

export const engineModels = (eng: Engine, realm?: Realm) =>
  get<ModelsPayload>(API + "/" + eng + "/models", { query: realm ? { realm } : undefined });

export type LogsPayload = { total?: number; logs?: LogRow[]; max_id?: number };
export type LogRow = { at?: string | number; level?: string; tag?: string; msg?: string; [k: string]: unknown };

export const engineLogs = (eng: Engine, limit = 200) =>
  get<LogsPayload>(API + "/" + eng + "/logs", { query: { limit } });

export type UsagePayload = {
  requests?: number;
  errors?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  credit?: number;
  cost_cny?: number;
  by_model?: Record<string, number>;
  [k: string]: unknown;
};

export const engineUsage = (eng: Engine) => get<UsagePayload>(API + "/" + eng + "/usage");

export type SettingsPayload = Record<string, unknown> & {
  api_keys?: ApiKey[];
  deleted_api_keys?: ApiKey[];
  limits?: Record<string, number>;
};

export type ApiKey = {
  id?: string;
  name?: string;
  realm?: string;
  models?: string[];
  enabled?: boolean;
  masked?: string;
  [k: string]: unknown;
};

export const engineSettings = (eng: Engine) => get<SettingsPayload>(API + "/" + eng + "/settings");

// ------------------------------------------------------------ 引擎态写

export const saveSettings = (eng: Engine, patch: Record<string, unknown>) =>
  post<{ ok?: boolean; settings?: SettingsPayload }>(API + "/" + eng + "/settings/save", patch);

export const setAccount = (eng: Engine, uid: string, patch: Record<string, unknown>) =>
  post<{ account?: Account }>(API + "/" + eng + "/accounts/set", { uid, ...patch });

export const deleteAccount = (eng: Engine, uid: string) =>
  post<{ deleted?: boolean }>(API + "/" + eng + "/accounts/delete", { uid });

export const setAllAccounts = (eng: Engine, enabled: boolean) =>
  post<{ updated?: number }>(API + "/" + eng + "/accounts/set-all", { enabled });

// ------------------------------------------------------------ 聚合态

export type HubHealth = {
  ok?: boolean;
  version?: string;
  engines?: Partial<Record<Engine, HealthPayload | null>>;
  [k: string]: unknown;
};

export const hubHealth = () => get<HubHealth>(API + "/hub/health");

export type HubUsage = Partial<Record<Engine, UsagePayload>> & { [k: string]: unknown };

export type RangeOpts = { range?: string; since?: string; until?: string };
/** 区间过滤：不传 = 引擎默认（全量）。 */
export const hubUsage = (opts?: RangeOpts) => get<HubUsage>(API + "/hub/usage", { query: opts as Record<string, string | undefined>, timeoutMs: 60000 });

export type HubKeys = {
  merged?: MergedKey[];
  rules?: Record<string, unknown>;
  [k: string]: unknown;
};

export type MergedKey = { id?: string; name?: string; key?: string; enabled?: boolean; [k: string]: unknown };

export const hubKeys = () => get<HubKeys>(API + "/hub/keys");

/** 保存合并 key：hub 端点认的是「面板口令」，不是引擎令牌，故按次覆盖 header。 */
export const hubSaveKeys = (mergedKeys: MergedKey[], password: string) =>
  post<{ ok?: boolean }>(API + "/hub/settings/save", { merged_keys: mergedKeys }, { dedupe: true, token: password });

export type RouteStats = { total?: number; today?: Record<string, number> };
export const hubRouteStats = () => get<RouteStats>(API + "/hub/route-stats");

export const hubRouteLog = (limit = 50) => get<{ rows?: unknown[]; total?: number; error?: string }>(API + "/hub/route-log", { query: { limit } });

// ------------------------------------------------------- 合并（三路并发）

export type Settled<T> = { ok: true; value: T } | { ok: false; error: string };

/** 对每家引擎并发取数：任何一家失败都不影响其它家（返回 partial）。 */
export async function fanout<T>(
  fn: (eng: Engine, realm?: Realm) => Promise<T>,
  realms?: Partial<Record<Engine, Realm>>,
): Promise<{ values: Partial<Record<Engine, T>>; failed: Engine[] }> {
  const settled = await Promise.all(
    ENGINES.map(async (eng) => {
      try {
        return [eng, { ok: true as const, value: await fn(eng, realms?.[eng]) }] as const;
      } catch (err) {
        return [eng, { ok: false as const, error: (err as Error)?.message || "failed" }] as const;
      }
    }),
  );
  const values: Partial<Record<Engine, T>> = {};
  const failed: Engine[] = [];
  for (const [eng, r] of settled) {
    if (r.ok) values[eng] = r.value;
    else failed.push(eng);
  }
  return { values, failed };
}

export { setBase, readToken, writeToken, ApiError };
export type { EngineId };

/** 本会话记下的面板口令（登录时写入内存），用于需要口令的 hub 写操作。 */
export function getPanelPassword(): string {
  return panelPassword;
}

/** 允许显式注入口令（测试与特殊场景）；生产路径由 login 写入。 */
export function setPanelPassword(pw: string): void {
  panelPassword = pw;
}

/** 合并 Key 列表（只有掩码，明文只在创建时出现一次）。 */
export type MergedKeyRow = { id?: string; name?: string; key?: string; [k: string]: unknown };
export type HubKeysDetail = {
  ok?: boolean;
  merged?: MergedKeyRow[];
  counts?: Record<string, number>;
  rules?: Record<string, string>;
  wb?: MergedKeyRow[];
  qd?: MergedKeyRow[];
  trae?: MergedKeyRow[];
};

/** 详细 key 列表（含三家引擎各自的 key）。 */
export const hubKeysDetail = () => get<HubKeysDetail>(API + "/hub/keys");

/** 保存合并 key 表（整表替换，需要面板口令）。 */
/**
 * 保存合并 key 表（整表替换，需要面板口令）。
 * 口令优先用调用方传的（UI 就地输入），否则用本会话记住的；都没有就明确报错。
 */
export async function hubSaveMerged(keys: MergedKeyRow[], password?: string): Promise<void> {
  const pw = (password ?? panelPassword).trim();
  if (!pw) throw new ApiError("需要面板口令：请重新登录后再保存", { status: 401, kind: "unauthorized" });
  await post(API + "/hub/settings/save", { merged_keys: keys }, { token: pw, dedupe: true });
}

/** 网关自检（一次跑完所有判据）。 */
export type SelfCheck = {
  ok?: boolean;
  version?: string;
  checks?: { id: string; label: string; ok: boolean; detail?: string }[];
};
export const hubSelfcheck = () => get<SelfCheck>(API + "/hub/selfcheck", { timeoutMs: 30000 });

/** 调度器状态：每家引擎的开关与排程说明。 */
export type SchedulerState = {
  engines?: Record<string, { enabled?: boolean; mode?: string; mode_cn?: string; next?: string }>;
};
export const hubScheduler = () => get<SchedulerState>(API + "/hub/scheduler");

/** 翻转某家引擎的调度开关。 */
export const engineSchedulerToggle = (eng: Engine, enabled: boolean) =>
  post<{ ok?: boolean; enabled?: boolean }>(API + "/" + eng + "/scheduler/toggle", { enabled });

/* ── 账号行级动作（旧面板主力功能，之前整个漏了） ───────────── */

/** 试一条真实请求，验证这把账号还能不能用。 */
export type AccountTest = { ok?: boolean; uid?: string; model?: string; elapsed_ms?: number; reply?: string; error?: string };
export const testAccount = (eng: Engine, uid: string) =>
  post<AccountTest>(API + "/" + eng + "/accounts/test", { uid }, { timeoutMs: 60000 });

/** 刷新账号额度（不传 uid 就是全部）。 */
export type BulkResult = { results?: { uid?: string; ok?: boolean; error?: string }[] };
export const refreshAccounts = (eng: Engine, uid?: string) =>
  post<BulkResult>(API + "/" + eng + "/accounts/refresh", uid ? { uid } : {}, { timeoutMs: 120000 });

/** 签到（不传 uid = 该引擎全部国内账号）。 */
export const checkinAccounts = (eng: Engine, uid?: string) =>
  post<{ results?: { uid?: string; nickname?: string; ok?: boolean; code?: number; msg?: string }[] }>(
    API + "/" + eng + "/accounts/checkin", uid ? { uid } : {}, { timeoutMs: 120000 },
  );

/** 查积分（refresh=true 时顺带向上游刷新）。 */
export type CreditsResult = { results?: { uid?: string; ok?: boolean; credits?: { remain?: number; used?: number } }[] };
/** 注意：这条路由**只接受 POST** —— 用 GET 会被上游直接掐断（实测 502）。 */
export const accountCredits = (eng: Engine, uid?: string, refresh = false) =>
  post<CreditsResult>(API + "/" + eng + "/accounts/credits",
    refresh ? { uid: uid, refresh: true } : { uid: uid }, { timeoutMs: 60000 });

/* ── 导入账号 / 改面板密码 ─────────────────────────────────── */

/** 扫描本机桌面客户端里已登录的账号（只读，不导入）。 */
export type DesktopAccount = { uid?: string; nickname?: string; email?: string; [k: string]: unknown };
export type DesktopScan = {
  detected?: { path?: string; file?: string; [k: string]: unknown }[];
  accounts?: DesktopAccount[];
  pool_uids?: string[];
};
export const scanDesktop = (eng: Engine) =>
  post<DesktopScan>(API + "/" + eng + "/accounts/import/desktop", {}, { timeoutMs: 60000 });

/** 把扫描结果导入（all=true 全导，否则只导选中的 uid 列表）。 */
export const importDesktop = (eng: Engine, payload: { all?: boolean; uids?: string[] }) =>
  post<{ added?: number; updated?: number; skipped?: number; error?: string }>(
    API + "/" + eng + "/accounts/import/desktop", payload, { timeoutMs: 120000 },
  );

/** JSON 导入：支持本网关导出文件 / 账号数组 / 单个账号对象。 */
export const importAccounts = (eng: Engine, accounts: unknown[]) =>
  post<{ added?: number; updated?: number; skipped?: number; invalid?: number; error?: string }>(
    API + "/" + eng + "/accounts/import", { accounts: accounts }, { timeoutMs: 120000 },
  );

/** PAT 导入：只有 Qoder 有这条路由。 */
export const importPat = (eng: Engine, pat: string, realm?: string) =>
  post<{ ok?: boolean; error?: string }>(API + "/" + eng + "/accounts/import/pat",
    realm ? { pat: pat, realm: realm } : { pat: pat }, { timeoutMs: 60000 });

/** 改面板密码：成功后**令牌会轮换**，必须把新令牌写回，否则当前会话立刻失效。 */
export async function changePanelPassword(eng: Engine, current: string, next: string): Promise<void> {
  const res = await post<{ ok?: boolean; token?: string }>(
    API + "/" + eng + "/panel/password", { current: current, new: next }, { dedupe: true },
  );
  if (res?.token) {
    writeToken(res.token, eng);
    writeToken(res.token);
    panelPassword = next;
  }
}

/** 立即巡检（触发调度器跑一轮）。 */
export const triggerScheduler = (eng: Engine) =>
  post<{ ok?: boolean; msg?: string }>(API + "/" + eng + "/scheduler/trigger", {}, { timeoutMs: 120000 });

/** 清空该引擎的运行日志。 */
export const clearLogs = (eng: Engine) =>
  post<{ ok?: boolean; cleared?: number }>(API + "/" + eng + "/logs/clear", {});

/* ── 用量透视（按账号 / 性能 / 最近请求） ─────────────────── */

/** 按账号汇总：谁的请求最多、token 最多。 */
export type UsageAccountRow = {
  account?: string; requests?: number; prompt_tokens?: number; completion_tokens?: number;
  total_tokens?: number; cached_tokens?: number; credit?: number;
};
export const usageByAccount = (eng: Engine, opts?: RangeOpts) =>
  get<{ accounts?: UsageAccountRow[] }>(API + "/" + eng + "/usage/by-account", {
    query: opts as Record<string, string | undefined>,
    timeoutMs: 60000,
  });

/** 性能分位：首字/生成 p50·p90·p99，以及成功率。 */
export type UsagePerf = {
  sampled?: number; success?: number; errors?: number; client_aborted?: number;
  success_rate_pct?: number; sample_capped?: boolean;
  ttft_ms?: { avg?: number; p50?: number; p90?: number; p99?: number; samples?: number };
  generation_ms?: { avg?: number; p50?: number; p90?: number; samples?: number };
};
export const usagePerf = (eng: Engine) => get<UsagePerf>(API + "/" + eng + "/usage/perf");

/** 最近请求（用量账本，逐条带时间戳 —— 画时序比 logs 更准）。 */
export type UsageRecentRow = { at?: number; iso?: string; model?: string; account?: string; total_tokens?: number; [k: string]: unknown };
export const usageRecent = (eng: Engine, limit = 200) =>
  get<{ total?: number; rows?: UsageRecentRow[] }>(API + "/" + eng + "/usage/recent", { query: { limit: limit } });

/* ── 定价 ─────────────────────────────────────────────────── */

export type PricingState = {
  interval_minutes?: number; enabled?: boolean; master_enabled?: boolean; running?: boolean;
  last_run?: number | null; last_run_label?: string;
  [k: string]: unknown;
};
export const pricingState = (eng: Engine) => get<PricingState>(API + "/" + eng + "/pricing");
/** 立即刷新定价（返回刷新后的状态）。 */
export const pricingRefresh = (eng: Engine) =>
  post<PricingState>(API + "/" + eng + "/pricing/refresh", {}, { timeoutMs: 120000 });

/* ── OAuth 导入（第三种正规导入方式） ─────────────────────── */

export type OAuthStart = { state?: string; authUrl?: string; realm?: string; platform?: string };
export const oauthStart = (eng: Engine, realm?: string) =>
  post<OAuthStart>(API + "/" + eng + "/accounts/login/start", realm ? { realm: realm } : {});

export type OAuthPoll = { status?: string; message?: string; account?: unknown };
export const oauthPoll = (eng: Engine, state: string) =>
  get<OAuthPoll>(API + "/" + eng + "/accounts/login/poll", { query: { state: state } });

export const oauthCancel = (eng: Engine, state?: string) =>
  post<{ ok?: boolean }>(API + "/" + eng + "/accounts/login/cancel", state ? { state: state } : {});

/* ── 积分明细（客户包，解释"积分为什么少"） ───────────────── */

export type CreditPackage = {
  name?: string; remain?: number; size?: number; unit?: string;
  cycle_end_time?: string; days_left?: number | null; is_expired?: boolean;
};
export type CreditsDetail = {
  ok?: boolean; uid?: string; nickname?: string; realm?: string;
  credits?: { remain?: number; used?: number; size?: number; used_percent?: string; remain_percent?: string; packages?: CreditPackage[] };
};
export const accountCreditsDetail = (eng: Engine, uid: string, refresh = false) =>
  get<CreditsDetail>(API + "/" + eng + "/accounts/credits/detail", {
    query: refresh ? { uid: uid, refresh: "1" } : { uid: uid }, timeoutMs: 60000,
  });

/* ── 代理槽位 ─────────────────────────────────────────────── */

export type ProxySlot = {
  id?: string; name?: string; url?: string; enabled?: boolean; label?: string;
  ip?: string; country?: string; country_code?: string; ip_type?: string; isp?: string;
  probed_at?: number; bound?: number;
};
export type ProxyCandidate = {
  ok?: boolean; url?: string; exit_ip?: string; latency_ms?: number; error?: string;
  country?: string; country_code?: string; ip_type?: string; isp?: string;
};
export const proxySlots = (eng: Engine) =>
  get<{ slots?: ProxySlot[] }>(API + "/" + eng + "/proxy/slots");
/** 整表保存（新增/删除/改名都走它）。 */
export const proxySlotsSave = (eng: Engine, slots: ProxySlot[]) =>
  post<{ slots?: ProxySlot[] }>(API + "/" + eng + "/proxy/slots/save", { slots: slots });
/** 按 **id** 测试一个槽位（传 url 会 404「no such proxy slot」）。 */
export const proxySlotTest = (eng: Engine, id: string) =>
  post<{ ok?: boolean; ip?: string; country?: string; latency_ms?: number; error?: string }>(
    API + "/" + eng + "/proxy/slots/test", { id: id }, { timeoutMs: 60000 },
  );
/** 扫描本机常见代理端口，返回可用候选。 */
export const proxyDiscover = (eng: Engine) =>
  post<{ candidates?: ProxyCandidate[] }>(API + "/" + eng + "/proxy/discover", {}, { timeoutMs: 90000 });

/** 授权页能否在面板里内嵌（服务端探 X-Frame-Options / CSP）。 */
export const frameCheck = (url: string) =>
  get<{ frameable?: boolean; reason?: string }>(API + "/hub/frame-check", { query: { url } });

/** 取回引擎 Key 的明文（面板会话必需；只给「复制」用）。 */
export const revealEngineKey = (eng: Engine, id: string) =>
  get<{ id?: string; key?: string }>(API + "/" + eng + "/settings/reveal", { query: { id: id } });

/** 取回合并 Key 的明文 —— 需要面板口令本体（和 settings/save 同一套授权）。 */
export const revealMergedKey = (id: string, password: string) =>
  get<{ id?: string; key?: string }>(API + "/hub/keys/reveal", { query: { id: id }, token: password });

/**
 * 用一把**明文 key** 真打一次 /v1/models。
 * 「明明有 key 却提示无效」时，这个按钮能把问题钉死在哪一端：
 *   200 = 这把 key 网关认、路由通；401 = 网关不认这把（掩码/空格/已删）。
 */
export async function probeKey(key: string): Promise<{ ok: boolean; status: number; detail: string }> {
  const token = String(key || "").trim();
  if (!token) return { ok: false, status: 0, detail: "empty" };
  try {
    const res = await fetch("/v1/models", { headers: { Authorization: "Bearer " + token } });
    if (res.ok) return { ok: true, status: res.status, detail: "" };
    let detail = "";
    try {
      const body = await res.json();
      detail = String(body?.error?.message ?? body?.error ?? "");
    } catch {
      detail = "";
    }
    return { ok: false, status: res.status, detail: detail };
  } catch (e) {
    return { ok: false, status: 0, detail: (e as Error).message };
  }
}
/** /tasks：trae 的积分和签到状态只在这里（accounts 载荷里没有）。 */
export type TasksPayload = {
  tasks?: { task_code?: string; last_run?: string; status?: string; ok?: boolean }[];
  accounts?: { nickname?: string; realm?: string; credits?: { remain?: number; size?: number } }[];
};
export const engineTasks = (eng: Engine) => get<TasksPayload>(API + "/" + eng + "/tasks", { timeoutMs: 90000 });

/** 三家合并的时序（按桶）。range: today/7d/30d/month/all；也支持 since/until/bucket。 */
export type TimeseriesPayload = {
  labels?: number[];
  series?: Record<string, number[]>;
  tokens?: Record<string, number[]>;
  sum_tokens?: number[]; sum_credit?: number[];
  sum_prompt?: number[]; sum_cached?: number[]; sum_errors?: number[];
  totals?: { requests?: number };
  errors?: Record<string, string | null>;
  bucket_seconds?: number;
};
export const hubTimeseries = (opts: { range?: string; since?: string; until?: string; bucket?: number }) =>
  get<TimeseriesPayload>(API + "/hub/timeseries", { query: opts as Record<string, string | number>, timeoutMs: 60000 });

/** 按模型合并的用量（同名模型跨引擎求和）。 */
export type ByModelPayload = { by_model?: Record<string, { requests?: number; total_tokens?: number; prompt_tokens?: number; cached_tokens?: number }> };
export const hubByModel = (opts?: RangeOpts) => get<ByModelPayload>(API + "/hub/by-model", { query: opts as Record<string, string | undefined>, timeoutMs: 60000 });

/** 跑一条维护任务（token_keepalive / usage_refresh / daily_checkin …）。
 *
 * 为什么要它：面板的「刷新积分」原来调的是 /accounts/refresh —— 那是**刷新令牌**，
 * 不是拉积分（trae 会回「token 未进入刷新窗口」而什么都不做）。积分要靠引擎的
 * usage_refresh 任务去上游取。
 */
export const runTask = (eng: Engine, taskCode: string) =>
  post<{ ok?: boolean; logs?: string[] }>(API + "/" + eng + "/tasks/run", { task_code: taskCode }, { timeoutMs: 180000 });

/** 版本检查：默认走 6 小时缓存；force=true 直接问 GitHub。 */
export type UpdateInfo = {
  current?: string; latest?: string; update_available?: boolean;
  url?: string; notes?: string; checked_at?: number; error?: string | null;
  cached?: boolean; zip?: string; published_at?: string;
};
export const hubUpdate = (force = false) =>
  get<UpdateInfo>(API + "/hub/update" + (force ? "?refresh=1" : ""), { timeoutMs: 20000 });
export const hubUpdateApply = () =>
  post<{ ok: boolean; updated?: boolean; from?: string; to?: string; written?: number;
         skipped?: number; restart_required?: boolean; reason?: string }>(
    API + "/hub/update/apply", {}, { timeoutMs: 120000 });
export const hubRestart = () =>
  post<{ ok: boolean; restarting?: boolean; reason?: string }>(API + "/hub/update/restart", {}, { timeoutMs: 15000 });
