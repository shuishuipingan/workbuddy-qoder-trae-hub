import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LangProvider } from '@/lib/i18n';
import { ErrorBoundary } from './error-boundary';

function Bomb({ explode }: { explode: boolean }) {
  if (explode) throw new Error('boom-from-child');
  return <p>子组件正常</p>;
}

function renderWithBoundary(explode: boolean) {
  return render(
    <LangProvider>
      <ErrorBoundary>
        <Bomb explode={explode} />
      </ErrorBoundary>
    </LangProvider>,
  );
}

beforeEach(() => {
  // 组件会 console.error 完整栈，测试里静音掉，避免污染输出
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
});

describe('ErrorBoundary', () => {
  it('子组件抛错时给出兜底卡片（不是白屏），并显示错误摘要', () => {
    renderWithBoundary(true);
    const alert = screen.getByRole('alert');
    expect(alert).toBeTruthy();
    expect(alert.textContent).toContain('boom-from-child');
  });

  it('点「重试」后重新渲染子树', async () => {
    let explode = true;
    function Toggle() {
      return <Bomb explode={explode} />;
    }
    render(
      <LangProvider>
        <ErrorBoundary>
          <Toggle />
        </ErrorBoundary>
      </LangProvider>,
    );
    expect(screen.getByRole('alert')).toBeTruthy();
    explode = false;
    const retry = screen.getByRole('button', { name: /重试|Retry/i });
    await userEvent.click(retry);
    expect(screen.getByText('子组件正常')).toBeInTheDocument();
  });
});
