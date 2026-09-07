import { useCallback, useEffect, useRef, useState } from 'react';

const AFFECT_API = '/api/v1/affect';
const KG_API = '/api/v1/knowledge-graph';
const POLL_MS = 2000;

// ── Tipos (espelham o payload de GET /api/v1/affect) ──────────────────────────
type Axis = 'ira' | 'alegria' | 'tristeza' | 'medo' | 'confianca';

interface RecentEvent {
  t: string | null;
  trigger: string;
  deltas: Record<string, number>;
  why: string;
}

interface AffectHealth {
  appraisals: number;
  updates: number;
  failures: number;
  last_error: string | null;
  last_error_at: string | null;
  last_update_at: string | null;
  pending_tasks: number;
}

interface AffectPayload {
  user_id: string;
  stats_raw: Record<Axis, number>;
  stats_live: Record<Axis, number>;
  baselines: Record<Axis, number>;
  bands: Record<Axis, string>;
  dominante: { eixo: Axis; banda: string; combo: string | null } | null;
  humor_geral: number;
  postura: { key: string; text: string; since: string | null } | null;
  last_trigger: string;
  grudge: { count?: number };
  recent: RecentEvent[];
  updated_at: string | null;
  enabled: boolean;
  health?: AffectHealth;
}

// Erro de appraisal mais novo que isto (e sem update posterior) → banner.
const HEALTH_ERROR_WINDOW_MS = 10 * 60 * 1000;

function appraiserDegraded(health?: AffectHealth): boolean {
  if (!health?.last_error_at) return false;
  const errAt = new Date(health.last_error_at).getTime();
  if (Date.now() - errAt > HEALTH_ERROR_WINDOW_MS) return false;
  // Um update bem-sucedido depois do erro significa que se recuperou.
  if (health.last_update_at && new Date(health.last_update_at).getTime() > errAt) return false;
  return true;
}

// ── Constantes visuais ─────────────────────────────────────────────────────────
const AXES: Axis[] = ['ira', 'alegria', 'tristeza', 'medo', 'confianca'];

// Gatilhos simuláveis pelo painel. É o controle mais útil dos três: postura e a
// linha "Sobre a discussão em andamento" dependem de um evento recente no
// `recent`, não do valor dos eixos — nenhum arrasto de slider liga isso.
const SIMULABLE_TRIGGERS = ['insult', 'wrong_name', 'obtuse', 'threat_coercion', 'contested', 'apology'];

type BirraAct = {
  id: string; entity_id: string | null; mecanica: string; fala: string;
  requested: Record<string, unknown>; performed: Record<string, unknown> | null;
  revert_at: string | null;
};
type BirraPayload = {
  enabled: boolean;
  active: boolean;
  episode: { armed_at: string; act_count: number; physical_acts: number } | null;
  pending: BirraAct[];
};

const AXIS_META: Record<Axis, { label: string; color: string; emoji: string }> = {
  ira:       { label: 'Ira',       color: '#f87171', emoji: '😠' },
  alegria:   { label: 'Alegria',   color: '#fbbf24', emoji: '😄' },
  tristeza:  { label: 'Tristeza',  color: '#60a5fa', emoji: '😞' },
  medo:      { label: 'Medo',      color: '#a78bfa', emoji: '😨' },
  confianca: { label: 'Confiança', color: '#34d399', emoji: '🛡️' },
};

const COMBO_META: Record<string, { label: string; emoji: string }> = {
  deboche_afiado:   { label: 'Deboche afiado',    emoji: '😏' },
  acuado_agressivo: { label: 'Acuado & agressivo', emoji: '🥷' },
  amargo:           { label: 'Amargo',             emoji: '😒' },
};

const POSTURE_META: Record<string, { label: string; emoji: string; color: string }> = {
  dobra_aposta:     { label: 'Dobrando a aposta',   emoji: '😤', color: '#f87171' },
  acuado_agressivo: { label: 'Acuado & agressivo',  emoji: '🥷', color: '#fb923c' },
  cede_terreno:     { label: 'Cedendo terreno',     emoji: '⚖️', color: '#fbbf24' },
  reavalia:         { label: 'Reavaliando posição', emoji: '🤔', color: '#60a5fa' },
  racional:         { label: 'Racional',            emoji: '🧊', color: '#34d399' },
};

const TRIGGER_COLORS: Record<string, string> = {
  wrong_name: '#f87171', insult: '#f87171', rude: '#fb923c', teasing: '#fbbf24',
  threat_coercion: '#a78bfa', contested: '#60a5fa', proven_wrong: '#60a5fa',
  conceded: '#34d399', compliment: '#34d399', warmth: '#34d399', big_win: '#34d399',
  dismissed: '#94a3b8', apology: '#2dd4bf', manipulation: '#e879f9', reset: '#64748b',
};

function fmtTime(iso: string | null | undefined) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtDateTime(iso: string | null | undefined) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

// ── Barra de um status ─────────────────────────────────────────────────────────
function StatBar({ axis, live, raw, baseline }: {
  axis: Axis; live: number; raw: number; baseline: number;
}) {
  const meta = AXIS_META[axis];
  const decaying = Math.abs(raw - live) >= 1;
  const deviation = Math.abs(live - baseline);
  // Mais saturada quanto mais longe do baseline.
  const opacity = 0.45 + Math.min(deviation / 60, 1) * 0.55;
  const disadvantage = axis === 'confianca' && live < 40;

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
        <span style={{ fontSize: 14 }}>{meta.emoji}</span>
        <span style={{ color: '#e2e8f0', fontSize: 13, fontWeight: 600, width: 80 }}>{meta.label}</span>
        <span style={{ color: meta.color, fontSize: 14, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>
          {live.toFixed(0)}
        </span>
        <span style={{ color: '#475569', fontSize: 11 }}>/ 100</span>
        {decaying && (
          <span style={{ color: '#64748b', fontSize: 11 }}>
            {raw > live ? '▼' : '▲'} de {raw.toFixed(0)}, voltando ao normal
          </span>
        )}
        {disadvantage && (
          <span style={{
            color: '#f87171', fontSize: 11, fontWeight: 700, background: 'rgba(248,113,113,0.12)',
            padding: '1px 8px', borderRadius: 8,
          }}>
            EM DESVANTAGEM
          </span>
        )}
        <span style={{ marginLeft: 'auto', color: '#475569', fontSize: 10 }}>
          repouso {baseline.toFixed(0)}
        </span>
      </div>
      <div style={{ position: 'relative', height: 10, background: '#1e2235', borderRadius: 5 }}>
        {/* fantasma do valor bruto (ainda decaindo) */}
        {decaying && (
          <div style={{
            position: 'absolute', left: 0, top: 0, bottom: 0,
            width: `${Math.min(raw, 100)}%`, background: meta.color, opacity: 0.18,
            borderRadius: 5, transition: 'width 0.6s ease',
          }} />
        )}
        {/* valor vivo */}
        <div style={{
          position: 'absolute', left: 0, top: 0, bottom: 0,
          width: `${Math.min(live, 100)}%`, background: meta.color, opacity,
          borderRadius: 5, transition: 'width 0.6s ease',
        }} />
        {/* tick do baseline */}
        <div style={{
          position: 'absolute', left: `${baseline}%`, top: -2, bottom: -2,
          width: 2, background: '#94a3b8', opacity: 0.7, borderRadius: 1,
        }} title={`baseline ${baseline}`} />
      </div>
    </div>
  );
}

// ── Painel ─────────────────────────────────────────────────────────────────────
export default function AffectPanel() {
  const [users, setUsers] = useState<string[]>([]);
  const [userId, setUserId] = useState('');
  const [data, setData] = useState<AffectPayload | null>(null);
  const [error, setError] = useState('');
  const [resetting, setResetting] = useState(false);
  const [flash, setFlash] = useState('');
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Ajuste manual (docs/modo-birra-plan.md §15): o polling PAUSA enquanto edita,
  // senão o valor volta sozinho embaixo do dedo.
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<string, number>>({});
  const [draftGrudge, setDraftGrudge] = useState(0);
  const [applying, setApplying] = useState(false);
  const [birra, setBirra] = useState<BirraPayload | null>(null);

  useEffect(() => {
    fetch(`${KG_API}/users`)
      .then(r => (r.ok ? r.json() : { user_ids: [] }))
      .then((d: { user_ids: string[] }) => {
        setUsers(d.user_ids ?? []);
        if (d.user_ids?.length > 0) setUserId(d.user_ids[0]);
      })
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
    if (!userId) return;
    try {
      const res = await fetch(`${AFFECT_API}?user_id=${encodeURIComponent(userId)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const payload: AffectPayload = await res.json();
      setData(payload);
      setError('');
      return payload;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'erro ao carregar');
    }
    return null;
  }, [userId]);

  const loadBirra = useCallback(async () => {
    if (!userId) return;
    try {
      const res = await fetch(`${AFFECT_API}/birra?user_id=${encodeURIComponent(userId)}`);
      if (res.ok) setBirra(await res.json());
    } catch { /* card some, o resto do painel segue */ }
  }, [userId]);

  // Dois efeitos separados de propósito. Juntos, entrar em modo de ajuste (que
  // muda `editing`) re-disparava o reset de troca de usuário: `setData(null)`
  // pintava "carregando…" e `setEditing(false)` desligava o modo no mesmo tique.
  // Reset pertence à troca de USUÁRIO; o polling é que olha `editing`.
  useEffect(() => {
    setData(null);
    setBirra(null);
    setEditing(false);
    load();
    loadBirra();
  }, [load, loadBirra]);

  useEffect(() => {
    // Pausa o polling durante a edição — senão o valor volta sozinho embaixo do dedo.
    if (editing) return;
    const iv = setInterval(() => { load(); loadBirra(); }, POLL_MS);
    return () => clearInterval(iv);
  }, [load, loadBirra, editing]);

  const startEdit = () => {
    if (!data) return;
    setDraft({ ...data.stats_live });
    setDraftGrudge(data.grudge?.count ?? 0);
    setEditing(true);
  };

  const cancelEdit = () => { setEditing(false); load(); };

  const postSet = async (body: Record<string, unknown>, msg: string) => {
    setApplying(true);
    try {
      const res = await fetch(`${AFFECT_API}/set?user_id=${encodeURIComponent(userId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFlash(msg);
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(''), 4000);
      const fresh = await load();
      await loadBirra();
      return fresh;
    } catch (e) {
      setError(e instanceof Error ? e.message : 'erro ao gravar');
    } finally {
      setApplying(false);
    }
    return null;
  };

  const applyEdit = async () => {
    await postSet(
      { stats: draft, grudge_count: draftGrudge, reason: 'ajuste manual pelo painel' },
      'Estado ajustado manualmente.',
    );
    setEditing(false);
  };

  const simulateTrigger = async (trigger: string) => {
    const fresh = await postSet(
      { trigger, reason: `gatilho '${trigger}' simulado pelo painel` },
      `Gatilho '${trigger}' registrado.`,
    );
    // Sem isto os sliders ficariam no valor antigo depois do gatilho mexer no
    // estado — e o painel pareceria não ter feito nada.
    if (fresh) {
      setDraft({ ...fresh.stats_live });
      setDraftGrudge(fresh.grudge?.count ?? 0);
    }
  };

  const endBirra = async () => {
    if (!userId) return;
    if (!window.confirm('Encerrar a birra e desfazer os atos pendentes?')) return;
    try {
      const res = await fetch(`${AFFECT_API}/birra/end?user_id=${encodeURIComponent(userId)}`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const out = await res.json();
      setFlash(`Birra encerrada — ${out.reverted ?? 0} ato(s) desfeito(s).`);
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(''), 4000);
      await loadBirra();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'erro ao encerrar');
    }
  };

  const doReset = async () => {
    if (!userId || resetting) return;
    if (!window.confirm('Resetar o estado emocional do Neo para os baselines?')) return;
    setResetting(true);
    try {
      const res = await fetch(`${AFFECT_API}/reset?user_id=${encodeURIComponent(userId)}`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFlash('Estado resetado para os baselines.');
      if (flashTimer.current) clearTimeout(flashTimer.current);
      flashTimer.current = setTimeout(() => setFlash(''), 4000);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'erro no reset');
    } finally {
      setResetting(false);
    }
  };

  const card: React.CSSProperties = {
    background: '#13161f', border: '1px solid #1e2235', borderRadius: 12, padding: 20,
  };

  const dominante = data?.dominante ?? null;
  const combo = dominante?.combo ? COMBO_META[dominante.combo] : null;
  const domMeta = dominante ? AXIS_META[dominante.eixo] : null;
  const posture = data?.postura ? POSTURE_META[data.postura.key] : null;
  const humor = data?.humor_geral ?? 0;
  const humorLabel = humor <= -0.15 ? 'pesado' : humor >= 0.35 ? 'leve' : 'neutro';
  const humorColor = humor <= -0.15 ? '#f87171' : humor >= 0.35 ? '#34d399' : '#94a3b8';
  const grudgeCount = data?.grudge?.count ?? 0;

  return (
    <div style={{ padding: 24, color: '#e2e8f0', fontFamily: 'system-ui, sans-serif', maxWidth: 900 }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700, margin: 0 }}>🎭 Emoções do Neo</h1>
        <select
          value={userId}
          onChange={e => setUserId(e.target.value)}
          style={{
            background: '#1e2235', color: '#e2e8f0', border: '1px solid #2d3350',
            borderRadius: 8, padding: '6px 10px', fontSize: 13,
          }}
        >
          {users.length === 0 && <option value="">sem usuários</option>}
          {users.map(uid => (
            <option key={uid} value={uid}>{uid.slice(0, 8)}…</option>
          ))}
        </select>
        <span style={{ color: '#34d399', fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{
            width: 8, height: 8, borderRadius: 4, background: error ? '#f87171' : '#34d399',
            display: 'inline-block',
          }} />
          {error ? `erro: ${error}` : 'ao vivo (2s)'}
        </span>
        {data && data.enabled === false && (
          <span style={{
            color: '#fbbf24', fontSize: 12, background: 'rgba(251,191,36,0.1)',
            padding: '3px 10px', borderRadius: 8,
          }}>
            ⚠ NEO_AFFECT_ENABLED=false — estado não é atualizado por conversas
          </span>
        )}
      </div>

      {flash && (
        <div style={{
          background: 'rgba(52,211,153,0.1)', border: '1px solid #34d399', color: '#34d399',
          borderRadius: 8, padding: '8px 14px', fontSize: 13, marginBottom: 16,
        }}>
          {flash}
        </div>
      )}

      {data && appraiserDegraded(data.health) && (
        <div style={{
          background: 'rgba(248,113,113,0.08)', border: '1px solid #f87171', color: '#f87171',
          borderRadius: 8, padding: '8px 14px', fontSize: 13, marginBottom: 16,
        }}>
          ⚠ Appraiser degradado — última falha {fmtDateTime(data.health!.last_error_at)}
          {data.health!.last_error ? `: ${data.health!.last_error}` : ''}
          {' '}(floors determinísticos continuam ativos)
        </div>
      )}

      {!data ? (
        <div style={{ color: '#64748b', fontSize: 14 }}>carregando…</div>
      ) : (
        <>
          {/* Card dominante + humor */}
          <div style={{ display: 'flex', gap: 16, marginBottom: 16, flexWrap: 'wrap' }}>
            <div style={{ ...card, flex: '1 1 300px', display: 'flex', alignItems: 'center', gap: 16 }}>
              <span style={{ fontSize: 40 }}>
                {combo ? combo.emoji : dominante ? AXIS_META[dominante.eixo].emoji : '🙂'}
              </span>
              <div>
                <div style={{ fontSize: 16, fontWeight: 700, color: combo || !domMeta ? '#e2e8f0' : domMeta.color }}>
                  {combo
                    ? combo.label
                    : dominante
                      ? `${AXIS_META[dominante.eixo].label} ${dominante.banda === 'alto' ? 'alta' : 'média'}`
                      : 'De boa'}
                </div>
                <div style={{ color: '#64748b', fontSize: 12, marginTop: 2 }}>
                  último gatilho: <span style={{ color: TRIGGER_COLORS[data.last_trigger] ?? '#94a3b8' }}>
                    {data.last_trigger}
                  </span>
                  {' · '}atualizado {fmtDateTime(data.updated_at)}
                </div>
              </div>
            </div>
            <div style={{ ...card, flex: '0 1 220px' }}>
              <div style={{ color: '#64748b', fontSize: 11, textTransform: 'uppercase', letterSpacing: 1 }}>
                Humor geral
              </div>
              <div style={{ color: humorColor, fontSize: 18, fontWeight: 700, marginTop: 4 }}>{humorLabel}</div>
              <div style={{ color: '#475569', fontSize: 11, marginTop: 2 }}>
                valência {humor.toFixed(2)}
                {grudgeCount > 0 && (
                  <span style={{ color: '#fb923c' }}> · rancor ativo ×{grudgeCount}</span>
                )}
              </div>
            </div>
          </div>

          {/* Postura em discussão */}
          {data.postura && posture && (
            <div style={{
              ...card, marginBottom: 16, borderColor: posture.color,
              display: 'flex', gap: 12, alignItems: 'flex-start',
            }}>
              <span style={{ fontSize: 24 }}>{posture.emoji}</span>
              <div>
                <div style={{ color: posture.color, fontSize: 13, fontWeight: 700 }}>
                  Discussão em andamento — {posture.label}
                </div>
                <div style={{ color: '#94a3b8', fontSize: 13, marginTop: 4, lineHeight: 1.5 }}>
                  {data.postura.text}
                </div>
              </div>
            </div>
          )}

          {/* Barras — modo leitura ou modo ajuste */}
          <div style={{ ...card, marginBottom: 16 }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14,
            }}>
              <span style={{
                color: '#64748b', fontSize: 11, textTransform: 'uppercase', letterSpacing: 1,
              }}>
                {editing ? 'Ajuste manual — polling pausado' : 'Estado'}
              </span>
              <button
                onClick={() => (editing ? cancelEdit() : startEdit())}
                style={{
                  marginLeft: 'auto', background: editing ? '#2d3350' : '#1e2235',
                  color: '#e2e8f0', border: '1px solid #2d3350', borderRadius: 8,
                  padding: '4px 12px', fontSize: 12, cursor: 'pointer',
                }}
              >
                {editing ? 'Cancelar' : '✎ Ajustar'}
              </button>
              {editing && (
                <button
                  onClick={applyEdit}
                  disabled={applying}
                  style={{
                    background: '#34d399', color: '#0b0e16', border: 'none', borderRadius: 8,
                    padding: '4px 14px', fontSize: 12, fontWeight: 700, cursor: 'pointer',
                  }}
                >
                  {applying ? 'aplicando…' : 'Aplicar'}
                </button>
              )}
            </div>

            {AXES.map(axis => (
              editing ? (
                <div key={axis} style={{ marginBottom: 12 }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 4 }}>
                    <span style={{ fontSize: 14 }}>{AXIS_META[axis].emoji}</span>
                    <span style={{ color: '#e2e8f0', fontSize: 13, fontWeight: 600, width: 80 }}>
                      {AXIS_META[axis].label}
                    </span>
                    <span style={{
                      color: AXIS_META[axis].color, fontSize: 14, fontWeight: 700,
                      fontVariantNumeric: 'tabular-nums',
                    }}>
                      {Math.round(draft[axis] ?? 0)}
                    </span>
                    {Math.round(draft[axis] ?? 0) !== Math.round(data.stats_live[axis] ?? 0) && (
                      <span style={{ color: '#fbbf24', fontSize: 11 }}>
                        ← era {Math.round(data.stats_live[axis] ?? 0)}
                      </span>
                    )}
                    <span style={{ marginLeft: 'auto', color: '#475569', fontSize: 10 }}>
                      repouso {(data.baselines[axis] ?? 0).toFixed(0)}
                    </span>
                  </div>
                  <input
                    type="range" min={0} max={100} step={1}
                    value={draft[axis] ?? 0}
                    onChange={e => setDraft(d => ({ ...d, [axis]: Number(e.target.value) }))}
                    style={{ width: '100%', accentColor: AXIS_META[axis].color }}
                  />
                </div>
              ) : (
                <StatBar
                  key={axis}
                  axis={axis}
                  live={data.stats_live[axis] ?? 0}
                  raw={data.stats_raw[axis] ?? 0}
                  baseline={data.baselines[axis] ?? 0}
                />
              )
            ))}

            {editing && (
              <div style={{ borderTop: '1px solid #1e2235', marginTop: 8, paddingTop: 12 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                  <span style={{ color: '#e2e8f0', fontSize: 13, fontWeight: 600 }}>
                    Ofensas na janela (24h)
                  </span>
                  <input
                    type="number" min={0} max={20}
                    value={draftGrudge}
                    onChange={e => setDraftGrudge(Math.max(0, Number(e.target.value)))}
                    style={{
                      background: '#1e2235', color: '#e2e8f0', border: '1px solid #2d3350',
                      borderRadius: 6, padding: '4px 8px', width: 64, fontSize: 13,
                    }}
                  />
                  <span style={{ color: '#64748b', fontSize: 11 }}>
                    a birra exige ira ≥ 60 <b>e</b> ≥ 2 ofensas — só o slider de ira não arma
                  </span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <span style={{ color: '#64748b', fontSize: 12 }}>Simular gatilho:</span>
                  {SIMULABLE_TRIGGERS.map(t => (
                    <button
                      key={t}
                      onClick={() => simulateTrigger(t)}
                      style={{
                        background: '#1e2235', color: TRIGGER_COLORS[t] ?? '#94a3b8',
                        border: '1px solid #2d3350', borderRadius: 8, padding: '3px 10px',
                        fontSize: 12, cursor: 'pointer',
                      }}
                    >
                      {t}
                    </button>
                  ))}
                </div>
                <div style={{ color: '#475569', fontSize: 11, marginTop: 8 }}>
                  O gatilho é o único controle que liga a <b>postura</b> — ela depende de um
                  evento recente, não do valor dos eixos.
                </div>
              </div>
            )}
          </div>

          {/* Card de birra */}
          {birra && (birra.active || birra.pending.length > 0) && (
            <div style={{
              ...card, marginBottom: 16, borderColor: '#f87171',
              background: 'rgba(248,113,113,0.06)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <span style={{ fontSize: 20 }}>😤</span>
                <span style={{ fontSize: 15, fontWeight: 700, color: '#f87171' }}>
                  Modo Birra {birra.active ? 'ATIVO' : '— atos pendentes'}
                </span>
                <button
                  onClick={endBirra}
                  style={{
                    marginLeft: 'auto', background: '#f87171', color: '#0b0e16', border: 'none',
                    borderRadius: 8, padding: '4px 14px', fontSize: 12, fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  Encerrar e desfazer
                </button>
              </div>
              {birra.episode && (
                <div style={{ color: '#94a3b8', fontSize: 12, marginBottom: 8 }}>
                  desde {fmtTime(birra.episode.armed_at)} · {birra.episode.act_count} ato(s),
                  {' '}{birra.episode.physical_acts} físico(s)
                </div>
              )}
              {birra.pending.map(a => (
                <div key={a.id} style={{ fontSize: 12, color: '#e2e8f0', padding: '3px 0' }}>
                  <b>{a.entity_id}</b> — {a.mecanica} ({a.fala})
                  {a.revert_at && (
                    <span style={{ color: '#fbbf24' }}> · desfaz em {fmtTime(a.revert_at)}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Timeline */}
          <div style={{ ...card, marginBottom: 16 }}>
            <div style={{
              color: '#64748b', fontSize: 11, textTransform: 'uppercase',
              letterSpacing: 1, marginBottom: 12,
            }}>
              Eventos recentes
            </div>
            {data.recent.length === 0 ? (
              <div style={{ color: '#475569', fontSize: 13 }}>nenhum evento ainda</div>
            ) : (
              [...data.recent].reverse().map((ev, i) => (
                <div key={i} style={{
                  display: 'flex', gap: 10, alignItems: 'baseline', padding: '6px 0',
                  borderBottom: i < data.recent.length - 1 ? '1px solid #1e2235' : 'none',
                  fontSize: 13,
                }}>
                  <span style={{ color: '#475569', fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}>
                    {fmtTime(ev.t)}
                  </span>
                  <span style={{
                    color: TRIGGER_COLORS[ev.trigger] ?? '#94a3b8', fontWeight: 600,
                    background: 'rgba(148,163,184,0.08)', padding: '1px 8px',
                    borderRadius: 6, flexShrink: 0,
                  }}>
                    {ev.trigger}
                  </span>
                  <span style={{ color: '#64748b', flexShrink: 0 }}>
                    {Object.entries(ev.deltas ?? {})
                      .map(([axis, d]) => `${AXIS_META[axis as Axis]?.label ?? axis} ${d > 0 ? '+' : ''}${d}`)
                      .join(' · ')}
                  </span>
                  <span style={{ color: '#94a3b8', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {ev.why}
                  </span>
                </div>
              ))
            )}
          </div>

          {/* Reset */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <button
              onClick={doReset}
              disabled={resetting || !userId}
              style={{
                background: 'transparent', border: '1px solid #7c3aed', color: '#a78bfa',
                borderRadius: 8, padding: '8px 18px', fontSize: 13, fontWeight: 600,
                cursor: resetting ? 'wait' : 'pointer',
              }}
            >
              ⟲ Resetar para baseline
            </button>
            {data.health && (
              <span style={{ color: '#475569', fontSize: 11 }}>
                appraisals {data.health.appraisals} · updates {data.health.updates} ·
                falhas {data.health.failures} · em voo {data.health.pending_tasks}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
