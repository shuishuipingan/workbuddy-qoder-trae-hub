/**
 * 数据指标页：按「区间 + 概览 + 排行 + 热力图 + 缓存命中」组织（对齐 Agent2API 的统计页）。
 *
 * 数据来源都经过实测，不编字段：
 *   · /api/hub/timeseries  按桶的 requests / total_tokens / prompt_tokens / cached_tokens / errors
 *   · /api/hub/usage       每家的请求/Token/积分 + by_model（同名模型跨引擎求和）
 *   · /api/{eng}/usage/by-account  按账号的请求与 Token
 *
 * 诚实的边界：引擎的时序只返回**有数据的桶**（稀疏）。所以热力图里空格=当天确实没有请求，
 * 不是"数据丢了"；卡片标题也写清区间。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AreaChart, BarChart, Donut } from '@/components/charts';
import { Button, Card, SkeletonRows } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { ENGINES, ENGINE_LABELS, hubByModel, hubTimeseries, hubUsage, usageByAccount, type Engine } from '@/lib/api';

type RangeKey = "today" | "7d" | "30d" | "month" | "all" | "custom";

const RANGES: { key: RangeKey; zh: string; en: string }[] = [
  { key: "today", zh: "今天", en: "Today" },
  { key: "7d", zh: "7 天", en: "7 days" },
  { key: "30d", zh: "30 天", en: "30 days" },
  { key: "month", zh: "本月", en: "This month" },
  { key: "all", zh: "全部", en: "All" },
  { key: "custom", zh: "自定义", en: "Custom" },
];

function fmtInt(n: number | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return Math.round(n).toLocaleString("en-US");
}

function fmtCompact(n: number | undefined): string {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}

function dayKey(sec: number): string {
  const d = new Date(sec * 1000);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

/** 数字滚动：数据变化时从旧值缓动到新值，让"刷新"这件事看得见。 */
function useCountUp(target: number, ms = 600): number {
  const [value, setValue] = useState(target);
  const fromRef = useRef<number>(target);
  useEffect(function () {
    const from = fromRef.current;
    const start = performance.now();
    let raf = 0;
    const tick = function (now: number) {
      const p = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - p, 3);
      setValue(from + (target - from) * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
      else fromRef.current = target;
    };
    raf = requestAnimationFrame(tick);
    return function () { cancelAnimationFrame(raf); };
  }, [target, ms]);
  return value;
}

/** 一张 KPI 卡：标题 + 主数值 + 副标题；数值带滚动动画。 */
function Kpi(props: { label: string; value: number; hint?: string; format?: (n: number) => string; tone?: string }) {
  const shown = useCountUp(props.value);
  const fmt = props.format ?? fmtInt;
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2.5 transition-[transform,box-shadow] duration-200 hover:-translate-y-0.5 hover:shadow-2">
      <p className="text-[11px] text-fg-mute">{props.label}</p>
      <p className={"mt-1 text-[20px] font-semibold leading-tight tnum " + (props.tone ?? "text-fg")}>{fmt(shown)}</p>
      {props.hint ? <p className="mt-0.5 truncate text-[10.5px] text-fg-mute" title={props.hint}>{props.hint}</p> : null}
    </div>
  );
}

/** 365 天热力图：一格一天，空格=当天没有请求（引擎只返回有数据的桶，这是事实不是缺数据）。 */
function Heatmap(props: { values: Record<string, number>; days?: number; t: (k: string) => string }) {
  const days = props.days ?? 180;
  const cells = useMemo(function () {
    const out: { key: string; value: number }[] = [];
    const today = new Date();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today.getTime() - i * 86400000);
      const key = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      out.push({ key: key, value: props.values[key] ?? 0 });
    }
    return out;
  }, [props.values, days]);
  const max = cells.reduce(function (a, c) { return Math.max(a, c.value); }, 0);
  function tone(v: number): string {
    if (!v) return "bg-surface-2";
    const r = max > 0 ? v / max : 0;
    if (r > 0.66) return "bg-accent";
    if (r > 0.33) return "bg-accent/70";
    if (r > 0.10) return "bg-accent/45";
    return "bg-accent/25";
  }
  return (
    <div>
      <div className="flex flex-wrap gap-[3px]">
        {cells.map(function (c) {
          return (
            <span
              key={c.key}
              title={c.key + " · " + fmtInt(c.value) + " tokens"}
              className={"h-2.5 w-2.5 rounded-[2px] transition-transform duration-150 hover:scale-150 " + tone(c.value)}
            />
          );
        })}
      </div>
      <div className="mt-2 flex items-center gap-2 text-[10.5px] text-fg-mute">
        <span>0</span>
        <span className="h-2.5 w-2.5 rounded-[2px] bg-surface-2" />
        <span className="h-2.5 w-2.5 rounded-[2px] bg-accent/25" />
        <span className="h-2.5 w-2.5 rounded-[2px] bg-accent/45" />
        <span className="h-2.5 w-2.5 rounded-[2px] bg-accent/70" />
        <span className="h-2.5 w-2.5 rounded-[2px] bg-accent" />
        <span>{"> " + fmtCompact(max)}</span>
      </div>
    </div>
  );
}

/** 双序列折线：命中率（左轴 %）+ Token（右轴），用于"近 24 小时"。 */
function RatioTrend(props: { points: { at: number; ratio: number | null; tokens: number }[] }) {
  const W = 640;
  const Hh = 200;
  const pts = props.points;
  const [hover, setHover] = useState<number | null>(null);
  const hasAny = pts.some(function (p) { return (p.ratio !== null && p.ratio > 0) || p.tokens > 0; });
  if (!pts.length || !hasAny) {
    return (
      <p className="rounded-md border border-dashed border-line-faint bg-surface-2/30 py-4 text-center text-[11px] text-fg-mute">
        近 24 小时没有带缓存的请求（引擎只返回有数据的桶）
      </p>
    );
  }
  const maxTok = pts.reduce(function (a, p) { return Math.max(a, p.tokens); }, 1);
  const step = pts.length > 1 ? W / (pts.length - 1) : W;
  const ratioPts = pts.map(function (p, i) {
    const y = Hh - ((p.ratio ?? 0) / 100) * Hh;
    return (i * step).toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  const tokPts = pts.map(function (p, i) {
    const y = Hh - (p.tokens / maxTok) * Hh;
    return (i * step).toFixed(1) + "," + y.toFixed(1);
  }).join(" ");
  function tick(i: number): string {
    const d = new Date(pts[i].at * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  return (
    <svg viewBox={"0 0 " + W + " " + Hh} style={{ width: "100%", height: Hh }} role="img" aria-label="缓存命中率与 Token 趋势" preserveAspectRatio="none">
      {[0, 0.25, 0.5, 0.75, 1].map(function (r) {
        return <line key={r} x1={0} x2={W} y1={Hh * r} y2={Hh * r} stroke="var(--line-faint)" strokeWidth={1} strokeDasharray={r === 1 ? "" : "2 4"} />;
      })}
      <polyline points={tokPts} fill="none" stroke="var(--warn)" strokeWidth={1.5} strokeDasharray="4 3" opacity="0.85" />
      <polyline points={ratioPts} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" pathLength={1} className="chart-line" />
      {pts.map(function (p, i) {
        return <circle key={i} cx={i * step} cy={Hh - ((p.ratio ?? 0) / 100) * Hh} r={2.2} fill="var(--accent)" opacity={hover === null || hover === i ? 1 : 0.35} />;
      })}
      {hover !== null ? (function () {
        const hx = hover * step;
        const hy = Hh - ((pts[hover].ratio ?? 0) / 100) * Hh;
        const text = tick(hover) + " · " + (pts[hover].ratio === null ? "—" : pts[hover].ratio.toFixed(1) + "%");
        const boxW = Math.max(88, text.length * 6.4 + 14);
        const boxX = Math.min(Math.max(hx - boxW / 2, 2), W - boxW - 2);
        return (
          <g>
            <line x1={hx} x2={hx} y1={0} y2={Hh} stroke="var(--accent)" strokeWidth={1} strokeDasharray="3 3" opacity="0.5" />
            <circle cx={hx} cy={hy} r={4} fill="var(--accent)" stroke="var(--surface)" strokeWidth={2} />
            <rect x={boxX} y={4} width={boxW} height={20} rx={5} fill="var(--surface)" stroke="var(--line)" />
            <text x={boxX + boxW / 2} y={17.5} fontSize={10.5} textAnchor="middle" fill="var(--fg)">{text}</text>
          </g>
        );
      })() : null}
      <rect
        x={0} y={0} width={W} height={Hh} fill="transparent" style={{ cursor: "crosshair" }}
        onMouseMove={function (e) {
          const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
          const rel = (e.clientX - rect.left) / Math.max(1, rect.width);
          setHover(Math.min(pts.length - 1, Math.max(0, Math.round(rel * (pts.length - 1)))));
        }}
        onMouseLeave={function () { setHover(null); }}
      />
    </svg>
  );
}

export default function MetricsView() {
  const { t, lang } = useLang();
  const [range, setRange] = useState<RangeKey>("7d");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");

  const daily = range === "30d" || range === "month" || range === "all";
  // 区间参数：自定义走 since/until，其余走 range；**下面所有查询都用它**，
  // 否则切换区间只有上面那几个数字变、下面的图一动不动（看起来像死数据）。
  const rangeOpts = useMemo(function () {
    return range === "custom" && since && until
      ? { since: since, until: until }
      : { range: range === "custom" ? "7d" : range };
  }, [range, since, until]);

  const ts = useQuery({
    queryKey: ["metrics-ts", rangeOpts],
    queryFn: function () { return hubTimeseries({ ...rangeOpts, bucket: daily ? 86400 : 3600 }); },
    staleTime: 30_000,
  });
  const tsHourly = useQuery({
    queryKey: ["metrics-ts-hourly", rangeOpts],
    queryFn: function () { return hubTimeseries({ ...rangeOpts, bucket: 3600 }); },
    staleTime: 60_000,
  });
  const tsYear = useQuery({
    queryKey: ["metrics-ts-daily", rangeOpts],
    queryFn: function () { return hubTimeseries({ ...rangeOpts, bucket: 86400 }); },
    staleTime: 300_000,
  });
  const usage = useQuery({
    queryKey: ["metrics-usage", rangeOpts],
    queryFn: function () { return hubUsage(rangeOpts); },
    staleTime: 30_000,
  });
  const models = useQuery({
    queryKey: ["metrics-by-model", rangeOpts],
    queryFn: function () { return hubByModel(rangeOpts); },
    staleTime: 60_000,
  });
  const accounts = useQuery({
    queryKey: ["metrics-accounts", rangeOpts],
    queryFn: async function () {
      const out: { account: string; engine: Engine; requests: number; tokens: number }[] = [];
      for (const eng of ENGINES) {
        try {
          const doc = await usageByAccount(eng, rangeOpts);
          for (const row of (doc?.accounts ?? [])) {
            const r = row as Record<string, unknown>;
            out.push({
              account: String(r.account ?? ""),
              engine: eng,
              requests: Number(r.requests ?? 0),
              tokens: Number(r.total_tokens ?? r.tokens ?? 0),
            });
          }
        } catch { /* 单家失败不影响其它家 */ }
      }
      return out.filter(function (r) { return r.account; }).sort(function (a, b) { return b.tokens - a.tokens; });
    },
    staleTime: 60_000,
  });

  const agg = useMemo(function () {
    const labels = ts.data?.labels ?? [];
    const sumTok = ts.data?.sum_tokens ?? [];
    const sumPrompt = ts.data?.sum_prompt ?? [];
    const sumCached = ts.data?.sum_cached ?? [];
    const sumErrors = ts.data?.sum_errors ?? [];
    const requests = (ts.data?.series ? ENGINES.reduce(function (a, e) {
      return a + (ts.data.series?.[e] ?? []).reduce(function (x, y) { return x + y; }, 0);
    }, 0) : 0);
    const errors = sumErrors.reduce(function (a, b) { return a + b; }, 0);
    const tokens = sumTok.reduce(function (a, b) { return a + b; }, 0);
    const prompt = sumPrompt.reduce(function (a, b) { return a + b; }, 0);
    const cached = sumCached.reduce(function (a, b) { return a + b; }, 0);
    const days = new Set(labels.map(function (v, i) { return (sumTok[i] ?? 0) > 0 ? dayKey(v) : ""; }).filter(Boolean));
    let streak = 0;
    for (let i = 0; i < 400; i++) {
      const d = new Date(Date.now() - i * 86400000);
      const k = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      if (days.has(k)) streak += 1;
      else if (i > 0) break;
    }
    return { requests: requests, errors: errors, tokens: tokens, prompt: prompt, cached: cached, days: days.size, streak: streak };
  }, [ts.data]);

  const byModel = useMemo(function () {
    const src = (models.data?.by_model ?? {}) as Record<string, { requests?: number; total_tokens?: number }>;
    return Object.keys(src).map(function (name) {
      return { label: name, value: Number(src[name]?.total_tokens ?? 0), requests: Number(src[name]?.requests ?? 0) };
    }).sort(function (a, b) { return b.value - a.value; });
  }, [models.data]);

  const byEngine = useMemo(function () {
    return ENGINES.map(function (e) {
      const row = (usage.data?.[e] ?? {}) as { total_tokens?: number };
      return { label: ENGINE_LABELS[e], value: Number(row.total_tokens ?? 0) };
    }).sort(function (a, b) { return b.value - a.value; });
  }, [usage.data]);

  const topModel = byModel[0];
  const successRate = agg.requests > 0 ? ((agg.requests - agg.errors) / agg.requests) * 100 : undefined;

  const cacheWindows = useMemo(function () {
    const labels = tsHourly.data?.labels ?? [];
    const prompt = tsHourly.data?.sum_prompt ?? [];
    const cached = tsHourly.data?.sum_cached ?? [];
    const now = Date.now() / 1000;
    function win(sec: number) {
      let p = 0, c = 0;
      for (let i = 0; i < labels.length; i++) {
        if (now - labels[i] <= sec) { p += prompt[i] ?? 0; c += cached[i] ?? 0; }
      }
      return { prompt: p, cached: c, ratio: p > 0 ? (c / p) * 100 : null };
    }
    return { m10: win(600), h1: win(3600), h24: win(86400), d7: win(7 * 86400) };
  }, [tsHourly.data]);

  const cacheTrend = useMemo(function () {
    const labels = tsHourly.data?.labels ?? [];
    const prompt = tsHourly.data?.sum_prompt ?? [];
    const cached = tsHourly.data?.sum_cached ?? [];
    const tokens = tsHourly.data?.sum_tokens ?? [];
    const now = Date.now() / 1000;
    const out: { at: number; ratio: number | null; tokens: number }[] = [];
    for (let i = 0; i < labels.length; i++) {
      if (now - labels[i] > 86400) continue;
      out.push({ at: labels[i], ratio: (prompt[i] ?? 0) > 0 ? ((cached[i] ?? 0) / prompt[i]) * 100 : null, tokens: tokens[i] ?? 0 });
    }
    return out;
  }, [tsHourly.data]);

  const heatValues = useMemo(function () {
    const labels = tsYear.data?.labels ?? [];
    const tokens = tsYear.data?.sum_tokens ?? [];
    const out: Record<string, number> = {};
    for (let i = 0; i < labels.length; i++) {
      const k = dayKey(labels[i]);
      out[k] = (out[k] ?? 0) + (tokens[i] ?? 0);
    }
    return out;
  }, [tsYear.data]);

  /** 按天 Token 趋势：**固定用日桶**（tsYear），否则 7 天区间会退化成小时桶，
   *  卡片标题写"按天"而横轴是 23:00/01:00，对不上。 */
  const dailyTrend = useMemo(function () {
    const tokens = tsYear.data?.sum_tokens ?? [];
    const labels = tsYear.data?.labels ?? [];
    const take = Math.min(tokens.length, 120);
    return {
      values: tokens.slice(-take),
      labels: labels.slice(-take).map(function (sec) {
        const d = new Date(sec * 1000);
        return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
      }),
    };
  }, [tsYear.data]);

  const busy = ts.isFetching || usage.isFetching;

  return (
    <div className="metrics-page flex flex-col gap-4">
      <Card
        title={t("metrics.overview")}
        subtitle={t("metrics.overviewHint")}
        actions={(
          <div className="flex flex-wrap items-center gap-1.5">
            <div className="flex gap-0.5 rounded-md border border-line p-0.5">
              {RANGES.map(function (r) {
                const active = range === r.key;
                return (
                  <button
                    key={r.key}
                    type="button"
                    onClick={function () { setRange(r.key); }}
                    className={"h-7 cursor-pointer rounded px-2.5 text-[11.5px] transition-[background-color,color,transform] duration-150 active:scale-95 " +
                      (active ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")}
                  >
                    {lang === "en" ? r.en : r.zh}
                  </button>
                );
              })}
            </div>
            {range === "custom" ? (
              <div className="flex items-center gap-1">
                <input type="date" value={since} onChange={function (e) { setSince(e.target.value); }}
                  className="h-7 rounded-md border border-line bg-surface px-2 text-[11px] text-fg" />
                <span className="text-[11px] text-fg-mute">~</span>
                <input type="date" value={until} onChange={function (e) { setUntil(e.target.value); }}
                  className="h-7 rounded-md border border-line bg-surface px-2 text-[11px] text-fg" />
              </div>
            ) : null}
            <Button size="sm" variant="secondary" loading={busy} onClick={function () { void ts.refetch(); void usage.refetch(); void models.refetch(); }}>
              {t("common.refresh")}
            </Button>
          </div>
        )}
      >
        {ts.isPending ? (
          <SkeletonRows rows={2} cols={6} />
        ) : (
          <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Kpi label={t("metrics.kpi.requests")} value={agg.requests} hint={t("metrics.kpi.requestsHint")} />
            <Kpi label={t("metrics.kpi.success")} value={Math.max(0, agg.requests - agg.errors)}
              hint={successRate === undefined ? t("metrics.kpi.rateNa") : t("metrics.kpi.rate", { v: successRate.toFixed(1) })}
              tone="text-ok" />
            <Kpi label={t("metrics.kpi.tokens")} value={agg.tokens} format={fmtCompact} hint={t("metrics.kpi.tokensHint")} />
            <Kpi label={t("metrics.kpi.activeDays")} value={agg.days} hint={t("metrics.kpi.activeDaysHint")} />
            <Kpi label={t("metrics.kpi.streak")} value={agg.streak} hint={t("metrics.kpi.streakHint")} />
            <Kpi label={t("metrics.kpi.topModel")} value={topModel ? topModel.value : 0} format={fmtCompact}
              hint={topModel ? topModel.label + " · " + fmtInt(topModel.requests) + " req" : t("metrics.kpi.topModelNa")} />
          </div>
        )}
      </Card>

      <Card title={t("metrics.topAccounts")} subtitle={t("metrics.topAccountsHint")}>
        {accounts.isPending ? <SkeletonRows rows={4} cols={2} /> : (
          <div className="grid gap-x-6 gap-y-1 md:grid-cols-2">
            <BarChart
              data={(accounts.data ?? []).slice(0, 5).map(function (r) {
                return { label: r.account.slice(0, 8) + " · " + ENGINE_LABELS[r.engine], value: r.tokens };
              })}
              formatValue={fmtCompact}
              ariaLabel={t("metrics.topAccounts")}
            />
            <BarChart
              data={(accounts.data ?? []).slice(5, 10).map(function (r) {
                return { label: r.account.slice(0, 8) + " · " + ENGINE_LABELS[r.engine], value: r.tokens };
              })}
              formatValue={fmtCompact}
              ariaLabel={t("metrics.topAccounts")}
            />
          </div>
        )}
      </Card>

      <div className="grid items-stretch gap-4 lg:grid-cols-2">
        <Card title={t("metrics.modelUsage")} subtitle={t("metrics.modelUsageHint")}>
          <BarChart
            data={byModel.slice(0, 6).map(function (r) { return { label: r.label, value: r.value }; })}
            formatValue={fmtCompact}
            tone="ok"
            ariaLabel={t("metrics.modelUsage")}
          />
        </Card>
        <Card title={t("metrics.providerUsage")} subtitle={t("metrics.topProvidersHint")}>
          <div className="flex items-center gap-5">
            <Donut
              segments={byEngine.map(function (r, i) {
                return { value: r.value, tone: i === 0 ? "accent" : i === 1 ? "accent-2" : "ok" };
              })}
              centerLabel={fmtCompact(byEngine.reduce(function (a, r) { return a + r.value; }, 0))}
              centerHint={t("metrics.kpi.tokens")}
              ariaLabel={t("metrics.providerUsage")}
            />
            <div className="min-w-0 flex-1">
              <BarChart
                data={byEngine.map(function (r) { return { label: r.label, value: r.value }; })}
                formatValue={fmtCompact}
                tone="accent-2"
                ariaLabel={t("metrics.providerUsage")}
              />
            </div>
          </div>
        </Card>
      </div>

      <Card title={t("metrics.heatmap")} subtitle={t("metrics.heatmapHint")}>
        {tsYear.isPending ? <SkeletonRows rows={3} cols={8} /> : <Heatmap values={heatValues} t={t} />}
      </Card>

      <Card title={t("metrics.cacheHit")} subtitle={t("metrics.cacheHitHint")}>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {([
            { key: "m10", label: t("metrics.win.m10"), v: cacheWindows.m10 },
            { key: "h1", label: t("metrics.win.h1"), v: cacheWindows.h1 },
            { key: "h24", label: t("metrics.win.h24"), v: cacheWindows.h24 },
            { key: "d7", label: t("metrics.win.d7"), v: cacheWindows.d7 },
          ]).map(function (w) {
            return (
              <div key={w.key} className="rounded-lg border border-line bg-surface px-3 py-2.5 transition-transform duration-200 hover:-translate-y-0.5">
                <p className="text-[11px] text-fg-mute">{w.label}</p>
                <p className="mt-1 text-[18px] font-semibold tnum text-fg">
                  {w.v.ratio === null ? "无请求" : w.v.ratio.toFixed(1) + "%"}
                </p>
                <p className="text-[10.5px] text-fg-mute">{t("metrics.hitOf", { c: fmtCompact(w.v.cached), p: fmtCompact(w.v.prompt) })}</p>
              </div>
            );
          })}
        </div>
      </Card>

      <Card title={t("metrics.cacheTrend")} subtitle={t("metrics.cacheTrendHint")}>
        {tsHourly.isPending ? <SkeletonRows rows={3} cols={6} /> : <RatioTrend points={cacheTrend} />}
      </Card>

      <Card title={t("metrics.tokenTrend")} subtitle={t("metrics.tokenTrendHint")}>
        {dailyTrend.values.length > 1 ? (
          <AreaChart data={dailyTrend.values} labels={dailyTrend.labels} height={220} formatValue={fmtCompact} ariaLabel={t("metrics.tokenTrend")} />
        ) : (
          <p className="rounded-md border border-dashed border-line-faint bg-surface-2/30 py-4 text-center text-[11px] text-fg-mute">{t("metrics.noData")}</p>
        )}
      </Card>
    </div>
  );
}
