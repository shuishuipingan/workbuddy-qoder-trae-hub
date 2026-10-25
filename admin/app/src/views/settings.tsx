import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Copy, KeyRound, Plus, RefreshCw, Save, Sparkles, Trash2 } from 'lucide-react';
import {
  Badge, Button, Card, EngineBadge, Field, Input, SkeletonRows, Switch, TableWrap, Td, Th, Tr, useToast,
} from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { settingLabel } from '@/lib/setting-labels';
import { UpdateCard } from '@/components/app/update';
import {
  ENGINE_LABELS, ENGINES, changePanelPassword, getPanelPassword, engineSchedulerToggle, engineSettings, hubKeysDetail, hubSaveMerged,
  hubScheduler, hubSelfcheck, pricingRefresh, pricingState, proxyDiscover, proxySlotTest, proxySlots, proxySlotsSave,
  probeKey, revealEngineKey, revealMergedKey, saveSettings,
  type ApiKey, type Engine, type SettingsPayload,
} from '@/lib/api';
const DEFAULT_MINUTES = 5;

/** 四护栏 + 到期窗口：即使后端没回 limits，也把这几行摆出来（值给「—」）。 */
const KNOWN_LIMITS = [
  "reserve_credits",
  "daily_token_limit",
  "daily_credit_limit",
  "model_daily_token_limit",
  "expiring_window_days",
] as const;

const LIMIT_REALMS = ["global", "intl", "cn"] as const;

type TFn = (k: string, vars?: Record<string, string | number>) => string;
type LimitCell = { realm: string; value: number | null };
type LimitRow = { key: string; cells: LimitCell[] };

function readNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function minutesOf(s: SettingsPayload | undefined): number {
  const n = readNumber(s?.pricing_refresh_minutes);
  return n !== null && n >= 0 ? Math.round(n) : DEFAULT_MINUTES;
}

/** 草稿 → 提交值：只接受「非负整数」，其它一律 null（按钮随之禁用）。 */
function parseDraft(v: string): number | null {
  const raw = v.trim();
  if (!raw) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0 || !Number.isInteger(n)) return null;
  return n;
}

function fmtNum(n: number): string {
  return n.toLocaleString("en-US");
}

/** 护栏键名是后端配置里的原始标识；能给中文注解的就给（悬停可见），不新造词条。 */
function limitHint(key: string): string | undefined {
  switch (key) {
    case "reserve_credits": return "overview.pool.whyReserve";
    case "daily_token_limit": return "accounts.kindDaily";
    case "daily_credit_limit": return "accounts.kindCredit";
    case "expiring_window_days": return "accounts.credits.expiry";
    default: return undefined;
  }
}

function realmText(realm: unknown, t: TFn): string {
  const r = String(realm ?? "");
  if (!r) return t("common.all");
  if (r === "cn") return t("realm.cn.short");
  if (r === "intl") return t("realm.intl.short");
  if (r === "global") return t("realm.all");
  return r;
}

/** limits 既可能是 {key:{global,cn,intl}}（实测），也可能是扁平的 {key:number}；两种都摊平成行。 */
function parseLimits(raw: unknown): LimitRow[] {
  const src = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const present = Object.keys(src);
  const ordered = [...KNOWN_LIMITS.filter((k) => !present.includes(k)), ...present];
  return ordered.map((key) => {
    const v = src[key];
    if (v && typeof v === "object") {
      const bag = v as Record<string, unknown>;
      const cells = LIMIT_REALMS
        .filter((r) => r in bag)
        .map((r) => ({ realm: r as string, value: readNumber(bag[r]) }));
      return { key, cells: cells.length ? cells : [{ realm: "global", value: null }] };
    }
    return { key, cells: [{ realm: "global", value: readNumber(v) }] };
  });
}

/** 限额字段名 → 词条；没有词条时回退原始键名（不显示 settings.limits.xxx）。 */
function limitLabel(t: (k: string) => string, key: string): string {
  const id = "settings.limits." + key;
  const val = t(id);
  return val === id ? key : val;
}
export default function SettingsView() {
  const { t } = useLang();
  const toast = useToast();
  const qc = useQueryClient();

  const [engine, setEngine] = useState<Engine>("wb");
  const [draft, setDraft] = useState(String(DEFAULT_MINUTES));
  const [fieldError, setFieldError] = useState<string | null>(null);

  const queryKey = ["settings", engine] as const;
  const settings = useQuery({ queryKey, queryFn: () => engineSettings(engine) });

  const saved = minutesOf(settings.data);

  // 切引擎 / 重新取数后把草稿对齐到服务端值；用户正在输入时 baseline 不变，不会被覆盖。
  useEffect(() => {
    setDraft(String(saved));
    setFieldError(null);
  }, [saved, engine]);

  const save = useMutation({
    mutationFn: (minutes: number) => saveSettings(engine, { pricing_refresh_minutes: minutes }),
    onSuccess: (res, minutes) => {
      setFieldError(null);
      // 立即把新值写进缓存（后端没回整份 settings 时也不至于把输入框弹回旧值），随后再拉一次真值。
      qc.setQueryData(queryKey, (old: SettingsPayload | undefined): SettingsPayload => ({
        ...(old ?? {}),
        ...(res?.settings ?? {}),
        pricing_refresh_minutes: minutes,
      }));
      toast.push("ok", t("common.saved"));
      void qc.invalidateQueries({ queryKey });
    },
    onError: (err) => {
      // 就地报错（Field 的 error），不靠 toast：用户的眼睛在输入框上。
      setFieldError(t("common.saveFailed", { detail: (err as Error)?.message || t("common.unknown") }));
    },
  });


  const parsed = parseDraft(draft);
  const canSave = parsed !== null && parsed !== saved && !save.isPending;

  const authError = settings.error as (Error & { status?: number }) | null;
  const unauthorized = authError?.status === 401;

  const limits = parseLimits(settings.data?.limits);
  const isDefaultPwd = settings.data?.panel_password_is_default === true;
  const authRequired = settings.data?.auth_required === true;
  const masked = String(settings.data?.api_key_masked ?? "");

  const submit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSave || parsed === null) return;
    save.mutate(parsed);
  };

  return (
    <div className="flex flex-col gap-4">
      <Card
        title={t("settings.title")}
        subtitle={t("settings.desc")}
        actions={(
          <>
            <EngineBadge engine={engine} label={ENGINE_LABELS[engine]} />
            <Button
              size="sm"
              variant="secondary"
              icon={<RefreshCw size={14} />}
              loading={settings.isFetching}
              onClick={() => void settings.refetch()}
            >
              {t("common.refresh")}
            </Button>
          </>
        )}
      >
        <div
          role="tablist"
          aria-label={t("engine.label")}
          className="mb-3 flex w-fit gap-1 rounded-md border border-line p-0.5"
        >
          {ENGINES.map((eng) => (
            <button
              key={eng}
              role="tab"
              aria-selected={engine === eng}
              onClick={() => setEngine(eng)}
              className={
                "min-h-11 cursor-pointer rounded px-2.5 text-xs transition-[background-color,color,transform] duration-150 active:translate-y-px focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent " +
                (engine === eng ? "bg-accent-soft font-medium text-accent" : "text-fg-dim hover:bg-surface-2")
              }
            >
              {ENGINE_LABELS[eng]}
            </button>
          ))}
        </div>

        {settings.isPending ? <SkeletonRows rows={6} cols={3} /> : null}

        {settings.isError ? (
          <div className="flex flex-col items-start gap-2">
            <p className="text-sm text-err">{unauthorized ? t("state.loginTitle") : t("state.errTitle")}</p>
            <p className="max-w-xl text-xs text-fg-mute mix-space">
              {unauthorized ? t("error.unauthorized") : t("state.errDesc")}
            </p>
            {!unauthorized && authError?.message ? (
              <p className="max-w-xl truncate font-mono text-[11px] text-fg-mute" title={String(authError.message)}>
                {String(authError.message)}
              </p>
            ) : null}
            <Button
              size="sm"
              variant="secondary"
              icon={<RefreshCw size={14} />}
              loading={settings.isFetching}
              onClick={() => void settings.refetch()}
            >
              {t("common.retry")}
            </Button>
          </div>
        ) : null}

        {settings.data ? (
          <>
            {isDefaultPwd ? (
              <div
                role="status"
                className="mb-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-warn"
              >
                <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span className="min-w-0 mix-space">{t("auth.defaultHint")}</span>
              </div>
            ) : null}

            <dl className="grid gap-2 sm:grid-cols-3">
              <div className="flex flex-col gap-1 rounded-md border border-line bg-surface-2/40 px-3 py-2">
                <dt className="text-[11px] text-fg-mute">{t("auth.password") + " · " + t("engine.default")}</dt>
                <dd className="flex min-w-0 items-center gap-2 text-sm">
                  <Badge tone={isDefaultPwd ? "warn" : "ok"}>
                    {isDefaultPwd ? t("common.yes") : t("common.no")}
                  </Badge>
                </dd>
              </div>

              <div className="flex flex-col gap-1 rounded-md border border-line bg-surface-2/40 px-3 py-2">
                <dt className="text-[11px] text-fg-mute">{t("overview.engine.apiKeyRequired")}</dt>
                <dd className="flex min-w-0 items-center gap-2 text-sm">
                  <Badge tone={authRequired ? "ok" : "warn"}>
                    {authRequired ? t("common.yes") : t("common.no")}
                  </Badge>
                </dd>
              </div>

              <div className="flex flex-col gap-1 rounded-md border border-line bg-surface-2/40 px-3 py-2">
                <dt className="text-[11px] text-fg-mute">{t("col.key")}</dt>
                <dd className="min-w-0 truncate font-mono text-[11px] text-fg-dim" title={masked || t("common.dash")}>
                  {masked || t("common.dash")}
                </dd>
              </div>
            </dl>
          </>
        ) : null}
      </Card>

      {settings.isPending ? (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title={t("settings.protocolTitle")}><SkeletonRows rows={3} cols={4} /></Card>
          <Card title={t("accounts.col.quota")}><SkeletonRows rows={4} cols={3} /></Card>
        </div>
      ) : null}

      {settings.data ? (
        <>
          <div className="grid gap-4 lg:grid-cols-2">

            <Card title={t("accounts.col.quota")} subtitle={t("settings.limitsHint")}>
              {limits.length === 0 ? (
                <p className="py-6 text-center text-[11.5px] text-fg-mute">{t("common.dash")}</p>
              ) : (
                <ul className="flex flex-col gap-2.5">
                  {limits.map((row) => {
                    const vals = row.cells.map((c) => (c.value === null ? 0 : c.value));
                    const max = Math.max(1, ...vals);
                    return (
                      <li key={row.key}>
                        <div className="mb-1 flex items-baseline justify-between gap-2">
                          <span className="truncate text-[11.5px] text-fg-dim" title={limitHint(row.key) ? t(limitHint(row.key)!) : row.key}>
                            {limitLabel(t, row.key)}
                          </span>
                          <span className="shrink-0 text-[11px] tnum text-fg-mute">
                            {row.cells[0]?.value == null ? t("common.dash") : fmtNum(row.cells[0].value)}
                          </span>
                        </div>
                        <div className="flex flex-col gap-1">
                          {(row.cells.some((c) => c.value !== null) ? row.cells.filter((c) => c.value !== null) : [row.cells[0]]).map((c) => (
                            <div key={c.realm} className="flex items-center gap-2">
                              <span className="w-10 shrink-0 text-[10px] text-fg-mute">{realmText(c.realm, t)}</span>
                              <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2">
                                <span
                                  className="block h-full rounded-full"
                                  style={{
                                    width: c.value === null ? "0%" : Math.max(2, (c.value / max) * 100) + "%",
                                    background: c.value === null ? "transparent" : "var(--accent)",
                                  }}
                                />
                              </span>
                              <span className="w-12 shrink-0 text-right text-[10.5px] tnum text-fg-dim">
                                {c.value === null ? t("common.dash") : fmtNum(c.value)}
                              </span>
                            </div>
                          ))}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Card>
          </div>

          <Card title={t("app.config")}>
            <form className="flex flex-col gap-3" onSubmit={submit} noValidate>
              <Field
                className="w-full sm:w-64"
                label={t("settings.field.pricingRefresh")}
                hint={t("common.minutes")}
                error={fieldError}
                required
              >
                {(p) => (
                  <Input
                    {...p}
                    invalid={p.invalid || parsed === null}
                    type="number"
                    min={0}
                    step={1}
                    inputMode="numeric"
                    autoComplete="off"
                    spellCheck={false}
                    value={draft}
                    onChange={(e) => {
                      setDraft(e.target.value);
                      setFieldError(null);
                    }}
                    className="h-11 tnum text-xs"
                  />
                )}
              </Field>
              <div>
                <Button
                  type="submit"
                  size="sm"
                  variant="primary"
                  icon={<Save size={14} />}
                  loading={save.isPending}
                  loadingText={t("common.saving")}
                  disabled={!canSave}
                >
                  {t("common.save")}
                </Button>
              </div>
            </form>
          </Card>
        </>
      ) : null}
        <UpdateCard t={t} />
        <AdvancedSettingsCard engine={engine} settings={settings.data} t={t} />
        <PasswordCard t={t} />
        <PricingCard t={t} />
        <ProxyCard t={t} />
        <HubPanels t={t} only="rest" />
    </div>
  );
}

/**
 * 引擎 Key：粘贴即加、一键生成、逐条删除。
 *
 * 之前这里是只读表 —— 用户的原话是「添加方法太麻烦了，而且没法单独加三个 key」。
 * 引擎侧的写入口是 POST /settings/save：
 *   · api_keys 里 **key 留空 = 保留原值**（面板只显示掩码，不可能回传明文）
 *   · 带上 deleted_api_key_ids 就是 upsert：只退役点名的 id，
 *     不会因为列表不全而误删（引擎 wb_proxy.py:19145-19158 的契约）
 */
export function EngineKeyCard({ engine, rows, t }: { engine: Engine; rows: ApiKey[]; t: TFn }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [value, setValue] = useState('');
  const [name, setName] = useState('');
  /** 刚生成的明文：落库后再也拿不回来（面板只存掩码），所以在这里显示一次 */
  const [created, setCreated] = useState<string>('');
  /** 正在取回明文的行 id（掩码不能复制，必须回引擎要一次） */
  const [revealing, setRevealing] = useState<string>('');
  /** 取回后的明文：既复制到剪贴板，也显示在卡片上（浏览器拒绝写剪贴板时还能手选） */
  const [revealed, setRevealed] = useState<{ id: string; key: string } | null>(null);

  async function copyKey(id: string) {
    setRevealing(id);
    try {
      const res = await revealEngineKey(engine, id);
      const key = String(res?.key || '');
      if (!key) throw new Error(t('settings.keyRevealFailed'));
      setRevealed({ id: id, key: key });
      try {
        await navigator.clipboard.writeText(key);
        toast.push('ok', t('common.copied'));
      } catch {
        // 剪贴板被浏览器拒绝（没有用户手势 / 无权限）：明文已经显示出来，手选即可
        toast.push('warn', t('settings.keyCopyManual'));
      }
    } catch (e) {
      toast.push('err', (e as Error).message || t('settings.keyRevealFailed'));
    } finally {
      setRevealing('');
    }
  }

  const keep: { id: string; name: string }[] = rows
    .filter(function (r): r is ApiKey & { id: string } {
      return typeof r.id === 'string' && r.id.length > 0;
    })
    .map(function (r) { return { id: r.id, name: r.name ?? '' }; });

  const save = useMutation({
    mutationFn: function (arg: { api_keys: Record<string, unknown>[]; deleted_api_key_ids: string[] }) {
      return saveSettings(engine, arg);
    },
    onSuccess: function () {
      toast.push('ok', t('common.saved'));
      setValue('');
      setName('');
      void qc.invalidateQueries({ queryKey: ['settings', engine] });
    },
    onError: function (e) { toast.push('err', (e as Error).message); },
  });

  function add(explicit?: string) {
    const key = (explicit ?? value).trim();
    if (!key) return;
    const next: Record<string, unknown>[] = keep.map(function (r) {
      // 明文不留空 = 引擎会覆盖掉原值，所以老条目只回传 id（key 留空即"保持"）
      return { id: r.id, name: r.name };
    });
    next.push({ key: key, name: name.trim() || undefined });
    save.mutate({ api_keys: next, deleted_api_key_ids: [] });
  }

  function remove(id: string) {
    save.mutate({
      api_keys: keep.filter(function (r) { return r.id !== id; })
        .map(function (r) { return { id: r.id, name: r.name }; }),
      deleted_api_key_ids: [id],
    });
  }

  function randomKey(prefix: string) {
    // 与上游看板一致：前缀 + 32 位十六进制（crypto 随机，不用 Math.random）
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    const hex = Array.from(bytes).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    return prefix + hex;
  }

  function generate() {
    setValue(randomKey('sk-'));
  }

  /** 一键：生成 → 直接落库 → 把明文留在卡片上（只此一次） */
  function generateAndAdd() {
    const key = randomKey('sk-');
    setValue(key);
    setCreated(key);
    add(key);
  }

  return (
    <div className="flex flex-col gap-2">
      {rows.length ? (
        <TableWrap>
          <thead>
            <tr>
              <Th>{t('common.name')}</Th>
              <Th>{t('col.key')}</Th>
              <Th>{t('col.realm')}</Th>
              <Th>{t('common.status')}</Th>
              <Th className="text-right">{t('accounts.col.actions')}</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map(function (k, i) {
              const enabled = k.enabled !== false;
              return (
                <Tr key={String(k.id ?? k.masked ?? k.name ?? i)}>
                  <Td className="max-w-[160px] truncate text-fg">{String(k.name || t('common.unnamed'))}</Td>
                  <Td className="max-w-[220px] truncate font-mono text-[11px]">{String(k.masked || t('common.dash'))}</Td>
                  <Td><Badge tone="neutral">{realmText(k.realm, t)}</Badge></Td>
                  <Td>
                    <Badge tone={enabled ? 'ok' : 'neutral'}>
                      {enabled ? t('common.enabled') : t('common.disabled')}
                    </Badge>
                  </Td>
                  <Td className="text-right">
                    <span className="row-actions inline-flex items-center gap-0.5">
                      {k.id ? (
                        <Button size="icon" variant="ghost" aria-label={t('common.copy')} title={t('common.copy')}
                          loading={revealing === k.id}
                          onClick={function () { void copyKey(String(k.id)); }}>
                          <Copy size={13} />
                        </Button>
                      ) : null}
                      {k.id ? (
                        <Button size="icon" variant="ghost" aria-label={t('common.delete')} title={t('common.delete')}
                          onClick={function () { remove(String(k.id)); }}>
                          <Trash2 size={13} />
                        </Button>
                      ) : null}
                    </span>
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </TableWrap>
      ) : (
        <p className="py-3 text-center text-[11.5px] text-fg-mute">{t('settings.noKeys')}</p>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <Field label={t('col.key')} className="min-w-[240px] flex-1">
          {(p) => (
            <Input {...p} value={value} onChange={function (e) { setValue(e.target.value); }}
              placeholder="sk-…" onKeyDown={function (e) { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
          )}
        </Field>
        <Field label={t('common.name')} className="w-36">
          {(p) => <Input {...p} value={name} onChange={function (e) { setName(e.target.value); }} placeholder={t('settings.keyNameHint')} />}
        </Field>
        <Button variant="secondary" onClick={generate} icon={<KeyRound size={13} />}>{t('settings.keyGenerate')}</Button>
        <Button variant="secondary" loading={save.isPending} onClick={generateAndAdd} icon={<Sparkles size={13} />}>{t('settings.keyGenerateAndAdd')}</Button>
        <Button variant="primary" loading={save.isPending} disabled={!value.trim()} onClick={function () { add(); }}>{t('settings.keyAdd')}</Button>
      </div>
      {revealed ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-2">
          <span className="text-[11px] text-fg-mute">{t('settings.keyRevealed')}</span>
          <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg">{revealed.key}</code>
          <Button size="sm" variant="secondary" icon={<Copy size={12} />}
            onClick={function () { void navigator.clipboard.writeText(revealed.key).then(function () { toast.push('ok', t('common.copied')); }).catch(function () { toast.push('warn', t('settings.keyCopyManual')); }); }}>
            {t('common.copy')}
          </Button>
          <Button size="sm" variant="ghost" onClick={function () { setRevealed(null); }}>{t('common.close')}</Button>
        </div>
      ) : null}
      {created ? (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-ok/40 bg-ok-soft px-2.5 py-2">
          <span className="text-[11px] text-fg">{t('settings.keyCreatedHint')}</span>
          <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg">{created}</code>
          <Button size="sm" variant="secondary" icon={<Copy size={12} />}
            onClick={function () { void navigator.clipboard.writeText(created); toast.push('ok', t('common.copied')); }}>
            {t('common.copy')}
          </Button>
          <Button size="sm" variant="ghost" onClick={function () { setCreated(''); }}>{t('common.close')}</Button>
        </div>
      ) : null}
      <p className="text-[10.5px] text-fg-mute">{t('settings.keyAddHint')}</p>
    </div>
  );
}


/**
 * 网关级的三块：合并 Key（增删）、自检、调度器。
 * 之前只做了只读列表，等于把「能用」砍成了「能看」。
 */
export function HubPanels({ t, only }: { t: (k: string, v?: Record<string, string | number>) => string; only?: "keys" | "rest" }) {
  const qc = useQueryClient();
  const toast = useToast();
  const keys = useQuery({ queryKey: ['hub-keys-detail'], queryFn: hubKeysDetail });
  const selfcheck = useQuery({ queryKey: ['hub-selfcheck'], queryFn: hubSelfcheck, enabled: false, retry: 0 });
  const scheduler = useQuery({ queryKey: ['hub-scheduler'], queryFn: hubScheduler });

  const [name, setName] = useState('');
  const [secret, setSecret] = useState('');
  // 作用域：留空 = 不限制（对齐 Agent2API 的 per-key 限制思路）
  const [scopeModels, setScopeModels] = useState('');
  const [scopeEngines, setScopeEngines] = useState('');
  const [confirmId, setConfirmId] = useState<string | null>(null);
  /** 刷新页面后内存口令会丢：保存前如果为空，就地要一次（比直接报错友好） */
  const [pw, setPw] = useState('');
  const [revealedMerged, setRevealedMerged] = useState<{ id: string; key: string } | null>(null);
  /** 每把合并 key 的实测结果：ok / bad，未测则无键 */
  const [probe, setProbe] = useState<Record<string, { ok: boolean; status: number; detail: string }>>({});

  async function testMerged(id: string) {
    const password = needPw ? pw : getPanelPassword();
    if (!password) { toast.push('warn', t('settings.keyRevealNeedPw')); return; }
    try {
      const res = await revealMergedKey(id, password);
      const key = String(res?.key || '');
      if (!key) throw new Error(t('settings.keyRevealFailed'));
      const out = await probeKey(key);
      setProbe(function (old) { return { ...old, [id]: out }; });
      toast.push(out.ok ? 'ok' : 'err', out.ok ? t('settings.keyProbeOk') : (t('settings.keyProbeBad', { status: out.status }) + (out.detail ? ' · ' + out.detail.slice(0, 60) : '')));
    } catch (e) {
      toast.push('err', (e as Error).message || t('settings.keyRevealFailed'));
    }
  }
  const needPw = !getPanelPassword();

  const merged = keys.data?.merged ?? [];

  /** 合并 Key 的明文只在融合层存着：取回要面板口令（和保存同一套授权） */
  async function copyMerged(id: string) {
    const password = needPw ? pw : getPanelPassword();
    if (!password) { toast.push('warn', t('settings.keyRevealNeedPw')); return; }
    try {
      const res = await revealMergedKey(id, password);
      const key = String(res?.key || '');
      if (!key) throw new Error(t('settings.keyRevealFailed'));
      setRevealedMerged({ id: id, key: key });
      try {
        await navigator.clipboard.writeText(key);
        toast.push('ok', t('common.copied'));
      } catch {
        toast.push('warn', t('settings.keyCopyManual'));
      }
    } catch (e) {
      toast.push('err', (e as Error).message || t('settings.keyRevealFailed'));
    }
  }

  const save = useMutation({
    mutationFn: function (next: typeof merged) { return hubSaveMerged(next, needPw ? pw : undefined); },
    onSuccess: function () {
      toast.push('ok', t('common.saved'));
      setName(''); setSecret(''); setConfirmId(null);
      void qc.invalidateQueries({ queryKey: ['hub-keys-detail'] });
      void qc.invalidateQueries({ queryKey: ['hub-keys'] });
    },
    onError: function (err) { toast.push('err', t('common.saveFailed', { detail: (err as Error).message })); },
  });

  const toggle = useMutation({
    mutationFn: function (v: { eng: string; enabled: boolean }) {
      return engineSchedulerToggle(v.eng as never, v.enabled);
    },
    onSuccess: function () { void qc.invalidateQueries({ queryKey: ['hub-scheduler'] }); },
    onError: function (err) { toast.push('err', t('common.saveFailed', { detail: (err as Error).message })); },
  });

  function addKey() {
    const n = name.trim();
    const k = secret.trim();
    if (!n || !k) return;
    const models = scopeModels.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    const engines = scopeEngines.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    const entry: Record<string, unknown> = { name: n, key: k };
    if (models.length) entry.models = models;
    if (engines.length) entry.engines = engines;
    save.mutate(merged.concat([entry as never]));
  }

  return (
    <>
      {only === "rest" ? null : (
      <Card
        title={t("health.mergedKeys", { n: merged.length })}
        subtitle={t("health.mergedKeyHint")}
        actions={<Badge tone="neutral">{t("common.count", { n: merged.length })}</Badge>}
      >
        {keys.isError ? (
          <div className="mb-3 flex items-center gap-2 rounded-md border border-err/30 bg-err-soft px-2.5 py-2 text-[11.5px] text-err">
            <span className="min-w-0 flex-1 truncate">{String((keys.error as Error)?.message || t("state.errTitle"))}</span>
            <Button size="sm" variant="secondary" onClick={function () { void keys.refetch(); }}>{t("common.retry")}</Button>
          </div>
        ) : null}

        {revealedMerged ? (
          <div className="mb-2 flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-2">
            <span className="text-[11px] text-fg-mute">{t('settings.keyRevealed')}</span>
            <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg">{revealedMerged.key}</code>
            <Button size="sm" variant="secondary" icon={<Copy size={12} />}
              onClick={function () { void navigator.clipboard.writeText(revealedMerged.key).then(function () { toast.push('ok', t('common.copied')); }).catch(function () { toast.push('warn', t('settings.keyCopyManual')); }); }}>
              {t('common.copy')}
            </Button>
            <Button size="sm" variant="ghost" onClick={function () { setRevealedMerged(null); }}>{t('common.close')}</Button>
          </div>
        ) : null}

        {merged.length > 0 ? (
          <ul className="mb-3 flex flex-col">
            {merged.map(function (k, i) {
              const id = String(k.id ?? i);
              return (
                <li key={id} className="flex items-center gap-2 border-b border-line-faint py-1.5 last:border-0">
                  <span className="min-w-0 flex-1 truncate text-[12px] text-fg">{String(k.name || t("common.unnamed"))}</span>
                  <span className="shrink-0 font-mono text-[11px] text-fg-mute">{String(k.key || t("common.dash"))}</span>
                  {Array.isArray((k as Record<string, unknown>).models) && ((k as Record<string, unknown>).models as unknown[]).length ? (
                    <Badge tone="info">{"models:" + ((k as Record<string, unknown>).models as unknown[]).length}</Badge>
                  ) : null}
                  {Array.isArray((k as Record<string, unknown>).engines) && ((k as Record<string, unknown>).engines as unknown[]).length ? (
                    <Badge tone="accent">{((k as Record<string, unknown>).engines as unknown[]).join("/")}</Badge>
                  ) : null}
                  {probe[id] ? (
                    <Badge tone={probe[id].ok ? 'ok' : 'err'}>{probe[id].ok ? 'OK' : String(probe[id].status || 'ERR')}</Badge>
                  ) : null}
                  <Button size="sm" variant="ghost" onClick={function () { void testMerged(id); }}>{t('settings.keyProbe')}</Button>
                  <Button size="icon" variant="ghost" aria-label={t("common.copy")} title={t("common.copy")}
                    onClick={function () { void copyMerged(id); }}>
                    <Copy size={12} />
                  </Button>
                  {confirmId === id ? (
                    <Button size="sm" variant="danger" loading={save.isPending} onClick={function () { save.mutate(merged.filter(function (_, j) { return j !== i; })); }}>
                      {t("common.confirm")}
                    </Button>
                  ) : (
                    <Button size="sm" variant="ghost" icon={<Trash2 size={12} />} onClick={function () { setConfirmId(id); }}>
                      {t("common.delete")}
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        ) : null}

        <div className="flex flex-wrap items-end gap-2">
          <Field label={t("common.name")} className="w-40">
            {(p) => <Input {...p} value={name} onChange={function (e) { setName(e.target.value); }} placeholder="merged-1" />}
          </Field>
          <Field label={t("col.key")} className="min-w-[220px] flex-1">
            {(p) => <Input {...p} value={secret} onChange={function (e) { setSecret(e.target.value); }} placeholder="sk-..." type="password" autoComplete="off" />}
          </Field>
          <Button
            variant="secondary"
            icon={<Sparkles size={13} />}
            onClick={function () {
              const bytes = new Uint8Array(16);
              crypto.getRandomValues(bytes);
              const hex = Array.from(bytes).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
              setSecret('mrg-' + hex);
              if (!name.trim()) setName('merged-' + String(Date.now()).slice(-4));
            }}
          >
            {t('settings.keyGenerate')}
          </Button>
          <Field label={t("settings.scope.models")} className="min-w-[180px] flex-1">
            {(p) => <Input {...p} value={scopeModels} onChange={function (e) { setScopeModels(e.target.value); }} placeholder={t("settings.scope.modelsPh")} />}
          </Field>
          <Field label={t("settings.scope.engines")} className="w-36">
            {(p) => <Input {...p} value={scopeEngines} onChange={function (e) { setScopeEngines(e.target.value); }} placeholder="wb,qd" />}
          </Field>

          <Button
            variant="primary"
            loading={save.isPending}
            disabled={!name.trim() || !secret.trim()}
            onClick={addKey}
            icon={<Plus size={13} />}
          >
            {t("common.add")}
          </Button>
        </div>
        {needPw ? (
          <div className="mt-2 max-w-[240px]">
            <Field label={t("auth.password")} hint={t("settings.keyRevealHint")}>
              {(p) => <Input {...p} type="password" autoComplete="current-password" value={pw} onChange={function (e) { setPw(e.target.value); }} />}
            </Field>
          </div>
        ) : (
          <p className="mt-2 text-[11px] text-fg-mute">{t("settings.keyRevealHint")}</p>
        )}
      </Card>
      )}
      {only === "keys" ? null : (
        <>

      <Card
        title={t("health.title")}
        subtitle={t("settings.selfcheckHint")}
        actions={(
          <Button size="sm" variant="secondary" loading={selfcheck.isFetching} onClick={function () { void selfcheck.refetch(); }}>
            {t("common.refresh")}
          </Button>
        )}
      >
        {selfcheck.data?.checks?.length ? (
          <ul className="flex flex-col gap-1">
            {selfcheck.data.checks.map(function (c) {
              return (
                <li key={c.id} className="flex items-start gap-2 text-[11.5px]">
                  <span className={c.ok ? "text-ok" : "text-err"} aria-hidden="true">{c.ok ? "✓" : "✗"}</span>
                  <span className="min-w-0 flex-1 text-fg-dim">{c.label}</span>
                  {c.detail ? <span className="max-w-[40%] truncate font-mono text-[10.5px] text-fg-mute">{c.detail}</span> : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="py-4 text-center text-[11.5px] text-fg-mute">{t("common.none")}</p>
        )}
      </Card>

      <Card title={t("settings.scheduler")}>
        {scheduler.isError ? (
          <div className="mb-2 flex items-center gap-2 rounded-md border border-err/30 bg-err-soft px-2.5 py-2 text-[11.5px] text-err">
            <span className="min-w-0 flex-1 truncate">{String((scheduler.error as Error)?.message || t("state.errTitle"))}</span>
            <Button size="sm" variant="secondary" onClick={function () { void scheduler.refetch(); }}>{t("common.retry")}</Button>
          </div>
        ) : null}

        <ul className="flex flex-col gap-2">
          {ENGINES.map(function (eng) {
            const st = scheduler.data?.engines?.[eng];
            const on = st?.enabled !== false;
            return (
              <li key={eng} className="flex items-center gap-3">
                <EngineBadge engine={eng} label={ENGINE_LABELS[eng]} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate text-[11px] text-fg-mute">{st?.mode_cn || st?.mode || "—"}</span>
                <Switch
                  checked={on}
                  disabled={toggle.isPending}
                  onCheckedChange={function (v: boolean) { toggle.mutate({ eng: eng, enabled: v }); }}
                  label={ENGINE_LABELS[eng]}
                />
              </li>
            );
          })}
        </ul>
      </Card>
        </>
      )}
    </>
  );
}

/** 改面板密码：成功后服务端会轮换令牌，api 层负责把新令牌写回，否则当前会话立刻失效。 */
function PasswordCard({ t }: { t: (k: string, v?: Record<string, string | number>) => string }) {
  const [cur, setCur] = useState('');
  const [next, setNext] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();
  const save = useMutation({
    mutationFn: function () { return changePanelPassword("wb", cur, next); },
    onSuccess: function () {
      toast.push('ok', t('common.saved'));
      setCur(''); setNext(''); setErr(null);
    },
    onError: function (e) { setErr((e as Error).message); },
  });
  return (
    <Card title={t("auth.password")} subtitle={t("settings.limitsHint")}>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={function (e) { e.preventDefault(); if (!cur || !next) return; save.mutate(); }}
        noValidate
      >
        <Field label={t("auth.password")} className="w-44">
          {(p) => <Input {...p} type="password" autoComplete="current-password" value={cur} onChange={function (e) { setCur(e.target.value); }} />}
        </Field>
        <Field label={t("common.new")} error={err} className="w-44">
          {(p) => <Input {...p} type="password" autoComplete="new-password" value={next} onChange={function (e) { setNext(e.target.value); }} invalid={!!err} />}
        </Field>
        <Button type="submit" variant="primary" loading={save.isPending} disabled={!cur || !next} icon={<Save size={13} />}>
          {t("common.save")}
        </Button>
      </form>
    </Card>
  );
}

/** 定价刷新：展示当前状态 + 手动触发一次。 */
function PricingCard({ t }: { t: (k: string, v?: Record<string, string | number>) => string }) {
  const toast = useToast();
  const q = useQuery({ queryKey: ['pricing'], queryFn: function () { return pricingState('wb'); } });
  const run = useMutation({
    mutationFn: function () { return pricingRefresh('wb'); },
    onSuccess: function () { toast.push('ok', t('common.done')); void q.refetch(); },
    onError: function (e) { toast.push('err', (e as Error).message); },
  });
  const d = q.data;
  return (
    <Card
      title={t("common.cost")}
      subtitle={t("settings.limitsHint")}
      actions={(
        <Button size="sm" variant="secondary" loading={run.isPending} onClick={function () { run.mutate(); }}>
          {t("overview.actions.creditsRefresh")}
        </Button>
      )}
    >
      {q.isError ? (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-err/30 bg-err-soft px-2.5 py-2 text-[11.5px] text-err">
          <span className="min-w-0 flex-1 truncate">{String((q.error as Error)?.message || t("state.errTitle"))}</span>
          <Button size="sm" variant="secondary" onClick={function () { void q.refetch(); }}>{t("common.retry")}</Button>
        </div>
      ) : null}
      <div className="grid gap-2 sm:grid-cols-4">
        <Row label={t("common.duration")}>{String(d?.interval_minutes ?? "—")} min</Row>
        <Row label={t("app.status")}>{d ? (d.running ? t("common.ok") : t("common.disabled")) : "—"}</Row>
        <Row label={t("common.enabled")}>{d ? (d.enabled !== false ? t("common.yes") : t("common.no")) : "—"}</Row>
        <Row label={t("settings.lastRun")}>{String(d?.last_run_label ?? "—")}</Row>
      </div>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="rounded-md border border-line-faint px-2.5 py-1.5">
      <p className="text-[10px] uppercase tracking-wide text-fg-mute">{label}</p>
      <p className="truncate text-[12px] text-fg">{children}</p>
    </div>
  );
}

/** 代理槽位：列出 / 新增 / 删除 / 测试 / 扫描本机候选。 */
function ProxyCard({ t }: { t: (k: string, v?: Record<string, string | number>) => string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['proxy-slots'], queryFn: function () { return proxySlots('wb'); } });
  const [name, setName] = useState('');
  const [url, setUrl] = useState('');
  const [cands, setCands] = useState<{ url: string; ok?: boolean; latency_ms?: number }[] | null>(null);

  const save = useMutation({
    mutationFn: function (next: { name?: string; url?: string }[]) { return proxySlotsSave("wb", next as never); },
    onSuccess: function () { toast.push("ok", t("common.saved")); setName(""); setUrl(""); void qc.invalidateQueries({ queryKey: ["proxy-slots"] }); },
    onError: function (e) { toast.push("err", (e as Error).message); },
  });

  const test = useMutation({
    mutationFn: function (id: string) { return proxySlotTest("wb", id); },
    onSuccess: function (res) { toast.push(res?.ok ? "ok" : "warn", String(res?.ip || res?.error || "")); void qc.invalidateQueries({ queryKey: ["proxy-slots"] }); },
    onError: function (e) { toast.push("err", (e as Error).message); },
  });

  const disc = useMutation({
    mutationFn: function () { return proxyDiscover("wb"); },
    onSuccess: function (res) {
      const list = (res?.candidates ?? []).filter(function (c) { return c.url; }).map(function (c) { return { url: String(c.url), ok: c.ok, latency_ms: c.latency_ms }; });
      setCands(list);
      toast.push(list.length ? "ok" : "warn", String(list.length));
    },
    onError: function (e) { toast.push("err", (e as Error).message); },
  });

  const slots = q.data?.slots ?? [];

  return (
    <Card
      title={t("proxy.title")}
      subtitle={t("proxy.hint")}
      actions={(<Button size="sm" variant="secondary" loading={disc.isPending} onClick={function () { disc.mutate(); }}>{t("proxy.discover")}</Button>)}
    >
      {q.isError ? (
        <div className="mb-2 flex items-center gap-2 rounded-md border border-err/30 bg-err-soft px-2.5 py-2 text-[11.5px] text-err">
          <span className="min-w-0 flex-1 truncate">{String((q.error as Error)?.message || t("state.errTitle"))}</span>
          <Button size="sm" variant="secondary" onClick={function () { void q.refetch(); }}>{t("common.retry")}</Button>
        </div>
      ) : null}

      {slots.length ? (
        <ul className="mb-2 flex flex-col">
          {slots.map(function (s) {
            return (
              <li key={String(s.id)} className="flex items-center gap-2 border-b border-line-faint py-1.5 last:border-0">
                <span className="min-w-0 flex-1 truncate text-[12px] text-fg">{String(s.name || s.label || s.id)}</span>
                <span className="hidden max-w-[220px] shrink-0 truncate font-mono text-[11px] text-fg-mute sm:block">{String(s.url || "")}</span>
                {s.country ? <Badge tone="info">{String(s.country_code || s.country)}</Badge> : null}
                <Badge tone="neutral">{t("proxy.bound")} {String(s.bound ?? 0)}</Badge>
                <Button size="sm" variant="ghost" loading={test.isPending} onClick={function () { if (s.id) test.mutate(String(s.id)); }}>{t("proxy.test")}</Button>
                <Button size="sm" variant="ghost" icon={<Trash2 size={12} />} onClick={function () { save.mutate(slots.filter(function (x) { return x.id !== s.id; })); }}>{t("common.delete")}</Button>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="mb-2 py-3 text-center text-[11.5px] text-fg-mute">—</p>
      )}

      {cands ? (
        <div className="mb-2 rounded-md border border-line-faint p-2">
          <p className="mb-1 text-[10px] uppercase tracking-wide text-fg-mute">{t("proxy.discover")}</p>
          <ul className="flex flex-col gap-1">
            {cands.length === 0 ? <li className="text-[11px] text-fg-mute">—</li> : null}
            {cands.map(function (c) {
              return (
                <li key={c.url} className="flex items-center gap-2 text-[11px]">
                  <span className="min-w-0 flex-1 truncate font-mono text-fg-dim">{c.url}</span>
                  <Badge tone={c.ok ? "ok" : "err"}>{c.ok ? "ok" : "fail"}</Badge>
                  <span className="tnum text-fg-mute">{c.latency_ms ? c.latency_ms + "ms" : ""}</span>
                  <Button size="sm" variant="ghost" onClick={function () { save.mutate(slots.concat([{ name: c.url.replace(/^https?:\/\//, ""), url: c.url }]) as never); }}>{t("proxy.add")}</Button>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      <div className="flex flex-wrap items-end gap-2">
        <Field label={t("proxy.name")} className="w-36">
          {(p) => <Input {...p} value={name} onChange={function (e) { setName(e.target.value); }} placeholder="home" />}
        </Field>
        <Field label={t("proxy.url")} className="min-w-[220px] flex-1">
          {(p) => <Input {...p} value={url} onChange={function (e) { setUrl(e.target.value); }} placeholder="http://127.0.0.1:7890" />}
        </Field>
        <Button variant="primary" loading={save.isPending} disabled={!name.trim() || !url.trim()}
          onClick={function () { save.mutate(slots.concat([{ name: name.trim(), url: url.trim() }]) as never); }}>
          {t("proxy.add")}
        </Button>
      </div>
    </Card>
  );
}

/**
 * 高级设置：把引擎 /settings 里**所有可写的基础类型**都变成控件。
 *
 * 为什么要有：同步上游之后，引擎新增的开关（models_only_available、daily_chat_web、
 * log_db_enabled…）在面板里一个都看不到 —— 用户的原话是"光同步，有些 web 都没有控制的开关"。
 * 这里按类型自动渲染：bool→开关、number→数字框、string→文本框；
 * 复杂结构（list/dict）与只读元数据（*_max/_default/_masked…）不渲染。
 */
const META_SUFFIX = ["_max", "_min", "_default", "_masked", "_dir", "_set", "_by_panel"];
const READONLY_KEYS = new Set([
  "version", "settings_file", "usage_dir", "accounts_dir", "api_key_masked", "api_key_set", "api_key_set_by_panel",
  "api_keys", "limits", "prompt", "identity", "identity_version_defaults",
  "deleted_api_keys", "hidden_pages", "realm_routing", "daily_chat_web_builtin_prompts",
  "panel_password_is_default", "pricing_refresh_minutes",
]);

function editableKeys(doc: Record<string, unknown> | undefined): { key: string; kind: "bool" | "number" | "string"; value: unknown }[] {
  if (!doc) return [];
  const out: { key: string; kind: "bool" | "number" | "string"; value: unknown }[] = [];
  for (const key of Object.keys(doc).sort()) {
    if (READONLY_KEYS.has(key)) continue;
    if (META_SUFFIX.some(function (sfx) { return key.endsWith(sfx); })) continue;
    const v = doc[key];
    if (typeof v === "boolean") out.push({ key: key, kind: "bool", value: v });
    else if (typeof v === "number") out.push({ key: key, kind: "number", value: v });
    else if (typeof v === "string" && v.length <= 80) out.push({ key: key, kind: "string", value: v });
  }
  return out;
}

function AdvancedSettingsCard(props: {
  engine: Engine;
  settings: SettingsPayload | undefined;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { lang } = useLang();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [q, setQ] = useState("");
  useEffect(function () { setDraft({}); }, [props.engine]);

  const items = editableKeys(props.settings as Record<string, unknown> | undefined);
  const shown = q.trim()
    ? items.filter(function (it) { return it.key.toLowerCase().includes(q.trim().toLowerCase()); })
    : items;
  const dirtyCount = Object.keys(draft).length;

  const save = useMutation({
    mutationFn: function () { return saveSettings(props.engine, draft); },
    onSuccess: function () {
      toast.push("ok", props.t("common.saved") + " · " + dirtyCount);
      setDraft({});
      void qc.invalidateQueries({ queryKey: ["settings", props.engine] });
    },
    onError: function (e) { toast.push("err", (e as Error).message); },
  });

  if (!items.length) {
    return (
      <Card title={props.t("settings.advanced.title")}>
        <p className="py-2 text-[11.5px] text-fg-mute">{props.t("settings.advanced.none")}</p>
      </Card>
    );
  }

  function setValue(key: string, value: unknown) {
    setDraft(function (old) { return { ...old, [key]: value }; });
  }
  function valueOf(it: { key: string; value: unknown }) {
    return it.key in draft ? draft[it.key] : it.value;
  }

  return (
    <Card
      title={props.t("settings.advanced.title")}
      subtitle={props.t("settings.advanced.hint", { n: items.length })}
      actions={(
        <>
          <Input value={q} onChange={function (e) { setQ(e.target.value); }} placeholder={props.t("settings.advanced.search")} className="h-7 w-40" />
          <Button size="sm" variant="primary" loading={save.isPending} disabled={dirtyCount === 0} onClick={function () { save.mutate(); }}>
            {props.t("common.save")}{dirtyCount ? " (" + dirtyCount + ")" : ""}
          </Button>
        </>
      )}
    >
      <div className="grid max-h-[320px] gap-1 overflow-auto sm:grid-cols-2">
        {shown.map(function (it) {
          const val = valueOf(it);
          const changed = it.key in draft;
          return (
            <div key={it.key} className={"flex items-center justify-between gap-2 rounded px-1.5 py-1 " + (changed ? "bg-accent-soft/40" : "")}>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[11.5px] text-fg" title={settingLabel(it.key, lang)}>{settingLabel(it.key, lang)}</span>
                <span className="block truncate font-mono text-[9.5px] text-fg-mute" title={it.key}>{it.key}</span>
              </span>
              {it.kind === "bool" ? (
                <Switch checked={val === true} onCheckedChange={function (v: boolean) { setValue(it.key, v); }} label={it.key} />
              ) : it.kind === "number" ? (
                <Input type="number" value={String(val)} onChange={function (e) { setValue(it.key, Number(e.target.value)); }} className="h-6 w-24 text-right" />
              ) : (
                <Input value={String(val)} onChange={function (e) { setValue(it.key, e.target.value); }} className="h-6 w-40" />
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
