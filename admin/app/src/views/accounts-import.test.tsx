import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui/toast';
import { ImportDialog } from './accounts-import';

type Call = { url: string; method: string; body: string | null };
let calls: Call[] = [];

function mockFetch(handler: (url: string, method: string, body: string | null) => Response) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    const body = typeof init?.body === 'string' ? init.body : null;
    calls.push({ url, method, body });
    return handler(url, method, body);
  }));
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

// Radix 弹层会给 body 加 pointer-events:none，默认的 userEvent 会拒绝点击
const user = () => userEvent.setup({ pointerEventsCheck: 0 });

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <ImportDialog open={true} onClose={onClose} engine="wb" />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
  return { onClose };
}

function fillTextarea(text: string) {
  const ta = document.querySelector('textarea') as HTMLTextAreaElement;
  expect(ta).toBeTruthy();
  fireEvent.change(ta, { target: { value: text } });
  return ta;
}

function confirmButton(): HTMLElement {
  // jsdom 的 navigator.language 是 en-US，界面走英文词条 —— 断言必须中英兼容
  const btn = screen.getAllByRole('button').find((b) => /确认导入|确认|^\s*import\s*$/i.test((b.textContent || '').trim()));
  expect(btn).toBeTruthy();
  return btn!;
}

beforeEach(() => {
  sessionStorage.setItem('hub-panel-token', 'tk');
  vi.spyOn(window, 'open').mockImplementation(() => null);
});
afterEach(() => { vi.restoreAllMocks(); });

describe('ImportDialog', () => {
  it('JSON 导入：把数组原样 POST 给 /accounts/import', async () => {
    mockFetch(() => json({ added: 2, updated: 1, skipped: 3, invalid: 0 }));
    renderDialog();

    fillTextarea(JSON.stringify([{ name: 'a' }, { name: 'b' }]));
    await user().click(confirmButton());

    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST' && c.url.includes('/accounts/import'));
      expect(post).toBeTruthy();
      expect(post!.body).toContain('"accounts"');
      expect(post!.body).toContain('"name":"a"');
    });
  });

  it('JSON 非法时给提示，不发请求', async () => {
    mockFetch(() => json({ added: 0 }));
    renderDialog();

    fillTextarea('{not json');
    await user().click(confirmButton());

    await waitFor(() => expect(document.querySelector('[role=status]')).toBeTruthy());
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('OAuth：开始授权后展示授权链接并轮询 poll', async () => {
    mockFetch((url) => {
      if (url.includes('/login/start')) return json({ state: 'st-1', authUrl: 'https://example.test/auth?state=st-1' });
      if (url.includes('/login/poll')) return json({ status: 'pending' });
      return json({});
    });
    renderDialog();

    const tab = screen.getAllByRole('tab').find((t) => /OAuth/.test(t.textContent || ''))!;
    await user().click(tab);
    const start = await screen.findByRole('button', { name: /打开授权链接|open auth link/i });
    await user().click(start);

    await waitFor(() => expect(document.body.textContent).toContain('https://example.test/auth?state=st-1'));
    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/login/poll') && c.url.includes('st-1'))).toBe(true);
    }, { timeout: 5000 });
  });
});
