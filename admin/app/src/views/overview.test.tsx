import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { LangProvider } from '@/lib/i18n';
import OverviewView from './overview';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <MemoryRouter>
          <OverviewView />
        </MemoryRouter>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Handler = (url: string) => Response;
function mockFetch(handler: Handler) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => handler(String(input))));
}
const down = () => json({ error: "down" }, 502);

beforeEach(() => sessionStorage.setItem("hub-panel-token", "tk"));

describe('OverviewView 三态', () => {
  it('加载中显示骨架（aria-busy / animate-pulse）', async () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it('全部失败时给错误态 + 重试按钮', async () => {
    mockFetch(() => down());
    renderView();
    const retry = await screen.findByRole("button", { name: /重试|Retry/i });
    expect(retry).toBeTruthy();
  });

  it('空数据时给空状态与主操作（不是一片空白）', async () => {
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true, engines: {} });
      if (url.includes("/usage")) return json({ requests: 0, errors: 0, total_tokens: 0 });
      if (url.includes("/accounts")) return json({ accounts: [], usable: 0 });
      return json({});
    });
    renderView();
    const box = await waitFor(() => {
      const el = document.querySelector("[data-empty-state]");
      if (!el) throw new Error("no empty state yet");
      return el as HTMLElement;
    });
    expect(box).toBeTruthy();
    expect(within(box).getAllByRole("button").length).toBeGreaterThan(0);
  });

  it('有数据时渲染 KPI 与引擎表；一家失败给出局部提示', async () => {
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true, engines: { wb: { ok: true, version: "1.7.16" } } });
      if (url.includes("/qd/usage")) return down();
      if (url.includes("/usage")) return json({ requests: 1200, errors: 3, total_tokens: 45600, cost_cny: 1.25 });
      if (url.includes("/accounts")) return json({ accounts: [{ uid: "a" }], usable: 1 });
      return json({});
    });
    renderView();
    await waitFor(() => expect(screen.getByText("2,400")).toBeInTheDocument());
    expect(screen.getAllByText("6").length).toBeGreaterThan(0); // 两家各 3 条错误（KPI 与环形图例都会出现）
    // 局部失败提示里应出现失败那家的名字
    const status = screen.getAllByRole("status").map((n) => n.textContent || "").join(" ");
    expect(status).toMatch(/Qoder/);
  });
});
