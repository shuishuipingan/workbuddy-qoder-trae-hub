import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useVirtualizer } from '@tanstack/react-virtual';
import { AlertTriangle, CalendarCheck, Coins, Info, Play, RefreshCw, RotateCcw, Search, Trash2, Upload, Users, Zap } from 'lucide-react';
import {
  Badge, Button, Card, Dialog, DialogContent, EmptyState, Input, SkeletonRows, Switch,
  TableWrap, Td, Th, Tr, useToast,
} from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { ImportDialog } from './accounts-import';
import { AccountDetail } from './accounts-detail';
import { ENGINES, ENGINE_LABELS, checkinAccounts, engineTasks, deleteAccount, engineAccounts, refreshAccounts, runTask, setAccount, setAllAccounts, testAccount, triggerScheduler, type Account, type AccountsPayload, type Engine, type Realm } from '@/lib/api';

const VIRTUAL_THRESHOLD = 40;

function uidOf(a: Account): string {
  return String(a.uid || a.id || "");
}

function nameOf(a: Account): string {
  return String(a.nickname || a.email || uidOf(a).slice(0, 8) || "—");
}

/** 今天是否已签到：wb/qd 账号带 lastCheckin（"YYYY-MM-DD HH:mm:ss"）；trae 没有这个字段，返回 undefined。 */
function checkedToday(a: Account): boolean | undefined {
  const raw = (a as Record<string, unknown>).todayCheckedIn;
  if (typeof raw === "boolean") return raw;
  const last = String((a as Record<string, unknown>).lastCheckin || "").trim();
  if (!last) return undefined;
  const d = new Date();
  const today = d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  return last.startsWith(today);
}

/** 有界并发：一次最多 n 个在跑，结果按原顺序返回。 */
async function runPool<T, R>(items: T[], limit: number, worker: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let cursor = 0;
  const runners = new Array(Math.min(limit, items.length || 1)).fill(0).map(async function () {
    for (;;) {
      const i = cursor++;
      if (i >= items.length) return;
      out[i] = await worker(items[i]);
    }
  });
  await Promise.all(runners);
  return out;
}
export default function AccountsView() {
  const { t } = useLang();
  const toast = useToast();
  const qc = useQueryClient();

  const [engine, setEngine] = useState<Engine>("wb");
  const [realm, setRealm] = useState<Realm>("cn");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [toDelete, setToDelete] = useState<Account | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [detail, setDetail] = useState<Account | null>(null);

  const queryKey = ["accounts", engine, realm] as const;
  const list = useQuery({
    queryKey,
    queryFn: () => engineAccounts(engine, realm),
  });

  const rows = useMemo(() => {
    const all = (list.data?.accounts ?? []) as Account[];
    const kw = q.trim().toLowerCase();
    if (!kw) return all;
    return all.filter((a) => (nameOf(a) + " " + uidOf(a)).toLowerCase().includes(kw));
  }, [list.data, q]);

  /** 行级启停：乐观更新 + 失败回滚（用户看到的是"立刻生效"，出错再退回）。 */
  const toggle = useMutation({
    mutationFn: (a: Account) => setAccount(engine, uidOf(a), { enabled: !(a.enabled !== false) }),
    onMutate: async (a) => {
      setPending(uidOf(a));
      await qc.cancelQueries({ queryKey });
      const prev = qc.getQueryData(queryKey);
      qc.setQueryData(queryKey, (old: typeof list.data) => {
        if (!old?.accounts) return old;
        return {
          ...old,
          accounts: old.accounts.map((x) => (uidOf(x) === uidOf(a) ? { ...x, enabled: !(a.enabled !== false) } : x)),
        };
      });
      return { prev };
    },
    onError: (err, _a, ctx) => {
      if (ctx?.prev) qc.setQueryData(queryKey, ctx.prev);
      toast.push("err", t("common.saveFailed", { detail: (err as Error)?.message || "" }));
    },
    onSuccess: (_d, a) => {
      toast.push("ok", a.enabled !== false ? t("accounts.toast.disabled") : t("accounts.toast.enabled"));
    },
    onSettled: () => { setPending(null); void qc.invalidateQueries({ queryKey }); },
  });

  const remove = useMutation({
    mutationFn: (a: Account) => deleteAccount(engine, uidOf(a)),
    onSuccess: (res) => {
      if (res?.deleted === false) { toast.push("warn", t("accounts.toast.alreadyGone")); }
      else { toast.push("ok", t("accounts.toast.deleted")); }
      setToDelete(null);
      void qc.invalidateQueries({ queryKey });
    },
    onError: (err) => toast.push("err", t("common.saveFailed", { detail: (err as Error)?.message || "" })),
    onSettled: () => setPending(null),
  });

  const bulk = useMutation({
    mutationFn: (enabled: boolean) => setAllAccounts(engine, enabled),
    onSuccess: (_d, enabled) => {
      toast.push("ok", enabled ? t("accounts.toast.enabled") : t("accounts.toast.disabled"));
      void qc.invalidateQueries({ queryKey });
    },
    onError: (err) => toast.push("err", t("common.saveFailed", { detail: (err as Error)?.message || "" })),
  });

  /** 试一条真实请求：能立刻知道这把账号还有没有效 */
  const testOne = useMutation({
    mutationFn: (a: Account) => testAccount(engine, uidOf(a)),
    onSuccess: function (res, a) {
      if (res?.ok) toast.push("ok", nameOf(a) + " · " + String(res.model || "") + " · " + String(res.elapsed_ms ?? "?") + "ms");
      else toast.push("err", nameOf(a) + " · " + String(res?.error || t("common.error")));
    },
    onError: function (err) { toast.push("err", (err as Error).message); },
    onSettled: function () { setPending(null); },
  });

  /** 全部签到进度（浏览器里直接看到"还剩几个"） */
  const [checkinProgress, setCheckinProgress] = useState<{ done: number; total: number } | null>(null);

  // Baseline frozen when the page opens (or when the user resets it): used to show
  // whether an account credit went UP or DOWN since then.
  const creditsBaseline = useRef<Record<string, number>>({});
  useEffect(function () { creditsBaseline.current = {}; }, [engine, realm]);

  function resetCreditsBaseline() {
    const next: Record<string, number> = {};
    for (const item of (list.data?.accounts ?? [])) {
      const c = (item.credits || {}) as { remain?: number };
      const uid = uidOf(item);
      if (uid && typeof c.remain === "number") next[uid] = c.remain;
    }
    creditsBaseline.current = next;
    void list.refetch();
  }

  const checkin = useMutation({
    mutationFn: async function (a?: Account) {
      // 单个账号：直接签这一个（不分区域，引擎的 uid 分支不看 realm）
      if (a) {
        const res = await checkinAccounts(engine, uidOf(a));
        return { results: res?.results ?? [], label: nameOf(a) };
      }
      // ★ 全部签到：**跨引擎 + 跨区域**，一次点完全部签掉。
      //   以前只签当前标签页/当前区域的账号，所以用户得来回切板块一个一个点。
      //   现在先把三家两个区域的账号拉齐，跳过"今天已签"，再有界并发地签。
      const all: { eng: Engine; uid: string; name: string }[] = [];
      for (const eng of ENGINES) {
        // 两个区域都取（不是"all"——引擎的 realm 参数只认 cn/intl）
        const parts = await Promise.all([
          engineAccounts(eng, "cn").catch(function () { return { accounts: [] } as AccountsPayload; }),
          engineAccounts(eng, "intl").catch(function () { return { accounts: [] } as AccountsPayload; }),
        ]);
        const seen = new Set<string>();
        const merged = (parts[0].accounts ?? []).concat(parts[1].accounts ?? []);
        for (const item of merged) {
          const key0 = uidOf(item) || JSON.stringify(item).slice(0, 40);
          if (seen.has(key0)) continue;
          seen.add(key0);
          if (checkedToday(item) === true) continue;      // 今天签过的不再打一遍
          const uid = uidOf(item);
          if (!uid) continue;
          all.push({ eng: eng, uid: uid, name: nameOf(item) });
        }
      }
      if (all.length === 0) return { results: [], label: t("accounts.checkinAll") };
      toast.push("info", t("accounts.checkinRunning", { n: String(all.length) }));
      let done = 0;
      setCheckinProgress({ done: 0, total: all.length });
      const results = await runPool(all, 12, async function (one) {
        try {
          const res = await checkinAccounts(one.eng, one.uid);
          const row = (res?.results ?? [])[0] ?? {};
          done += 1; setCheckinProgress({ done: done, total: all.length });
          return { uid: one.uid, nickname: one.name, ok: row.ok === true, code: row.code, msg: String((row as Record<string, unknown>).msg ?? (row as Record<string, unknown>).error ?? "") };
        } catch (e) {
          done += 1; setCheckinProgress({ done: done, total: all.length });
          return { uid: one.uid, nickname: one.name, ok: false, code: 0, msg: (e as Error).message || "request failed" };
        }
      });
      return { results: results as { uid?: string; nickname?: string; ok?: boolean; code?: number; msg?: string }[], label: t("accounts.checkinAll") };
    },
    onSuccess: function (out) {
      setCheckinProgress(null);
      const rows = out?.results ?? [];
      const okN = rows.filter(function (r) { return r.ok; }).length;
      if (rows.length === 0) toast.push("warn", t("accounts.checkinNothing"));
      else {
        const failed = rows.filter(function (r) { return !r.ok; });
        const firstMsg = failed.length ? String(failed[0].msg || "").slice(0, 60) : "";
        toast.push(okN === rows.length ? "ok" : "warn",
          (out?.label ?? "") + " · " + okN + "/" + rows.length + (firstMsg ? " · " + firstMsg : ""));
      }
      void qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: function (err) { setCheckinProgress(null); toast.push("err", (err as Error).message); },
  });

  const refreshCredits = useMutation({
    mutationFn: async function () {
      // 先让引擎去上游拉一次用量（which 会回填 credits）；
      // 引擎没有这个任务时才退回刷新令牌/账号。
      try {
        await runTask(engine, "usage_refresh");
      } catch {
        await refreshAccounts(engine);
      }
    },
    onSuccess: function (res: unknown) {
      const doc = (res || {}) as { results?: { ok?: boolean }[]; logs?: string[] };
      const rows = doc.results ?? [];
      if (rows.length) {
        const okN = rows.filter(function (r) { return r.ok; }).length;
        toast.push(okN === rows.length ? "ok" : "warn", okN + "/" + rows.length);
      } else {
        toast.push("ok", String((doc.logs || [])[0] || t("common.done")));
      }
      void qc.invalidateQueries({ queryKey });
    },
    onError: function (err) { toast.push("err", (err as Error).message); },
  });

  const patrol = useMutation({
    mutationFn: function () { return triggerScheduler(engine); },
    onSuccess: function (res) { toast.push(res?.ok ? 'ok' : 'warn', String(res?.msg || t('common.done'))); },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  // trae 的积分/签到状态只在 /tasks 里（accounts 载荷没有 credits）。
  // 其它引擎也顺手取一次：失败就静默忽略，不影响账号列表。
  const tasksQuery = useQuery({
    queryKey: ["tasks", engine],
    queryFn: function () { return engineTasks(engine); },
    retry: 0,
    staleTime: 60_000,
  });
  /** 昵称 → 积分/tasks 里的签到时间 */
  const taskCredits = (() => {
    const map: Record<string, { remain?: number; size?: number }> = {};
    for (const acc of (tasksQuery.data?.accounts ?? [])) {
      const nick = String(acc.nickname || "").trim();
      if (nick && acc.credits) map[nick] = acc.credits as { remain?: number; size?: number };
    }
    return map;
  })();

  /** 按区域分组的 tasks 积分：昵称对不上时按"同区域第 N 个"对齐（trae 的昵称会被截断）。 */
  const taskCreditsByRealm: Record<string, { remain?: number; size?: number }[]> = {};
  for (const acc of (tasksQuery.data?.accounts ?? [])) {
    const realm = String(acc.realm || "");
    if (!realm || !acc.credits) continue;
    (taskCreditsByRealm[realm] = taskCreditsByRealm[realm] || []).push(acc.credits as { remain?: number; size?: number });
  }

  /** 昵称可能不完全一致（账号池是 "睡睡平安…0202"，tasks 是 "睡睡平安"）：先精确，再前缀。 */
  function creditsFromTasks(name: string): { remain?: number; size?: number } | undefined {
    if (!name) return undefined;
    if (taskCredits[name]) return taskCredits[name];
    const hit = Object.keys(taskCredits).find(function (k) {
      return k && (name.startsWith(k) || k.startsWith(name));
    });
    return hit ? taskCredits[hit] : undefined;
  }
  const checkinTask = (tasksQuery.data?.tasks ?? []).find(function (x: { task_code?: string }) { return String(x.task_code) === "daily_checkin"; });

  /** 列表正在取数（刷新按钮的 loading 用它） */
  const busy = list.isFetching;

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={t("accounts.title")}
        subtitle={t("accounts.desc")}
        actions={(
          <>
            <Button size="sm" variant="ghost" icon={<Play size={13} />} loading={patrol.isPending} onClick={function () { patrol.mutate(); }}>
              {t("rewards.qd.checkin").replace(/.*/, "立即巡检")}
            </Button>
            <Button size="sm" variant="primary" icon={<Upload size={13} />} onClick={function () { setImportOpen(true); }}>
              {t("accounts.import.title")}
            </Button>
            <Button size="sm" variant="ghost" icon={<CalendarCheck size={13} />} loading={checkin.isPending} onClick={function () { checkin.mutate(undefined); }}>
              {checkinProgress ? checkinProgress.done + "/" + checkinProgress.total : t("accounts.checkinAll")}
            </Button>
            <Button size="sm" variant="ghost" icon={<Coins size={13} />} loading={refreshCredits.isPending} onClick={function () { refreshCredits.mutate(); }}>
              {t("credit.refresh")}
            </Button>
            <Button size="sm" variant="ghost" icon={<RotateCcw size={13} />} onClick={resetCreditsBaseline} title={t("accounts.creditsBaselineReset")}>
              {t("accounts.creditsBaseline")}
            </Button>
            <Button size="sm" variant="ghost" loading={bulk.isPending} onClick={() => bulk.mutate(true)}>
              {t("common.enable")}
            </Button>
            <Button size="sm" variant="ghost" loading={bulk.isPending} onClick={() => bulk.mutate(false)}>
              {t("common.disable")}
            </Button>
            <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} loading={busy} onClick={() => void list.refetch()}>
              {t("common.refresh")}
            </Button>
          </>
        )}
      >
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div role="tablist" aria-label={t("engine.label")} className="flex gap-1 rounded-md border border-line p-0.5">
            {ENGINES.map((eng) => (
              <button
                key={eng}
                role="tab"
                aria-selected={engine === eng}
                onClick={() => setEngine(eng)}
                className={
                  "min-h-11 cursor-pointer rounded px-2.5 text-xs transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                  (engine === eng ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")
                }
              >
                {ENGINE_LABELS[eng]}
              </button>
            ))}
          </div>
          <div role="tablist" aria-label={t("accounts.col.realm")} className="flex gap-1 rounded-md border border-line p-0.5">
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
          <div className="relative ml-auto w-full sm:w-64">
            <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-mute" aria-hidden="true" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("accounts.searchPlaceholder")}
              className="h-11 pl-8 text-xs"
              aria-label={t("common.search")}
            />
          </div>
        </div>

        {list.isPending ? <SkeletonRows rows={6} cols={4} /> : null}

        {list.isError ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-sm text-err">{t("state.errTitle")}</p>
            <p className="text-xs text-fg-mute">{String((list.error as Error)?.message || "")}</p>
            <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} onClick={() => void list.refetch()}>
              {t("common.retry")}
            </Button>
          </div>
        ) : null}

        {!list.isPending && !list.isError && rows.length === 0 ? (
          <EmptyState
            icon={<Users size={20} />}
            title={q ? t("accounts.filtered.empty") : t("accounts.empty.title")}
            description={q ? t("accounts.filtered.emptyDesc") : t("accounts.empty.desc")}
            action={(
              <Button size="sm" variant="secondary" onClick={() => (q ? setQ("") : void list.refetch())}>
                {q ? t("accounts.filtered.reset") : t("common.refresh")}
              </Button>
            )}
          />
        ) : null}

        {!list.isPending && !list.isError && rows.length > 0 ? (
          <AccountTable
            rows={rows}
            pending={pending}
            onToggle={(a) => toggle.mutate(a)}
            onDelete={(a) => setToDelete(a)}
            onTest={(a) => { setPending(uidOf(a)); testOne.mutate(a); }}
            onDetail={(a) => setDetail(a)}
            onCheckin={(a) => checkin.mutate(a)}
            creditsBaseline={creditsBaseline.current}
            taskCredits={taskCredits}
            creditsFromTasks={creditsFromTasks}
            taskCreditsByRealm={taskCreditsByRealm}
            checkinLastRun={String(checkinTask?.last_run || "")}
            t={t}
          />
        ) : null}
      </Card>

      <Dialog open={!!toDelete} onOpenChange={(v) => (!v ? setToDelete(null) : null)}>
        {toDelete ? (
          <DialogContent
            title={t("accounts.delete.title")}
            description={t("accounts.delete.desc", { name: nameOf(toDelete) })}
            footer={(
              <>
                <Button variant="ghost" onClick={() => setToDelete(null)}>{t("common.cancel")}</Button>
                <Button
                  variant="danger"
                  loading={remove.isPending}
                  onClick={() => { setPending(uidOf(toDelete)); remove.mutate(toDelete); }}
                >
                  {t("common.delete")}
                </Button>
              </>
            )}
          >
            <p className="flex items-start gap-2 text-xs text-fg-mute">
              <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warn" aria-hidden="true" />
              {t("common.delete") + " · " + t("accounts.dirLabel")}
            </p>
          </DialogContent>
        ) : null}
      </Dialog>
      <AccountDetail account={detail} engine={engine} onClose={function () { setDetail(null); }} />
      <ImportDialog open={importOpen} onClose={function () { setImportOpen(false); }} engine={engine} />
    </div>
  );
}

function AccountTable(props: {
  rows: Account[];
  pending: string | null;
  onToggle: (a: Account) => void;
  onDelete: (a: Account) => void;
  onTest: (a: Account) => void;
  onCheckin: (a: Account) => void;
  creditsBaseline?: Record<string, number>;
  taskCredits?: Record<string, { remain?: number; size?: number }>;
  creditsFromTasks?: (name: string) => { remain?: number; size?: number } | undefined;
  taskCreditsByRealm?: Record<string, { remain?: number; size?: number }[]>;
  checkinLastRun?: string;
  onDetail: (a: Account) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const { rows, pending, onToggle, onDelete, onTest, onCheckin, onDetail, t, creditsBaseline, taskCredits, creditsFromTasks, taskCreditsByRealm, checkinLastRun } = props;
  // 同一区域内的序号（昵称被截断时用它对齐 tasks 的积分）
  const realmSeen: Record<string, number> = {};
  const indexInRealm = new Map<string, number>();
  for (const row of rows) {
    const key = uidOf(row) || JSON.stringify(row).slice(0, 24);
    const realm = String(row.realm || "");
    const n = realmSeen[realm] || 0;
    realmSeen[realm] = n + 1;
    indexInRealm.set(key, n);
  }
  function taskCreditFor(a: Account): { remain?: number; size?: number } | undefined {
    const byName = creditsFromTasks ? creditsFromTasks(nameOf(a)) : undefined;
    if (byName) return byName;
    const list = taskCreditsByRealm ? taskCreditsByRealm[String(a.realm || "")] : undefined;
    if (!list) return undefined;
    const idx = indexInRealm.get(uidOf(a) || "") || 0;
    return list[idx];
  }
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const virtual = rows.length > VIRTUAL_THRESHOLD;
  const v = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 44,
    overscan: 8,
    enabled: virtual,
    initialRect: { width: 800, height: 520 },
  });

  if (!virtual) {
    return (
      <TableWrap>
        <thead>
          <tr>
            <Th>{t("accounts.col.nickname")}</Th>
            <Th>{t("accounts.col.realm")}</Th>
            <Th className="min-w-[132px] text-right">{t("accounts.col.credits")}</Th>
            <Th>{t("common.enabled")}</Th>
            <Th className="text-right">{t("accounts.col.actions")}</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((a) => (
            <Row key={uidOf(a)} a={a} pending={pending} onToggle={onToggle} onDelete={onDelete} onTest={onTest} onCheckin={onCheckin} onDetail={onDetail} t={t}
              taskCredits={taskCredits} taskCredit={taskCreditFor(a)} creditsFromTasks={function () { return undefined; }} checkinLastRun={checkinLastRun} creditDelta={(() => {
                const c2 = (a.credits || {}) as { remain?: number };
                const uid2 = uidOf(a);
                if (!uid2 || typeof c2.remain !== "number" || !creditsBaseline) return undefined;
                const base2 = creditsBaseline[uid2];
                if (base2 === undefined) return undefined;
                return c2.remain - base2;
              })()} />
          ))}
        </tbody>
      </TableWrap>
    );
  }

  return (
    <div ref={scrollRef} className="max-h-[520px] overflow-auto rounded-md border border-line">
      <div style={{ height: v.getTotalSize(), position: "relative" }}>
        {v.getVirtualItems().map((item) => {
          const a = rows[item.index];
          return (
            <div
              key={uidOf(a)}
              style={{ position: "absolute", top: 0, left: 0, right: 0, transform: "translateY(" + item.start + "px)" }}
              className="flex items-center gap-3 border-b border-line-faint px-3 text-sm"
            >
              <span className="min-w-0 flex-1 truncate text-fg-dim">{nameOf(a)}</span>
              <span className="w-16 shrink-0 text-xs text-fg-mute">{String(a.realm || "—")}</span>
              <Switch
                checked={a.enabled !== false}
                disabled={pending === uidOf(a)}
                onCheckedChange={() => onToggle(a)}
                label={nameOf(a)}
              />
              <Button
                size="icon"
                variant="ghost"
                aria-label={t("common.delete")}
                onClick={() => onDelete(a)}
              >
                <Trash2 size={14} />
              </Button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Row(props: {
  a: Account;
  creditDelta?: number;
  taskCredit?: { remain?: number; size?: number };
  taskCredits?: Record<string, { remain?: number; size?: number }>;
  creditsFromTasks?: (name: string) => { remain?: number; size?: number } | undefined;
  checkinLastRun?: string;
  pending: string | null;
  onToggle: (a: Account) => void;
  onTest: (a: Account) => void;
  onCheckin: (a: Account) => void;
  onDelete: (a: Account) => void;
  onDetail: (a: Account) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const { a, pending, onToggle, onDelete, onTest, onCheckin, onDetail, t } = props;
  return (
    <Tr>
      <Td className="max-w-[220px]">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-fg">{nameOf(a)}</span>
          {(() => {
            let state = checkedToday(a);
            if (state === undefined && props.checkinLastRun) {
              const d0 = new Date();
              const today0 = d0.getFullYear() + "-" + String(d0.getMonth() + 1).padStart(2, "0") + "-" + String(d0.getDate()).padStart(2, "0");
              state = String(props.checkinLastRun).startsWith(today0);
            }
            if (state === undefined) return null;
            return (
              <Badge tone={state ? "ok" : "neutral"} className="shrink-0">
                {state ? t("accounts.checkedToday") : t("accounts.notCheckedToday")}
              </Badge>
            );
          })()}
        </span>
      </Td>
      <Td><Badge tone="neutral">{String(a.realm || "—")}</Badge></Td>
      <Td className="text-right">
        {(() => {
          // trae 没有 a.credits：积分在 /tasks 的 accounts[].credits 里，按昵称对齐
          const fromTasks = props.taskCredit;
          const c = ((a.credits || fromTasks || {}) as { remain?: number; used?: number; size?: number; remain_percent?: string });
          const quotaUnknown = (c as { quota_known?: boolean }).quota_known === false;
          const remain = typeof c.remain === 'number' && !quotaUnknown ? c.remain : undefined;
          if (remain === undefined) {
            return (
              <span className="text-[10.5px] text-fg-mute" title={t("accounts.creditsUnknownHint")}>
                {t("accounts.creditsUnknown")}
              </span>
            );
          }
          const pct = typeof c.size === 'number' && c.size > 0 ? Math.max(0, Math.min(100, (remain / c.size) * 100)) : 0;
          return (
            <span className="inline-flex flex-col items-end gap-0.5" title={t("accounts.creditsHint", { remain: Math.round(remain), used: Math.round(c.used || 0), size: Math.round(c.size || 0) })}>
              <span className="flex items-center justify-end gap-1 whitespace-nowrap text-[11.5px] tnum text-fg">
                {remain.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                {typeof props.creditDelta === "number" && Math.abs(props.creditDelta) >= 0.01 ? (
                  <span className={props.creditDelta > 0 ? "text-[10px] text-ok" : "text-[10px] text-err"}>
                    {props.creditDelta > 0 ? "\u25b2" : "\u25bc"}
                    {Math.abs(props.creditDelta).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </span>
                ) : null}
              </span>
              <span className="h-0.5 w-10 overflow-hidden rounded-full bg-surface-2">
                <span className="block h-full rounded-full" style={{ width: pct + '%', background: pct < 15 ? 'var(--warn)' : 'var(--ok)' }} />
              </span>
            </span>
          );
        })()}
      </Td>
      <Td>
        <Switch
          checked={a.enabled !== false}
          disabled={pending === uidOf(a)}
          onCheckedChange={() => onToggle(a)}
          label={nameOf(a)}
        />
      </Td>
      <Td className="text-right">
        <span className="row-actions inline-flex items-center gap-0.5">
          <Button size="icon" variant="ghost" aria-label={t("common.detail")} title={t("common.detail")} onClick={function () { onDetail(a); }}>
            <Info size={13} />
          </Button>
          <Button size="icon" variant="ghost" aria-label={t("common.detail")} title="测试" disabled={pending === uidOf(a)} onClick={() => onTest(a)}>
            <Zap size={13} />
          </Button>
          <Button size="icon" variant="ghost" aria-label={t("rewards.qd.checkin")} title={t("rewards.qd.checkin")} onClick={() => onCheckin(a)}>
            <CalendarCheck size={13} />
          </Button>
          <Button size="icon" variant="ghost" aria-label={t("common.delete")} onClick={() => onDelete(a)}>
            <Trash2 size={13} />
          </Button>
        </span>
      </Td>
    </Tr>
  );
}
