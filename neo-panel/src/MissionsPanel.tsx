import { useEffect, useState } from 'react';
import MissionDetailPanel from './MissionDetailPanel';

interface Mission {
  id: string;
  user_id: string;
  title: string;
  goal: string;
  contact_jid: string;
  status: string;
  channel: string;
  created_at: string;
  last_activity_at: string;
  completed_at: string | null;
  extra_metadata: Record<string, any>;
  result_summary: string | null;
}

const STATUS_COLORS: Record<string, string> = {
  pending: '#f59e0b',
  awaiting_confirmation: '#3b82f6',
  awaiting_reply: '#8b5cf6',
  cancelled: '#ef4444',
  completed: '#22c55e',
  failed: '#ef4444',
};

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pendente',
  awaiting_confirmation: 'Aguardando Aprovação',
  awaiting_reply: 'Aguardando Resposta',
  cancelled: 'Cancelada',
  completed: 'Concluída',
  failed: 'Falhou',
};

function missionCode(id: string): string {
  return id ? `MIS-${id.slice(0, 8).toUpperCase()}` : 'MIS-';
}

export default function MissionsPanel() {
  const [missions, setMissions] = useState<Mission[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('');
  const [openMissionId, setOpenMissionId] = useState<string | null>(null);

  const fetchMissions = async () => {
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set('status', statusFilter);
      params.set('limit', '100');
      const res = await fetch(`http://localhost:8000/api/v1/missions?${params}`);
      if (!res.ok) throw new Error('Failed to fetch');
      const data = await res.json();
      setMissions(Array.isArray(data) ? data : []);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMissions();
    const interval = setInterval(fetchMissions, 3000);
    return () => clearInterval(interval);
  }, [statusFilter]);

  const handleApprove = async (missionId: string) => {
    try {
      const res = await fetch(`http://localhost:8000/api/v1/missions/${missionId}/approve`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok) alert(data.detail || 'Erro ao aprovar');
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleCancel = async (missionId: string) => {
    if (!confirm('Cancelar esta missão?')) return;
    try {
      await fetch(`http://localhost:8000/api/v1/missions/${missionId}/cancel`, { method: 'POST' });
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const filtered = missions.filter(m => {
    if (filter) {
      const q = filter.toLowerCase();
      return m.title.toLowerCase().includes(q)
        || m.contact_jid.toLowerCase().includes(q)
        || m.goal.toLowerCase().includes(q)
        || m.id.toLowerCase().includes(q)
        || missionCode(m.id).toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <div style={{ padding: '20px', color: '#e2e8f0', height: '100%', overflow: 'auto' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <h2 style={{ margin: 0, color: '#e2e8f0' }}>📋 Missões WhatsApp</h2>
        <button onClick={fetchMissions} style={{ padding: '4px 12px', cursor: 'pointer' }}>🔄 Atualizar</button>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={{ padding: '4px 8px' }}>
          <option value="">Todos os status</option>
          <option value="awaiting_confirmation">Aguardando Aprovação</option>
          <option value="awaiting_reply">Aguardando Resposta</option>
          <option value="pending">Pendente</option>
          <option value="completed">Concluída</option>
          <option value="cancelled">Cancelada</option>
          <option value="failed">Falhou</option>
        </select>
        <input
          placeholder="Buscar..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          style={{ padding: '4px 8px', flex: 1, maxWidth: 250 }}
        />
      </div>

      {loading && <p>Carregando...</p>}
      {error && <p style={{ color: '#ef4444' }}>Erro: {error}</p>}

      {!loading && filtered.length === 0 && (
        <p style={{ color: '#64748b' }}>Nenhuma missão encontrada.</p>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {filtered.map(mission => {
          const meta = mission.extra_metadata || {};
          const draftText = meta.draft_text || '';
          return (
            <div
              key={mission.id}
              onClick={() => setOpenMissionId(mission.id)}
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
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span
                    title={mission.id}
                    style={{
                      background: '#13161f',
                      border: '1px solid #334155',
                      borderRadius: 4,
                      padding: '2px 7px',
                      fontSize: 11,
                      fontWeight: 700,
                      color: '#93c5fd',
                      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                    }}
                  >
                    {missionCode(mission.id)}
                  </span>
                  <span style={{
                    background: STATUS_COLORS[mission.status] || '#64748b',
                    borderRadius: 4,
                    padding: '2px 8px',
                    fontSize: 11,
                    fontWeight: 600,
                    color: '#fff',
                  }}>
                    {STATUS_LABELS[mission.status] || mission.status}
                  </span>
                  <strong style={{ color: '#e2e8f0', fontSize: 14 }}>{mission.title || mission.contact_jid}</strong>
                </div>
                <div style={{ display: 'flex', gap: 6 }}>
                  {mission.status === 'awaiting_confirmation' && (
                    <>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleApprove(mission.id); }}
                        style={{ background: '#22c55e', border: 'none', borderRadius: 4, padding: '4px 12px', color: '#fff', cursor: 'pointer', fontSize: 12 }}>
                        ✅ Aprovar
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); handleCancel(mission.id); }}
                        style={{ background: '#ef4444', border: 'none', borderRadius: 4, padding: '4px 12px', color: '#fff', cursor: 'pointer', fontSize: 12 }}>
                        ❌ Cancelar
                      </button>
                    </>
                  )}
                  {mission.status === 'awaiting_reply' && (
                    <span style={{ color: '#8b5cf6', fontSize: 12 }}>⏳ Aguardando resposta...</span>
                  )}
                </div>
              </div>

              {mission.goal && (
                <p style={{ margin: 0, fontSize: 12, color: '#94a3b8' }}>🗒️ {mission.goal}</p>
              )}

              {draftText && (
                <p style={{ margin: 0, fontSize: 13, color: '#cbd5e1', fontStyle: 'italic', padding: '6px 10px', background: '#13161f', borderRadius: 4, borderLeft: '3px solid #3b82f6' }}>
                  💬 "{draftText}"
                </p>
              )}

              <div style={{ display: 'flex', gap: 16, fontSize: 11, color: '#64748b' }}>
                <span>📱 {mission.contact_jid}</span>
                {meta.contact_phone_e164 && <span>📞 {meta.contact_phone_e164}</span>}
                {meta.mode && <span>⚙️ {meta.mode}</span>}
                <span>🕐 {mission.last_activity_at ? new Date(mission.last_activity_at).toLocaleString('pt-BR') : '-'}</span>
                {mission.result_summary && <span>📌 {mission.result_summary}</span>}
              </div>
            </div>
          );
        })}
      </div>

      {openMissionId && (
        <MissionDetailPanel
          missionId={openMissionId}
          onClose={() => setOpenMissionId(null)}
        />
      )}
    </div>
  );
}
