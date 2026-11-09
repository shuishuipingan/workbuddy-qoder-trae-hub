import { useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { hubRouting, hubRoutingSave, hubRoutePreview } from '@/lib/api';
import type { RouteRule, RoutingDoc } from '@/lib/api';

const S: Record<string, React.CSSProperties> = {
  page: { display: 'flex', flexDirection: 'column', gap: 16 },
  head: { display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12 },
  h1: { fontSize: 19, fontWeight: 600, color: 'var(--fg)', margin: 0 },
  sub: { fontSize: 12.5, color: 'var(--fg-mute)', margin: '4px 0 0', maxWidth: 680, lineHeight: 1.5 },
  row: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' as const },
  card: { background: 'var(--surface)', border: '1px solid var(--line-faint)', borderRadius: 12, padding: 14 },
  btn: { appearance: 'none' as const, border: '1px solid var(--line)', background: 'var(--surface-2)',
         color: 'var(--fg)', borderRadius: 8, padding: '6px 12px', fontSize: 12.5, cursor: 'pointer' },
  btnPrimary: { appearance: 'none' as const, border: '1px solid transparent', background: 'var(--accent)',
                color: 'var(--accent-fg)', borderRadius: 8, padding: '6px 14px', fontSize: 12.5, cursor: 'pointer' },
  input: { background: 'var(--surface-2)', border: '1px solid var(--line-faint)', borderRadius: 8,
           color: 'var(--fg)', padding: '6px 10px', fontSize: 12.5, outline: 'none' },
  hop: { display: 'flex', gap: 6, alignItems: 'center', fontSize: 12,
         color: 'var(--fg-dim)', background: 'var(--surface-2)', border: '1px solid var(--line-faint)',
         borderRadius: 999, padding: '3px 10px' },
};

type Hop = { engine: string; realm: string };
const ENGINES = ['wb', 'qd', 'trae'];
const REALMS = ['cn', 'intl'];

function newRule(): RouteRule {
  return {
    id: 'r' + Date.now().toString(36), name: '新规则', enabled: true, priority: 100,
    match: { models: [], realms: [], engines: [] },
    action: { chain: [{ engine: 'wb', realm: 'cn' }], stop_on_miss: false },
  };
}

export default function RoutingView(props: { t?: (k: string) => string }) {
  const t = props.t || ((k: string) => k);
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ['hub-routing'], queryFn: hubRouting, staleTime: 10_000 });
  const doc: RoutingDoc = q.data || { rules: [], policy: { strategy: 'first-available', session_affinity: true, max_fallbacks: 2 }, revision: 0 };
  const [draft, setDraft] = useState<RouteRule[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [model, setModel] = useState('');
  const [pv, setPv] = useState<{ hops: { engine: string; realm: string; ready: number; holds_model: boolean }[]; matched_name: string | null } | null>(null);

  const rules = draft ?? doc.rules;
  const dirty = draft !== null;

  const update = (id: string, fn: (r: RouteRule) => void) => {
    const next = rules.map((r) => { if (r.id !== id) return r; const copy = JSON.parse(JSON.stringify(r)) as RouteRule; fn(copy); return copy; });
    setDraft(next);
  };

  const save = async () => {
    setBusy(true);
    try {
      await hubRoutingSave({ rules, policy: doc.policy, revision: doc.revision });
      setDraft(null); setMsg(t('routing.saved'));
      await qc.invalidateQueries({ queryKey: ['hub-routing'] });
    } catch (e) { setMsg(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const runPreview = async () => {
    if (!model.trim()) return;
    setBusy(true);
    try {
      const res = await hubRoutePreview(model.trim());
      setPv({ hops: res.hops || [], matched_name: res.matched_name });
    } catch (e) { setMsg(String((e as Error).message || e)); }
    finally { setBusy(false); }
  };

  const header = useMemo(() => (
    <div style={S.head}>
      <div>
        <h1 style={S.h1}>{t('routing.title')}</h1>
        <p style={S.sub}>{t('routing.subtitle')}</p>
      </div>
      <div style={S.row}>
        <button style={S.btn} onClick={() => setDraft([...rules, newRule()])}>{t('routing.new')}</button>
        <button style={S.btnPrimary} disabled={!dirty || busy} onClick={save}>{t('routing.save')}</button>
      </div>
    </div>
  ), [rules, dirty, busy, t]);

  return (
    <div style={S.page}>
      {header}
      {msg ? <div style={{ ...S.card, fontSize: 12.5, color: 'var(--fg-dim)' }}>{msg}</div> : null}

      <div style={{ ...S.card, display: 'flex', gap: 8, alignItems: 'center' }}>
        <input style={{ ...S.input, flex: 1 }} placeholder={t('routing.previewPh')}
               value={model} onChange={(e) => setModel(e.target.value)} />
        <button style={S.btn} disabled={busy} onClick={runPreview}>{t('routing.preview')}</button>
      </div>

      {pv ? (
        <div style={S.card}>
          <div style={{ fontSize: 12, color: 'var(--fg-mute)', marginBottom: 8 }}>
            {t('routing.hops')}{pv.matched_name ? ' · ' + pv.matched_name : ''}
          </div>
          <div style={S.row}>
            {pv.hops.length === 0 ? <span style={{ fontSize: 12, color: 'var(--fg-mute)' }}>—</span> : null}
            {pv.hops.map((h, i) => (
              <span key={i} style={{ ...S.hop, opacity: h.holds_model ? 1 : 0.5 }}>
                {i + 1}. {h.engine}/{h.realm} · {h.ready}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      {rules.length === 0 ? (
        <div style={{ ...S.card, fontSize: 12.5, color: 'var(--fg-mute)' }}>{t('routing.empty')}</div>
      ) : null}

      {rules.map((rule) => {
        const hops: Hop[] = rule.action.chain || [];
        return (
          <div key={rule.id} style={S.card}>
            <div style={S.row}>
              <input style={{ ...S.input, width: 160 }} value={rule.name}
                     onChange={(e) => update(rule.id, (r) => { r.name = e.target.value; })} />
              <input style={{ ...S.input, width: 74 }} value={rule.priority} type="number"
                     onChange={(e) => update(rule.id, (r) => { r.priority = Number(e.target.value) || 100; })} />
              <label style={{ fontSize: 12, color: 'var(--fg-dim)', display: 'flex', gap: 6, alignItems: 'center' }}>
                <input type="checkbox" checked={rule.enabled}
                       onChange={(e) => update(rule.id, (r) => { r.enabled = e.target.checked; })} />
                {rule.enabled ? 'on' : 'off'}
              </label>
              <button style={{ ...S.btn, marginLeft: 'auto' }}
                      onClick={() => setDraft(rules.filter((x) => x.id !== rule.id))}>{t('routing.delete')}</button>
            </div>

            <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--fg-mute)' }}>{t('routing.models')}</div>
            <input style={{ ...S.input, width: '100%', marginTop: 4 }}
                   value={rule.match.models.join(', ')}
                   onChange={(e) => update(rule.id, (r) => {
                     r.match.models = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
                   })} />

            <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--fg-mute)' }}>{t('routing.chain')}</div>
            <div style={{ ...S.row, marginTop: 6 }}>
              {hops.map((hop, idx) => (
                <span key={idx} style={S.hop}>
                  {idx + 1}.
                  <select value={hop.engine} style={{ ...S.input, padding: '2px 4px' }}
                          onChange={(e) => update(rule.id, (r) => { r.action.chain[idx].engine = e.target.value; })}>
                    {ENGINES.map((x) => <option key={x} value={x}>{x}</option>)}
                  </select>
                  <select value={hop.realm} style={{ ...S.input, padding: '2px 4px' }}
                          onChange={(e) => update(rule.id, (r) => { r.action.chain[idx].realm = e.target.value; })}>
                    <option value="">any</option>
                    {REALMS.map((x) => <option key={x} value={x}>{x}</option>)}
                  </select>
                  <button style={{ appearance: 'none', border: 0, background: 'transparent', color: 'var(--fg-mute)', cursor: 'pointer' }}
                          onClick={() => update(rule.id, (r) => { r.action.chain.splice(idx, 1); if (!r.action.chain.length) r.action.chain.push({ engine: 'wb', realm: 'cn' }); })}>×</button>
                </span>
              ))}
              <button style={S.btn}
                      onClick={() => update(rule.id, (r) => { r.action.chain.push({ engine: 'wb', realm: 'cn' }); })}>{t('routing.addHop')}</button>
            </div>

            <label style={{ marginTop: 10, display: 'flex', gap: 6, alignItems: 'center', fontSize: 12, color: 'var(--fg-dim)' }}>
              <input type="checkbox" checked={rule.action.stop_on_miss}
                     onChange={(e) => update(rule.id, (r) => { r.action.stop_on_miss = e.target.checked; })} />
              {t('routing.stopOnMiss')}
            </label>
          </div>
        );
      })}
    </div>
  );
}
