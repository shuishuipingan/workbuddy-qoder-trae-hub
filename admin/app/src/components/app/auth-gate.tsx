import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { LockKeyhole } from 'lucide-react';
import { useLang } from '@/lib/i18n';
import { Button } from '@/components/ui/button';
import { Field } from '@/components/ui/field';
import { Input } from '@/components/ui/input';
import { engineHealth, hasToken, login, logout } from '@/lib/api';

type Mode = "checking" | "auth" | "ready" | "unreachable";

/**
 * 三态门：checking / auth / ready（+ 网关不可达）。
 *  · 未登录：只渲染登录卡，主壳隐藏（两者绝不共用一屏）
 *  · 401：清令牌回登录
 *  · 网关 5xx/断网：给出「重试」而不是把错误吞掉
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const { t } = useLang();
  const [mode, setMode] = useState<Mode>("checking");
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<string>("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const probe = useCallback(async () => {
    setMode("checking");
    setDetail("");
    if (!hasToken()) { setMode("auth"); return; }
    try {
      await engineHealth("wb");
      setMode("ready");
    } catch (err) {
      const status = (err as { status?: number }).status || 0;
      if (status === 401) { await logout(); setMode("auth"); }
      else { setDetail((err as Error)?.message || "unreachable"); setMode("unreachable"); }
    }
  }, []);

  useEffect(() => { void probe(); }, [probe]);

  // 任意请求命中 401（令牌轮换）→ 回到登录态，而不是让各视图显示"没数据"
  useEffect(() => {
    const onUnauthorized = () => { setMode("auth"); setError(null); };
    window.addEventListener("hub-unauthorized", onUnauthorized);
    return () => window.removeEventListener("hub-unauthorized", onUnauthorized);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    const r = await login(password, "wb");
    setBusy(false);
    if (r.ok) { setPassword(""); setMode("ready"); return; }
    setError(r.error === "unauthorized" ? t("auth.invalid") : t("error.network", { detail: r.error }));
  }

  if (mode === "ready") return <>{children}</>;

  return (
    <div className="grid min-h-dvh place-items-center bg-canvas px-4">
      <section className="w-full max-w-sm rounded-xl border border-line bg-surface p-6 shadow-2">
        <div className="mb-5 flex items-center gap-2">
          <span className="grid h-10 w-10 place-items-center rounded-lg bg-accent-soft text-accent">
            <LockKeyhole size={18} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold">{t("app.name")}</h1>
            <p className="truncate text-xs text-fg-mute">{t("app.tagline")}</p>
          </div>
        </div>

        {mode === "checking" ? <p className="text-sm text-fg-mute">{t("app.loading")}</p> : null}

        {mode === "unreachable" ? (
          <div className="flex flex-col gap-3">
            <p className="text-sm text-err">{t("error.network", { detail })}</p>
            <Button variant="secondary" onClick={() => void probe()}>{t("common.retry")}</Button>
          </div>
        ) : null}

        {mode === "auth" ? (
          <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
            <Field label={t("auth.password")} error={error} required>
              {(p) => (
                <Input
                  {...p}
                  type="password"
                  autoComplete="current-password"
                  autoFocus
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  invalid={p.invalid}
                  placeholder="••••••"
                />
              )}
            </Field>
            <Button type="submit" variant="primary" block loading={busy} loadingText={t("app.loading")}>
              {t("auth.submit")}
            </Button>
          </form>
        ) : null}
      </section>
    </div>
  );
}
