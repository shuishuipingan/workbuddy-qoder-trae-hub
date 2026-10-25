import { describe, it, expect } from 'vitest';
import { bucketSeries, ratio } from './series';

describe('bucketSeries', () => {
  it('空输入返回空序列（不产生 NaN）', () => {
    expect(bucketSeries([])).toEqual({ counts: [], labels: [] });
    expect(bucketSeries([{ at: '' }, {}])).toEqual({ counts: [], labels: [] });
  });

  it('按时间顺序分桶，总数守恒', () => {
    const base = 1_700_000_000;
    const rows = [];
    for (let i = 0; i < 12; i++) rows.push({ at: base + i * 60 });
    const r = bucketSeries(rows, 4);
    expect(r.counts.length).toBe(4);
    expect(r.counts.reduce((a, b) => a + b, 0)).toBe(12);
    // 标签与桶一一对应（tooltip 要显示"这个点是什么时候"），
    // 之前只有首/中/尾三个，悬停读数只能退化成 #N。
    expect(r.labels.length).toBe(r.counts.length);
    expect(r.labels[0]).toMatch(/^\d{2}:\d{2}$/);
  });

  it('支持秒级与毫秒级时间戳混用', () => {
    const rows = [{ at: 1_700_000_000 }, { at: 1_700_000_060_000 }];
    const r = bucketSeries(rows, 2);
    expect(r.counts.reduce((a, b) => a + b, 0)).toBeGreaterThanOrEqual(1);
});

  it('纯时间串（20:56:29）解析不了时回退到 ts', () => {
    const rows = [
      { time: '20:56:29', ts: '2026-10-20 20:56:29' },
      { time: '20:57:29', ts: '2026-10-20 20:57:29' },
    ];
    const r = bucketSeries(rows, 2);
    expect(r.counts.reduce((a, b) => a + b, 0)).toBe(2);
  });
});

describe('ratio', () => {
  it('分母为 0 时返回 undefined 而不是 0', () => {
    expect(ratio(0, 0)).toBeUndefined();
    expect(ratio(1, 0)).toBeUndefined();
  });
  it('正常计算百分比', () => {
    expect(ratio(1, 4)).toBe(25);
  });
});
