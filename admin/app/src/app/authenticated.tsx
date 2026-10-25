import { lazy, Suspense, type ComponentType } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom';
import { queryClient } from '@/lib/query';
import { AppShell } from '@/components/app/app-shell';
import { SkeletonRows } from '@/components/ui/skeleton';
import { ToastProvider } from '@/components/ui/toast';

const RELOAD_FLAG = "hub-chunk-reloaded";

/**
 * 懒加载 + 「发布后旧 chunk 消失」兜底。
 *
 * 场景：用户开着页面，我们重新 build 部署 → 旧哈希文件被删，客户端再点别的路由就 404。
 * 做法：导入失败先整页刷新一次（新 HTML 会引用新 chunk）；用 sessionStorage 标记防止循环刷新。
 */
/** 两次自动刷新之间至少隔这么久：发布窗口撞上一次就够，不该把整页刷新锁死在会话里。 */
const RELOAD_MIN_GAP_MS = 15_000;

function lazyWithReload(factory: () => Promise<{ default: ComponentType }>) {
  return lazy(async () => {
    try {
      const mod = await factory();
      try { sessionStorage.removeItem(RELOAD_FLAG); } catch { /* ignore */ }
      return mod;
    } catch (err) {
      // 之前是"整个会话只允许刷新一次"——发布窗口只持续几百毫秒，
      // 但用户可能在同一标签页里连撞两次（旧 HTML + 旧 chunk），第二次就卡死了。
      // 改成按时间间隔放行：15 秒内只刷一次，足够防止死循环。
      let last = 0;
      try { last = Number(sessionStorage.getItem(RELOAD_FLAG) || 0); } catch { /* ignore */ }
      if (!Number.isFinite(last)) last = 0;
      if (Date.now() - last > RELOAD_MIN_GAP_MS) {
        try { sessionStorage.setItem(RELOAD_FLAG, String(Date.now())); } catch { /* ignore */ }
        window.location.reload();
        return await new Promise<never>(() => { /* 等刷新，永不 resolve */ });
      }
      throw err;
    }
  });
}
const OverviewView = lazyWithReload(() => import('@/views/overview'));
const AccountsView = lazyWithReload(() => import('@/views/accounts'));
const ModelsView = lazyWithReload(() => import('@/views/models'));
const MetricsView = lazyWithReload(() => import('@/views/metrics'));
const LogsView = lazyWithReload(() => import('@/views/logs'));
const ApiView = lazyWithReload(() => import('@/views/api'));
const SettingsView = lazyWithReload(() => import('@/views/settings'));
const KeysView = lazyWithReload(() => import('@/views/keys'));
const RewardsView = lazyWithReload(() => import('@/views/rewards'));

function RouteFallback() {
  return (
    <div className="rounded-lg border border-line bg-surface p-4" aria-busy="true">
      <SkeletonRows rows={5} cols={4} />
    </div>
  );
}

/**
 * 登录之后才加载的一整套（查询客户端 + 路由 + 外壳 + 8 个视图）。
 * 目的：登录页只用 Button/Field/Input，不该为 TanStack、路由与 12 个基元付首屏成本。
 */
export default function Authenticated() {
  return (
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
      <HashRouter>
        <Suspense fallback={<RouteFallback />}>
          <Routes>
            <Route element={<AppShell />}>
              <Route path="/overview" element={<OverviewView />} />
              <Route path="/accounts" element={<AccountsView />} />
              <Route path="/models" element={<ModelsView />} />
              <Route path="/metrics" element={<MetricsView />} />
              <Route path="/logs" element={<LogsView />} />
              <Route path="/api" element={<ApiView />} />
              <Route path="/settings" element={<SettingsView />} />
            <Route path="/keys" element={<KeysView />} />
              <Route path="/rewards" element={<RewardsView />} />
              <Route path="*" element={<Navigate to="/overview" replace />} />
            </Route>
          </Routes>
        </Suspense>
      </HashRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}
