/**
 * 把带时间戳的记录分桶成序列（图表用）。
 *
 * /usage 只给总量，没有时序；但 /logs 每条都有 at —— 用它算真实的时间分布，
 * 比编一条假曲线诚实。
 */
export type Timed = { at?: number | string; time?: number | string; ts?: number | string };

function toMs(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v < 1e12 ? v * 1000 : v;
  if (typeof v === 'string' && v) {
    const n = Number(v);
    if (Number.isFinite(n) && n > 0) return n < 1e12 ? n * 1000 : n;
    const parsed = Date.parse(v);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

export function bucketSeries(rows: Timed[], buckets = 24): { counts: number[]; labels: string[] } {
  const stamps: number[] = [];
  for (const r of rows) {
    // 注意：不能写 r.at ?? r.time ?? r.ts —— 日志里的 time 是 "20:56:29" 这种纯时间串，
    // 它"存在但解析不了"，会让回退逻辑永远卡在它身上。必须逐个试，取第一个能解析的。
    let ms: number | null = null;
    for (const candidate of [r.ts, r.at, r.time]) {
      ms = toMs(candidate);
      if (ms !== null) break;
    }
    if (ms !== null) stamps.push(ms);
  }
  if (!stamps.length) return { counts: [], labels: [] };

  stamps.sort((a, b) => a - b);
  const first = stamps[0];
  const last = stamps[stamps.length - 1];
  const span = Math.max(1, last - first);
  const size = span / buckets;
  const counts = new Array(buckets).fill(0) as number[];

  for (const ms of stamps) {
    const idx = Math.min(buckets - 1, Math.floor((ms - first) / size));
    counts[idx] += 1;
  }

  const fmt = (ms: number) => new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  // 每个桶一个标签（取桶中心时刻）：悬停读数需要"这个点是什么时候"。
  // 之前只给首/中/尾三个，tooltip 只能退化成 #14。
  const labels: string[] = [];
  for (let i = 0; i < buckets; i++) labels.push(fmt(first + size * (i + 0.5)));
  return { counts, labels };
}

/** 成功率等百分比：分母为 0 时返回 undefined（不要显示 0%） */
export function ratio(numerator: number, denominator: number): number | undefined {
  if (!Number.isFinite(numerator) || !Number.isFinite(denominator) || denominator <= 0) return undefined;
  return (numerator / denominator) * 100;
}
