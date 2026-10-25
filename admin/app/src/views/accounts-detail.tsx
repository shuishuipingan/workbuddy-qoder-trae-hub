import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, RefreshCw } from 'lucide-react';
import { Badge, Button, Dialog, DialogContent, Field, Input, useToast } from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { accountCredits, accountCreditsDetail, setAccount, type Account, type Engine } from '@/lib/api';

function uidOf(a: Account): string {
  return String(a.uid || a.id || '');
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-line-faint py-1.5 last:border-0">
      <span className="shrink-0 text-[11px] text-fg-mute">{label}</span>
      <span className="min-w-0 text-right text-[11.5px] text-fg-dim">{children}</span>
    </div>
  );
}

/**
 * 账号详情：把「这个账号为什么不可用」直接摊开 —— 令牌过期、要重登、冷却、限流、上次错误。
 * 这些字段后端一直有，之前界面上完全看不到，等于让用户猜。
 */
export function AccountDetail(props: { account: Account | null; engine: Engine; onClose: () => void }) {
  const { t } = useLang();
  const toast = useToast();
  const uid = props.account ? uidOf(props.account) : '';
  const qc = useQueryClient();
  const [note, setNote] = useState<string>(String(props.account?.note ?? ''));
  const [weight, setWeight] = useState<string>(String(props.account?.weight ?? 1));
  const save = useMutation({
    mutationFn: function () {
      const w = Number(weight);
      return setAccount(props.engine, uid, { note: note, weight: Number.isFinite(w) ? w : 1 });
    },
    onSuccess: function () {
      toast.push('ok', t('common.saved'));
      void qc.invalidateQueries({ queryKey: ['accounts'] });
    },
    onError: function (e) { toast.push('err', (e as Error).message); },
  });
  const detailCredits = useQuery({
    queryKey: ['account-credits-detail', props.engine, uid],
    queryFn: function () { return accountCreditsDetail(props.engine, uid); },
    enabled: !!uid,
    retry: 0,
  });

  const credits = useQuery({
    queryKey: ['account-credits', props.engine, uid],
    queryFn: function () { return accountCredits(props.engine, uid); },
    enabled: !!uid,
    retry: 0,
  });

  if (!props.account) return null;
  const a = props.account;
  const c = credits.data?.results?.[0]?.credits;

  function copyUid() {
    navigator.clipboard.writeText(uid)
      .then(function () { toast.push('ok', t('common.copied')); })
      .catch(function () { toast.push('err', t('common.copyFailed')); });
  }

  return (
    <Dialog open={true} onOpenChange={function (v) { if (!v) props.onClose(); }}>
      <DialogContent
        title={String(a.nickname || a.email || uid.slice(0, 8))}
        description={uid}
        footer={(
          <>
            <Button variant="ghost" icon={<Copy size={12} />} onClick={copyUid}>{t('common.copy')}</Button>
            <Button
              variant="secondary"
              icon={<RefreshCw size={12} />}
              loading={credits.isFetching}
              onClick={function () { void credits.refetch(); }}
            >
              {t('credit.refresh')}
            </Button>
            <Button variant="primary" onClick={props.onClose}>{t('common.close')}</Button>
          </>
        )}
      >
        <div className="grid gap-4 sm:grid-cols-2">
          <section>
            <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-fg-mute">{t('app.status')}</p>
            <div className="flex flex-wrap gap-1">
              <Badge tone={a.enabled !== false ? 'ok' : 'neutral'}>
                {a.enabled !== false ? t('common.enabled') : t('common.disabled')}
              </Badge>
              {a.tokenExpired ? <Badge tone="err">token 过期</Badge> : null}
              {a.needRelogin ? <Badge tone="warn">需重新登录</Badge> : null}
              {a.inCooldown ? <Badge tone="warn">{'冷却 ' + String(a.cooldownFor ?? '')}</Badge> : null}
              {a.restrictedFor ? <Badge tone="err">{'限流 ' + String(a.restrictedFor)}</Badge> : null}
              {a.hasRefreshToken ? <Badge tone="info">可自动续期</Badge> : null}
            </div>
            <div className="mt-2">
              <Row label={t('accounts.field.realm')}>{String(a.realm || '—')}</Row>
              <Row label={t('accounts.field.platform')}>{String(a.platform || '—')}</Row>
              <Row label={t('accounts.field.source')}>{String(a.source || '—')}</Row>
              <Row label={t('accounts.field.expires')}>{String(a.expiresIn || '—')}</Row>
              <Row label={t('accounts.field.fails')}>{String(a.fails ?? 0)}</Row>
              <Row label={t('accounts.field.weight')}>{String(a.weight ?? '—')}</Row>
            </div>
          </section>

          <section>
            <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-fg-mute">{t('credit.label')}</p>
            {credits.isPending ? (
              <p className="py-3 text-[11px] text-fg-mute">{t('app.loading')}</p>
            ) : c ? (
              <div>
                <p className="text-[19px] font-semibold tnum text-fg">
                  {typeof c.remain === 'number' ? Math.round(c.remain).toLocaleString('en-US') : '—'}
                </p>
                <Row label={t('accounts.usedCredits')}>
                  <span className="tnum">{typeof c.used === 'number' ? Math.round(c.used).toLocaleString('en-US') : '—'}</span>
                </Row>
              </div>
            ) : (
              <p className="py-3 text-[11px] text-fg-mute">{credits.isError ? String((credits.error as Error).message) : '—'}</p>
            )}
            {detailCredits.data?.credits?.packages?.length ? (
              <div className="mt-2">
                <p className="mb-1 text-[10px] font-medium uppercase tracking-[0.08em] text-fg-mute">{t('accounts.field.packages')}</p>
                <ul className="max-h-32 overflow-auto rounded-md border border-line-faint">
                  {detailCredits.data.credits.packages.slice(0, 12).map(function (p, i) {
                    return (
                      <li key={i} className="flex items-center gap-2 border-b border-line-faint px-2 py-1 text-[10.5px] last:border-0">
                        <span className="min-w-0 flex-1 truncate text-fg-dim" title={String(p.name ?? "")}>{String(p.name ?? "")}</span>
                        <span className="shrink-0 tnum text-fg-mute">{String(p.remain ?? 0) + "/" + String(p.size ?? 0)}</span>
                        <span className="shrink-0 text-[10px] text-fg-mute">{p.is_expired ? "expired" : (p.days_left != null ? p.days_left + "d" : "")}</span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null}
            {a.note ? <Row label={t('accounts.field.note')}>{String(a.note)}</Row> : null}
            {a.disabledModels && a.disabledModels.length ? (
              <Row label={t('accounts.field.disabledModels')}>{a.disabledModels.join(', ')}</Row>
            ) : null}
          </section>
        </div>

        <div className="mt-3 grid gap-2 border-t border-line-faint pt-3 sm:grid-cols-[minmax(0,1fr)_120px_auto] sm:items-end">
          <Field label={t("common.name") + " / note"}>
            {(p) => <Input {...p} value={note} onChange={function (e) { setNote(e.target.value); }} placeholder="—" />}
          </Field>
          <Field label={t('accounts.field.weight')}>
            {(p) => <Input {...p} type="number" min={0} step={1} value={weight} onChange={function (e) { setWeight(e.target.value); }} />}
          </Field>
          <Button variant="secondary" loading={save.isPending} disabled={!note && !weight} onClick={function () { save.mutate(); }}>
            {t("common.save")}
          </Button>
        </div>

        {a.lastError ? (
          <div className="mt-3 rounded-md border border-err/30 bg-err-soft px-2.5 py-2">
            <p className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-err">{t('common.error')}</p>
            <p className="break-words font-mono text-[11px] text-fg-dim">{String(a.lastError)}</p>
            {a.lastErrorDetail ? (
              <p className="mt-1 break-words font-mono text-[10.5px] text-fg-mute">{String(JSON.stringify(a.lastErrorDetail)).slice(0, 300)}</p>
            ) : null}
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
