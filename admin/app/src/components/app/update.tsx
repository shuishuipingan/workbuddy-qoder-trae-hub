/**
 * 更新相关的两个组件（卡片 + 顶部提醒条）。
 *
 * 放在独立文件里，而不是塞进 settings.tsx：
 *   · 面板外壳（app-shell）要挂提醒条 —— 从外壳 import 整个 settings 视图会
 *     把懒加载的整页打进主包，首屏白付这份体积。
 *   · 两处共用同一份状态（queryKey = hub-update），刷新一次两边都更新。
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Sparkles } from 'lucide-react';
import { Badge, Button, Card, useToast } from '@/components/ui';
import { hubUpdate, hubUpdateApply, hubRestart } from '@/lib/api';

/**
 * 版本与更新：检查 GitHub Release → 一键更新 → 重启生效。
 *
 * 为什么放在设置页而不是弹窗：更新是低频、一次性动作，弹窗会打断操作；
 * 但"有新版本"这件事要在面板顶部提醒一次（见 UpdateBanner）。
 */
export function UpdateCard({ t }: { t: (k: string, v?: Record<string, string | number>) => string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [applying, setApplying] = useState(false);
  const info = useQuery({ queryKey: ['hub-update'], queryFn: function () { return hubUpdate(false); }, staleTime: 6 * 3600_000 });

  const apply = useMutation({
    mutationFn: function () { return hubUpdateApply(); },
    onMutate: function () { setApplying(true); },
    onSettled: function () { setApplying(false); },
    onSuccess: function (res: { ok?: boolean; updated?: boolean; from?: string; to?: string; reason?: string }) {
      if (!res.ok) { toast.push('err', String(res.reason || t('update.failed'))); return; }
      if (!res.updated) { toast.push('ok', t('update.alreadyLatest')); return; }
      toast.push('ok', t('update.updated', { from: String(res.from || ''), to: String(res.to || '') }));
      void qc.invalidateQueries({ queryKey: ['hub-update'] });
    },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const restart = useMutation({
    mutationFn: function () { return hubRestart(); },
    onSuccess: function (res: { ok?: boolean; reason?: string }) {
      if (!res.ok) { toast.push('err', String(res.reason || t('update.restartFailed'))); return; }
      toast.push('warn', t('update.restarting'));
      window.setTimeout(function () { location.reload(); }, 6000);
    },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const cur = info.data?.current || '—';
  const latest = info.data?.latest || '—';
  const hasNew = info.data?.update_available === true;

  return (
    <Card
      title={t('update.title')}
      subtitle={t('update.hint')}
      actions={(
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="secondary" loading={info.isFetching} onClick={function () { void info.refetch(); }}>
            {t('update.check')}
          </Button>
          <Button size="sm" variant="primary" loading={applying} disabled={!hasNew} onClick={function () { apply.mutate(); }}>
            {t('update.apply')}
          </Button>
          <Button size="sm" variant="ghost" loading={restart.isPending} onClick={function () { restart.mutate(); }}>
            {t('update.restart')}
          </Button>
        </div>
      )}
    >
      <div className="flex flex-wrap items-center gap-3 text-[11.5px]">
        <span className="text-fg-mute">{t('update.current')}</span>
        <Badge tone="neutral">v{cur}</Badge>
        <span className="text-fg-mute">{t('update.latest')}</span>
        <Badge tone={hasNew ? 'accent' : 'ok'}>v{latest}</Badge>
        {info.data?.error ? <span className="text-err">{info.data.error}</span> : null}
        {hasNew && info.data?.url ? (
          <a className="link-underline text-accent" href={String(info.data.url)} target="_blank" rel="noreferrer">{t('update.releaseNotes')}</a>
        ) : null}
      </div>
      {hasNew && info.data?.notes ? (
        <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-surface-2/60 p-2.5 font-mono text-[10.5px] text-fg-dim">{String(info.data.notes).slice(0, 1600)}</pre>
      ) : null}
    </Card>
  );
}

/** 顶部提醒条：有新版本时出现一次，可以点"立即更新"或关掉。 */
export function UpdateBanner({ t }: { t: (k: string, v?: Record<string, string | number>) => string }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [hidden, setHidden] = useState(false);
  const info = useQuery({ queryKey: ['hub-update'], queryFn: function () { return hubUpdate(false); }, staleTime: 6 * 3600_000 });
  const apply = useMutation({
    mutationFn: function () { return hubUpdateApply(); },
    onSuccess: function (res: { ok?: boolean; updated?: boolean; from?: string; to?: string; reason?: string }) {
      if (!res.ok) { toast.push('err', String(res.reason || t('update.failed'))); return; }
      toast.push('ok', res.updated ? t('update.updated', { from: String(res.from || ''), to: String(res.to || '') }) : t('update.alreadyLatest'));
      void qc.invalidateQueries({ queryKey: ['hub-update'] });
    },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });
  if (hidden || info.data?.update_available !== true) return null;
  return (
    <div className="mb-3 flex items-center gap-2 rounded-lg border border-accent/40 bg-accent-soft px-3 py-2 text-[11.5px] text-accent">
      <Sparkles size={14} />
      <span className="min-w-0 flex-1 truncate">{t('update.banner', { v: String(info.data?.latest || '') })}</span>
      <Button size="sm" variant="primary" loading={apply.isPending} onClick={function () { apply.mutate(); }}>{t('update.apply')}</Button>
      <Button size="sm" variant="ghost" onClick={function () { setHidden(true); }}>{t('common.close')}</Button>
    </div>
  );
}
