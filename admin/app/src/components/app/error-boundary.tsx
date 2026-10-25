import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RotateCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useLang } from '@/lib/i18n';

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * 渲染异常兜底：没有它，任何一个视图抛错就是整页白屏（用户什么都看不到、也无法自救）。
 * 这里给出：一句人话 + 错误摘录 + 「重试」（重置边界重新渲染）+「重新加载」（整页刷新）。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 控制台留完整栈，便于排查（生产环境也保留，面板本身是内网工具）
    console.error('[panel] 渲染异常', error, info.componentStack);
  }

  private reset = () => this.setState({ error: null });

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return <Fallback error={this.state.error} onReset={this.reset} />;
  }
}

function Fallback({ error, onReset }: { error: Error; onReset: () => void }) {
  const { t } = useLang();
  return (
    <div className="grid min-h-dvh place-items-center bg-canvas px-4">
      <section
        role="alert"
        className="w-full max-w-lg rounded-xl border border-line bg-surface p-6 shadow-2"
      >
        <div className="mb-3 flex items-center gap-2">
          <span className="grid h-10 w-10 place-items-center rounded-lg bg-danger-bg text-danger-fg">
            <AlertTriangle size={18} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-fg">{t('state.errTitle')}</h1>
            <p className="truncate text-xs text-fg-mute">{t('state.errDesc')}</p>
          </div>
        </div>
        <pre className="mb-4 max-h-40 overflow-auto rounded-md border border-line bg-surface-2 p-3 font-mono text-[11px] leading-5 text-fg-dim">
          {error.name}: {error.message}
        </pre>
        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" onClick={onReset} icon={<RotateCw size={14} />}>
            {t('common.retry')}
          </Button>
          <Button variant="ghost" onClick={() => window.location.reload()}>
            {t('common.reload')}
          </Button>
        </div>
      </section>
    </div>
  );
}
