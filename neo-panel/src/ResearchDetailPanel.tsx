import { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  RESEARCH_STATUS_COLORS,
  RESEARCH_STATUS_LABELS,
  cancelResearch,
  isActiveResearch,
} from './ResearchPanel';

interface ResearchDetailPanelProps {
  taskId: string;
  onClose: () => void;
}

interface ResearchSource {
  n: number;
  title: string;
  url: string;
  published_at?: string | null;
}

interface ValidationFailure {
  check: string;
  detail: string;
  section: string | null;
}

interface CoverageRow {
  id: string;
  question: string;
  evidence: number;
  source_types: string[];
  has_strong_source: boolean;
}

interface ResearchQuality {
  validation?: { passed: boolean; failures: ValidationFailure[] };
  coverage?: CoverageRow[];
  rubric?: Record<string, number>;
  issues?: { section: string; issue: string; fix: string }[];
  revised?: boolean;
}

interface ResearchRequirements {
  explicit?: { id: string; text: string; check: string; value: any }[];
  implicit_scope?: string;
  language?: string;
}

interface ResearchDetail {
  id: string;
  job_id: string;
  topic: string;
  instructions: string | null;
  depth: string;
  status: string;
  model: string;
  backend: string | null;
  stats: Record<string, any>;
  quality: ResearchQuality;
  requirements: ResearchRequirements;
  sources: ResearchSource[];
  report_markdown: string | null;
  report_path: string | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

const PHASES = [
  'queued', 'planning', 'searching', 'reading', 'synthesizing',
  'validating', 'revising', 'completed',
];

const RUBRIC_LABELS: Record<string, string> = {
  depth: 'Profundidade',
  coherence: 'Coerência',
  coverage: 'Cobertura',
  evidence: 'Evidência',
};

const fmt = (iso: string | null) => (iso ? new Date(iso).toLocaleString('pt-BR') : '-');

export default function ResearchDetailPanel({ taskId, onClose }: ResearchDetailPanelProps) {
  const [task, setTask] = useState<ResearchDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchDetail = async () => {
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/v1/research/${taskId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setTask(await res.json());
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchDetail();
    const interval = setInterval(() => {
      // Continua atualizando enquanto a pesquisa roda (fase ao vivo no drawer).
      fetchDetail();
    }, 5000);
    return () => clearInterval(interval);
  }, [taskId]);

  const handleCancel = async () => {
    if (!confirm('Cancelar esta pesquisa?')) return;
    try {
      await cancelResearch(taskId);
      fetchDetail();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const stats = task?.stats || {};
  const quality = task?.quality || {};
  const requirements = task?.requirements?.explicit || [];
  const rubric = quality.rubric || {};
  const validation = quality.validation;
  const coverage = quality.coverage || [];
  const active = task ? isActiveResearch(task.status) : false;
  const phaseIndex = task ? PHASES.indexOf(task.status) : -1;
  const hasQuality = Boolean(validation || Object.keys(rubric).length || coverage.length);

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={drawerStyle} onClick={e => e.stopPropagation()}>
        <div style={headerStyle}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
            <span style={{ fontSize: 18 }}>🔎</span>
            <strong style={{ fontSize: 15, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {task?.topic || 'Pesquisa'}
            </strong>
            {task && (
              <span style={{
                background: RESEARCH_STATUS_COLORS[task.status] || '#64748b',
                borderRadius: 4,
                padding: '2px 10px',
                fontSize: 12,
                fontWeight: 600,
                color: '#fff',
                whiteSpace: 'nowrap',
              }}>
                {RESEARCH_STATUS_LABELS[task.status] || task.status}
              </span>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            {active && (
              <button onClick={handleCancel} style={{ ...closeBtnStyle, borderColor: '#ef4444', color: '#fca5a5' }}>
                ❌ Cancelar
              </button>
            )}
            <button onClick={onClose} style={closeBtnStyle}>✕ Fechar</button>
          </div>
        </div>

        <div style={{ flex: 1, overflow: 'auto', padding: '16px 20px' }}>
          {loading && !task && <p>Carregando...</p>}
          {error && <p style={{ color: '#ef4444' }}>Erro: {error}</p>}

          {task && (
            <>
              {/* Timeline de fases */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 18, flexWrap: 'wrap' }}>
                {PHASES.map((phase, i) => {
                  const reached = phaseIndex >= i || task.status === 'completed';
                  const isCurrent = task.status === phase && active;
                  const terminalBad = ['failed', 'timeout', 'cancelled'].includes(task.status);
                  return (
                    <div key={phase} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                      <span style={{
                        padding: '3px 10px',
                        borderRadius: 12,
                        fontSize: 11,
                        fontWeight: isCurrent ? 700 : 500,
                        background: isCurrent ? '#3b82f6' : reached && !terminalBad ? 'rgba(59,130,246,0.18)' : '#13161f',
                        color: isCurrent ? '#fff' : reached && !terminalBad ? '#93c5fd' : '#475569',
                        border: '1px solid ' + (isCurrent ? '#3b82f6' : '#2d3355'),
                      }}>
                        {RESEARCH_STATUS_LABELS[phase] || phase}
                      </span>
                      {i < PHASES.length - 1 && <span style={{ color: '#334155', fontSize: 10 }}>→</span>}
                    </div>
                  );
                })}
                {['failed', 'timeout', 'cancelled'].includes(task.status) && (
                  <span style={{
                    padding: '3px 10px',
                    borderRadius: 12,
                    fontSize: 11,
                    fontWeight: 700,
                    background: RESEARCH_STATUS_COLORS[task.status],
                    color: '#fff',
                  }}>
                    {RESEARCH_STATUS_LABELS[task.status]}
                  </span>
                )}
              </div>

              {/* Metadados */}
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))', gap: 12, marginBottom: 18 }}>
                <Field label="Profundidade">{task.depth}</Field>
                <Field label="Modelo">{task.model || '-'}</Field>
                <Field label="Backend de busca">{task.backend || '-'}</Field>
                <Field label="Criada em">{fmt(task.created_at)}</Field>
                <Field label="Iniciada em">{fmt(task.started_at)}</Field>
                <Field label="Terminada em">{fmt(task.finished_at)}</Field>
                {stats.writer_model && <Field label="Modelo escritor">{stats.writer_model}</Field>}
                {stats.queries != null && <Field label="Queries planejadas">{stats.queries}</Field>}
                {stats.searches != null && <Field label="Buscas executadas">{stats.searches}</Field>}
                {stats.fetches != null && <Field label="Páginas lidas">{stats.fetches}</Field>}
                {stats.rounds != null && <Field label="Rodadas de coleta">{stats.rounds}</Field>}
                {stats.evidence_count != null && <Field label="Evidências">{stats.evidence_count}</Field>}
                {stats.revisions != null && <Field label="Revisões">{stats.revisions}</Field>}
                {stats.llm_calls != null && <Field label="Chamadas LLM">{stats.llm_calls}</Field>}
                {stats.duration_s != null && <Field label="Duração">{stats.duration_s}s</Field>}
              </div>

              {hasQuality && (
                <div style={{ marginBottom: 18, padding: '12px 16px', background: '#13161f', border: '1px solid #2d3355', borderRadius: 8 }}>
                  <SectionTitle>Qualidade</SectionTitle>

                  {Object.keys(rubric).length > 0 && (
                    <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 12 }}>
                      {Object.entries(rubric).map(([key, score]) => (
                        <div key={key} style={{ fontSize: 12, color: '#cbd5e1' }}>
                          <span style={{ color: '#64748b' }}>{RUBRIC_LABELS[key] || key}:</span>{' '}
                          <strong style={{ color: score >= 4 ? '#22c55e' : score >= 3 ? '#fbbf24' : '#ef4444' }}>
                            {score}/5
                          </strong>
                        </div>
                      ))}
                      {quality.revised && (
                        <span style={{ fontSize: 11, color: '#a855f7' }}>✎ relatório revisado após a crítica</span>
                      )}
                    </div>
                  )}

                  {requirements.length > 0 && (
                    <div style={{ marginBottom: 10 }}>
                      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 4 }}>Requisitos do pedido</div>
                      {requirements.map(req => {
                        const failed = (validation?.failures || []).some(
                          f => f.check === req.check || (req.check === 'min_sources' && f.check === 'min_sources'),
                        );
                        return (
                          <div key={req.id} style={{ fontSize: 12, color: failed ? '#fca5a5' : '#86efac' }}>
                            {failed ? '✗' : '✓'} {req.text}
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {validation && (
                    validation.failures.length > 0 ? (
                      <div style={{ marginBottom: 10 }}>
                        <div style={{ fontSize: 11, color: '#64748b', marginBottom: 4 }}>
                          Checagens não cumpridas ({validation.failures.length})
                        </div>
                        {validation.failures.map((f, i) => (
                          <div key={i} style={{ fontSize: 12, color: '#fbbf24' }}>
                            ⚠️ {f.detail}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div style={{ fontSize: 12, color: '#86efac', marginBottom: 10 }}>
                        ✓ Todas as checagens automáticas passaram
                      </div>
                    )
                  )}

                  {coverage.length > 0 && (
                    <div>
                      <div style={{ fontSize: 11, color: '#64748b', marginBottom: 4 }}>
                        Cobertura (sub-pergunta × evidência)
                      </div>
                      {coverage.map(row => (
                        <div key={row.id} style={{ fontSize: 12, color: '#94a3b8', display: 'flex', gap: 8 }}>
                          <span style={{ color: row.evidence === 0 ? '#ef4444' : row.has_strong_source ? '#22c55e' : '#fbbf24', flexShrink: 0 }}>
                            {row.evidence}×
                          </span>
                          <span style={{ flex: 1 }}>{row.question}</span>
                          {row.source_types.length > 0 && (
                            <span style={{ color: '#475569', flexShrink: 0 }}>{row.source_types.join(', ')}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {task.instructions && (
                <div style={{ marginBottom: 16 }}>
                  <SectionTitle>Direcionamento</SectionTitle>
                  <p style={{ margin: 0, fontSize: 13, color: '#cbd5e1' }}>{task.instructions}</p>
                </div>
              )}

              {task.error && (
                <div style={{ marginBottom: 16, padding: '10px 14px', background: 'rgba(239,68,68,0.08)', border: '1px solid #7f1d1d', borderRadius: 8 }}>
                  <SectionTitle>Erro</SectionTitle>
                  <p style={{ margin: 0, fontSize: 13, color: '#fca5a5' }}>{task.error}</p>
                </div>
              )}

              {task.sources && task.sources.length > 0 && (
                <div style={{ marginBottom: 18 }}>
                  <SectionTitle>Fontes ({task.sources.length})</SectionTitle>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    {task.sources.map(s => (
                      <div key={s.n} style={{ fontSize: 12, color: '#94a3b8' }}>
                        <span style={{ color: '#64748b' }}>[{s.n}]</span>{' '}
                        <a href={s.url} target="_blank" rel="noreferrer" style={{ color: '#93c5fd' }}>
                          {s.title || s.url}
                        </a>
                        {s.published_at && <span style={{ color: '#64748b' }}> — {s.published_at}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {task.report_path && (
                <p style={{ fontSize: 11, color: '#64748b', marginBottom: 12 }}>
                  📁 Cópia no workspace do Neo: <code>{task.report_path}</code>
                </p>
              )}

              {task.report_markdown ? (
                <div>
                  <SectionTitle>Relatório</SectionTitle>
                  <div style={{
                    background: '#13161f',
                    border: '1px solid #2d3355',
                    borderRadius: 8,
                    padding: '16px 20px',
                    fontSize: 14,
                    lineHeight: 1.65,
                    color: '#d7dee9',
                  }} className="research-markdown">
                    <ReactMarkdown
                      components={{
                        a: (props) => <a {...props} target="_blank" rel="noreferrer" style={{ color: '#93c5fd' }} />,
                        h1: (props) => <h1 {...props} style={{ fontSize: 20, borderBottom: '1px solid #2d3355', paddingBottom: 6 }} />,
                        h2: (props) => <h2 {...props} style={{ fontSize: 17, marginTop: 20 }} />,
                        h3: (props) => <h3 {...props} style={{ fontSize: 15, marginTop: 16 }} />,
                        code: (props) => <code {...props} style={{ background: '#1e2235', padding: '1px 5px', borderRadius: 4, fontSize: 12.5 }} />,
                        table: (props) => <table {...props} style={{ borderCollapse: 'collapse', width: '100%' }} />,
                        td: (props) => <td {...props} style={{ border: '1px solid #2d3355', padding: '4px 8px' }} />,
                        th: (props) => <th {...props} style={{ border: '1px solid #2d3355', padding: '4px 8px', background: '#1e2235' }} />,
                      }}
                    >
                      {task.report_markdown}
                    </ReactMarkdown>
                  </div>
                </div>
              ) : (
                active && (
                  <p style={{ color: '#64748b', fontSize: 13 }}>
                    ⏳ Pesquisa em andamento — o relatório aparece aqui quando terminar.
                  </p>
                )
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div style={{ fontSize: 11, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 6, fontWeight: 700 }}>
      {children}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div style={{ fontSize: 10, color: '#64748b', textTransform: 'uppercase', letterSpacing: 0.5, marginBottom: 3 }}>
        {label}
      </div>
      <div style={{ fontSize: 13, color: '#e2e8f0' }}>{children}</div>
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  background: 'rgba(0, 0, 0, 0.55)',
  zIndex: 1000,
  display: 'flex',
  justifyContent: 'flex-end',
};

const drawerStyle: React.CSSProperties = {
  width: 'min(820px, 100%)',
  height: '100%',
  background: '#0f1320',
  borderLeft: '1px solid #2d3355',
  display: 'flex',
  flexDirection: 'column',
  color: '#e2e8f0',
};

const headerStyle: React.CSSProperties = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  padding: '14px 20px',
  borderBottom: '1px solid #2d3355',
  gap: 12,
};

const closeBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: '1px solid #2d3355',
  color: '#cbd5e1',
  borderRadius: 6,
  padding: '4px 10px',
  cursor: 'pointer',
  fontSize: 14,
};
