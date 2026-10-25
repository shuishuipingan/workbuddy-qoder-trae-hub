import { useEffect, useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';

const LOGO = (import.meta.env.BASE_URL || './') + 'logo.jpg';
import { ChevronLeft, Menu, Moon, PanelLeft, Sun, X } from 'lucide-react';
import { useLang } from '@/lib/i18n';
import { UpdateBanner } from '@/components/app/update';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/cn';
import { GROUP_LABEL, ROUTES, type RouteMeta } from '@/app/routes';
import { NavItem } from './nav-item';
import { RouteTransition } from './route-transition';

const THEME_KEY = 'hub-theme';
const NAV_KEY = 'hub-nav-open';

function readTheme(): 'dark' | 'light' {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}
/** 默认展开；用户收进去过就记住。 */
function readOpen(): boolean {
  try { return localStorage.getItem(NAV_KEY) !== '0'; } catch { return true; }
}

function Group(props: { group: RouteMeta['group']; t: (k: string) => string; onNavigate?: () => void }) {
  const items = ROUTES.filter(function (r) { return r.group === props.group; });
  if (!items.length) return null;
  return (
    <div className="flex flex-col gap-0.5">
      <p className="px-2 pb-0.5 pt-2 text-[9.5px] font-medium uppercase tracking-[0.09em] text-fg-mute">
        {props.t(GROUP_LABEL[props.group])}
      </p>
      {items.map(function (r) {
        return <NavItem key={r.id} route={r} label={props.t(r.labelKey)} onNavigate={props.onNavigate} />;
      })}
    </div>
  );
}

export function AppShell() {
  const lang = useLang();
  const t = lang.t;
  const [theme, setTheme] = useState(readTheme);
  const [open, setOpen] = useState(readOpen);
  const [navOpen, setNavOpen] = useState(false);
  /** 中等宽度（<1280）自动收成 FAB：胶囊太挤会显得局促，不如收起来 */
  const [isNarrow, setIsNarrow] = useState(function () {
    try { return window.matchMedia('(max-width: 1151px)').matches; } catch { return false; }
  });
  const location = useLocation();

  const current = ROUTES.find(function (r) { return location.pathname.startsWith(r.path); }) ?? ROUTES[0];
  const title = t(current.labelKey);

  useEffect(function () {
    try { localStorage.setItem(NAV_KEY, open ? '1' : '0'); } catch { /* ignore */ }
  }, [open]);

  useEffect(function () {
    if (typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(max-width: 1151px)');
    function onChange(e: MediaQueryListEvent) { setIsNarrow(e.matches); }
    mq.addEventListener('change', onChange);
    return function () { mq.removeEventListener('change', onChange); };
  }, []);

  // ⌘/Ctrl + B 收放侧栏（编辑器的通用习惯）
  useEffect(function () {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        e.preventDefault();
        setOpen(function (v) { return !v; });
      }
    }
    window.addEventListener('keydown', onKey);
    return function () { window.removeEventListener('keydown', onKey); };
  }, []);

  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    setTheme(next);
  }

  const nav = (
    <nav aria-label={t('nav.title')} className="flex flex-col px-1.5 pb-1.5">
      <Group group="main" t={t} onNavigate={function () { setNavOpen(false); }} />
      <Group group="ops" t={t} onNavigate={function () { setNavOpen(false); }} />
      <Group group="config" t={t} onNavigate={function () { setNavOpen(false); }} />
    </nav>
  );

  return (
    <div className="min-h-dvh bg-canvas text-fg">
      {/* ── 悬浮矮胶囊：内容多高它多高，垂直居中 ───────────────── */}
      {open && !isNarrow ? (
        <aside
          className={cn(
            'nav-pill fixed left-3 z-30 hidden max-h-[calc(100dvh-24px)] flex-col lg:flex',
            'w-[208px] overflow-hidden rounded-2xl border border-line-faint bg-surface/85 shadow-2 backdrop-blur-xl lg:flex',
          )}
        >
          {/* 品牌：主名称在上，喵枢 + 版本在下 */}
          <div className="flex items-start gap-2 border-b border-line-faint/70 px-2.5 pb-2 pt-2.5">
            <img src={LOGO} alt="" aria-hidden="true" className="logo-bob mt-0.5 h-7 w-7 shrink-0 rounded-[8px] object-cover ring-1 ring-line-faint" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-[12px] font-semibold leading-4 tracking-tight" title={t('app.name')}>
                {t('app.name')}
              </p>
              <p className="truncate text-[10px] leading-4 text-fg-mute">{t('app.short')} · v1.0.0</p>
            </div>
            <button
              onClick={function () { setOpen(false); }}
              aria-label="收起导航"
              title="收起导航（Ctrl/⌘ + B）"
              className="icon-btn mt-0.5 grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-fg-mute"
            >
              <ChevronLeft size={13} />
            </button>
          </div>

          <div className="nav-scroll min-h-0 overflow-y-auto">{nav}</div>

          <div className="flex items-center gap-1 border-t border-line-faint/70 px-1.5 py-1.5">
            <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label={t('theme.toggle')} title={t('theme.toggle')}>
              {theme === 'dark' ? <Sun size={13} /> : <Moon size={13} />}
            </Button>
            <button
              onClick={function () { lang.setLang(lang.lang === 'zh' ? 'en' : 'zh'); }}
              className="icon-btn ml-auto h-7 cursor-pointer rounded-md px-2 text-[11px] text-fg-dim"
            >
              {lang.lang === 'zh' ? 'EN' : '中'}
            </button>
          </div>
        </aside>
      ) : (
        /* 收进去（或中等宽度自动收）之后：只留一枚贴边的圆形按钮 */
        <button
          onClick={function () { setOpen(true); }}
          aria-label="展开导航"
          title="展开导航（Ctrl/⌘ + B）"
          className={cn(
            'fab fixed left-3 z-30 hidden h-11 w-11 place-items-center rounded-full lg:grid',
            'border border-line-faint bg-surface/90 shadow-2 backdrop-blur-xl lg:grid',
          )}
        >
          <PanelLeft size={16} />
        </button>
      )}

      {/* ── 主区域 ─────────────────────────────────────────── */}
      <div className={cn('flex min-h-dvh min-w-0 flex-col transition-[padding] duration-250 ease-[cubic-bezier(0.22,1,0.36,1)]', open && !isNarrow ? 'lg:pl-[228px]' : 'lg:pl-[72px]')}>
        <header className="sticky top-0 z-20 border-b border-line-faint bg-canvas">
          <div className="flex h-12 items-center gap-2 px-3 sm:px-4">
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              aria-label={navOpen ? '关闭导航' : '打开导航'}
              aria-expanded={navOpen}
              onClick={function () { setNavOpen(function (v) { return !v; }); }}
            >
              {navOpen ? <X size={16} /> : <Menu size={16} />}
            </Button>
            <img src={LOGO} alt="" aria-hidden="true" className="h-5 w-5 rounded-[5px] object-cover lg:hidden" />
            <span className="hidden items-center gap-1.5 text-[11px] text-fg-mute sm:flex">
              <span className="max-w-[220px] truncate">{t('app.name')}</span>
              <span className="text-line-strong">/</span>
            </span>
            <h1 className="min-w-0 truncate text-[13px] font-semibold">{title}</h1>
            <div className="ml-auto flex items-center gap-1 lg:hidden">
              <Button variant="ghost" size="icon" onClick={toggleTheme} aria-label={t('theme.toggle')}>
                {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
              </Button>
            </div>
          </div>
        </header>

        {navOpen ? (
          <div className="border-b border-line-faint bg-surface lg:hidden" data-mobile-nav="1">
            <div className="flex h-10 items-center gap-2 px-3">
              <img src={LOGO} alt="" aria-hidden="true" className="h-6 w-6 rounded-[6px] object-cover" />
              <div className="min-w-0">
                <p className="truncate text-[12px] font-semibold leading-4">{t('app.name')}</p>
                <p className="truncate text-[10px] leading-4 text-fg-mute">{t('app.short')}</p>
              </div>
            </div>
            {nav}
          </div>
        ) : null}

        <main className="@container min-w-0 flex-1 px-3 py-3 sm:px-4 sm:py-4">
          <RouteTransition>
            <UpdateBanner t={t} />
          <Outlet />
          </RouteTransition>
        </main>
      </div>
    </div>
  );
}
