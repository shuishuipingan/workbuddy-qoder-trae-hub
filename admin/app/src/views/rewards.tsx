import { useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle, CalendarCheck, Coins, Copy, Flame, Gift, RefreshCw, Ticket,
} from 'lucide-react';
import {
  Badge, Button, Card, EmptyState, EngineBadge, SkeletonCard, SkeletonRows, useToast,
} from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { get, post } from '@/lib/http';
import { API } from '@/lib/api';

/**
 * 签到与福利：一条聚合端点 + 两套写动作。
 *
 * 读：GET /api/hub/rewards → { wb, qd, errors }
 *   aggregator 并发取两家 /tasks，**不可达不抛**：对应一侧为 null，errors.<eng> 给可读原因。
 *   归一字段（顶层）checkin_done / streak_days / claimable_credits 有就用，没有就回落本地计算。
 *
 * 写（聚合层只读，写仍走引擎路由 —— 令牌按路径里的引擎自动分槽，见 lib/http.engineOfPath）：
 *   POST /api/wb/tasks/run        { uid: "all" } → { ok, credit_added, logs[], accounts_count }
 *   POST /api/wb/tasks/travel     { uid: "all" } → { ok, results[], msg, accounts_count }
 *   POST /api/qd/accounts/checkin { uid: "all" } → { results[], accounts[] }
 *   POST /api/qd/tasks/run        { uid: "all" } → { ok, credit_added, logs[], accounts_count }
 *
 * ★ 两家互不影响：每一侧的数据错误、按钮 loading、失败说明都是各自的键，
 *   一家挂了另一张卡照常可用（这是这一页最要紧的性质）。
 */

const REWARDS_KEY = ['hub-rewards'] as const;

type Tone = 'neutral' | 'accent' | 'ok' | 'warn' | 'err' | 'info';
type EngineKey = 'wb' | 'qd';
type T = (k: string, vars?: Record<string, string | number>) => string;

type RewardTask = {
  task_code?: string;
  name?: string;
  description?: string;
  status?: string;
  current?: number;
  target?: number;
  reward_credit?: number;
  reward_energy?: number;
  reward_text?: string;
  code?: string;
};

type RewardSummary = {
  streak_days?: number;
  energy?: number;
  plan?: string;
  travel?: { state?: string } | string;
  codes?: { code?: string; campaign?: string }[];
  campaigns?: { items?: { name?: string; title?: string; key?: string }[] };
};

type RewardSide = {
  tasks?: RewardTask[];
  summary?: RewardSummary;
  account?: { uid?: string; nickname?: string } | null;
  accounts?: { uid?: string; nickname?: string }[];
  msg?: string;
  checkin_done?: boolean | null;
  streak_days?: number | null;
  claimable_credits?: number | null;
};

type RewardsPayload = {
  wb?: RewardSide | null;
  qd?: RewardSide | null;
  errors?: Record<string, string>;
};

type ActionSpec = {
  key: string;
  label: string;
  variant: 'primary' | 'secondary';
  run: () => Promise<unknown>;
};

type Notice = { engine: EngineKey; tone: 'warn' | 'err'; text: string };

/** 读数：undefined / '' 一律当「不知道」，**绝不回落到 0**（0 是"确实是零"）。 */
function numOrNull(v: unknown): number | null {
  if (v === undefined || v === null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

/** 任务状态 → 徽标。取值实测：completed(可领) / claimed(已领) / not_accepted(不可领) / in_progress。 */
function statusOf(task: RewardTask): { tone: Tone; key: string } {
  const s = String(task?.status ?? '').toLowerCase();
  if (s === 'claimed') return { tone: 'ok', key: 'rewards.status.claimed' };
  if (s === 'completed') return { tone: 'info', key: 'rewards.status.claimable' };
  if (s === 'in_progress' || s === 'running') return { tone: 'warn', key: 'rewards.status.doing' };
  if (s === 'not_accepted') return { tone: 'neutral', key: 'rewards.status.locked' };
  if (!s) return { tone: 'neutral', key: 'rewards.status.unknown' };
  return { tone: 'neutral', key: 'rewards.status.other' };
}

function progressOf(task: RewardTask): string {
  const target = numOrNull(task.target);
  if (target === null || target <= 1) return '';
  return fmtNum(numOrNull(task.current)) + ' / ' + fmtNum(target);
}

function rewardOf(task: RewardTask, t: T): string {
  if (typeof task.reward_text === 'string' && task.reward_text.trim()) return task.reward_text.trim();
  const credit = numOrNull(task.reward_credit);
  if (credit !== null && credit > 0) return t('rewards.creditUnit', { n: fmtNum(credit) });
  const energy = numOrNull(task.reward_energy);
  if (energy !== null && energy > 0) return t('rewards.energyUnit', { n: fmtNum(energy) });
  return '';
}

/** 可领统计：优先用聚合层归一字段 claimable_credits（口径含去重），条数按本地 status==='completed'。 */
function claimStats(side: RewardSide | null): { count: number | null; credit: number | null } {
  if (!side) return { count: null, credit: null };
  const norm = numOrNull(side.claimable_credits);
  if (!Array.isArray(side.tasks)) return { count: null, credit: norm };
  let count = 0;
  let credit = 0;
  for (const task of side.tasks) {
    if (String(task?.status ?? '').toLowerCase() === 'completed') {
      count += 1;
      credit += numOrNull(task?.reward_credit) ?? 0;
    }
  }
  return { count, credit: norm !== null ? norm : credit };
}

function streakOf(side: RewardSide | null): number | null {
  const top = numOrNull(side?.streak_days);
  if (top !== null) return top;
  return numOrNull(side?.summary?.streak_days);
}

/** travel 的取值只在词条里这五个；未知状态一律归到 unknown，避免拼出缺词条的键。 */
const TRAVEL_STATES = ['arrived', 'idle', 'traveling', 'done', 'unknown'] as const;

function travelOf(side: RewardSide | null): string {
  const raw = side?.summary?.travel;
  const state = typeof raw === 'string' ? raw : (raw && typeof raw === 'object' ? raw.state : '');
  const s = String(state ?? '').toLowerCase();
  return (TRAVEL_STATES as readonly string[]).includes(s) ? s : 'unknown';
}

/** 写回包归一：两家形状不同，只取共性（credit_added 才是正确字段名 —— 旧名求和会静默变 0）。 */
function resultOf(res: unknown): { ok: boolean; credit: number | null; msg: string } {
  if (!res || typeof res !== 'object') return { ok: true, credit: null, msg: '' };
  const r = res as { ok?: unknown; credit_added?: unknown; msg?: unknown; logs?: unknown };
  let msg = '';
  if (typeof r.msg === 'string' && r.msg.trim()) msg = r.msg.trim();
  else if (Array.isArray(r.logs) && r.logs.length) {
    const last = r.logs[r.logs.length - 1];
    if (typeof last === 'string') msg = last;
  }
  return { ok: r.ok !== false, credit: numOrNull(r.credit_added), msg: msg.slice(0, 240) };
}

export default function RewardsView() {
  const { t } = useLang();
  const toast = useToast();
  const qc = useQueryClient();

  const query = useQuery({ queryKey: REWARDS_KEY, queryFn: () => get<RewardsPayload>(API + '/hub/rewards') });

  /** 在飞的动作键（按按钮而不是按页）：wb 跑着时 qd 照点，反之亦然。 */
  const inflight = useRef<Set<string>>(new Set());
  const [pending, setPending] = useState<string[]>([]);
  const [notice, setNotice] = useState<Notice | null>(null);

  const data = (query.data ?? {}) as RewardsPayload;
  const wb = data.wb ?? null;
  const qd = data.qd ?? null;

  function errorText(err: unknown): string {
    if (!err) return '';
    const e = err as { message?: string; kind?: string; status?: number };
    if (e.kind === 'unauthorized' || e.status === 401) return t('state.loginTitle');
    return e.message || t('common.failed');
  }

  const topError = query.isError ? errorText(query.error) : '';
  const wbError = data.errors?.wb ? String(data.errors.wb) : topError;
  const qdError = data.errors?.qd ? String(data.errors.qd) : topError;

  const wbClaim = claimStats(wb);
  const qdClaim = claimStats(qd);
  const wbStreak = streakOf(wb);
  const qdStreak = streakOf(qd);

  async function runAction(key: string, engine: EngineKey, run: () => Promise<unknown>) {
    if (inflight.current.has(key)) return;
    inflight.current.add(key);
    setPending(Array.from(inflight.current));
    setNotice((prev) => (prev && prev.engine === engine ? null : prev));
    try {
      const res = await run();
      const sum = resultOf(res);
      if (sum.ok) {
        const text = sum.credit !== null && sum.credit > 0
          ? t('rewards.toast.got', { n: fmtNum(sum.credit) })
          : (sum.msg || t('common.done'));
        toast.push('ok', text);
      } else {
        const text = sum.msg || t('common.failed');
        toast.push('warn', text);
        setNotice({ engine, tone: 'warn', text });
      }
      void qc.invalidateQueries({ queryKey: REWARDS_KEY });
    } catch (err) {
      const text = errorText(err);
      toast.push('err', t('rewards.state.failed') + ' · ' + text);
      setNotice({ engine, tone: 'err', text });
    } finally {
      inflight.current.delete(key);
      setPending(Array.from(inflight.current));
    }
  }

  const wbActions: ActionSpec[] = [
    { key: 'wbRun', label: t('rewards.wb.run'), variant: 'primary', run: () => post(API + '/wb/tasks/run', { uid: 'all' }) },
    { key: 'wbTravel', label: t('rewards.wb.travel'), variant: 'secondary', run: () => post(API + '/wb/tasks/travel', { uid: 'all' }) },
  ];
  const qdActions: ActionSpec[] = [
    { key: 'qdCheckin', label: t('rewards.qd.checkin'), variant: 'primary', run: () => post(API + '/qd/accounts/checkin', { uid: 'all' }) },
    { key: 'qdClaimAll', label: t('rewards.qd.claimAll'), variant: 'secondary', run: () => post(API + '/qd/tasks/run', { uid: 'all' }) },
  ];

  const retry = () => { void query.refetch(); };
  const updatedAt = query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toLocaleTimeString() : '';

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm text-fg-dim mix-space">{t('rewards.desc')}</p>
          {updatedAt && !query.isPending ? (
            <p className="mt-0.5 truncate text-[11px] text-fg-mute tnum">
              {t('rewards.lastRefreshed', { time: updatedAt })}
            </p>
          ) : null}
        </div>
        <Button
          variant="secondary"
          size="sm"
          icon={<RefreshCw size={14} />}
          loading={query.isFetching}
          onClick={retry}
        >
          {t('rewards.refresh')}
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {query.isPending ? (
          [0, 1, 2, 3].map((i) => <SkeletonCard key={i} />)
        ) : (
          <>
            <Stat
              stat="wbClaimable"
              label={t('rewards.top.wbClaimable')}
              value={fmtNum(wbClaim.count)}
              hint={wbClaim.credit ? t('rewards.top.credit', { n: fmtNum(wbClaim.credit) }) : undefined}
              icon={<Gift size={16} />}
            />
            <Stat
              stat="qdClaimable"
              label={t('rewards.top.qdClaimable')}
              value={fmtNum(qdClaim.count)}
              hint={qdClaim.credit ? t('rewards.top.credit', { n: fmtNum(qdClaim.credit) }) : undefined}
              icon={<Coins size={16} />}
            />
            <Stat stat="wbStreak" label={t('rewards.top.wbStreak')} value={fmtNum(wbStreak)} icon={<Flame size={16} />} />
            <Stat stat="qdStreak" label={t('rewards.top.qdStreak')} value={fmtNum(qdStreak)} icon={<CalendarCheck size={16} />} />
          </>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <EngineCard
          engine="wb"
          short={t('rewards.wb.short')}
          title={t('rewards.wb.title')}
          desc={t('rewards.wb.desc')}
          payload={wb}
          error={wbError}
          loading={query.isPending}
          pending={pending}
          notice={notice && notice.engine === 'wb' ? notice : null}
          actions={wbActions}
          onAction={(key, engine, run) => void runAction(key, engine, run)}
          onRetry={retry}
          t={t}
        />
        <EngineCard
          engine="qd"
          short={t('rewards.qd.short')}
          title={t('rewards.qd.title')}
          desc={t('rewards.qd.desc')}
          payload={qd}
          error={qdError}
          loading={query.isPending}
          pending={pending}
          notice={notice && notice.engine === 'qd' ? notice : null}
          actions={qdActions}
          onAction={(key, engine, run) => void runAction(key, engine, run)}
          onRetry={retry}
          t={t}
        />
      </div>
    </div>
  );
}

/** 顶部小结的一格：取不到时 value 就是「—」，不留 0 误导。 */
function Stat(props: { stat: string; label: string; value: string; hint?: string; icon: ReactNode }) {
  const { stat, label, value, hint, icon } = props;
  return (
    <div className="rounded-lg border border-line bg-surface p-4 shadow-1">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-xs text-fg-mute">{label}</p>
        <span className="shrink-0 text-fg-mute" aria-hidden="true">{icon}</span>
      </div>
      <p data-stat={stat} className="mt-2 text-2xl font-semibold tracking-tight tnum text-fg">{value}</p>
      {hint ? <p className="mt-1 truncate text-[11px] text-fg-mute tnum">{hint}</p> : null}
    </div>
  );
}

function EngineCard(props: {
  engine: EngineKey;
  short: string;
  title: string;
  desc: string;
  payload: RewardSide | null;
  error: string;
  loading: boolean;
  pending: string[];
  notice: Notice | null;
  actions: ActionSpec[];
  onAction: (key: string, engine: EngineKey, run: () => Promise<unknown>) => void;
  onRetry: () => void;
  t: T;
}) {
  const { engine, short, title, desc, payload, error, loading, pending, notice, actions, onAction, onRetry, t } = props;
  const toast = useToast();

  const summary = payload?.summary ?? {};
  const tasks = Array.isArray(payload?.tasks) ? payload.tasks : [];
  const accounts = Array.isArray(payload?.accounts) ? payload.accounts : [];
  const upstreamMsg = typeof payload?.msg === 'string' ? payload.msg : '';

  const codes = Array.from(new Set(
    (Array.isArray(summary.codes) ? summary.codes : [])
      .map((c) => (c && typeof c === 'object' && typeof c.code === 'string' ? c.code : ''))
      .filter((c) => c.length > 0),
  ));

  const campaignItems = summary.campaigns?.items;
  const campaigns = (Array.isArray(campaignItems) ? campaignItems : [])
    .map((c) => String(c?.name || c?.title || c?.key || ''))
    .filter((c) => c.length > 0);

  const done = payload?.checkin_done;
  const checkinValue: ReactNode = done === true
    ? <Badge tone="ok">{t('rewards.top.checkinDone')}</Badge>
    : done === false
      ? <Badge tone="warn">{t('rewards.top.checkinTodo')}</Badge>
      : <span className="text-fg-mute">—</span>;

  const kvs: { label: string; value: ReactNode }[] = [
    { label: t('rewards.field.streak'), value: <span className="tnum">{fmtNum(numOrNull(summary.streak_days))}</span> },
    { label: t('rewards.field.energy'), value: <span className="tnum">{fmtNum(numOrNull(summary.energy))}</span> },
    { label: t('rewards.field.travel'), value: <span>{t('rewards.travel.' + travelOf(payload))}</span> },
    { label: t('rewards.field.plan'), value: <span>{typeof summary.plan === 'string' && summary.plan ? summary.plan : '—'}</span> },
    { label: t('rewards.field.accounts'), value: <span className="tnum">{fmtNum(accounts.length)}</span> },
    {
      label: t(engine === 'wb' ? 'rewards.top.wbCheckin' : 'rewards.top.qdCheckin'),
      value: checkinValue,
    },
  ];

  const copy = async (code: string) => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('no clipboard');
      await navigator.clipboard.writeText(code);
      toast.push('ok', t('rewards.codes.copy') + ' · ' + code);
    } catch {
      toast.push('warn', code);
    }
  };

  const emptyNode = (
    <EmptyState
      className="mt-3"
      icon={engine === 'wb' ? <Gift size={20} /> : <Ticket size={20} />}
      title={t('rewards.state.empty')}
      description={upstreamMsg || t(engine === 'qd' ? 'rewards.state.emptyDescQd' : 'rewards.state.emptyDescWb')}
      action={(
        <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} onClick={onRetry}>
          {t('common.retry')}
        </Button>
      )}
    />
  );

  return (
    <Card title={title} subtitle={desc} actions={<EngineBadge engine={engine} label={short} />}>
      <div className="flex flex-wrap gap-2">
        {actions.map((a) => (
          <Button
            key={a.key}
            size="sm"
            variant={a.variant}
            loading={pending.includes(a.key)}
            onClick={() => onAction(a.key, engine, a.run)}
          >
            {a.label}
          </Button>
        ))}
      </div>

      {notice ? (
        <p
          role="alert"
          className={
            'mt-2 flex items-start gap-1.5 text-xs ' + (notice.tone === 'err' ? 'text-err' : 'text-warn')
          }
        >
          <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words">{notice.text}</span>
        </p>
      ) : null}

      {/* 有数据但这一侧仍报了错（部分可用）：留一行提示，别把已经拿到的内容丢掉 */}
      {error && payload ? (
        <p role="status" className="mt-2 flex items-start gap-1.5 text-[11px] text-warn">
          <AlertTriangle size={12} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 break-words mix-space">{error}</span>
        </p>
      ) : null}

      {loading && !payload ? (
        <div className="mt-3">
          <SkeletonRows rows={4} cols={2} />
        </div>
      ) : error && !payload ? (
        <div className="mt-3 flex flex-col items-start gap-2 rounded-md border border-err/40 bg-err-soft p-3">
          <p className="flex items-center gap-1.5 text-sm font-medium text-err">
            <AlertTriangle size={14} aria-hidden="true" />
            {t('rewards.state.failed')}
          </p>
          <p className="break-words text-xs text-fg-mute mix-space">{error}</p>
          <Button size="sm" variant="secondary" icon={<RefreshCw size={14} />} onClick={onRetry}>
            {t('common.retry')}
          </Button>
        </div>
      ) : payload ? (
        <>
          <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
            {kvs.map((item) => (
              <div key={item.label} className="min-w-0">
                <dt className="truncate text-[11px] text-fg-mute">{item.label}</dt>
                <dd className="mt-0.5 min-w-0 truncate text-sm text-fg">{item.value}</dd>
              </div>
            ))}
          </dl>

          {codes.length ? (
            <div className="mt-3">
              <p className="text-[11px] text-fg-mute">{t('rewards.codes.title')}</p>
              <ul className="mt-1 flex flex-wrap gap-1.5">
                {codes.map((code) => (
                  <li key={code}>
                    <button
                      type="button"
                      onClick={() => void copy(code)}
                      title={t('rewards.codes.copy')}
                      aria-label={t('rewards.codes.copy') + ' ' + code}
                      className="inline-flex cursor-pointer items-center gap-1 rounded-full border border-line bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-fg-dim transition-colors hover:border-line-strong hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent active:translate-y-px disabled:cursor-not-allowed disabled:opacity-55"
                    >
                      <Copy size={11} aria-hidden="true" />
                      <span className="truncate">{code}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {campaigns.length ? (
            <p className="mt-3 truncate text-[11px] text-fg-mute mix-space">
              {t('rewards.field.campaigns') + ': ' + campaigns.join(' · ')}
            </p>
          ) : null}

          {tasks.length ? (
            <ul className="mt-3 flex flex-col divide-y divide-line-faint border-t border-line-faint">
              {tasks.map((task, i) => {
                const st = statusOf(task);
                const progress = progressOf(task);
                const reward = rewardOf(task, t);
                return (
                  <li key={(task.task_code || task.name || 'task') + ':' + i} className="flex items-start gap-2 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-xs font-medium text-fg">{task.name || task.task_code || '—'}</p>
                      {task.description ? (
                        <p className="mt-0.5 truncate text-[11px] text-fg-mute">{task.description}</p>
                      ) : null}
                    </div>
                    {progress ? <span className="tnum shrink-0 text-[11px] text-fg-mute">{progress}</span> : null}
                    {reward ? <span className="tnum shrink-0 text-[11px] text-fg-dim">{reward}</span> : null}
                    <Badge tone={st.tone}>{t(st.key)}</Badge>
                  </li>
                );
              })}
            </ul>
          ) : emptyNode}
        </>
      ) : emptyNode}
    </Card>
  );
}
