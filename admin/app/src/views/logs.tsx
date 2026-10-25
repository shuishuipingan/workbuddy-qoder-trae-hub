import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, RefreshCw, ScrollText, Search, Trash2 } from 'lucide-react';
import { Badge, Button, Card, EmptyState, EngineBadge, Input, SkeletonRows, useToast } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { ENGINE_LABELS, ENGINES, clearLogs, engineLogs, type Engine, type LogRow } from '@/lib/api';

type Level = 'all' | 'error' | 'warn' | 'info';
type Tagged = { eng: Engine; row: LogRow };

function levelOf(row: LogRow): string {
  return String(row.level || row.lvl || 'info').toLowerCase();
}
function timeOf(row: LogRow): string {
  const v = row.ts ?? row.at ?? row.time;
  if (typeof v === 'number') return new Date(v < 1e12 ? v * 1000 : v).toLocaleTimeString('en-GB', { hour12: false });
  if (typeof v === 'string' && v) return v.replace('T', ' ').slice(v.length > 12 ? 11 : 0, v.length > 12 ? 19 : 99);
  return '—';
}
function msgOf(row: LogRow): string {
  return String(row.msg ?? row.message ?? row.text ?? '');
}
const TONE: Record<string, 'err' | 'warn' | 'info' | 'neutral'> = {
  error: 'err', err: 'err', warn: 'warn', warning: 'warn', info: 'info', debug: 'neutral',
};

export default function LogsView() {
  const { t } = useLang();
  const toast = useToast();
  const [scope, setScope] = useState<'all' | Engine>('all');
  const [level, setLevel] = useState<Level>('all');
  const [q, setQ] = useState('');
  const [auto, setAuto] = useState(false);
  const [picked, setPicked] = useState<Tagged | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const qc = useQueryClient();

  const targets: Engine[] = scope === 'all' ? [...ENGINES] : [scope];

  const logs = useQuery({
    queryKey: ['logs', scope],
    queryFn: async () => {
      const rows: Tagged[] = [];
      const failed: Engine[] = [];
      for (const eng of targets) {
        try {
          const data = await engineLogs(eng, 200);
          for (const row of (data.logs ?? []) as LogRow[]) rows.push({ eng: eng, row: row });
        } catch { failed.push(eng); }
      }
      rows.sort(function (a, b) {
        const ta = Date.parse(String(a.row.ts ?? '')) || Number(a.row.at) || 0;
        const tb = Date.parse(String(b.row.ts ?? '')) || Number(b.row.at) || 0;
        return tb - ta;
      });
      return { rows: rows, failed: failed };
    },
    refetchInterval: auto ? 5000 : false,
  });

  useEffect(function () { return function () { setAuto(false); }; }, []);

  const rows = useMemo(function () {
    const kw = q.trim().toLowerCase();
    return (logs.data?.rows ?? []).filter(function (r) {
      if (level !== 'all' && levelOf(r.row) !== level) return false;
      if (kw && (msgOf(r.row) + ' ' + String(r.row.tag ?? '')).toLowerCase().indexOf(kw) < 0) return false;
      return true;
    });
  }, [logs.data, level, q]);

  const clear = useMutation({
    mutationFn: async function () {
      for (const eng of targets) { await clearLogs(eng); }
    },
    onSuccess: function () { toast.push('ok', t('common.done')); setConfirmClear(false); void qc.invalidateQueries({ queryKey: ['logs'] }); },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const failed = logs.data?.failed ?? [];
  const selected = picked ?? rows[0] ?? null;

  function copy(text: string) {
    navigator.clipboard.writeText(text).then(function () { toast.push('ok', t('common.copied')); })
      .catch(function () { toast.push('err', t('common.copyFailed')); });
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label={t('engine.label')} className="flex gap-0.5 rounded-md border border-line-faint p-0.5">
          {(['all', ...ENGINES] as const).map(function (s) {
            return (
              <button key={s} role="tab" aria-selected={scope === s} onClick={function () { setScope(s); setPicked(null); }}
                className={'h-7 cursor-pointer rounded px-2 text-[11.5px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ' +
                  (scope === s ? 'bg-surface-2 font-medium text-fg' : 'text-fg-dim hover:bg-surface-2/70')}>
                {s === 'all' ? t('engine.merge') : ENGINE_LABELS[s]}
              </button>
            );
          })}
        </div>
        <div role="tablist" aria-label={t('logs.level')} className="flex gap-0.5 rounded-md border border-line-faint p-0.5">
          {(['all', 'error', 'warn', 'info'] as Level[]).map(function (l) {
            return (
              <button key={l} role="tab" aria-selected={level === l} onClick={function () { setLevel(l); }}
                className={'h-7 cursor-pointer rounded px-2 text-[11.5px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ' +
                  (level === l ? 'bg-surface-2 font-medium text-fg' : 'text-fg-dim hover:bg-surface-2/70')}>
                {l === 'all' ? t('logs.levelAll') : l}
              </button>
            );
          })}
        </div>
        <div className="relative ml-auto w-full sm:w-56">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-mute" aria-hidden="true" />
          <Input value={q} onChange={function (e) { setQ(e.target.value); }} className="pl-8" placeholder={t('common.search')} aria-label={t('common.search')} />
        </div>
        <label className="flex items-center gap-1.5 text-[11.5px] text-fg-dim">
          <input type="checkbox" checked={auto} onChange={function (e) { setAuto(e.target.checked); }} className="h-3.5 w-3.5 accent-[var(--accent)]" />
          {t('logs.auto')}
        </label>
        {confirmClear ? (
          <>
            <Button size="sm" variant="danger" loading={clear.isPending} onClick={function () { clear.mutate(); }}>{t("common.confirm")}</Button>
            <Button size="sm" variant="ghost" onClick={function () { setConfirmClear(false); }}>{t("common.cancel")}</Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" icon={<Trash2 size={13} />} onClick={function () { setConfirmClear(true); }}>
            {t("logs.tab.runtime")} · {t("common.delete")}
          </Button>
        )}
        <Button size="sm" variant="secondary" icon={<RefreshCw size={13} />} loading={logs.isFetching} onClick={function () { void logs.refetch(); }}>
          {t('common.refresh')}
        </Button>
      </div>

      {failed.length ? (
        <div role="status" className="rounded-md border border-warn/35 bg-warn-soft px-2.5 py-1.5 text-[11.5px] text-warn">
          {t('models.partialWarn', { engines: failed.map(function (e) { return ENGINE_LABELS[e]; }).join(' / ') })}
        </div>
      ) : null}

      {logs.isPending ? <Card title={t('logs.title')}><SkeletonRows rows={8} cols={3} /></Card> : null}

      {logs.isError ? (
        <Card title={t('state.errTitle')}>
          <Button size="sm" variant="secondary" onClick={function () { void logs.refetch(); }}>{t('common.retry')}</Button>
        </Card>
      ) : null}

      {!logs.isPending && !logs.isError && rows.length === 0 ? (
        <EmptyState
          icon={<ScrollText size={18} />}
          title={level === 'error' ? t('logs.errorEmpty.title') : t('logs.empty.title')}
          description={level === 'error' ? t('logs.errorEmpty.desc') : t('logs.empty.desc')}
          action={<Button size="sm" variant="secondary" onClick={function () { setQ(''); setLevel('all'); }}>{t('accounts.filtered.reset')}</Button>}
        />
      ) : null}

      {!logs.isPending && !logs.isError && rows.length > 0 ? (
        <div className="grid items-start gap-3 @3xl:grid-cols-[minmax(0,1fr)_minmax(0,380px)]">
          {/* 左侧：紧凑列表 */}
          <div className="overflow-hidden rounded-lg border border-line-faint bg-surface">
            <div className="max-h-[calc(100vh-14rem)] min-h-[420px] overflow-auto">
              {rows.slice(0, 300).map(function (r, i) {
                const lv = levelOf(r.row);
                const active = selected && selected.row === r.row && selected.eng === r.eng;
                return (
                  <button
                    key={r.eng + '-' + i}
                    onClick={function () { setPicked(r); }}
                    className={'flex w-full cursor-pointer items-center gap-2 border-b border-line-faint px-2.5 py-1.5 text-left transition-colors last:border-0 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent ' +
                      (active ? 'bg-accent-soft' : 'hover:bg-surface-2/60')}
                  >
                    <span className="w-[52px] shrink-0 font-mono text-[10.5px] tnum text-fg-mute">{timeOf(r.row)}</span>
                    <Badge tone={TONE[lv] || 'neutral'} className="shrink-0">{lv}</Badge>
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-dim">{msgOf(r.row)}</span>
                    <EngineBadge engine={r.eng} className="hidden shrink-0 sm:inline-flex" />
                  </button>
                );
              })}
            </div>
          </div>

          {/* 右侧：详情 */}
          <div className="sticky top-3 rounded-lg border border-line-faint bg-surface p-3.5">
            {selected ? (
              <>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Badge tone={TONE[levelOf(selected.row)] || 'neutral'}>{levelOf(selected.row)}</Badge>
                    <EngineBadge engine={selected.eng} label={ENGINE_LABELS[selected.eng]} />
                  </div>
                  <Button size="sm" variant="ghost" icon={<Copy size={12} />} onClick={function () { copy(msgOf(selected.row)); }}>
                    {t('common.copy')}
                  </Button>
                </div>
                <dl className="mb-3 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                  <dt className="text-fg-mute">{t('logs.col.time')}</dt>
                  <dd className="text-right font-mono tnum text-fg-dim">{String(selected.row.ts ?? '—')}</dd>
                  <dt className="text-fg-mute">tag</dt>
                  <dd className="truncate text-right font-mono text-fg-dim">{String(selected.row.tag ?? '—')}</dd>
                  <dt className="text-fg-mute">id</dt>
                  <dd className="text-right font-mono tnum text-fg-dim">{String(selected.row.id ?? '—')}</dd>
                </dl>
                <pre className="max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-2/60 p-2.5 font-mono text-[11px] leading-5 text-fg-dim">
                  {msgOf(selected.row)}
                </pre>
              </>
            ) : (
              <p className="py-8 text-center text-[11.5px] text-fg-mute">{t('logs.empty.title')}</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
