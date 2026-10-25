import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react';
import { CheckCircle2, Info, TriangleAlert, XCircle, X } from 'lucide-react';
import { cn } from '@/lib/cn';

export type ToastTone = "ok" | "info" | "warn" | "err";
export type ToastItem = { id: number; tone: ToastTone; message: string };

type Ctx = { push: (tone: ToastTone, message: string) => void };
const ToastCtx = createContext<Ctx | null>(null);

const ICONS = { ok: CheckCircle2, info: Info, warn: TriangleAlert, err: XCircle };
const TONE_CLASS: Record<ToastTone, string> = {
  ok: "border-ok/40 bg-ok-soft text-ok",
  info: "border-info/40 bg-info-soft text-info",
  warn: "border-warn/40 bg-warn-soft text-warn",
  err: "border-err/40 bg-err-soft text-err",
};

/** 轻提示：只做"结果播报"，**不用它承担表单校验**（那必须就地提示）。 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const push = useCallback((tone: ToastTone, message: string) => {
    const id = ++seq.current;
    setItems((prev) => [...prev, { id, tone, message }].slice(-4));
    window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 4200);
  }, []);

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2"
        aria-live="polite"
        aria-atomic="false"
      >
        {items.map((t) => {
          const Icon = ICONS[t.tone];
          return (
            <div
              key={t.id}
              className={cn(
                "hub-content pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 text-sm shadow-2",
                TONE_CLASS[t.tone],
              )}
              data-state="open"
              role="status"
            >
              <Icon size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
              <span className="min-w-0 flex-1 break-words">{t.message}</span>
              <button
                type="button"
                onClick={() => setItems((prev) => prev.filter((x) => x.id !== t.id))}
                className="shrink-0 cursor-pointer rounded p-0.5 text-current/80 transition-colors hover:text-current
                           focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent"
                aria-label="关闭提示"
              >
                <X size={14} aria-hidden="true" />
              </button>
            </div>
          );
        })}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): Ctx {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
