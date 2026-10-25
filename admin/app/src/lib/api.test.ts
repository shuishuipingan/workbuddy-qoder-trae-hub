import { describe, it, expect, beforeEach, vi } from 'vitest';
import { __resetInflight, readToken, writeToken, request, get } from '@/lib/http';
import { login, fanout, ENGINES } from '@/lib/api';

type Call = { url: string; init: RequestInit };
let calls: Call[] = [];

function mockFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init || {} });
    return handler(url, init || {});
  }));
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  __resetInflight();
  sessionStorage.clear();
});

describe('http', () => {
  it('附带 X-Panel-Token（有令牌时）', async () => {
    writeToken("t-123");
    mockFetch(() => json({ ok: true }));
    await get("/api/wb/settings");
    const h = calls[0].init.headers as Record<string, string>;
    expect(h["X-Panel-Token"]).toBe("t-123");
  });

  it('401 抛 ApiError(kind=unauthorized)', async () => {
    mockFetch(() => json({ error: "nope" }, 401));
    await expect(get("/api/wb/accounts")).rejects.toMatchObject({ status: 401, kind: "unauthorized" });
  });

  it('写操作按方法+路径+body 在失去重', async () => {
    mockFetch(async () => {
      await new Promise((r) => setTimeout(r, 30));
      return json({ ok: true });
    });
    const body = { pricing_refresh_minutes: 5 };
    await Promise.all([
      request("/api/wb/settings/save", { method: "POST", body }),
      request("/api/wb/settings/save", { method: "POST", body }),
      request("/api/wb/settings/save", { method: "POST", body }),
    ]);
    expect(calls.length).toBe(1);
  });

  it('读操作不去重（允许并发刷新）', async () => {
    mockFetch(async () => { await new Promise((r) => setTimeout(r, 20)); return json({ ok: true }); });
    await Promise.all([get("/a"), get("/a")]);
    expect(calls.length).toBe(2);
  });

  it('服务端错误带出 message', async () => {
    mockFetch(() => json({ error: { message: "boom" } }, 500));
    await expect(get("/x")).rejects.toMatchObject({ status: 500, message: "boom" });
  });
});

describe('逐引擎令牌槽（每家令牌独立）', () => {
  it('readToken(engine) 优先取该引擎槽，缺失回落主槽', () => {
    writeToken("main-tk");
    expect(readToken("qd")).toBe("main-tk");
    writeToken("qd-tk", "qd");
    expect(readToken("qd")).toBe("qd-tk");
    expect(readToken("wb")).toBe("main-tk");
  });

  it('/api/qd/... 的请求带的是 qd 令牌，不是主令牌', async () => {
    writeToken("main-tk");
    writeToken("qd-tk", "qd");
    mockFetch(() => json({ ok: true }));
    await get("/api/qd/usage");
    const headers = calls[0].init.headers as Record<string, string>;
    expect(headers["X-Panel-Token"]).toBe("qd-tk");
  });

  it('登录成功后为其余两家补登，各存各的槽', async () => {
    mockFetch((url) => {
      const m = /\/api\/(wb|qd|trae)\/panel\/login/.exec(url);
      if (m) return json({ ok: true, token: m[1] + "-tk" });
      return json({});
    });
    const r = await login("admin", "wb");
    expect(r.ok).toBe(true);
    await new Promise((res) => setTimeout(res, 60));
    expect(readToken("wb")).toBe("wb-tk");
    expect(readToken("qd")).toBe("qd-tk");
    expect(readToken("trae")).toBe("trae-tk");
  });
});

describe('api.login', () => {
  it('第一家成功就直接用', async () => {
    mockFetch((url) => (url.includes("/wb/panel/login") ? json({ ok: true, token: "tk" }) : json({}, 500)));
    const r = await login("admin", "wb");
    expect(r.ok).toBe(true);
    if (r.ok) { expect(r.token).toBe("tk"); expect(r.degraded).toBeUndefined(); }
  });

  it('第一家 5xx 时挨家试，成功者标 degraded', async () => {
    mockFetch((url) => {
      if (url.includes("/wb/panel/login")) return json({ error: "down" }, 502);
      if (url.includes("/qd/panel/login")) return json({ ok: true, token: "qd-tk" });
      return json({}, 500);
    });
    const r = await login("admin", "wb");
    expect(r.ok).toBe(true);
    if (r.ok) { expect(r.degraded).toBe("qd"); }
  });

  it('第一家 401 直接判密码错，不再试别家', async () => {
    mockFetch(() => json({ error: "unauthorized" }, 401));
    const r = await login("bad", "wb");
    expect(r).toEqual({ ok: false, error: "unauthorized" });
    expect(calls.length).toBe(1);
  });
});

describe('api.fanout', () => {
  it('一家失败不影响其它两家', async () => {
    const res = await fanout(async (eng) => {
      if (eng === "qd") throw new Error("boom");
      return { n: 1 };
    });
    expect(Object.keys(res.values).sort()).toEqual(["trae", "wb"]);
    expect(res.failed).toEqual(["qd"]);
    expect(Object.keys(res.values).length + res.failed.length).toBe(ENGINES.length);
  });
});
