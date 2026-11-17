import { useState, useEffect } from 'react';

const API = '/api/v1/context';
const KG_API = '/api/v1/knowledge-graph';

// ── Type colors ───────────────────────────────────────────────────────
const TYPE_COLORS: Record<string, string> = {
  person:           '#818cf8',
  project:          '#60a5fa',
  company:          '#34d399',
  device:           '#f472b6',
  technology:       '#22d3ee',
  document:         '#fbbf24',
  workout:          '#a78bfa',
  exercise:         '#c084fc',
  muscle_group:     '#f87171',
  meal:             '#f472b6',
  food:             '#a3e635',
  supplement:       '#22d3ee',
  health_state:     '#34d399',
  restriction:      '#f87171',
  routine:          '#fbbf24',
  goal:             '#fb923c',
  preference:       '#2dd4bf',
  vehicle:          '#60a5fa',
  property:         '#fbbf24',
  account:          '#60a5fa',
  pet:              '#f472b6',
  place:            '#60a5fa',
  software:         '#22d3ee',
};
const DEFAULT_COLOR = '#94a3b8';

function typeColor(t: string) { return TYPE_COLORS[t] ?? DEFAULT_COLOR; }

// ── Sensitivity badge ─────────────────────────────────────────────────
const SENS: Record<string, { bg: string; fg: string }> = {
  public:    { bg: '#065f4622', fg: '#34d399' },
  internal:  { bg: '#1e3a5f44', fg: '#60a5fa' },
  private:   { bg: '#312e8144', fg: '#a78bfa' },
  sensitive: { bg: '#9f123944', fg: '#f87171' },
  secret:    { bg: '#7f1d1d44', fg: '#fca5a5' },
};

function SensTag({ level }: { level: string }) {
  const s = SENS[level] ?? { bg: '#1e293b', fg: '#94a3b8' };
  return (
    <span style={{
      background: s.bg,
      color: s.fg,
      fontSize: 10,
      fontWeight: 700,
      padding: '2px 6px',
      borderRadius: 999,
      letterSpacing: '0.04em',
      textTransform: 'uppercase' as const,
      flexShrink: 0,
    }}>
      {level}
    </span>
  );
}

// ── Types ─────────────────────────────────────────────────────────────
interface CandidateTrace {
  layer: string;
  source: string;
  key: string;
  pin: boolean;
  est_tokens: number;
  relevance: number;
  final_score: number;
  included: boolean;
  skip_reason: string;
}

interface RecoveredMemory {
  content: string;
  source: string;
  score: number;
}

interface DecisionInfo {
  intent: string;
  needs_memory: boolean;
  knowledge_keywords: string[];
}

interface PromptPreviewResult {
  system_prompt: string;
  trace: CandidateTrace[];
  prompt_tokens_est: number;
  decision: DecisionInfo;
  contexts_found: Neighborhood[];
  context_text: string;
  recovered: RecoveredMemory[];
  keywords: string[];
}

interface EntityRecord {
  id: string;
  name: string;
  type: string;
  aliases: string[];
  confidence: number;
  sensitivity: string;
}

interface FieldRecord {
  id: string;
  key: string;
  value: unknown;
  unit: string | null;
  sensitivity: string;
}

interface RelationRecord {
  id: string;
  from_entity_id: string;
  relation_type: string;
  to_entity_id: string;
  sensitivity: string;
  from_name: string | null;
  from_type: string | null;
  to_name: string | null;
  to_type: string | null;
}

interface Neighborhood {
  entity: EntityRecord;
  fields: FieldRecord[];
  relations: RelationRecord[];
}

interface ContextResult {
  contexts_found: Neighborhood[];
  context_text: string;
  metadata: Record<string, unknown>;
}

// ── Entity neighborhood card ──────────────────────────────────────────
function NeighborhoodCard({ n }: { n: Neighborhood }) {
  const [open, setOpen] = useState(true);
  const { entity, fields, relations } = n;
  const tc = typeColor(entity.type);

  return (
    <div style={{
      background: '#13161f',
      border: '1px solid #1e2235',
      borderLeft: `3px solid ${tc}`,
      borderRadius: 8,
      overflow: 'hidden',
      marginBottom: 8,
    }}>
      <div
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '10px 14px',
          cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        <span style={{
          background: tc + '22',
          color: tc,
          fontSize: 10,
          fontWeight: 700,
          padding: '2px 7px',
          borderRadius: 4,
          letterSpacing: '0.04em',
          textTransform: 'uppercase' as const,
          flexShrink: 0,
        }}>
          {entity.type}
        </span>
        <span style={{ color: '#e2e8f0', fontWeight: 600, fontSize: 14 }}>
          {entity.name}
        </span>
        {entity.aliases.length > 0 && (
          <span style={{ color: '#475569', fontSize: 12 }}>
            aka {entity.aliases.join(', ')}
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
          <SensTag level={entity.sensitivity} />
          <span style={{ color: '#475569', fontSize: 11 }}>
            {fields.length}f · {relations.length}r
          </span>
          <span style={{ color: '#475569', fontSize: 12 }}>{open ? '▲' : '▼'}</span>
        </span>
      </div>

      {open && (
        <div style={{ padding: '0 14px 14px', borderTop: '1px solid #1e2235' }}>
          {fields.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div style={sectionLabel}>Campos</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {fields.map(f => (
                  <div key={f.id} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 13 }}>
                    <span style={{ color: '#7c3aed', minWidth: 130, flexShrink: 0 }}>{f.key}</span>
                    <span style={{ color: '#e2e8f0', flex: 1, wordBreak: 'break-word' as const }}>
                      {String(f.value)}{f.unit ? ` ${f.unit}` : ''}
                    </span>
                    <SensTag level={f.sensitivity} />
                  </div>
                ))}
              </div>
            </div>
          )}

          {relations.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <div style={sectionLabel}>Relações</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
                {relations.map(r => {
                  const isFrom = r.from_entity_id === entity.id;
                  const targetName = isFrom ? r.to_name : r.from_name;
                  const targetType = isFrom ? r.to_type : r.from_type;
                  const tc2 = targetType ? typeColor(targetType) : DEFAULT_COLOR;
                  return (
                    <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                      <span style={{ color: isFrom ? '#22d3ee' : '#f472b6', fontSize: 13, flexShrink: 0 }}>
                        {isFrom ? '→' : '←'}
                      </span>
                      <span style={{ color: '#94a3b8', minWidth: 100, flexShrink: 0 }}>{r.relation_type}</span>
                      <span style={{ color: tc2, fontWeight: 500 }}>
                        {targetName ?? (isFrom ? r.to_entity_id : r.from_entity_id)}
                      </span>
                      {targetType && (
                        <span style={{ color: '#475569', fontSize: 11 }}>[{targetType}]</span>
                      )}
                      <SensTag level={r.sensitivity} />
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {fields.length === 0 && relations.length === 0 && (
            <div style={{ color: '#475569', fontSize: 12, fontStyle: 'italic', marginTop: 10 }}>
              Sem campos ou relações
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Shared styles ─────────────────────────────────────────────────────
const sectionLabel: React.CSSProperties = {
  color: '#64748b',
  fontSize: 10,
  fontWeight: 700,
  textTransform: 'uppercase',
  letterSpacing: '0.06em',
  marginBottom: 6,
};

const inputStyle: React.CSSProperties = {
  background: '#0f1117',
  border: '1px solid #1e2235',
  borderRadius: 8,
  padding: '8px 12px',
  color: '#e2e8f0',
  fontSize: 13,
  outline: 'none',
  width: '100%',
  boxSizing: 'border-box',
  fontFamily: 'inherit',
};

// ── Main panel ────────────────────────────────────────────────────────
export default function PKSContextDebugPanel() {
  const [users, setUsers] = useState<string[]>([]);
  const [userId, setUserId] = useState('');
  const [userInput, setUserInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ContextResult | null>(null);
  const [preview, setPreview] = useState<PromptPreviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copiedPrompt, setCopiedPrompt] = useState(false);
  const [showTrace, setShowTrace] = useState(false);

  useEffect(() => {
    fetch(`${KG_API}/users`)
      .then(r => r.ok ? r.json() : { user_ids: [] })
      .then((d: { user_ids: string[] }) => {
        setUsers(d.user_ids ?? []);
        if (d.user_ids?.length > 0) setUserId(d.user_ids[0]);
      })
      .catch(() => {});
  }, []);

  const handleSubmit = async () => {
    if (!userId.trim() || !userInput.trim()) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setPreview(null);
    const body = JSON.stringify({ user_id: userId.trim(), user_input: userInput.trim() });
    const headers = { 'Content-Type': 'application/json' };
    try {
      // Uma chamada só: o /prompt-preview agora reproduz o turno real inteiro
      // (classify → recover_context → register → build_prompt), read-only, e
      // devolve entidades, memória semântica e a decisão junto com o prompt.
      const resPreview = await fetch(`${API}/prompt-preview`, { method: 'POST', headers, body });
      if (!resPreview.ok) throw new Error(`preview ${resPreview.status}: ${await resPreview.text()}`);
      const previewData: PromptPreviewResult = await resPreview.json();
      setPreview(previewData);
      setResult({
        contexts_found: previewData.contexts_found,
        context_text: previewData.context_text,
        metadata: { keywords: previewData.keywords },
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Erro desconhecido');
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleSubmit();
  };

  const handleCopy = () => {
    if (!result?.context_text) return;
    navigator.clipboard.writeText(result.context_text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const keywords = (result?.metadata?.keywords as string[] | undefined) ?? [];
  const entityCount = result?.contexts_found.length ?? 0;
  const decision = preview?.decision;
  const recovered = preview?.recovered ?? [];

  return (
    <div style={{
      fontFamily: "'Inter', 'Segoe UI', system-ui, sans-serif",
      background: '#0f1117',
      color: '#e2e8f0',
      width: '100%',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        background: '#1a1d2e',
        borderBottom: '1px solid #2d3148',
        padding: '14px 24px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        flexShrink: 0,
      }}>
        <div>
          <div style={{ fontSize: 17, fontWeight: 600, color: '#a78bfa' }}>
            🔍 PKS Context Debug
          </div>
          <div style={{ fontSize: 11, color: '#64748b', marginTop: 2 }}>
            Reproduz o turno real (classify → memória → registro → prompt), sem gravar nem chamar o LLM
          </div>
        </div>
        {result && (
          <div style={{
            marginLeft: 'auto',
            background: '#1e2235',
            border: '1px solid #2d3148',
            borderRadius: 20,
            padding: '4px 14px',
            fontSize: 11,
            color: '#94a3b8',
            display: 'flex',
            gap: 14,
          }}>
            <span><span style={{ color: '#a78bfa', fontWeight: 600 }}>{entityCount}</span> entidade{entityCount !== 1 ? 's' : ''}</span>
            <span><span style={{ color: '#22d3ee', fontWeight: 600 }}>{recovered.length}</span> memória{recovered.length !== 1 ? 's' : ''}</span>
            <span><span style={{ color: '#a78bfa', fontWeight: 600 }}>{keywords.length}</span> keyword{keywords.length !== 1 ? 's' : ''}</span>
            {preview && (
              <span><span style={{ color: '#34d399', fontWeight: 600 }}>~{preview.prompt_tokens_est}</span> tokens estimados</span>
            )}
          </div>
        )}
      </div>

      {/* Body */}
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>

        {/* Left column — input form */}
        <div style={{
          width: 300,
          flexShrink: 0,
          background: '#13161f',
          borderRight: '1px solid #1e2235',
          display: 'flex',
          flexDirection: 'column',
          padding: 20,
          gap: 16,
          overflowY: 'auto',
        }}>
          <div style={{ ...sectionLabel, marginBottom: 0 }}>Consulta</div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={{ fontSize: 12, color: '#94a3b8' }}>Usuário</label>
            <select
              value={userId}
              onChange={e => setUserId(e.target.value)}
              style={{ ...inputStyle, cursor: 'pointer', appearance: 'auto' }}
            >
              {users.length === 0 && (
                <option value="">Carregando…</option>
              )}
              {users.map(uid => (
                <option key={uid} value={uid}>{uid}</option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <label style={{ fontSize: 12, color: '#94a3b8' }}>
              Prompt
              <span style={{ color: '#475569', fontWeight: 400, marginLeft: 6 }}>⌘+Enter para enviar</span>
            </label>
            <textarea
              value={userInput}
              onChange={e => setUserInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Digite a mensagem do usuário..."
              rows={9}
              style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.5 }}
            />
          </div>

          <button
            onClick={handleSubmit}
            disabled={loading || !userId.trim() || !userInput.trim()}
            style={{
              background: loading || !userId.trim() || !userInput.trim() ? '#1e2235' : '#7c3aed',
              color: loading || !userId.trim() || !userInput.trim() ? '#64748b' : '#fff',
              border: 'none',
              borderRadius: 8,
              padding: '10px 16px',
              fontSize: 13,
              fontWeight: 600,
              cursor: loading || !userId.trim() || !userInput.trim() ? 'not-allowed' : 'pointer',
              transition: 'background 0.15s',
            }}
          >
            {loading ? '⏳ Consultando…' : '🔍 Analisar contexto + prompt'}
          </button>

          {error && (
            <div style={{
              background: '#7f1d1d22',
              border: '1px solid #7f1d1d66',
              borderRadius: 6,
              padding: '10px 12px',
              color: '#f87171',
              fontSize: 12,
              lineHeight: 1.5,
            }}>
              ❌ {error}
            </div>
          )}
        </div>

        {/* Right column — results */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 20 }}>

          {/* Empty state */}
          {!result && !loading && (
            <div style={{
              flex: 1,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#334155',
              gap: 14,
            }}>
              <span style={{ fontSize: 48 }}>🧠</span>
              <span style={{ fontSize: 14 }}>Preencha o formulário e clique em Consultar PKS</span>
              <span style={{ fontSize: 12, color: '#1e2235' }}>
                Os dados não são enviados ao LLM — retrieval + montagem do prompt sem request
              </span>
            </div>
          )}

          {result && (
            <>
              {/* Decision bar — real routing decision that drove retrieval */}
              {decision && (
                <div style={{
                  background: '#13161f',
                  border: '1px solid #1e2235',
                  borderRadius: 8,
                  padding: '12px 16px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  flexWrap: 'wrap',
                }}>
                  <span style={{ fontSize: 11, color: '#64748b', fontWeight: 600, flexShrink: 0 }}>
                    DECISÃO DE ROTEAMENTO
                  </span>
                  <span style={{
                    background: '#1e3a5f44', color: '#60a5fa', fontSize: 12, fontWeight: 700,
                    padding: '3px 10px', borderRadius: 6, letterSpacing: '0.03em',
                    textTransform: 'uppercase' as const,
                  }}>{decision.intent}</span>
                  <span style={{
                    background: decision.needs_memory ? '#065f4622' : '#1e2235',
                    color: decision.needs_memory ? '#34d399' : '#64748b',
                    fontSize: 12, fontWeight: 600, padding: '3px 10px', borderRadius: 999,
                    border: `1px solid ${decision.needs_memory ? '#065f46' : '#2d3148'}`,
                  }}>
                    needs_memory: {decision.needs_memory ? 'true' : 'false'}
                  </span>
                  {!decision.needs_memory && (
                    <span style={{ color: '#475569', fontSize: 11, fontStyle: 'italic' }}>
                      memória semântica pulada neste turno
                    </span>
                  )}
                </div>
              )}

              {/* Keywords bar */}
              <div style={{
                background: '#13161f',
                border: '1px solid #1e2235',
                borderRadius: 8,
                padding: '12px 16px',
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                flexWrap: 'wrap',
              }}>
                <span style={{ fontSize: 11, color: '#64748b', fontWeight: 600, flexShrink: 0 }}>
                  KEYWORDS EXTRAÍDAS
                </span>
                {keywords.length > 0
                  ? keywords.map(k => (
                    <span key={k} style={{
                      background: '#312e8144',
                      color: '#a78bfa',
                      fontSize: 12,
                      fontWeight: 600,
                      padding: '3px 10px',
                      borderRadius: 999,
                      border: '1px solid #312e8188',
                    }}>{k}</span>
                  ))
                  : <span style={{ color: '#475569', fontSize: 12 }}>nenhuma</span>
                }
              </div>

              {/* Neighborhoods */}
              <div>
                <div style={{ ...sectionLabel, marginBottom: 12 }}>
                  Entidades Recuperadas ({entityCount})
                </div>
                {entityCount === 0 ? (
                  <div style={{
                    background: '#13161f',
                    border: '1px solid #1e2235',
                    borderRadius: 8,
                    padding: 20,
                    color: '#475569',
                    fontSize: 13,
                    fontStyle: 'italic',
                    textAlign: 'center',
                  }}>
                    Nenhuma entidade encontrada para esse prompt.
                  </div>
                ) : (
                  result.contexts_found.map((n, i) => (
                    <NeighborhoodCard key={n.entity?.id ?? i} n={n} />
                  ))
                )}
              </div>

              {/* Retrieved layer — hybrid cross-session semantic memory */}
              {decision?.needs_memory && (
                <div>
                  <div style={{ ...sectionLabel, marginBottom: 12 }}>
                    Memória Semântica Recuperada — camada RETRIEVED ({recovered.length})
                  </div>
                  {recovered.length === 0 ? (
                    <div style={{
                      background: '#13161f', border: '1px solid #1e2235', borderRadius: 8,
                      padding: 20, color: '#475569', fontSize: 13, fontStyle: 'italic',
                      textAlign: 'center',
                    }}>
                      Nenhuma memória de sessão anterior recuperada.
                    </div>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                      {recovered.map((r, i) => (
                        <div key={i} style={{
                          background: '#13161f',
                          border: '1px solid #1e2235',
                          borderLeft: '3px solid #22d3ee',
                          borderRadius: 8,
                          padding: '10px 14px',
                          display: 'flex',
                          alignItems: 'flex-start',
                          gap: 10,
                          fontSize: 13,
                        }}>
                          <span style={{
                            background: '#22d3ee22', color: '#22d3ee', fontSize: 10, fontWeight: 700,
                            padding: '2px 7px', borderRadius: 4, letterSpacing: '0.04em',
                            textTransform: 'uppercase' as const, flexShrink: 0,
                          }}>{r.source}</span>
                          <span style={{ color: '#cbd5e1', flex: 1, wordBreak: 'break-word' as const }}>
                            {r.content}
                          </span>
                          <span style={{
                            color: r.score >= 0.25 ? '#34d399' : '#f87171',
                            fontSize: 12, fontWeight: 600, flexShrink: 0,
                          }} title="score final da busca híbrida (limiar de inclusão ≈ 0.25)">
                            {r.score.toFixed(3)}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Context text */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                  <div style={sectionLabel}>
                    Texto Injetado no System Prompt (PKS)
                  </div>
                  <button
                    onClick={handleCopy}
                    disabled={!result.context_text}
                    style={{
                      background: copied ? '#065f4622' : '#1e2235',
                      border: `1px solid ${copied ? '#065f46' : '#2d3148'}`,
                      color: copied ? '#34d399' : '#94a3b8',
                      borderRadius: 6,
                      padding: '3px 10px',
                      fontSize: 11,
                      cursor: result.context_text ? 'pointer' : 'not-allowed',
                      transition: 'all 0.15s',
                    }}
                  >
                    {copied ? '✓ Copiado' : 'Copiar'}
                  </button>
                </div>
                {result.context_text ? (
                  <pre style={{
                    background: '#13161f',
                    border: '1px solid #1e2235',
                    borderRadius: 8,
                    padding: 16,
                    fontSize: 12,
                    color: '#cbd5e1',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word' as const,
                    lineHeight: 1.7,
                    margin: 0,
                    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', ui-monospace, monospace",
                  }}>
                    {result.context_text}
                  </pre>
                ) : (
                  <div style={{
                    background: '#13161f',
                    border: '1px solid #1e2235',
                    borderRadius: 8,
                    padding: 20,
                    color: '#475569',
                    fontSize: 13,
                    fontStyle: 'italic',
                    textAlign: 'center',
                  }}>
                    Sem contexto gerado — PKS vazio ou nenhuma entidade relevante para o prompt.
                  </div>
                )}
              </div>

              {/* Full system prompt preview */}
              {preview && (
                <div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
                    <div style={sectionLabel}>System Prompt Completo</div>
                    <span style={{
                      background: '#0f2a1e',
                      border: '1px solid #065f46',
                      color: '#34d399',
                      borderRadius: 6,
                      padding: '3px 10px',
                      fontSize: 11,
                      fontWeight: 600,
                    }}>
                      ~{preview.prompt_tokens_est} tokens
                    </span>
                    <button
                      onClick={() => setShowTrace(v => !v)}
                      style={{
                        background: '#1e2235',
                        border: '1px solid #2d3148',
                        color: '#94a3b8',
                        borderRadius: 6,
                        padding: '3px 10px',
                        fontSize: 11,
                        cursor: 'pointer',
                        transition: 'all 0.15s',
                      }}
                    >
                      {showTrace ? '▲ Ocultar trace' : '▼ Ver trace de seleção'}
                    </button>
                    <button
                      onClick={() => {
                        navigator.clipboard.writeText(preview.system_prompt);
                        setCopiedPrompt(true);
                        setTimeout(() => setCopiedPrompt(false), 2000);
                      }}
                      style={{
                        background: copiedPrompt ? '#065f4622' : '#1e2235',
                        border: `1px solid ${copiedPrompt ? '#065f46' : '#2d3148'}`,
                        color: copiedPrompt ? '#34d399' : '#94a3b8',
                        borderRadius: 6,
                        padding: '3px 10px',
                        fontSize: 11,
                        cursor: 'pointer',
                        transition: 'all 0.15s',
                      }}
                    >
                      {copiedPrompt ? '✓ Copiado' : 'Copiar'}
                    </button>
                  </div>

                  {/* Candidate trace table */}
                  {showTrace && (
                    <div style={{
                      background: '#13161f',
                      border: '1px solid #1e2235',
                      borderRadius: 8,
                      padding: 14,
                      marginBottom: 12,
                      overflowX: 'auto',
                    }}>
                      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
                        <thead>
                          <tr style={{ color: '#64748b', textAlign: 'left' as const }}>
                            <th style={{ padding: '4px 8px', fontWeight: 700 }}>LAYER</th>
                            <th style={{ padding: '4px 8px', fontWeight: 700 }}>KEY</th>
                            <th style={{ padding: '4px 8px', fontWeight: 700 }}>PIN</th>
                            <th style={{ padding: '4px 8px', fontWeight: 700, textAlign: 'right' as const }}>TOKENS</th>
                            <th style={{ padding: '4px 8px', fontWeight: 700, textAlign: 'right' as const }}>SCORE</th>
                            <th style={{ padding: '4px 8px', fontWeight: 700 }}>STATUS</th>
                          </tr>
                        </thead>
                        <tbody>
                          {preview.trace.map((c, i) => (
                            <tr key={i} style={{
                              borderTop: '1px solid #1e2235',
                              background: c.included ? 'transparent' : '#0f0f1500',
                              opacity: c.included ? 1 : 0.5,
                            }}>
                              <td style={{ padding: '5px 8px', color: '#a78bfa', fontWeight: 600 }}>{c.layer}</td>
                              <td style={{ padding: '5px 8px', color: '#94a3b8', maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' as const }}>{c.key}</td>
                              <td style={{ padding: '5px 8px', color: c.pin ? '#fbbf24' : '#475569' }}>{c.pin ? '📌' : '—'}</td>
                              <td style={{ padding: '5px 8px', color: '#cbd5e1', textAlign: 'right' as const }}>{c.est_tokens}</td>
                              <td style={{ padding: '5px 8px', color: '#60a5fa', textAlign: 'right' as const }}>{c.pin ? '—' : c.final_score.toFixed(2)}</td>
                              <td style={{ padding: '5px 8px' }}>
                                {c.included ? (
                                  <span style={{ color: '#34d399', fontWeight: 600 }}>✓ incluído</span>
                                ) : (
                                  <span style={{ color: '#f87171' }}>✗ {c.skip_reason || 'excluído'}</span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}

                  {/* Full prompt text */}
                  <pre style={{
                    background: '#0a0d14',
                    border: '1px solid #1e3a5f',
                    borderRadius: 8,
                    padding: 16,
                    fontSize: 11.5,
                    color: '#cbd5e1',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word' as const,
                    lineHeight: 1.75,
                    margin: 0,
                    fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', ui-monospace, monospace",
                    maxHeight: 600,
                    overflowY: 'auto',
                  }}>
                    {preview.system_prompt}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
