import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { AlertTriangle, Brain, Boxes, Eye, RefreshCw, Search, Wrench } from 'lucide-react';
import { Button, Card, EmptyState, EngineBadge, Input, SkeletonRows, TableWrap, Td, Th, Tr } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import {
  ENGINE_LABELS, ENGINES, engineModels, hubHealth, type Engine, type ModelRow, type Realm,
} from '@/lib/api';

const VIRTUAL_THRESHOLD = 40;

type MergedModel = {
  id: string;
  name: string;
  engines: Engine[];
  ctx?: number;
  out?: number;
  vision: boolean;
  tools: boolean;
  reasoning: boolean;
};

function num(v: unknown): number | undefined {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

function fmtNum(n?: number): string {
  if (n === undefined) return "—";
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "K";
  return String(n);
}

function toMerged(rows: ModelRow[], eng: Engine): MergedModel[] {
  return rows.map((r) => {
    const caps = (r.capabilities || {}) as Record<string, boolean>;
    return {
      id: String(r.id || ""),
      name: String(r.name || r.id || ""),
      engines: [eng],
      ctx: num(r.context_length) ?? num(r.context_window),
      out: num(r.max_output_tokens) ?? num(r.max_completion_tokens),
      vision: !!(caps.vision ?? r.supports_vision),
      tools: !!(caps.tool_calls ?? r.supports_tool_calls),
      reasoning: !!(caps.reasoning ?? r.supports_reasoning),
    };
  });
}

export default function ModelsView() {
  const { t } = useLang();
  const [scope, setScope] = useState<"all" | Engine>("all");
  const [realm, setRealm] = useState<Realm>("cn");
  const [q, setQ] = useState("");
  const [caps, setCaps] = useState<{ vision: boolean; tools: boolean; reasoning: boolean }>({
    vision: false, tools: false, reasoning: false,
  });

  const targets: Engine[] = scope === "all" ? [...ENGINES] : [scope];

  const models = useQuery({
    queryKey: ["models", realm, scope],
    queryFn: async () => {
      const out: MergedModel[] = [];
      const failed: Engine[] = [];
      for (const eng of targets) {
        try {
          const data = await engineModels(eng, realm);
          out.push(...toMerged((data.data || []) as ModelRow[], eng));
        } catch { failed.push(eng); }
      }
      return { rows: out, failed };
    },
  });

  const health = useQuery({ queryKey: ["hub-health"], queryFn: hubHealth });

  const readyEngines = useMemo(() => {
    const engines = health.data?.engines ?? {};
    return new Set(ENGINES.filter((e) => (Number(engines[e]?.accounts_ready) || 0) > 0));
  }, [health.data]);

  const merged = useMemo(() => {
    const byId = new Map<string, MergedModel>();
    for (const r of models.data?.rows ?? []) {
      const prev = byId.get(r.id);
      if (!prev) { byId.set(r.id, { ...r, engines: [...r.engines] }); continue; }
      const engines = Array.from(new Set([...prev.engines, ...r.engines]));
      byId.set(r.id, {
        ...prev,
        engines,
        ctx: prev.ctx ?? r.ctx,
        out: prev.out ?? r.out,
        vision: prev.vision || r.vision,
        tools: prev.tools || r.tools,
        reasoning: prev.reasoning || r.reasoning,
      });
    }
    return Array.from(byId.values()).sort((a, b) => a.id.localeCompare(b.id));
  }, [models.data]);

  const rows = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return merged.filter((r) => {
      if (kw && !(r.name + " " + r.id).toLowerCase().includes(kw)) return false;
      if (caps.vision && !r.vision) return false;
      if (caps.tools && !r.tools) return false;
      if (caps.reasoning && !r.reasoning) return false;
      return true;
    });
  }, [merged, q, caps]);

  const callable = rows.filter((r) => r.engines.some((e) => readyEngines.has(e))).length;
  /** 用于把上下文/输出画成相对细条（对数刻度，否则 1M 与 4K 差 250 倍没法同屏看） */
  const failed = models.data?.failed ?? [];

  const toggleCap = (key: keyof typeof caps) => setCaps((c) => ({ ...c, [key]: !c[key] }));

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={t("models.title")}
        subtitle={t("models.desc")}
        actions={(
          <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={models.isFetching}
            onClick={() => void models.refetch()}>
            {t("common.refresh")}
          </Button>
        )}
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div role="tablist" aria-label={t("engine.label")} className="flex gap-1 rounded-md border border-line p-0.5">
            {(["all", ...ENGINES] as const).map((s) => (
              <button
                key={s}
                role="tab"
                aria-selected={scope === s}
                onClick={() => setScope(s)}
                className={
                  "min-h-11 cursor-pointer rounded px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                  (scope === s ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")
                }
              >
                {s === "all" ? t("engine.merge") : ENGINE_LABELS[s]}
              </button>
            ))}
          </div>
          <div role="tablist" aria-label={t("models.col.realm")} className="flex gap-1 rounded-md border border-line p-0.5">
            {(["cn", "intl"] as Realm[]).map((r) => (
              <button
                key={r}
                role="tab"
                aria-selected={realm === r}
                onClick={() => setRealm(r)}
                className={
                  "min-h-11 cursor-pointer rounded px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                  (realm === r ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")
                }
              >
                {t(r === "cn" ? "realm.cn.short" : "realm.intl.short")}
              </button>
            ))}
          </div>
          <div className="flex gap-1">
            {(["vision", "tools", "reasoning"] as const).map((k) => (
              <button
                key={k}
                aria-pressed={caps[k]}
                onClick={() => toggleCap(k)}
                className={
                  "min-h-11 cursor-pointer rounded border px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                  (caps[k] ? "border-accent bg-accent-soft text-accent" : "border-line text-fg-dim hover:bg-surface-2")
                }
              >
                {t("models.cap." + k)}
              </button>
            ))}
          </div>
          <div className="relative ml-auto w-full sm:w-64">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-mute" aria-hidden="true" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} className="h-11 pl-8 text-xs"
              placeholder={t("accounts.searchPlaceholder")} aria-label={t("common.search")} />
          </div>
        </div>

        <p className="mb-2 text-xs text-fg-mute tnum">
          {t("common.count", { n: rows.length })}
          <span className="mx-1">·</span>
          {t("models.callable", { c: callable })}
        </p>

        {failed.length ? (
          <div role="status" className="mb-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-warn">
            <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span className="mix-space">{t("models.partialWarn", { engines: failed.map((e) => ENGINE_LABELS[e]).join(" / ") })}</span>
          </div>
        ) : null}

        {models.isPending ? <SkeletonRows rows={8} cols={5} /> : null}

        {models.isError ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-sm text-err">{t("state.errTitle")}</p>
            <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} onClick={() => void models.refetch()}>
              {t("common.retry")}
            </Button>
          </div>
        ) : null}

        {!models.isPending && !models.isError && rows.length === 0 ? (
          <EmptyState
            icon={<Boxes size={20} />}
            title={t("accounts.filtered.empty")}
            description={t("accounts.filtered.emptyDesc")}
            action={<Button size="sm" variant="secondary" onClick={() => { setQ(""); setCaps({ vision: false, tools: false, reasoning: false }); }}>{t("accounts.filtered.reset")}</Button>}
          />
        ) : null}

        {!models.isPending && !models.isError && rows.length > 0 ? (
          <ModelTable rows={rows} t={t} />
        ) : null}
      </Card>
    </div>
  );
}

function ModelTable({ rows, t }: { rows: MergedModel[]; t: (k: string) => string }) {
  /** 细条用对数刻度：1M 与 4K 差 250 倍，线性刻度会让小值看不见 */
  const maxCtx = Math.max(1, ...rows.map((r) => r.ctx ?? 0));
  const maxOut = Math.max(1, ...rows.map((r) => r.out ?? 0));
  const barW = (v: number | undefined, max: number) => (v ? Math.max(3, Math.log10(1 + v) / Math.log10(1 + max) * 100) : 0) + "%";
  const ref = useRef<HTMLDivElement | null>(null);
  const virtual = rows.length > VIRTUAL_THRESHOLD;
  const v = useVirtualizer({
    count: rows.length,
    getScrollElement: () => ref.current,
    estimateSize: () => 40,
    overscan: 10,
    enabled: virtual,
    initialRect: { width: 800, height: 560 },
  });

  if (!virtual) {
    return (
      <TableWrap>
        <thead><tr>
          <Th>{t("models.col.name")}</Th>
          <Th>{t("engine.label")}</Th>
          <Th>{t("models.col.caps")}</Th>
          <Th className="text-right">{t("models.col.ctx")}</Th>
          <Th className="text-right">{t("models.col.out")}</Th>
        </tr></thead>
        <tbody>
          {rows.map((r) => <Row key={r.id} r={r} maxCtx={maxCtx} maxOut={maxOut} barW={barW} />)}
        </tbody>
      </TableWrap>
    );
  }

  return (
    <div className="overflow-hidden rounded-md border border-line">
      {/* 虚拟滚动分支也要有表头：否则用户不知道那两列数字是什么 */}
      <div className="flex items-center gap-3 border-b border-line bg-surface-2 px-3 py-2
                      text-[11px] font-semibold uppercase tracking-wide text-fg-mute">
        <span className="min-w-0 flex-1">{t("models.col.name")}</span>
        <span className="hidden w-28 shrink-0 sm:block">{t("engine.label")}</span>
        <span className="w-14 shrink-0">{t("models.col.caps")}</span>
        <span className="w-[92px] shrink-0 text-right">{t("models.col.ctx")}</span>
        <span className="w-[92px] shrink-0 text-right">{t("models.col.out")}</span>
      </div>
      <div ref={ref} className="max-h-[520px] overflow-auto">
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((item) => {
          const r = rows[item.index];
          return (
            <div key={r.id}
              style={{ position: "absolute", top: 0, left: 0, right: 0, transform: "translateY(" + item.start + "px)" }}
              className="flex items-center gap-3 border-b border-line-faint px-3 text-sm tnum">
              <span className="min-w-0 flex-1 truncate text-fg">{r.name}</span>
              <span className="hidden w-28 shrink-0 gap-1 sm:flex">{r.engines.map((e) => <EngineBadge key={e} engine={e} />)}</span>
              <span className="flex w-14 shrink-0 items-center gap-1">
                {r.vision ? <Eye size={12} aria-label={t("models.cap.vision")} className="text-accent" /> : null}
                {r.tools ? <Wrench size={12} aria-label={t("models.cap.tools")} className="text-accent-2" /> : null}
                {r.reasoning ? <Brain size={12} aria-label={t("models.cap.reasoning")} className="text-accent-3" /> : null}
              </span>
              <span className="flex w-[92px] shrink-0 items-center justify-end gap-1.5">
                <span className="h-1 w-10 overflow-hidden rounded-full bg-surface-2">
                  <span className="block h-full rounded-full" style={{ width: barW(r.ctx, maxCtx), background: "var(--accent)" }} />
                </span>
                <span className="w-10 text-right text-[11px] tnum text-fg-mute">{fmtNum(r.ctx)}</span>
              </span>
              <span className="flex w-[92px] shrink-0 items-center justify-end gap-1.5">
                <span className="h-1 w-10 overflow-hidden rounded-full bg-surface-2">
                  <span className="block h-full rounded-full" style={{ width: barW(r.out, maxOut), background: "var(--accent-2)" }} />
                </span>
                <span className="w-10 text-right text-[11px] tnum text-fg-mute">{fmtNum(r.out)}</span>
              </span>
            </div>
          );
        })}
      </div>
      </div>
    </div>
  );
}

function Row({ r, maxCtx, maxOut, barW }: { r: MergedModel; maxCtx: number; maxOut: number; barW: (v: number | undefined, max: number) => string }) {
  const { t } = useLang();
  return (
    <Tr>
      <Td className="max-w-[260px]">
        <span className="block truncate text-fg">{r.name}</span>
        <span className="block truncate font-mono text-[11px] text-fg-mute">{r.id}</span>
      </Td>
      <Td><span className="flex flex-wrap gap-1">{r.engines.map((e) => <EngineBadge key={e} engine={e} />)}</span></Td>
      <Td>
        <span className="flex items-center gap-1.5 text-fg-mute">
          {r.vision ? <Eye size={13} aria-label={t("models.cap.vision")} className="text-accent" /> : null}
          {r.tools ? <Wrench size={13} aria-label={t("models.cap.tools")} className="text-accent-2" /> : null}
          {r.reasoning ? <Brain size={13} aria-label={t("models.cap.reasoning")} className="text-accent-3" /> : null}
          {!r.vision && !r.tools && !r.reasoning ? <span className="text-[11px] text-fg-mute">—</span> : null}
        </span>
      </Td>
      <Td className="text-right">
        <span className="inline-flex w-[92px] items-center justify-end gap-1.5">
          <span className="h-1 w-10 overflow-hidden rounded-full bg-surface-2">
            <span className="block h-full rounded-full" style={{ width: barW(r.ctx, maxCtx), background: "var(--accent)" }} />
          </span>
          <span className="w-10 text-right tnum text-fg-dim">{fmtNum(r.ctx)}</span>
        </span>
      </Td>
      <Td className="text-right">
        <span className="inline-flex w-[92px] items-center justify-end gap-1.5">
          <span className="h-1 w-10 overflow-hidden rounded-full bg-surface-2">
            <span className="block h-full rounded-full" style={{ width: barW(r.out, maxOut), background: "var(--accent-2)" }} />
          </span>
          <span className="w-10 text-right tnum text-fg-dim">{fmtNum(r.out)}</span>
        </span>
      </Td>
    </Tr>
  );
}
