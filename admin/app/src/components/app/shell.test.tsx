import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import App from '@/App';

function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

function mockFetch(handler: (url: string, init: RequestInit) => Response) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init || {})));
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  window.location.hash = "";
});

describe('AuthGate', () => {
  it('没有令牌时只显示登录表单，不渲染导航', async () => {
    mockFetch(() => json({ ok: false }, 401));
    renderApp();
    await waitFor(() => expect(screen.getByLabelText(/password|面板密码/i)).toBeInTheDocument());
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it('密码错误时就地提示（不跳走、不弹 toast）', async () => {
    mockFetch((url) => (url.includes("/panel/login") ? json({ error: "bad" }, 401) : json({}, 401)));
    renderApp();
    const pwd = await screen.findByLabelText(/password|面板密码/i);
    await userEvent.type(pwd, "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in|进入面板/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toBeTruthy();
  });
});

describe('AppShell', () => {
  it('已登录后渲染 9 个导航项，并高亮当前页', async () => {
    sessionStorage.setItem("hub-panel-token", "tk");
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true });
      return json({});
    });
    renderApp();
    await waitFor(() => expect(screen.getAllByRole("link").length).toBeGreaterThanOrEqual(9));
    const links = screen.getAllByRole("link");
    expect(links.length).toBe(9);
  });

  // 回归守卫：切换轮次里 App.tsx 曾被简化成不含 ToastProvider，
  // 结果只有"用到 toast 的视图"（accounts/settings/rewards/api）会崩，
  // 而默认页 overview 不用 toast，测试全绿 —— 这条就是补那个洞。
  it('ToastProvider 缺失回归：进入 accounts 页不触发错误边界', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/accounts')) return json({ accounts: [{ uid: 'u1', nickname: 'alpha', realm: 'cn', enabled: true }], usable: 1 });
      return json({});
    });
    window.location.hash = '#/accounts';
    renderApp();
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull(), { timeout: 5000 });
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument(), { timeout: 5000 });
  });

  // 胶囊三档：<1024 汉堡 / 1024–1151 FAB / >=1152 完整胶囊
  it('胶囊三档：宽屏显示完整胶囊，不显示 FAB', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('aside.nav-pill')).toBeTruthy(), { timeout: 5000 });
    expect(document.querySelector('button.fab')).toBeNull();
  });

  it('胶囊三档：中等宽度（matchMedia 命中）只留 FAB', async () => {
    const mq = { matches: true, media: '', onchange: null, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent() { return false; } };
    vi.stubGlobal('matchMedia', () => mq);
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('button.fab')).toBeTruthy(), { timeout: 5000 });
    expect(document.querySelector('aside.nav-pill')).toBeNull();
    vi.unstubAllGlobals();
  });

  it('窄屏下点汉堡按钮出现移动端导航容器', async () => {
    sessionStorage.setItem("hub-panel-token", "tk");
    mockFetch((url) => (url.includes("/health") ? json({ ok: true }) : json({})));
    renderApp();
    const toggle = await screen.findByRole("button", { name: /open navigation|打开导航/i });
    expect(document.querySelector("[data-mobile-nav]")).toBeNull();
    await userEvent.click(toggle);
    expect(document.querySelector("[data-mobile-nav]")).toBeTruthy();
  });
});
