import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui/toast';
import { AccountDetail } from './accounts-detail';

type Call = { url: string; method: string; body: string | null };
let calls: Call[] = [];

function mockFetch(handler: (url: string, method: string) => Response) {
  calls = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    calls.push({ url, method, body: typeof init?.body === 'string' ? init.body : null });
    return handler(url, method);
  }));
}
const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { 'Content-Type': 'application/json' } });
const user = () => userEvent.setup({ pointerEventsCheck: 0 });

const ACCOUNT = { uid: 'u-1', nickname: 'alpha', realm: 'cn', enabled: true, weight: 1, note: '', fails: 0 };

function renderDetail(onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <AccountDetail account={ACCOUNT} engine="wb" onClose={onClose} />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
  return onClose;
}

beforeEach(() => { sessionStorage.setItem('hub-panel-token', 'tk'); });
afterEach(() => { vi.restoreAllMocks(); });

describe('AccountDetail', () => {
  it('展示账号字段并拉取积分', async () => {
    mockFetch((url) => {
      if (url.includes('/credits')) return json({ results: [{ uid: 'u-1', ok: true, credits: { remain: 2342, used: 526 } }] });
      return json({ ok: true });
    });
    renderDetail();
    await waitFor(() => expect(screen.getByText('2,342')).toBeInTheDocument());
    expect(document.body.textContent).toContain('alpha');
  });

  it('改备注后保存：POST /accounts/set 带上了新备注', async () => {
    mockFetch((url) => {
      if (url.includes('/credits')) return json({ results: [] });
      return json({ account: ACCOUNT });
    });
    renderDetail();

    // Input 组件不显式写 type（默认 text），所以按「非 number / 非 password」挑备注框
    const inputs = Array.from(document.querySelectorAll('input')) as HTMLInputElement[];
    const noteInput = inputs.find((i) => i.type !== 'number' && i.type !== 'password') as HTMLInputElement;
    expect(noteInput).toBeTruthy();
    fireEvent.change(noteInput, { target: { value: '备注X' } });

    const save = screen.getAllByRole('button').find((b) => /保存|save/i.test((b.textContent || '').trim()));
    await user().click(save!);

    await waitFor(() => {
      const post = calls.find((c) => c.method === 'POST' && c.url.includes('/accounts/set'));
      expect(post).toBeTruthy();
      expect(post!.body).toContain('备注X');
    });
  });
});
