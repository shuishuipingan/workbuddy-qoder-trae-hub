import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import ApiView from './api';

/** 路由探针：放在 Routes 外面，跳转后仍然可读。 */
function LocationProbe() {
  const loc = useLocation();
  return <output data-testid="location">{loc.pathname}</output>;
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <MemoryRouter initialEntries={["/api"]}>
            <LocationProbe />
            <Routes>
              <Route path="/api" element={<ApiView />} />
              <Route path="*" element={<div data-testid="elsewhere" />} />
            </Routes>
          </MemoryRouter>
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let calls: { url: string; method: string }[] = [];
function mockFetch(handler: (url: string, method: string) => Response) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    calls.push({ url, method });
    return handler(url, method);
  }));
}

const KEYS = {
  ok: true,
  counts: { merged: 1, wb: 1, qd: 0, trae: 0 },
  merged: [{ id: "m1", name: "default", key: "sk-hub…abcd", enabled: true }],
  rules: {
    merged: "全部模型 · 智能路由（国内优先 → 跨引擎回退）",
    wb: "只走 WorkBuddy（模型清单也只列 wb）",
    qd: "只走 Qoder（模型清单也只列 qd）",
    trae: "只走 Trae（模型清单也只列 trae）",
  },
  wb: [{ id: "k1", name: "wb", key: "sk-wb…0001" }],
  qd: [],
  trae: [],
};

const HEALTH = {
  ok: true,
  version: "0.9.4",
  engines: {
    wb: { ok: true, version: "1.8.2", accounts: 3 },
    qd: { ok: true, version: "2.0.1", accounts: 1 },
    trae: { ok: false, version: "0.3.0" },
  },
};

function stubClipboard(writeText: (text: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
}

beforeEach(() => {
  localStorage.setItem("lang", "zh");
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
  vi.unstubAllGlobals();
});

describe('ApiView', () => {
  it('加载中显示骨架（不空白）', () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    renderView();
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it('没有合并 key 时给引导与「去设置」，引擎端点显示版本与在线状态', async () => {
    mockFetch((url) => (url.includes("/hub/keys")
      ? json({ ...KEYS, merged: [], counts: { ...KEYS.counts, merged: 0 } })
      : json(HEALTH)));
    renderView();

    expect(await screen.findByText(/没有合并 key/)).toBeInTheDocument();
    // 引擎端点一览：三家名字 + 版本 + 在线状态（第三个版本是离线的）
    expect(await screen.findByText("1.8.2")).toBeInTheDocument();
    expect(screen.getByText("2.0.1")).toBeInTheDocument();
    expect(screen.getByText("0.3.0")).toBeInTheDocument();
    expect(screen.getAllByText(/^在线$/).length).toBe(2);
    expect(screen.getAllByText(/^离线$/).length).toBe(1);

    const go = screen.getByRole("button", { name: /设置|settings/i });
    await userEvent.click(go);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/settings"));
  });

  it('keys 接口失败给错误态；点重试会重新请求并渲染表格', async () => {
    let fail = true;
    mockFetch((url) => (url.includes("/hub/keys")
      ? (fail ? json({ error: "boom" }, 500) : json(KEYS))
      : json(HEALTH)));
    renderView();

    const retry = await screen.findByRole("button", { name: /重试|retry/i });
    fail = false;
    await userEvent.click(retry);

    expect(await screen.findByText(/合并 key 1 把/)).toBeInTheDocument();
    expect(calls.filter((c) => c.url.includes("/hub/keys")).length).toBe(2);
    // 表格里引擎 key 那一行仍然在（引擎 key 走自己的引擎）。
    expect(screen.getByText("全部模型 · 智能路由（国内优先 → 跨引擎回退）")).toBeInTheDocument();
  });

  it('复制地址会写入剪贴板并提示已复制', async () => {
    const writeText = vi.fn(async () => {});
    stubClipboard(writeText);
    mockFetch((url) => (url.includes("/hub/keys") ? json(KEYS) : json(HEALTH)));
    renderView();

    await screen.findByText(/合并 key 1 把/);
    const copyButtons = screen.getAllByRole("button", { name: /复制|copy/i });
    await userEvent.click(copyButtons[0]);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(window.location.origin + "/api/v1"));
    expect(await screen.findByText("已复制")).toBeInTheDocument();
  });

  it('剪贴板不可用时不静默：提示手动复制', async () => {
    stubClipboard(vi.fn(async () => { throw new Error("denied"); }));
    mockFetch((url) => (url.includes("/hub/keys") ? json(KEYS) : json(HEALTH)));
    renderView();

    await screen.findByText(/合并 key 1 把/);
    const copyButtons = screen.getAllByRole("button", { name: /复制|copy/i });
    await userEvent.click(copyButtons[0]);

    expect(await screen.findByText(/复制失败/)).toBeInTheDocument();
    expect((await screen.findAllByText(/浏览器不让直接写剪贴板/)).length).toBeGreaterThan(0);
  });
});
