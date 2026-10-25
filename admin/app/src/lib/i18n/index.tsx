import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { zh, en, LANGS, LANG_STORE_KEY, type Lang, type MessageKey } from './messages';

const dicts: Record<Lang, Record<string, string>> = { zh, en };

const warned = new Set<string>();

/** 取词条：缺失时回退中文，再缺就原样返回键名（并只在控制台报一次）。 */
export function translate(lang: Lang, key: string, vars?: Record<string, string | number>): string {
  const table = dicts[lang] || zh;
  let text = table[key];
  if (text === undefined) text = (zh as Record<string, string>)[key];
  if (text === undefined) {
    if (!warned.has(key)) { warned.add(key); console.warn("[i18n] missing key:", key); }
    text = key;
  }
  if (!vars) return text;
  return text.replace(/\{([a-zA-Z0-9_]+)\}/g, function (m, name: string) {
    const v = vars[name];
    return v === undefined || v === null ? m : String(v);
  });
}

type LangCtx = { lang: Lang; setLang: (l: Lang) => void; t: (k: string, vars?: Record<string, string | number>) => string };

const Ctx = createContext<LangCtx | null>(null);

function readLang(): Lang {
  try {
    const saved = localStorage.getItem(LANG_STORE_KEY);
    if (saved === "zh" || saved === "en") return saved;
    return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
  } catch {
    return "zh";
  }
}

export function LangProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readLang);

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  }, [lang]);

  const value = useMemo<LangCtx>(() => ({
    lang,
    setLang(next: Lang) {
      setLangState(next);
      try { localStorage.setItem(LANG_STORE_KEY, next); } catch { /* 隐私模式忽略 */ }
    },
    t: (k, vars) => translate(lang, k, vars),
  }), [lang]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useLang(): LangCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useLang must be used inside <LangProvider>");
  return ctx;
}

/** 最常用：只要 t。 */
export function useT() {
  return useLang().t;
}

export { zh, en, LANGS, LANG_STORE_KEY };
export type { Lang, MessageKey };
