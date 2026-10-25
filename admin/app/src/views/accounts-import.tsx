import { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ExternalLink, FileJson, KeyRound, MonitorDown, ShieldCheck } from 'lucide-react';
import { Button, Dialog, DialogContent, Field, Input, Textarea, useToast } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import {
  ENGINE_LABELS, importAccounts, importDesktop, importPat, oauthCancel, oauthPoll, oauthStart, scanDesktop,
  type DesktopAccount, type Engine,
} from '@/lib/api';

/**
 * 导入账号：JSON 文件 / PAT / 桌面客户端扫描。
 * 三种方式都先「预检」再「确认」，避免一次性把脏数据灌进账号池。
 */
export function ImportDialog(props: { open: boolean; onClose: () => void; engine: Engine }) {
  const { t } = useLang();
  const toast = useToast();
  const qc = useQueryClient();
  const [tab, setTab] = useState<'json' | 'pat' | 'desktop' | 'oauth'>('json');
  const [raw, setRaw] = useState('');
  const [pat, setPat] = useState('');
  const [scanned, setScanned] = useState<DesktopAccount[] | null>(null);
  const [oauth, setOauth] = useState<{ state: string; url: string } | null>(null);
  const [oauthStatus, setOauthStatus] = useState<string>('');

  function closeAll() {
    if (oauth) { void oauthCancel(props.engine, oauth.state).catch(function () { /* ignore */ }); }
    setOauth(null);
    props.onClose();
  }

  function done(msg: string) {
    toast.push('ok', msg);
    void qc.invalidateQueries({ queryKey: ['accounts'] });
    setOauth(null);
    props.onClose();
  }

  const importJson = useMutation({
    mutationFn: async function () {
      const text = raw.trim();
      if (!text) throw new Error('JSON 为空');
      let parsed: unknown;
      try { parsed = JSON.parse(text); } catch { throw new Error(t('accounts.import.jsonInvalid')); }
      const list = Array.isArray(parsed)
        ? parsed
        : (parsed && typeof parsed === 'object' && Array.isArray((parsed as { accounts?: unknown[] }).accounts)
          ? (parsed as { accounts: unknown[] }).accounts
          : [parsed]);
      return importAccounts(props.engine, list);
    },
    onSuccess: function (res) {
      const parts = [
        t('accounts.import.added', { n: String(res?.added ?? 0) }),
        t('accounts.import.updated', { n: String(res?.updated ?? 0) }),
        t('accounts.import.skipped', { n: String(res?.skipped ?? 0) }),
      ];
      const bad = Number(res?.invalid ?? 0);
      if (bad > 0) parts.push(t('accounts.import.invalid', { n: String(bad) }));
      toast.push(bad > 0 ? 'warn' : 'ok', parts.join(' · '));
      void qc.invalidateQueries({ queryKey: ['accounts'] });
      props.onClose();
    },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const scan = useMutation({
    mutationFn: function () { return scanDesktop(props.engine); },
    onSuccess: function (res) {
      const rows = res?.accounts ?? [];
      setScanned(rows);
      toast.push(rows.length ? 'ok' : 'warn', rows.length ? String(rows.length) : t('accounts.import.desktopNone'));
    },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const importDesk = useMutation({
    mutationFn: function (all: boolean) {
      const uids = (scanned ?? []).map(function (a) { return String(a.uid ?? ''); }).filter(Boolean);
      return importDesktop(props.engine, all ? { all: true } : { uids: uids });
    },
    onSuccess: function (res) { done(t('accounts.import.desktopImported', { n: String(res?.added ?? 0) })); },
    onError: function (err) { toast.push('err', (err as Error).message); },
  });

  const usePat = useMutation({
    mutationFn: function () { return importPat(props.engine, pat.trim()); },
    onSuccess: function () { done(t('common.saved')); },
    onError: function (err) {
      const st = (err as { status?: number }).status;
      toast.push(st === 404 ? 'warn' : 'err', st === 404 ? t('accounts.import.patUnsupported') : (err as Error).message);
    },
  });

  /** OAuth：拿到 state 后每 2s 问一次，后端说完成就收工 */
  useEffect(function () {
    if (!oauth) return;
    let stopped = false;
    const timer = setInterval(async function () {
      try {
        const res = await oauthPoll(props.engine, oauth.state);
        if (stopped) return;
        setOauthStatus(String(res?.status ?? ''));
        if (['ok', 'done', 'success', 'authorized'].indexOf(String(res?.status)) >= 0) {
          stopped = true;
          clearInterval(timer);
          done(t('accounts.import.oauthDone'));
        }
      } catch { /* 网络抖动忽略，下一轮再问 */ }
    }, 2000);
    return function () { stopped = true; clearInterval(timer); };
  }, [oauth]);

  const startOAuth = useMutation({
    mutationFn: function () { return oauthStart(props.engine); },
    onSuccess: function (res) {
      if (!res?.state || !res?.authUrl) { toast.push('err', t('accounts.import.oauthFailed', { detail: 'no url' })); return; }
      setOauth({ state: res.state, url: res.authUrl });
      setOauthStatus('pending');
      // 自动打开一次授权页；面板这边只等结果
      window.open(res.authUrl, "_blank", "noopener,noreferrer");
    },
    onError: function (e) {
      const msg = (e as Error).message || '';
      // 501 = 这个引擎没有设备授权流程（Trae 就是这样）：别只弹个错，直接把人送到 PAT 页签
      if (/unsupported|没有设备授权|no device/i.test(msg)) {
        toast.push('warn', msg);
        setTab('pat');
        return;
      }
      toast.push('err', t('accounts.import.oauthFailed', { detail: msg }));
    },
  });

  const busy = importJson.isPending || scan.isPending || importDesk.isPending || usePat.isPending;

  return (
    <Dialog open={props.open} onOpenChange={function (v) { if (!v) closeAll(); }}>
      <DialogContent
        title={t('accounts.import.title') + ' · ' + ENGINE_LABELS[props.engine]}
        description={t('accounts.import.jsonHint')}
        footer={(
          <>
            <Button variant="ghost" onClick={closeAll}>{t('common.cancel')}</Button>
            {tab === 'json' ? (
              <Button variant="primary" loading={importJson.isPending} disabled={!raw.trim()} onClick={function () { importJson.mutate(); }}>
                {t('accounts.import.confirm')}
              </Button>
            ) : null}
            {tab === 'pat' ? (
              <Button variant="primary" loading={usePat.isPending} disabled={!pat.trim()} onClick={function () { usePat.mutate(); }}>
                {t('accounts.import.confirm')}
              </Button>
            ) : null}
            {tab === 'desktop' ? (
              scanned && scanned.length > 0 ? (
                <Button variant="primary" loading={importDesk.isPending} onClick={function () { importDesk.mutate(false); }}>
                  {t('accounts.import.confirm')} ({scanned.length})
                </Button>
              ) : (
                <Button variant="secondary" loading={scan.isPending} onClick={function () { scan.mutate(); }}>
                  {t('accounts.import.scan')}
                </Button>
              )
            ) : null}
          </>
        )}
      >
        <div className="mb-3 flex gap-0.5 rounded-md border border-line-faint p-0.5" role="tablist">
          {([['json', FileJson, t('accounts.import.json')], ['pat', KeyRound, t('accounts.import.pat')], ['desktop', MonitorDown, t('accounts.import.desktop')], ['oauth', ShieldCheck, t('accounts.import.oauth')]] as const).map(function (item) {
            const Icon = item[1];
            return (
              <button
                key={item[0]}
                role="tab"
                aria-selected={tab === item[0]}
                onClick={function () { setTab(item[0]); }}
                className={'flex h-7 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded px-2 text-[11.5px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ' +
                  (tab === item[0] ? 'bg-surface-2 font-medium text-fg' : 'text-fg-dim hover:bg-surface-2/70')}
              >
                <Icon size={12} aria-hidden="true" />
                {item[2]}
              </button>
            );
          })}
        </div>

        {tab === 'json' ? (
          <Field label={t('accounts.import.json')} hint={t('accounts.import.jsonDesc')}>
            {(p) => (
              <Textarea
                {...p}
                rows={8}
                value={raw}
                onChange={function (e) { setRaw(e.target.value); }}
                placeholder='[{"name":"acc-1","refresh_token":"..."}]'
                className="font-mono text-[11px]"
              />
            )}
          </Field>
        ) : null}

        {tab === 'pat' ? (
          <Field label={t('accounts.import.pat')} hint={t('accounts.import.patHint')}>
            {(p) => (
              <Input
                {...p}
                value={pat}
                onChange={function (e) { setPat(e.target.value); }}
                placeholder={t('accounts.import.patPlaceholder')}
                type="password"
                autoComplete="off"
              />
            )}
          </Field>
        ) : null}

        {tab === 'desktop' ? (
          <div className="flex flex-col gap-2">
            <p className="text-[11.5px] text-fg-mute">{t('accounts.import.desktopDesc')}</p>
            {scanned === null ? null : scanned.length === 0 ? (
              <p className="rounded-md bg-surface-2/60 px-2.5 py-3 text-center text-[11.5px] text-fg-mute">
                {t('accounts.import.desktopNone')}
              </p>
            ) : (
              <ul className="max-h-40 overflow-auto rounded-md border border-line-faint">
                {scanned.map(function (a, i) {
                  return (
                    <li key={String(a.uid ?? i)} className="flex items-center gap-2 border-b border-line-faint px-2 py-1 text-[11.5px] last:border-0">
                      <span className="min-w-0 flex-1 truncate text-fg-dim">{String(a.nickname ?? a.email ?? a.uid ?? '—')}</span>
                      <span className="shrink-0 font-mono text-[10px] text-fg-mute">{String(a.uid ?? '').slice(0, 8)}</span>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        ) : null}

        {tab === 'oauth' ? (
          <div className="flex flex-col gap-2">
            <p className="text-[11.5px] text-fg-mute">{t('accounts.import.oauthDesc')}</p>
            {oauth ? (
              <div className="flex flex-col gap-2">
                {/* 内嵌授权：授权页直接在面板里打开，本窗口轮询结果，不用切浏览器 */}
                {/* 授权页由对方站点承载：实测 workbuddy 的登录页会 frame-bust，
                    iframe 里是空白 —— 与其给一块白板，不如在面板里做"等待 + 自动入池"。 */}
                <div className="rounded-md border border-line-faint bg-surface-2 px-3 py-3">
                  <div className="mb-1.5 flex items-center gap-2">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden="true" />
                    <p className="text-[12px] font-medium text-fg">{t('accounts.import.oauthWaitingTitle')}</p>
                  </div>
                  <p className="text-[11px] text-fg-mute">{t('accounts.import.oauthBlocked')}</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button size="sm" variant="secondary" icon={<ExternalLink size={12} />} onClick={function () { window.open(oauth.url, "_blank", "noopener,noreferrer"); }}>
                    {t('common.openInNewWindow')}
                  </Button>
                  <span className="text-[11px] text-fg-mute">{t('accounts.import.oauthWaiting')} · {oauthStatus}</span>
                </div>
                <p className="break-all font-mono text-[10px] text-fg-mute">{oauth.url}</p>
                <p className="text-[10.5px] text-fg-mute">{t('accounts.import.oauthIframeHint')}</p>
              </div>
            ) : (
              <Button variant="secondary" loading={startOAuth.isPending} onClick={function () { startOAuth.mutate(); }} icon={<ShieldCheck size={13} />}>
                {t('accounts.import.oauthOpen')}
              </Button>
            )}
            <p className="text-[10.5px] text-fg-mute">{t('accounts.import.oauthHint')}</p>
          </div>
        ) : null}

        {busy ? <p className="mt-2 text-[11px] text-fg-mute">{t('common.saving')}</p> : null}
      </DialogContent>
    </Dialog>
  );
}
