import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import ModelsView from './models';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ModelsView />
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

function mockFetch(handler: (url: string) => Response) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => handler(String(input))));
}

const MODELS = {
  data: [
    { id: 'alpha-1', name: 'Alpha 1', capabilities: { vision: true, tool_calls: true, reasoning: false }, context_length: 128000, max_output_tokens: 8192 },
    { id: 'beta-2', name: 'Beta 2', capabilities: { vision: false, tool_calls: false, reasoning: true }, context_length: 32000, max_output_tokens: 4096 },
  ],
};

beforeEach(() => sessionStorage.setItem('hub-panel-token', 'tk'));

describe('ModelsView', () => {
  it('加载中显示骨架', () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('合并态下按 id 去重（两家都有的模型只出现一次）', async () => {
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/models')) return json(MODELS);
      return json({});
    });
    renderView();
    await waitFor(() => expect(screen.getAllByText('Alpha 1').length).toBe(1));
    expect(screen.getAllByText('Beta 2').length).toBe(1);
  });

  it('搜索过滤生效', async () => {
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json(MODELS)));
    renderView();
    await waitFor(() => expect(screen.getByText('Alpha 1')).toBeInTheDocument());
    await userEvent.type(screen.getByRole('textbox'), 'beta');
    await waitFor(() => expect(screen.queryByText('Alpha 1')).toBeNull());
    expect(screen.getByText('Beta 2')).toBeInTheDocument();
  });

  it('能力筛选（视觉）只留带该能力的模型', async () => {
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json(MODELS)));
    renderView();
    await waitFor(() => expect(screen.getByText('Alpha 1')).toBeInTheDocument());
    const vision = screen.getByRole('button', { name: /vision|视觉/i });
    await userEvent.click(vision);
    await waitFor(() => expect(screen.queryByText('Beta 2')).toBeNull());
    expect(screen.getByText('Alpha 1')).toBeInTheDocument();
  });

  it('部分引擎失败时给告警，不静默空表', async () => {
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/wb/models')) return json(MODELS);
      return json({ error: 'down' }, 502);
    });
    renderView();
    await waitFor(() => expect(screen.getByText('Alpha 1')).toBeInTheDocument());
    expect(document.querySelectorAll('[role=status]').length).toBeGreaterThan(0);
  });
});
