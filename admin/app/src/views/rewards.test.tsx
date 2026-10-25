import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import { __resetInflight } from '@/lib/http';
import RewardsView from './rewards';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <RewardsView />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

let calls: { url: string; method: string; body: string | null }[] = [];
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

const DATA = {
  wb: {
    tasks: [
      { task_code: 'daily', name: '每日活跃打卡', status: 'completed', current: 1, target: 3, reward_credit: 30 },
      { task_code: 'chat', name: '每日对话', status: 'claimed', current: 3, target: 3, reward_credit: 20 },
    ],
    summary: { streak_days: 5, energy: 120, plan: 'Pro', travel: { state: 'idle' } },
    accounts: [{ uid: 'u1', nickname: 'alpha' }],
    checkin_done: true,
  },
  qd: {
    tasks: [{ task_code: 'checkin', name: 'QD 每日任务', status: 'completed', current: 1, target: 1, reward_credit: 100 }],
    summary: { streak_days: 3, energy: 800, plan: 'Pro', travel: { state: 'arrived' }, codes: [{ code: 'QD-ABC' }] },
    accounts: [{ uid: 'q1', nickname: 'beta' }],
    checkin_done: false,
  },
  errors: {},
};

const stat = (key: string) => document.querySelector('[data-stat="' + key + '"]')?.textContent ?? null;

beforeEach(() => {
  // 词条按语言取：这里钉死中文，断言里的按钮名/提示才稳定（jsdom 的 navigator 是 en-US）。
  localStorage.setItem('lang', 'zh');
  sessionStorage.setItem('hub-panel-token', 'tk');
  __resetInflight();
});

describe('RewardsView', () => {
  it('加载中显示骨架（不白屏）', () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('顶部四个小结取聚合值（可领条数 + 连续天数）', async () => {
    mockFetch(() => json(DATA));
    renderView();
    await waitFor(() => expect(stat('wbClaimable')).toBe('1'), { timeout: 4000 });
    expect(stat('wbStreak')).toBe('5');
    expect(stat('qdClaimable')).toBe('1');
    expect(stat('qdStreak')).toBe('3');
    // 卡片自己也渲染出任务行（两侧互不干扰，各自读自己那半边）
    expect(screen.getByText('每日活跃打卡')).toBeInTheDocument();
    expect(screen.getByText('QD 每日任务')).toBeInTheDocument();
  });

  it('一侧取不到时显示「—」，另一侧照常', async () => {
    mockFetch(() => json({ wb: { tasks: [], summary: {} }, qd: null, errors: {} }));
    renderView();
    await waitFor(() => expect(stat('wbClaimable')).toBe('0'), { timeout: 4000 });
    expect(stat('wbStreak')).toBe('—');
    expect(stat('qdClaimable')).toBe('—');
    expect(stat('qdStreak')).toBe('—');
    // 空态三件套（图标 + 引导 + 主操作）在，不留白
    const empty = document.querySelector('[data-empty-state]');
    expect(empty).toBeTruthy();
    expect(empty!.querySelectorAll('button').length).toBeGreaterThan(0);
  });

  it('接口失败给错误态 + 重试（重试真的重发请求）', async () => {
    mockFetch(() => json({ wb: null, qd: null, errors: { wb: 'wb down', qd: 'qd down' } }));
    renderView();
    await waitFor(() => expect(screen.getAllByText('wb down').length).toBe(1), { timeout: 4000 });
    expect(screen.getByText('qd down')).toBeInTheDocument();
    const retries = screen.getAllByRole('button', { name: /重试/ });
    expect(retries.length).toBeGreaterThanOrEqual(2);
    const before = calls.filter((c) => c.method === 'GET').length;
    await userEvent.click(retries[0]);
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET').length).toBe(before + 1), { timeout: 4000 });
  });

  it('WB 动作失败只影响自己：错误就地说明，QD 签到照常可点并成功', async () => {
    mockFetch((url, method) => {
      if (method === 'POST' && url.includes('/wb/tasks/run')) return json({ error: 'upstream said no' }, 500);
      if (method === 'POST' && url.includes('/qd/accounts/checkin')) return json({ ok: true, credit_added: 100 });
      return json(DATA);
    });
    renderView();
    await waitFor(() => expect(screen.getByText('每日活跃打卡')).toBeInTheDocument(), { timeout: 4000 });

    const runBtn = screen.getByRole('button', { name: '全自动领取成长积分' });
    await userEvent.click(runBtn);

    const post = await waitFor(() => {
      const hit = calls.find((c) => c.method === 'POST' && c.url.includes('/api/wb/tasks/run'));
      expect(hit).toBeTruthy();
      return hit!;
    }, { timeout: 4000 });
    expect(post.body).toContain('"uid":"all"');

    // 失败说明：toast + 卡内就地提示
    await waitFor(() => expect(screen.getByText(/福利接口取不到/)).toBeInTheDocument(), { timeout: 4000 });
    expect(screen.getByText('upstream said no')).toBeInTheDocument();

    // 另一家不受影响：QD 按钮仍可点，且真的打到 QD 的端点
    const checkin = screen.getByRole('button', { name: '每日签到' });
    expect(checkin).not.toBeDisabled();
    await userEvent.click(checkin);
    await waitFor(() => {
      expect(calls.some((c) => c.method === 'POST' && c.url.includes('/api/qd/accounts/checkin'))).toBe(true);
    }, { timeout: 4000 });
    await waitFor(() => expect(screen.getByText(/已领取 100 积分/)).toBeInTheDocument(), { timeout: 4000 });
  });
});
