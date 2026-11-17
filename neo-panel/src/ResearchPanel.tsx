import { useEffect, useState } from 'react';
import ResearchDetailPanel from './ResearchDetailPanel';

export interface ResearchQualitySummary {
  rubric_avg?: number;
  validation_passed?: boolean;
  failures?: number;
  revised?: boolean;
}

export interface ResearchTask {
  id: string;
  job_id: string;
  topic: string;
  depth: string;
  status: string;
  model: string;
  backend: string | null;
  stats: Record<string, any>;
  quality: ResearchQualitySummary;
  report_path: string | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  has_report: boolean;
}

export const RESEARCH_STATUS_COLORS: Record<string, string> = {
  queued: '#64748b',
  planning: '#3b82f6',
  searching: '#3b82f6',
  reading: '#3b82f6',
  synthesizing: '#3b82f6',
  validating: '#a855f7',
  revising: '#a855f7',
  completed: '#22c55e',
  failed: '#ef4444',
  timeout: '#ef4444',
  cancelled: '#64748b',
};

export const RESEARCH_STATUS_LABELS: Record<string, string> = {
  queued: 'Na fila',
  planning: 'Planejando',
  searching: 'Buscando',
  reading: 'Lendo fontes',
  synthesizing: 'Sintetizando',
  validating: 'Validando',
  revising: 'Revisando',
  completed: 'Concluída',
  failed: 'Falhou',
  timeout: 'Timeout',
  cancelled: 'Cancelada',
};

const ACTIVE_STATUSES = new Set([
  'queued', 'planning', 'searching', 'reading', 'synthesizing', 'validating', 'revising',
]);

export function isActiveResearch(status: string): boolean {
  return ACTIVE_STATUSES.has(status);
}

function durationLabel(t: ResearchTask): string {
  const start = t.started_at ? new Date(t.started_at).getTime() : null;
  if (!start) return '-';
  const end = t.finished_at ? new Date(t.finished_at).getTime() : Date.now();
  const secs = Math.max(0, Math.round((end - start) / 1000));
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m${String(secs % 60).padStart(2, '0')}s`;
}

export async function cancelResearch(taskId: string) {
  const res = await fetch(`http://127.0.0.1:8000/api/v1/research/${taskId}/cancel`, {
    method: 'POST',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.detail || data.message || 'Falha ao cancelar a pesquisa');
  }
  return data;
}

export default function ResearchPanel() {
  const [tasks, setTasks] = useState<ResearchTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [filter, setFilter] = useState<string>('');
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);

  const fetchTasks = async () => {
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set('status', statusFilter);
      params.set('limit', '100');
      const res = await fetch(`http://127.0.0.1:8000/api/v1/research?${params}`);
      if (!res.ok) throw new Error('Failed to fetch');
      const data = await res.json();
      setTasks(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();
    // Polling 5s enquanto houver pesquisa ativa; 15s parado (lista quieta).
    const interval = setInterval(() => {
      fetchTasks();
    }, tasks.some(t => isActiveResearch(t.status)) ? 5000 : 15000);
    return () => clearInterval(interval);
  }, [statusFilter, tasks.some(t => isActiveResearch(t.status))]);

  const handleCancel = async (taskId: string) => {
    if (!confirm('Cancelar esta pesquisa?')) return;
    try {
      await cancelResearch(taskId);
      fetchTasks();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const filtered = tasks.filter(t => {
    if (!filter) return true;
    const q = filter.toLowerCase();
    return t.topic.toLowerCase().includes(q) || t.id.toLowerCase().includes(q);
  });

  return (
    <div style={{ padding: '20px', color: '#e2e8f0', height: '100%', overflow: 'auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <h2 style={{ margin: 0, color: '#e2e8f0' }}>🔎 Pesquisas</h2>
        <button onClick={fetchTasks} style={{ padding: '4px 12px', cursor: 'pointer' }}>🔄 Atualizar</button>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={{ padding: '4px 8px' }}>
          <option value="">Todos os status</option>
          <option value="queued">Na fila</option>
          <option value="searching">Buscando</option>
          <option value="reading">Lendo fontes</option>
          <option value="synthesizing">Sintetizando</option>
          <option value="validating">Validando</option>
          <option value="revising">Revisando</option>
          <option value="completed">Concluída</option>
          <option value="failed">Falhou</option>
          <option value="timeout">Timeout</option>
          <option value="cancelled">Cancelada</option>
        </select>
        <input
          placeholder="Buscar tópico..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{ padding: '4px 8px', flex: 1, maxWidth: 250 }}
        />
      </div>

      {loading && <p>Carregando...</p>}
      {error && <p style={{ color: '#ef4444' }}>Erro: {error}</p>}

      {!loading && filtered.length === 0 && (
        <p style={{ color: '#64748b' }}>
          Nenhuma pesquisa ainda. Peça ao Neo: "pesquisa a fundo sobre X e me traz um relatório".
        </p>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {filtered.map(task => {
          const active = isActiveResearch(task.status);
          const stats = task.stats || {};
          const quality = task.quality || {};
          return (
            <div
              key={task.id}
              onClick={() => setOpenTaskId(task.id)}
              style={{
                background: '#1e2235',
                border: '1px solid #2d3355',
                borderRadius: 8,
                padding: '12px 16px',
                display: 'flex',
                flexDirection: 'column',
                gap: 6,
                cursor: 'pointer',
                transition: 'border-color 120ms ease',
              }}
              onMouseEnter={e => (e.currentTarget.style.borderColor = '#3b82f6')}
              onMouseLeave={e => (e.currentTarget.style.borderColor = '#2d3355')}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                  <span style={{
                    background: RESEARCH_STATUS_COLORS[task.status] || '#64748b',
                    borderRadius: 4,
                    padding: '2px 8px',
                    fontSize: 11,
                    fontWeight: 600,
                    color: '#fff',
                    whiteSpace: 'nowrap',
                    animation: active ? 'pulse 1.5s ease-in-out infinite' : undefined,
                  }}>
                    {active ? '● ' : ''}{RESEARCH_STATUS_LABELS[task.status] || task.status}
                  </span>
                  <span style={{
                    background: '#13161f',
                    border: '1px solid #334155',
                    borderRadius: 4,
                    padding: '2px 7px',
                    fontSize: 11,
                    color: '#93c5fd',
                    whiteSpace: 'nowrap',
                  }}>
                    {task.depth}
                  </span>
                  <strong style={{
                    color: '#e2e8f0',
                    fontSize: 14,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}>
                    {task.topic}
                  </strong>
                  {quality.validation_passed === false && (
                    <span
                      title={`${quality.failures || 0} requisito(s)/checagem(ns) não cumprido(s) — veja o detalhe`}
                      style={{
                        background: 'rgba(245,158,11,0.15)',
                        border: '1px solid #b45309',
                        borderRadius: 4,
                        padding: '2px 7px',
                        fontSize: 11,
                        color: '#fbbf24',
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                      }}>
                      ⚠️ {quality.failures || 0} pendência(s)
                    </span>
                  )}
                </div>
                {active && (
                  <button
                    onClick={(e) => { e.stopPropagation(); handleCancel(task.id); }}
                    style={{ background: '#ef4444', border: 'none', borderRadius: 4, padding: '4px 12px', color: '#fff', cursor: 'pointer', fontSize: 12, flexShrink: 0 }}>
                    ❌ Cancelar
                  </button>
                )}
              </div>

              {task.error && (
                <p style={{ margin: 0, fontSize: 12, color: '#fca5a5' }}>⚠️ {task.error}</p>
              )}

              <div style={{ display: 'flex', gap: 16, fontSize: 11, color: '#64748b', flexWrap: 'wrap' }}>
                <span>⏱ {durationLabel(task)}</span>
                {task.backend && <span>🌐 {task.backend}</span>}
                {stats.searches != null && <span>🔍 {stats.searches} buscas</span>}
                {stats.sources_used != null && <span>📄 {stats.sources_used} fontes</span>}
                {stats.rounds != null && <span>🔁 {stats.rounds} rodada(s)</span>}
                {quality.rubric_avg != null && <span>🎓 nota {quality.rubric_avg}/5</span>}
                <span>🕐 {task.created_at ? new Date(task.created_at).toLocaleString('pt-BR') : '-'}</span>
                {task.has_report && <span>📌 relatório pronto</span>}
              </div>
            </div>
          );
        })}
      </div>

      {openTaskId && (
        <ResearchDetailPanel
          taskId={openTaskId}
          onClose={() => setOpenTaskId(null)}
        />
      )}
    </div>
  );
}
