import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, RefreshCw, Users } from 'lucide-react';
import { Button, Card, EmptyState, EngineBadge, SkeletonRows } from '@/components/ui';
import { AreaChart, BarChart, Donut, Sparkline } from '@/components/charts';
import { useLang } from '@/lib/i18n';
import { ENGINE_LABELS, ENGINES, engineLogs, engineUsage, fanout, hubHealth, type Engine, type LogRow } from '@/lib/api';
import { bucketSeries, ratio } from '@/lib/series';

function fmt(n: number | undefined): string {
  if (n === undefined || n === null || !Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

function money(n: number | undefined): string {
  if (n === undefined || !Number.isFinite(n)) return '—';
  return '¥' + n.toFixed(2);
}

/** 一格 KPI：标签 / 主数字 / 备注，克制到只有这三层信息 */
function Kpi(props: { label: string; value: string; hint?: string; tone?: 'err' }) {
  return (
    <div className="rounded-lg border border-line-faint bg-surface px-3.5 py-3">
      <p className="text-[11px] text-fg-mute">{props.label}</p>
      <p className={'mt-1 text-[21px] font-semibold leading-7 tnum ' + (props.tone === 'err' ? 'text-err' : 'text-fg')}>
        {props.value}
      </p>
      {props.hint ? <p className="mt-0.5 truncate text-[11px] text-fg-mute">{props.hint}</p> : null}
    </div>
  );
}

export default function OverviewView() {
  const { t } = useLang();
  const navigate = useNavigate();

  const health = useQuery({ queryKey: ['hub-health'], queryFn: hubHealth });
  const usage = useQuery({ queryKey: ['overview-usage'], queryFn: () => fanout(async (eng: Engine) => engineUsage(eng)) });
  const logs = useQuery({
    queryKey: ['overview-logs'],
    queryFn: async () => {
      const rows: { eng: Engine; row: LogRow }[] = [];
      const failed: Engine[] = [];
      for (const eng of ENGINES) {
        try {
          const data = await engineLogs(eng, 200);
          for (const row of (data.logs ?? []) as LogRow[]) rows.push({ eng: eng, row: row });
        } catch { failed.push(eng); }
      }
      return { rows: rows, failed: failed };
    },
  });

  const totals = useMemo(() => {
    const values = Object.values(usage.data?.values ?? {}) as Array<Record<string, number> | undefined>;
    const sum = (k: string) => values.reduce((a, r) => a + (Number(r?.[k]) || 0), 0);
    return { requests: sum('requests'), errors: sum('errors'), tokens: sum('total_tokens'), credit: sum('credit'), cost: sum('cost_cny') };
  }, [usage.data]);

  const engines = health.data?.engines ?? {};
  const accountTotals = useMemo(() => {
    let total = 0; let ready = 0;
    for (const eng of ENGINES) {
      total += Number(engines[eng]?.accounts) || 0;
      ready += Number(engines[eng]?.accounts_ready) || 0;
    }
    return { total: total, ready: ready };
  }, [engines]);

  const series = useMemo(() => bucketSeries((logs.data?.rows ?? []).map(function (r) { return r.row; }), 24), [logs.data]);

  /** 每引擎一条迷你曲线：同一时间窗分桶，方便横向比较谁在忙 */
  const perEngineSeries = useMemo(function () {
    const out: Record<string, number[]> = {};
    for (const eng of ENGINES) {
      const rows = (logs.data?.rows ?? []).filter(function (x) { return x.eng === eng; }).map(function (x) { return x.row; });
      out[eng] = bucketSeries(rows, 24).counts;
    }
    return out;
  }, [logs.data]);

  const byModel = useMemo(() => {
    const map = new Map<string, number>();
    for (const eng of ENGINES) {
      const bm = (usage.data?.values?.[eng]?.by_model ?? {}) as Record<string, unknown>;
      for (const key of Object.keys(bm)) {
        const raw = bm[key];
        const n = typeof raw === 'number' ? raw : Number((raw as { requests?: number })?.requests) || 0;
        map.set(key, (map.get(key) ?? 0) + n);
      }
    }
    return Array.from(map, function (e) { return { label: e[0], value: e[1] }; }).sort(function (a, b) { return b.value - a.value; }).slice(0, 6);
  }, [usage.data]);

  /** 最近错误：运维每天第一眼要看的东西，直接摆在概览上 */
  const recentErrors = useMemo(function () {
    return (logs.data?.rows ?? [])
      .filter(function (x) { return String(x.row.level || '').toLowerCase() === 'error'; })
      .slice(0, 6);
  }, [logs.data]);

  const failed = Array.from(new Set([...(usage.data?.failed ?? []), ...(logs.data?.failed ?? [])]));
  const usageAllFailed = (usage.data?.failed?.length ?? 0) === ENGINES.length;
  const allFailed = health.isError || usageAllFailed;
  const loading = usage.isPending || health.isPending;
  const empty = !loading && !allFailed && totals.requests === 0 && accountTotals.total === 0;
  const successRate = ratio(totals.requests - totals.errors, totals.requests);

  function retryAll() {
    void health.refetch(); void usage.refetch(); void logs.refetch();
  }
  const busy = health.isFetching || usage.isFetching || logs.isFetching;

  if (loading) {
    return (
      <div className="flex flex-col gap-3">
        <div className="grid gap-3 @lg:grid-cols-2 @3xl:grid-cols-4">
          {[0, 1, 2, 3].map(function (i) {
            return <div key={i} className="rounded-lg border border-line-faint bg-surface px-3.5 py-3"><SkeletonRows rows={2} cols={1} /></div>;
          })}
        </div>
        <div className="rounded-lg border border-line-faint bg-surface p-4"><SkeletonRows rows={5} cols={3} /></div>
      </div>
    );
  }

  if (allFailed) {
    return (
      <Card title={t('state.errTitle')}>
        <div className="flex flex-col items-start gap-2">
          <p className="text-[12px] text-fg-dim">{t('state.errDesc')}</p>
          <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={retryAll} loading={busy}>{t('common.retry')}</Button>
        </div>
      </Card>
    );
  }

  if (empty) {
    return (
      <EmptyState
        icon={<Users size={18} />}
        title={t('state.emptyTitle')}
        description={t('state.emptyDesc')}
        action={<Button variant="primary" size="sm" onClick={function () { navigate('/accounts'); }} icon={<Users size={14} />}>{t('nav.accounts')}</Button>}
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11.5px] text-fg-mute">{t('overview.desc')}</p>
        <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={retryAll} loading={busy}>
          {busy ? t('app.loading') : t('common.refresh')}
        </Button>
      </div>

      {failed.length ? (
        <div role="status" className="flex items-start gap-2 rounded-md border border-warn/35 bg-warn-soft px-2.5 py-1.5 text-[11.5px] text-warn">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="mix-space">{t('overview.pool.partial', { engines: failed.map(function (e) { return ENGINE_LABELS[e]; }).join(' / ') })}</span>
        </div>
      ) : null}

      {/* KPI 条 */}
      <div className="grid gap-3 @lg:grid-cols-2 @3xl:grid-cols-4">
        <Kpi label={t('overview.kpi.requests')} value={fmt(totals.requests)} hint={t('engine.online', { up: String(ENGINES.filter(function (e) { return engines[e] && engines[e].ok !== false; }).length), total: String(ENGINES.length) })} />
        <Kpi label={t('overview.kpi.errors')} value={fmt(totals.errors)} hint={successRate === undefined ? '—' : t('metrics.successRate') + ' ' + successRate.toFixed(1) + '%'} tone={totals.errors > 0 ? 'err' : undefined} />
        <Kpi label={t('overview.kpi.tokens')} value={fmt(totals.tokens)} hint={t('overview.pool.ready') + ' ' + fmt(accountTotals.ready) + ' / ' + fmt(accountTotals.total)} />
        <Kpi label={t('overview.kpi.cost')} value={totals.cost > 0 ? money(totals.cost) : t('overview.kpi.noPricing')} hint={totals.credit > 0 ? t('overview.kpi.credits') + ' ' + fmt(totals.credit) : undefined} />
      </div>

      {/* 图表区 */}
      <div className="grid gap-3 @3xl:grid-cols-[minmax(0,1.9fr)_minmax(0,1fr)]">
        <Card title={t('overview.chart.traffic')} subtitle={t('overview.chart.window', { n: String((logs.data?.rows ?? []).length), from: series.labels[0] ?? '—' })}>
          <AreaChart
            data={series.counts}
            labels={series.labels}
            height={150}
            formatValue={fmt}
            ariaLabel={t('metrics.title')}
          />
          <div className="mt-2 flex items-center justify-between text-[10.5px] text-fg-mute">
            <span>{series.labels[0] ?? '—'}</span>
            <span>{series.labels[series.labels.length - 1] ?? '—'}</span>
          </div>
        </Card>

        <Card title={t('metrics.successRate')}>
          <div className="flex items-center gap-4">
            <Donut
              segments={[{ value: Math.max(0, totals.requests - totals.errors), tone: 'accent-2' }, { value: totals.errors, tone: 'err' }]}
              centerLabel={successRate === undefined ? '—' : successRate.toFixed(1) + '%'}
              centerHint={t('metrics.successRate')}
              ariaLabel={t('metrics.successRate')}
            />
            <ul className="flex flex-col gap-1.5 text-[11.5px]">
              <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent-2)' }} />{t('common.ok')} <span className="tnum text-fg-mute">{fmt(Math.max(0, totals.requests - totals.errors))}</span></li>
              <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--err)' }} />{t('common.error')} <span className="tnum text-fg-mute">{fmt(totals.errors)}</span></li>
            </ul>
          </div>
        </Card>
      </div>

      <div className="grid gap-3 @2xl:grid-cols-2">
        <Card title={t('metrics.byModel')}>
          <BarChart data={byModel} maxRows={6} formatValue={fmt} ariaLabel={t('metrics.byModel')} />
        </Card>

        <Card title={t('logs.tab.errors')} subtitle={t('logs.errorEmpty.desc')}>
          {recentErrors.length === 0 ? (
            <p className="py-6 text-center text-[11.5px] text-fg-mute">{t('logs.errorEmpty.title')}</p>
          ) : (
            <ul className="flex flex-col">
              {recentErrors.map(function (x, i) {
                return (
                  <li key={i} className="flex items-center gap-2 border-b border-line-faint py-1.5 last:border-0">
                    <span className="w-[52px] shrink-0 font-mono text-[10.5px] tnum text-fg-mute">{String(x.row.ts ?? '').slice(11, 19) || '—'}</span>
                    <EngineBadge engine={x.eng} className="shrink-0" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-dim" title={String(x.row.msg ?? '')}>{String(x.row.msg ?? '')}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>

      <div className="grid gap-3 @2xl:grid-cols-3">

          {ENGINES.map(function (eng) {
            const info = engines[eng];
            const down = !info || info.ok === false;
            const total = Number(info?.accounts) || 0;
            const ready = Number(info?.accounts_ready) || 0;
            const tone = eng === 'wb' ? 'accent' : eng === 'qd' ? 'accent-2' : 'accent-3';
            return (
              <div key={eng} className="flex flex-col gap-2 rounded-lg border border-line-faint bg-surface px-3.5 py-3">
                <div className="flex items-center justify-between gap-2">
                  <EngineBadge engine={eng} label={ENGINE_LABELS[eng]} />
                  <span className="flex items-center gap-1.5 text-[10.5px] text-fg-mute">
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: down ? 'var(--err)' : 'var(--ok)' }} />
                    {down ? t('common.error') : t('common.ok')}
                  </span>
                </div>
                <div className="flex items-baseline gap-1.5">
                  <span className="text-[19px] font-semibold leading-6 tnum">{ready}</span>
                  <span className="text-[11px] text-fg-mute tnum">/ {total} {t('overview.pool.ready')}</span>
                </div>
                <Sparkline data={perEngineSeries[eng] ?? []} height={26} ariaLabel={ENGINE_LABELS[eng] + ' 24h'} tone={tone} />
                <div className="flex items-center justify-between text-[10px] text-fg-mute">
                  <span className="font-mono">{String(info?.version || '—')}</span>
                  <span className="tnum">{t('logs.col.tokens')} {fmt(Number((usage.data?.values?.[eng] as Record<string, number> | undefined)?.total_tokens) || 0)}</span>
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}
