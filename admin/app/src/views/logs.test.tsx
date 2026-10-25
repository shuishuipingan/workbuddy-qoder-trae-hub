import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui/toast';
import LogsView from './logs';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <LogsView />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const LOGS = {
  total: 3,
  logs: [
    { at: 1700000000, level: 'error', msg: 'boom happened' },
    { at: 1700000001, level: 'info', msg: 'all good' },
    { at: 1700000002, level: 'warn', msg: 'slow upstream' },
  ],
};

function mockFetch(handler: (url: string) => Response) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => handler(String(input))));
}

beforeEach(() => sessionStorage.setItem('hub-panel-token', 'tk'));

describe('LogsView', () => {
  it('加载中显示骨架', () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  // 合并态会向三家各取一次，同样的日志因此出现三份 —— 这是预期行为，断言用倍数量。
  it('渲染日志行（三家合并 → 每条 3 份）', async () => {
    mockFetch(() => json(LOGS));
    renderView();
    await waitFor(() => expect(screen.getAllByText('boom happened').length).toBe(3), { timeout: 5000 });
    expect(screen.getAllByText('all good').length).toBe(3);
  });

  it('级别筛选只留 error', async () => {
    mockFetch(() => json(LOGS));
    renderView();
    await waitFor(() => expect(screen.getAllByText('all good').length).toBe(3), { timeout: 5000 });
    await userEvent.click(screen.getByRole('tab', { name: 'error' }));
    await waitFor(() => expect(screen.queryAllByText('all good').length).toBe(0), { timeout: 5000 });
    expect(screen.getAllByText('boom happened').length).toBeGreaterThanOrEqual(3); // 列表 3 份 + 详情可能再 1 份
  });

  it('搜索过滤生效', async () => {
    mockFetch(() => json(LOGS));
    renderView();
    await waitFor(() => expect(screen.getAllByText('all good').length).toBe(3), { timeout: 5000 });
    await userEvent.type(screen.getByRole('textbox'), 'slow');
    await waitFor(() => expect(screen.queryAllByText('all good').length).toBe(0), { timeout: 5000 });
    expect(screen.getAllByText('slow upstream').length).toBeGreaterThanOrEqual(3);
  });

  it('空日志给空状态而不是空白', async () => {
    mockFetch(() => json({ total: 0, logs: [] }));
    renderView();
    await waitFor(() => expect(document.querySelector('[data-empty-state]')).toBeTruthy(), { timeout: 5000 });
  });
});
