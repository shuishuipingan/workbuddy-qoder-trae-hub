import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Play, RefreshCw } from 'lucide-react';
import { Badge, Button, Card, Field, Input, Switch, useToast } from '@/components/ui';
import { API, engineSchedulerToggle, engineSchedulerTrigger, saveQdSchedule, saveWbSchedule, engineSettings } from '@/lib/api';
import { get } from '@/lib/http';

/**
 * 签到排程（Rewards 页顶部卡片）。
 *
 * 读：GET /api/hub/scheduler —— 门户已聚合三家引擎的调度状态（互不影响）。
 * 写：
 *   POST /api/qd/scheduler/config   { checkin_times:"09:00,21:00", keepalive_times, cron }
 *   POST /api/wb/settings           { schedule: { checkin:[9,21], travel, keepalive, cat } }
 *   POST /api/{engine}/scheduler/toggle | /trigger
 *
 * 两个引擎的粒度不同，**不能做成同一个输入框**：
 *   qd 是 `HH:MM` 逗号分隔（分钟级，还能填 5 段 cron）；
 *   wb 只认整点小时数组（0-23），填 09:30 会被后端拒绝或回落到默认。
 */

type T = (k: string, vars?: Record<string, string | number>) => string;

type SchedInfo = {
  enabled?: boolean;
  mode?: string;
  last_run_time?: string;
  next_run_time?: string;
  logs?: string[];
  checkin_times?: string;
  keepalive_times?: string;
  cron?: string;
  hours?: number[];
  daily_chat?: { times?: string[]; random?: boolean };
};

type SchedDoc = { engines?: Record<string, SchedInfo> };
type WbSchedule = { checkin?: number[]; travel?: number[]; keepalive?: number[]; cat?: number[] };

function hoursToText(value?: number[]): string {
  return (value ?? []).join(',');
}

/** 整点数组：只接受 0-23 的整数，其它一律丢弃（后端也是这么洗的）。 */
function parseHours(text: string): number[] {
  const out: number[] = [];
  for (const part of String(text || '').split(/[,\s]+/)) {
    const n = Number(part);
    if (!part || !Number.isInteger(n) || n < 0 || n > 23) continue;
    if (!out.includes(n)) out.push(n);
  }
  return out.sort((a, b) => a - b);
}

function Row(props: {
  engine: string;
  label: string;
  info: SchedInfo;
  enabled: boolean;
  busy: boolean;
  onToggle: (next: boolean) => void;
  onRun: () => void;
  t: T;
  children?: React.ReactNode;
}) {
  const { info, t } = props;
  const lastLog = (info.logs ?? []).slice(-1)[0] || '';
  return (
    <div className="rounded-lg border border-line bg-surface-2/40 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold text-fg">{props.label}</span>
        <Badge tone={props.enabled ? 'ok' : 'neutral'}>{props.enabled ? t('sched.on') : t('sched.off')}</Badge>
        <Switch
          checked={props.enabled}
          disabled={props.busy}
          onCheckedChange={props.onToggle}
          label={t('sched.enabledAria')}
        />
        <span className="ml-auto flex items-center gap-3 text-[11px] text-fg-mute tnum">
          <span>{t('sched.last')} {info.last_run_time || '—'}</span>
          <span>{t('sched.next')} {info.next_run_time || '—'}</span>
        </span>
        <Button
          size="sm"
          variant="secondary"
          icon={<Play size={13} />}
          loading={props.busy}
          onClick={props.onRun}
        >
          {t('sched.run')}
        </Button>
      </div>
      {props.children}
      {lastLog ? (
        <p className="mt-2 truncate text-[11px] text-fg-mute" title={lastLog}>{lastLog}</p>
      ) : null}
    </div>
  );
}

export function SchedulerCard({ t }: { t: T }) {
  const qc = useQueryClient();
  const toast = useToast();
  const sched = useQuery({
    queryKey: ['hub-scheduler'],
    queryFn: () => get<SchedDoc>(API + '/hub/scheduler'),
    staleTime: 15_000,
  });
  const wbSettings = useQuery({
    queryKey: ['wb-schedule'],
    queryFn: () => engineSettings('wb'),
    staleTime: 30_000,
  });

  const engines = sched.data?.engines ?? {};
  const qd = engines.qd ?? {};
  const wb = engines.wb ?? {};
  const trae = engines.trae ?? {};
  const wbSchedule = ((wbSettings.data as { schedule?: WbSchedule } | undefined)?.schedule) ?? {};

  const [qdCheckin, setQdCheckin] = useState('');
  const [qdKeepalive, setQdKeepalive] = useState('');
  const [qdCron, setQdCron] = useState('');
  const [wbCheckin, setWbCheckin] = useState('');
  const [wbTravel, setWbTravel] = useState('');
  const [wbKeepalive, setWbKeepalive] = useState('');
  const [wbCat, setWbCat] = useState('');
  const [busy, setBusy] = useState('');

  // 服务端数据到达（或保存后刷新）时同步草稿；用户正在编辑时不会被覆盖，
  // 因为只有这三个值真的变了才会重跑。
  useEffect(() => {
    setQdCheckin(String(qd.checkin_times ?? ''));
    setQdKeepalive(String(qd.keepalive_times ?? ''));
    setQdCron(String(qd.cron ?? ''));
  }, [qd.checkin_times, qd.keepalive_times, qd.cron]);

  useEffect(() => {
    setWbCheckin(hoursToText(wbSchedule.checkin));
    setWbTravel(hoursToText(wbSchedule.travel));
    setWbKeepalive(hoursToText(wbSchedule.keepalive));
    setWbCat(hoursToText(wbSchedule.cat));
  }, [wbSchedule.checkin, wbSchedule.travel, wbSchedule.keepalive, wbSchedule.cat]);

  async function refreshAll() {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['hub-scheduler'] }),
      qc.invalidateQueries({ queryKey: ['wb-schedule'] }),
    ]);
  }

  async function toggle(engine: string, enabled: boolean) {
    setBusy(engine + ':toggle');
    try {
      await engineSchedulerToggle(engine as 'wb' | 'qd' | 'trae', enabled);
      await refreshAll();
    } catch (err) {
      toast.push('err', (err as Error)?.message || t('sched.failed'));
    } finally {
      setBusy('');
    }
  }

  async function run(engine: string) {
    setBusy(engine + ':run');
    try {
      await engineSchedulerTrigger(engine as 'wb' | 'qd' | 'trae');
      toast.push('ok', t('sched.ran'));
      await refreshAll();
    } catch (err) {
      toast.push('err', (err as Error)?.message || t('sched.failed'));
    } finally {
      setBusy('');
    }
  }

  async function saveQd() {
    if (!qdCheckin.trim() && !qdCron.trim()) {
      toast.push('err', t('sched.needTime'));
      return;
    }
    setBusy('qd:save');
    try {
      await saveQdSchedule({
        checkin_times: qdCheckin.trim(),
        keepalive_times: qdKeepalive.trim(),
        cron: qdCron.trim(),
      });
      toast.push('ok', t('sched.saved'));
      await refreshAll();
    } catch (err) {
      toast.push('err', (err as Error)?.message || t('sched.failed'));
    } finally {
      setBusy('');
    }
  }

  async function saveWb() {
    const checkin = parseHours(wbCheckin);
    if (!checkin.length) {
      toast.push('err', t('sched.needHour'));
      return;
    }
    setBusy('wb:save');
    try {
      await saveWbSchedule({
        checkin,
        travel: parseHours(wbTravel),
        keepalive: parseHours(wbKeepalive),
        cat: parseHours(wbCat),
      });
      toast.push('ok', t('sched.saved'));
      await refreshAll();
    } catch (err) {
      toast.push('err', (err as Error)?.message || t('sched.failed'));
    } finally {
      setBusy('');
    }
  }

  const loading = sched.isPending;
  const chat = wb.daily_chat;

  return (
    <Card
      title={t('sched.title')}
      subtitle={t('sched.subtitle')}
      actions={
        <Button
          size="sm"
          variant="ghost"
          icon={<RefreshCw size={13} />}
          loading={sched.isFetching}
          onClick={() => void refreshAll()}
        >
          {t('common.retry')}
        </Button>
      }
    >
      {loading ? (
        <p className="py-3 text-xs text-fg-mute">{t('app.loading')}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <Row
            engine="qd"
            label="Qoder"
            info={qd}
            enabled={qd.enabled !== false}
            busy={busy.startsWith('qd')}
            onToggle={(next) => void toggle('qd', next)}
            onRun={() => void run('qd')}
            t={t}
          >
            <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
              <Field label={t('sched.checkin')} hint={t('sched.hintQd')}>
                {(p) => (
                  <Input {...p} value={qdCheckin} placeholder="09:00,21:00" onChange={(e) => setQdCheckin(e.target.value)} />
                )}
              </Field>
              <Field label={t('sched.keepalive')} hint={t('sched.hintQd')}>
                {(p) => (
                  <Input {...p} value={qdKeepalive} placeholder="22:00" onChange={(e) => setQdKeepalive(e.target.value)} />
                )}
              </Field>
              <Field label={t('sched.cron')} hint={t('sched.hintCron')}>
                {(p) => (
                  <Input {...p} value={qdCron} placeholder="0 9,21 * * *" onChange={(e) => setQdCron(e.target.value)} />
                )}
              </Field>
            </div>
            <div className="mt-2">
              <Button size="sm" variant="primary" loading={busy === 'qd:save'} onClick={() => void saveQd()}>
                {t('sched.save')}
              </Button>
            </div>
          </Row>

          <Row
            engine="wb"
            label="WorkBuddy"
            info={{ ...wb, next_run_time: wb.next_run_time || trae.next_run_time }}
            enabled={wb.enabled !== false}
            busy={busy.startsWith('wb')}
            onToggle={(next) => void toggle('wb', next)}
            onRun={() => void run('wb')}
            t={t}
          >
            <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Field label={t('sched.checkin')} hint={t('sched.hintWb')}>
                {(p) => (
                  <Input {...p} value={wbCheckin} placeholder="9,21" onChange={(e) => setWbCheckin(e.target.value)} />
                )}
              </Field>
              <Field label={t('sched.travel')} hint={t('sched.hintWb')}>
                {(p) => (
                  <Input {...p} value={wbTravel} placeholder="9,21" onChange={(e) => setWbTravel(e.target.value)} />
                )}
              </Field>
              <Field label={t('sched.keepalive')} hint={t('sched.hintWb')}>
                {(p) => (
                  <Input {...p} value={wbKeepalive} placeholder="22" onChange={(e) => setWbKeepalive(e.target.value)} />
                )}
              </Field>
              <Field label={t('sched.cat')} hint={t('sched.hintWb')}>
                {(p) => (
                  <Input {...p} value={wbCat} placeholder="1" onChange={(e) => setWbCat(e.target.value)} />
                )}
              </Field>
            </div>
            <div className="mt-2 flex items-center gap-3">
              <Button size="sm" variant="primary" loading={busy === 'wb:save'} onClick={() => void saveWb()}>
                {t('sched.save')}
              </Button>
              {chat?.times?.length ? (
                <span className="text-[11px] text-fg-mute tnum">
                  {t('sched.dailyChat')} {chat.times.join(' / ')}
                  {chat.random ? ' ' + t('sched.random') : ''}
                </span>
              ) : null}
            </div>
          </Row>

          <Row
            engine="trae"
            label="Trae"
            info={trae}
            enabled={trae.enabled !== false}
            busy={busy.startsWith('trae')}
            onToggle={(next) => void toggle('trae', next)}
            onRun={() => void run('trae')}
            t={t}
          >
            <p className="mt-2 text-[11px] text-fg-mute">{t('sched.fixed')}</p>
          </Row>
        </div>
      )}
    </Card>
  );
}

export default SchedulerCard;
