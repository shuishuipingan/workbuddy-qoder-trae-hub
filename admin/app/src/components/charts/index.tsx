/**
 * 极简 SVG 图表（零依赖）：迷你折线 / 面积图 / 横向条形 / 环形占比。
 *
 * 这一版重做了视觉与动效（上一版是直线折线 + 静态填充，看起来像草稿）：
 *   · 曲线用 Catmull-Rom 转三次贝塞尔平滑，不再有折角
 *   · 渐变 id 用 useId——原来写死 "hub-area-fill"，同页多个不同色系的图会互相串色
 *   · 线条入场用 pathLength=1 + dashoffset 动画；面积淡入；条形宽度从左长出
 *   · 悬停十字准星 + 高亮点 + 读数气泡（带描边与阴影，不再糊在网格上）
 *   · 全部动效在 prefers-reduced-motion 下关闭
 * 颜色仍全部走 CSS 变量；空数据不产生 NaN；每个图 role="img" + aria-label。
 */

import { useId, useMemo, useState } from 'react';

const TONE: Record<string, string> = {
  accent: 'var(--accent)',
  'accent-2': 'var(--accent-2)',
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  err: 'var(--err)',
  info: 'var(--info)',
};

function fmt0(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString('en-US') : '0';
}

function colorOf(tone: string): string {
  return TONE[tone] || tone;
}

function finite(data: number[]): number[] {
  return data.filter(function (n) { return Number.isFinite(n); });
}

function normalize(data: number[]) {
  const values = finite(data);
  const min = values.length ? Math.min.apply(null, values) : 0;
  const max = values.length ? Math.max.apply(null, values) : 0;
  return { values: values, min: min, max: max };
}

/** 点列（含端点） */
function toCoords(values: number[], width: number, height: number, min: number, max: number) {
  const span = max - min || 1;
  const step = values.length > 1 ? width / (values.length - 1) : width;
  return values.map(function (v, i) {
    const x = values.length > 1 ? i * step : width / 2;
    return { x: x, y: height - ((v - min) / span) * height };
  });
}

/**
 * Catmull-Rom → 三次贝塞尔：让折线变成过所有点的平滑曲线。
 * 张力固定 1/6（标准 CR），末点用自身复制补齐，避免曲线在两端甩出去。
 */
function smoothPath(points: { x: number; y: number }[]): string {
  if (points.length === 0) return '';
  if (points.length === 1) return 'M' + points[0].x.toFixed(2) + ',' + points[0].y.toFixed(2);
  let d = 'M' + points[0].x.toFixed(2) + ',' + points[0].y.toFixed(2);
  for (let i = 0; i < points.length - 1; i++) {
    const p0 = points[i - 1] || points[i];
    const p1 = points[i];
    const p2 = points[i + 1];
    const p3 = points[i + 2] || p2;
    const c1x = p1.x + (p2.x - p0.x) / 6;
    const c1y = p1.y + (p2.y - p0.y) / 6;
    const c2x = p2.x - (p3.x - p1.x) / 6;
    const c2y = p2.y - (p3.y - p1.y) / 6;
    d += ' C' + c1x.toFixed(2) + ',' + c1y.toFixed(2) + ' ' + c2x.toFixed(2) + ',' + c2y.toFixed(2) + ' ' + p2.x.toFixed(2) + ',' + p2.y.toFixed(2);
  }
  return d;
}

/** 迷你折线（KPI 卡内）：平滑 + 渐变填充 */
export function Sparkline(props: { data: number[]; height?: number; className?: string; ariaLabel?: string; tone?: string }) {
  const height = props.height ?? 28;
  const width = 120;
  const gid = useId().replace(/:/g, '');
  const norm = normalize(props.data);
  const pad = 2;
  const inner = Math.max(1, height - pad * 2);
  const pts = toCoords(norm.values, width, inner, norm.min, norm.max).map(function (p) { return { x: p.x, y: p.y + pad }; });
  const line = smoothPath(pts);
  const area = pts.length ? line + ' L' + width + ',' + height + ' L0,' + height + ' Z' : '';

  return (
    <svg
      role="img"
      aria-label={props.ariaLabel ?? '趋势'}
      viewBox={'0 0 ' + width + ' ' + height}
      className={props.className}
      style={{ width: '100%', height: height, color: colorOf(props.tone ?? 'accent') }}
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id={'spark-' + gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="currentColor" stopOpacity="0.28" />
          <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      {pts.length > 1 ? (
        <>
          <path d={area} fill={'url(#spark-' + gid + ')'} />
          <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round" pathLength={1} className="chart-line" vectorEffect="non-scaling-stroke" />
        </>
      ) : (
        <line x1={0} y1={height - pad} x2={width} y2={height - pad} stroke="var(--line)" strokeWidth={1} />
      )}
    </svg>
  );
}

/** 面积图：平滑曲线 + 渐变填充 + 4 条虚线网格 + 悬停读数 */
export function AreaChart(props: {
  data: number[];
  height?: number;
  labels?: string[];
  formatValue?: (n: number) => string;
  ariaLabel?: string;
  tone?: string;
}) {
  const height = props.height ?? 160;
  const W = 600;
  const [hover, setHover] = useState<number | null>(null);
  const gid = useId().replace(/:/g, '');
  const padTop = 14;
  const padBottom = 18;
  const inner = Math.max(1, height - padTop - padBottom);
  const norm = useMemo(function () { return normalize(props.data); }, [props.data]);
  const fmt = props.formatValue ?? function (n: number) { return n.toLocaleString('en-US'); };
  const color = colorOf(props.tone ?? 'accent');

  if (norm.values.length === 0) {
    return (
      <div
        role="img"
        aria-label={props.ariaLabel ?? '趋势图'}
        className="grid place-items-center rounded-md border border-dashed border-line-faint bg-surface-2/30 text-[11px] text-fg-mute"
        style={{ height: height }}
      >
        —
      </div>
    );
  }

  // 纵向留 8% 余量：尖峰过后全为 0 时，线不会贴着坐标轴（那样看起来像"断掉了"）
  const yPad = (norm.max - norm.min) * 0.08 || Math.max(1, norm.max * 0.08);
  const pts = toCoords(norm.values, W, inner, norm.min - yPad, norm.max).map(function (p) { return { x: p.x, y: p.y + padTop }; });
  const line = smoothPath(pts);
  const areaPath = line + ' L' + W + ',' + (height - padBottom) + ' L0,' + (height - padBottom) + ' Z';
  const step = norm.values.length > 1 ? W / (norm.values.length - 1) : W;

  return (
    <svg role="img" aria-label={props.ariaLabel ?? '趋势图'} viewBox={'0 0 ' + W + ' ' + height} style={{ width: '100%', height: height }} preserveAspectRatio="none">
      <defs>
        <linearGradient id={'area-' + gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.26" />
          <stop offset="70%" stopColor={color} stopOpacity="0.05" />
          <stop offset="100%" stopColor={color} stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0, 0.25, 0.5, 0.75, 1].map(function (r) {
        return (
          <line
            key={r}
            x1={0}
            x2={W}
            y1={padTop + inner * r}
            y2={padTop + inner * r}
            stroke="var(--line-faint)"
            strokeWidth={1}
            strokeDasharray={r === 1 ? '' : '2 4'}
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
      <path d={areaPath} fill={'url(#area-' + gid + ')'} className="chart-fade" />
      <path d={line} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" pathLength={1} className="chart-line" vectorEffect="non-scaling-stroke" />
      <text x={2} y={padTop - 4} fontSize={10} fill="var(--fg-mute)">{fmt(norm.max)}</text>
      {/* 零基线：后段全为 0 时，线会贴着这条线走 —— 有这条参考才不会看成"断线" */}
      <line x1={0} x2={W} y1={height - padBottom} y2={height - padBottom} stroke="var(--line)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
      <text x={2} y={height - padBottom - 3} fontSize={9} fill="var(--fg-mute)" opacity="0.8">0</text>
      {/* X 轴：只画首/中/尾三个刻度，避免密集时糊成一片 */}
      {props.labels && props.labels.length > 1 ? (function () {
        const idxs = [0, Math.floor((props.labels!.length - 1) / 2), props.labels!.length - 1];
        return idxs.map(function (i, k) {
          const x = i * step;
          const anchorX = k === 0 ? "start" : k === 2 ? "end" : "middle";
          const px = k === 0 ? 2 : k === 2 ? W - 2 : x;
          return (
            <text key={k} x={px} y={height - 4} fontSize={9.5} textAnchor={anchorX} fill="var(--fg-mute)">
              {props.labels![i]}
            </text>
          );
        });
      })() : null}

      <g aria-hidden="true">
        {hover !== null && norm.values[hover] !== undefined ? (function () {
          const hx = norm.values.length > 1 ? hover * step : W / 2;
          const lo = norm.min - yPad;
          const span = norm.max - lo || 1;
          const hy = padTop + inner - ((norm.values[hover] - lo) / span) * inner;
          const label = (props.labels && props.labels[hover]) || ('#' + (hover + 1));
          const text = label + ' · ' + fmt(norm.values[hover]);
          const boxW = Math.max(72, text.length * 6.2 + 16);
          const boxX = Math.min(Math.max(hx - boxW / 2, 2), W - boxW - 2);
          return (
            <g>
              <line x1={hx} x2={hx} y1={padTop} y2={padTop + inner} stroke={color} strokeWidth={1} strokeDasharray="3 3" opacity="0.55" />
              <circle cx={hx} cy={hy} r={4} fill={color} stroke="var(--surface)" strokeWidth={2} className="chart-dot" />
              <rect x={boxX} y={padTop + 2} width={boxW} height={20} rx={5} fill="var(--surface)" stroke="var(--line)" />
              <text x={boxX + boxW / 2} y={padTop + 15.5} fontSize={10.5} textAnchor="middle" fill="var(--fg)">{text}</text>
            </g>
          );
        })() : null}
        <rect
          x={0}
          y={padTop}
          width={W}
          height={inner}
          fill="transparent"
          style={{ cursor: 'crosshair' }}
          onMouseMove={function (e) {
            const rect = (e.currentTarget as SVGRectElement).getBoundingClientRect();
            const rel = (e.clientX - rect.left) / Math.max(1, rect.width);
            const n = norm.values.length;
            const i = n > 1 ? Math.round(rel * (n - 1)) : 0;
            setHover(Math.min(n - 1, Math.max(0, i)));
          }}
          onMouseLeave={function () { setHover(null); }}
        />
      </g>
    </svg>
  );
}

/** 横向条形：宽度从左长出（stagger），悬停整行抬起 */
export function BarChart(props: {
  data: { label: string; value: number }[];
  maxRows?: number;
  formatValue?: (n: number) => string;
  ariaLabel?: string;
  tone?: string;
}) {
  const maxRows = props.maxRows ?? 8;
  const fmt = props.formatValue ?? function (n: number) { return n.toLocaleString('en-US'); };
  const rows = props.data.filter(function (r) { return Number.isFinite(r.value); }).slice(0, maxRows);
  const max = rows.length ? Math.max.apply(null, rows.map(function (r) { return r.value; }).concat([1])) : 1;
  const color = colorOf(props.tone ?? 'accent');

  if (rows.length === 0) {
    return <p role="img" aria-label={props.ariaLabel ?? '排行'} className="py-6 text-center text-[11px] text-fg-mute">—</p>;
  }

  return (
    <ul role="img" aria-label={props.ariaLabel ?? '排行'} className="flex flex-col gap-1.5">
      {rows.map(function (r, i) {
        return (
          <li
            key={r.label}
            className="group/bar rounded-md px-1.5 py-1 transition-[background-color,transform] duration-150 hover:-translate-y-px hover:bg-surface-2/70"
          >
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="truncate text-[11.5px] text-fg-dim transition-colors group-hover/bar:text-fg" title={r.label}>{r.label}</span>
              <span className="shrink-0 text-[11px] tnum text-fg-mute transition-colors group-hover/bar:text-fg">{fmt(r.value)}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
              <div
                className="bar-grow h-full rounded-full transition-[filter] duration-200 group-hover/bar:brightness-110"
                style={{ width: Math.max(2, (r.value / max) * 100) + '%', background: color, animationDelay: (i * 45) + 'ms' }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** 环形占比：入场时按弧长画出来，悬停放大并显示占比 */
export function Donut(props: {
  segments: { value: number; tone?: string; label?: string }[];
  size?: number;
  thickness?: number;
  centerLabel?: string;
  centerHint?: string;
  ariaLabel?: string;
}) {
  const size = props.size ?? 112;
  const thickness = props.thickness ?? 9;
  const [hover, setHover] = useState<number | null>(null);
  const total = props.segments.reduce(function (a, s) { return a + (Number.isFinite(s.value) ? s.value : 0); }, 0);
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;
  let offset = 0;
  const arcs = props.segments.map(function (s, i) {
    const frac = total > 0 ? (Number.isFinite(s.value) ? s.value : 0) / total : 0;
    const dash = frac * c;
    const active = hover === i;
    const dimmed = hover !== null && !active;
    const el = (
      <circle
        key={i}
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={colorOf(s.tone ?? 'accent')}
        strokeWidth={active ? thickness + 4 : thickness}
        strokeDasharray={dash + ' ' + (c - dash)}
        strokeDashoffset={-offset}
        strokeLinecap="butt"
        opacity={dimmed ? 0.4 : 1}
        className="donut-arc"
        style={{
          transition: 'stroke-width 180ms cubic-bezier(0.22,1,0.36,1), opacity 180ms',
          cursor: 'pointer',
          animationDelay: (i * 90) + 'ms',
        }}
        onMouseEnter={function () { setHover(i); }}
        onMouseLeave={function () { setHover(null); }}
      />
    );
    offset += dash;
    return el;
  });

  return (
    <div role="img" aria-label={props.ariaLabel ?? '占比'} className="relative grid place-items-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-2)" strokeWidth={thickness} />
        {arcs}
      </svg>
      <div className="pointer-events-none absolute grid place-items-center text-center leading-tight">
        {hover !== null && total > 0 ? (
          <>
            <span className="text-[15px] font-semibold tnum">
              {(props.segments[hover].label ? props.segments[hover].label + ' ' : '') +
                Math.round((props.segments[hover].value / total) * 100) + '%'}
            </span>
            <span className="text-[10px] text-fg-mute">{fmt0(props.segments[hover].value)}</span>
          </>
        ) : (
          <>
            {props.centerLabel ? <span className="text-[15px] font-semibold tnum">{props.centerLabel}</span> : null}
            {props.centerHint ? <span className="text-[10px] text-fg-mute">{props.centerHint}</span> : null}
          </>
        )}
      </div>
    </div>
  );
}

