import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import AccountsView from './accounts';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <AccountsView />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

let calls: { url: string; method: string; body: string | null }[] = [];
function mockFetch(handler: (url: string, method: string, body: string | null) => Response) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || "GET").toUpperCase();
    const body = typeof init?.body === "string" ? init.body : null;
    calls.push({ url, method, body });
    return handler(url, method, body);
  }));
}

const ACCOUNTS = [
  { uid: "u1", nickname: "alpha", realm: "cn", enabled: true },
  { uid: "u2", nickname: "beta", realm: "cn", enabled: false },
];

beforeEach(() => {
  sessionStorage.setItem("hub-panel-token", "tk");
});

describe('AccountsView', () => {
  it('加载中显示骨架（不空白）', () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it('空列表给空状态 + 主操作', async () => {
    mockFetch(() => json({ accounts: [], usable: 0 }));
    renderView();
    await waitFor(() => expect(document.querySelector("[data-empty-state]")).toBeTruthy());
    const box = document.querySelector("[data-empty-state]") as HTMLElement;
    expect(box.querySelectorAll("button").length).toBeGreaterThan(0);
  });

  it('接口失败给错误态 + 重试', async () => {
    mockFetch(() => json({ error: "boom" }, 500));
    renderView();
    const btn = await screen.findByRole("button", { name: /retry|重试/i });
    expect(btn).toBeTruthy();
  });

  it('渲染账号行，切换开关会 POST 相反状态', async () => {
    mockFetch((url, method) => {
      if (method === "POST" && url.includes("/accounts/set")) return json({ account: {} });
      return json({ accounts: ACCOUNTS, usable: 1 });
    });
    renderView();
    await waitFor(() => expect(screen.getByText("alpha")).toBeInTheDocument());
    const switches = screen.getAllByRole("switch");
    await userEvent.click(switches[0]);
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/accounts/set"));
      expect(post).toBeTruthy();
      expect(post!.body).toContain("\"enabled\":false");
    });
  });

  it('删除要先确认；确认后才发 delete 请求', async () => {
    mockFetch((url, method) => {
      if (method === "POST" && url.includes("/accounts/delete")) return json({ deleted: true });
      return json({ accounts: ACCOUNTS, usable: 1 });
    });
    renderView();
    await waitFor(() => expect(screen.getByText("alpha")).toBeInTheDocument());
    const del = screen.getAllByRole("button", { name: /delete|删除/i })[0];
    await userEvent.click(del);
    // 未确认前不能发请求
    expect(calls.some((c) => c.url.includes("/accounts/delete"))).toBe(false);
    const confirm = await screen.findByRole("button", { name: /^delete$|^删除$/i });
    await userEvent.click(confirm);
    await waitFor(() => expect(calls.some((c) => c.url.includes("/accounts/delete"))).toBe(true));
  });
});
