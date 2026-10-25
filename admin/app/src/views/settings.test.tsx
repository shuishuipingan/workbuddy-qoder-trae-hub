import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import SettingsView from './settings';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <SettingsView />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

let calls: { url: string; method: string; body: string | null }[] = [];
function mockFetch(handler: (url: string, method: string) => Response) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : null });
    return handler(url, method);
  }));
}

const SETTINGS = {
  panel_password_is_default: true,
  auth_required: true,
  api_key_masked: '7260****',
  api_keys: [
    { id: 'k0', name: '国际', realm: 'intl', enabled: true, masked: '7260********ace78fb5' },
    { id: 'k1', name: '国内', realm: 'cn', enabled: false, masked: 'ddc9********abf2b6344' },
  ],
  limits: {
    reserve_credits: { global: 100, intl: null, cn: 200 },
    daily_token_limit: { global: 4000000, intl: null, cn: 2500000 },
  },
  pricing_refresh_minutes: 5,
};

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem('lang', 'zh');
  sessionStorage.setItem('hub-panel-token', 'tk');
});

describe('SettingsView', () => {
  it('加载中显示骨架；切引擎按新作用域重新取设置', async () => {
    mockFetch(() => json(SETTINGS));
    renderView();
    expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole('tab', { name: 'Qoder' }));
    await waitFor(() => expect(calls.some((c) => c.url.includes('/api/qd/settings'))).toBe(true));
    expect(calls.some((c) => c.url.includes('/api/wb/settings'))).toBe(true);
  });

  it('渲染面板状态（默认密码警告）与限额数字；Key 内容已全部移出', async () => {
  mockFetch(() => json(SETTINGS));
  renderView();

  // 默认密码 → 顶部警告条（auth.defaultHint）
  await waitFor(() => expect(screen.getByText(/当前是默认密码/)).toBeInTheDocument());
  // Key 相关内容整块搬去 #/keys 了：设置页不该再出现 Key 表格或键值
  expect(screen.queryByRole('table')).toBeNull();
  // 设置页只保留「当前生效的 key（掩码）」这类只读状态；新增/删除入口在 #/keys
  expect(document.querySelectorAll('input[placeholder*="sk-"]').length).toBe(0);
  // 限额：数字 tnum（等宽），缺失项给「—」
  expect(screen.getAllByText('4,000,000')[0]).toBeInTheDocument();
  expect(screen.getAllByText('—').length).toBeGreaterThan(0);
});
});
