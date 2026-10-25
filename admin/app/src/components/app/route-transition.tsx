import { useEffect, useRef, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';

/**
 * 路由淡入：**用 CSS 动画**而不是 JS 动画库。
 *
 * 原因：路由切换是最高频的过渡，走合成器层不占主线程，且不把动画库拖进首屏。
 * Framer Motion 仍保留在依赖里，用于视图内需要编排/手势的交互（按需 import，不进首屏）。
 */
export function RouteTransition({ children }: { children: ReactNode }) {
  const location = useLocation();
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // 重新触发动画：移除类 → 强制重排 → 加回类
    el.classList.remove("hub-route");
    void el.offsetWidth;
    el.classList.add("hub-route");
  }, [location.pathname]);

  return (
    <div key={location.pathname} ref={ref} className="hub-route">
      {children}
    </div>
  );
}
