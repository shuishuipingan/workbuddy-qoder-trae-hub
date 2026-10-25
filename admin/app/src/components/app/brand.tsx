/**
 * 品牌矢量件：一个网关标识 + 三家引擎的字形块。
 *
 * 为什么换掉旧的：侧栏原来用的是一张 `logo.jpg` 照片，缩到 24px 就是一团糊；
 * 引擎徽标是纯文字胶囊，扫列表时认不出是哪家。这里改成矢量：
 *   · LogoMark —— 圆角方块 + 三节点连线（"三路汇一"），渐变走令牌
 *   · EngineGlyph —— 品牌色底 + 白色首字母，12–14px 也清晰
 * 颜色全部来自 tokens.css 的 --brand-* / --accent*，没有硬编码。
 */
import { cn } from '@/lib/cn';

export function LogoMark({ size = 28, className }: { size?: number; className?: string }) {
  return (
    <span
      className={cn("relative inline-grid shrink-0 place-items-center overflow-hidden rounded-[9px]", className)}
      style={{ width: size, height: size }}
      aria-hidden="true"
    >
      <svg viewBox="0 0 32 32" width={size} height={size} role="presentation">
        <defs>
          <linearGradient id="hub-mark" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--accent)" />
            <stop offset="100%" stopColor="var(--accent-3)" />
          </linearGradient>
        </defs>
        <rect x="0" y="0" width="32" height="32" rx="9" fill="url(#hub-mark)" />
        {/* 三路汇一：左下、右下、正中 */}
        <g stroke="var(--accent-fg)" strokeWidth="1.9" strokeLinecap="round" opacity="0.92">
          <path d="M10 22 L16 15.5 L22 22" fill="none" />
          <path d="M16 15.5 L16 9.5" fill="none" />
        </g>
        <g fill="var(--accent-fg)">
          <circle cx="10" cy="22" r="2.3" />
          <circle cx="22" cy="22" r="2.3" />
          <circle cx="16" cy="9.5" r="2.6" />
        </g>
      </svg>
    </span>
  );
}

const ENGINE_HUE: Record<string, string> = {
  wb: "var(--brand-wb)",
  qd: "var(--brand-qd)",
  trae: "var(--brand-trae)",
};
const ENGINE_INITIAL: Record<string, string> = { wb: "W", qd: "Q", trae: "T" };

/** 引擎字形块：品牌色底 + 白色首字母（比文字胶囊更好扫）。 */
export function EngineGlyph({ engine, size = 16, className }: { engine: string; size?: number; className?: string }) {
  const hue = ENGINE_HUE[engine] || "var(--fg-mute)";
  return (
    <span
      className={cn("inline-grid shrink-0 place-items-center rounded-[5px] font-semibold leading-none", className)}
      style={{
        width: size,
        height: size,
        background: hue,
        color: "var(--fg-invert)",
        fontSize: Math.max(9, Math.round(size * 0.62)),
      }}
      aria-hidden="true"
    >
      {ENGINE_INITIAL[engine] || engine.slice(0, 1).toUpperCase()}
    </span>
  );
}
