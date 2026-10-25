import { NavLink } from 'react-router-dom';
import { cn } from '@/lib/cn';
import type { RouteMeta } from '@/app/routes';

/**
 * 导航项：紧凑一行、当前项用「浅底 + 左侧强调条 + 高亮图标」三重但不喧宾夺主。
 * 命中区在触屏上由 tokens.css 的 pointer:coarse 规则放大到 44px。
 */
export function NavItem(props: { route: RouteMeta; label: string; onNavigate?: () => void; compact?: boolean }) {
  const Icon = props.route.icon;
  return (
    <NavLink
      to={props.route.path}
      onClick={props.onNavigate}
      className={function (state) {
        return cn(
          'nav-link group relative flex h-7 items-center gap-2 rounded-[6px] px-2 text-[12.5px]',
          'cursor-pointer focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
          state.isActive ? 'bg-surface-2 font-medium text-fg' : 'text-fg-dim hover:bg-surface-2/70 hover:text-fg',
          props.compact ? 'mx-auto h-8 w-8 justify-center px-0' : '',
        );
      }}
    >
      {function (state) {
        return (
          <>
            {state.isActive && !props.compact ? (
              <span
                aria-hidden="true"
                className="absolute left-0 top-1/2 h-3.5 w-[2px] -translate-y-1/2 rounded-full bg-accent"
              />
            ) : null}
            <Icon size={14} aria-hidden="true" className={state.isActive ? 'shrink-0 text-accent' : 'shrink-0 opacity-80'} />
            {props.compact ? <span className="sr-only">{props.label}</span> : <span className="link-underline truncate">{props.label}</span>}
          </>
        );
      }}
    </NavLink>
  );
}
