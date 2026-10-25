import { lazy, Suspense } from 'react';
import { LangProvider } from '@/lib/i18n';
import { AuthGate } from '@/components/app/auth-gate';
import { ErrorBoundary } from '@/components/app/error-boundary';
import { Spinner } from '@/components/ui/spinner';

/** 已登录后的整套按需加载 —— 登录页不为它付首屏体积。 */
const Authenticated = lazy(() => import('@/app/authenticated'));

export default function App() {
  return (
    <LangProvider>
      <ErrorBoundary>
        <AuthGate>
        <Suspense
          fallback={(
            <div className="grid min-h-dvh place-items-center bg-canvas">
              <Spinner size={20} label="加载中" />
            </div>
          )}
        >
          <Authenticated />
        </Suspense>
        </AuthGate>
      </ErrorBoundary>
    </LangProvider>
  );
}
