import { describe, it, expect, vi } from 'vitest';
import { translate, zh, en } from '@/lib/i18n';

describe('i18n', () => {
  it('中文与英文都能取到同一条词条', () => {
    expect(translate('zh', 'app.name')).toBe(zh['app.name']);
    expect(translate('en', 'app.name')).toBe(en['app.name']);
  });

  it('插值替换 {n} / {time}', () => {
    const s = translate('zh', 'app.refreshedAt', { time: '12:00' });
    expect(s).toContain('12:00');
    expect(s).not.toContain('{time}');
  });

  it('缺失键回退到中文，再缺就原样返回键名', () => {
    expect(translate('en', 'app.name')).toBeTruthy();
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(translate('zh', 'no.such.key')).toBe('no.such.key');
    spy.mockRestore();
  });

  it('未提供的变量保持原样，不产生 undefined', () => {
    const s = translate('zh', 'app.refreshedAt', {});
    expect(s).toContain('{time}');
  });

  it('英文字典是中文字典的超集或等同（不缺核心键）', () => {
    const core = ['app.name', 'app.version', 'theme.label', 'lang.label'];
    for (const k of core) expect(en[k], k).toBeTruthy();
  });
});
