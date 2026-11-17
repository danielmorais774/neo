import { useEffect, useState } from 'react';
import MissionDetailPanel from './MissionDetailPanel';

interface Mission {
  id: string;
  user_id: string;
  session_id: string;
  parent_session_id: string | null;
  title: string;
  goal: string;
  contact_jid: string;
  status: string;
  channel: string;
  created_at: string;
  last_activity_at: string;
  completed_at: string | null;
  next_action_type?: string | null;
  next_action_params?: Record<string, any>;
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

function draftMessagesFor(mission: Mission): string[] {
  const meta = mission.extra_metadata || {};
  const value = mission.next_action_params?.messages || meta.draft_messages;
  if (Array.isArray(value)) {
    return value.map(v => String(v).trim()).filter(Boolean);
  }
  const fallback = mission.next_action_params?.text || meta.draft_text || '';
  return fallback ? [fallback] : [];
}

async function executeMissionCommand(
  missionId: string,
  command: 'approve' | 'cancel' | 'edit' | 'retry',
  editedText?: string,
) {
  const res = await fetch(`http://127.0.0.1:8000/api/v1/missions/${missionId}/command`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(editedText ? { command, edited_text: editedText } : { command }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.detail || data.message || `Falha ao executar ${command}`);
  }
  return data;
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
      const res = await fetch(`http://127.0.0.1:8000/api/v1/missions?${params}`);
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
      await executeMissionCommand(missionId, 'approve');
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleEdit = async (mission: Mission) => {
    const currentText =
      mission.next_action_params?.text ||
      mission.extra_metadata?.draft_text ||
      '';
    const editedText = window.prompt('Editar mensagem antes de enviar:', currentText);
    if (!editedText?.trim()) return;

    try {
      await executeMissionCommand(mission.id, 'edit', editedText.trim());
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleCancel = async (missionId: string) => {
    if (!confirm('Cancelar esta missão?')) return;
    try {
      await executeMissionCommand(missionId, 'cancel');
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleRetry = async (missionId: string) => {
    try {
      await executeMissionCommand(missionId, 'retry');
      fetchMissions();
    } catch (e: any) {
      alert(e.message);
    }
  };

  const handleDisableAll = async () => {
    const activeMissions = filtered.filter(m =>
      m.status === 'pending' || m.status === 'awaiting_confirmation'
    );
    if (activeMissions.length === 0) {
      alert('Não há missões ativas para desabilitar.');
      return;
    }
    if (!confirm(`Desabilitar todas as ${activeMissions.length} missões ativas?`)) return;

    let failed = 0;
    for (const mission of activeMissions) {
      try {
        await executeMissionCommand(mission.id, 'cancel');
      } catch {
        failed++;
      }
    }
    if (failed > 0) {
      alert(`${activeMissions.length - failed} missões desabilitadas, ${failed} falharam.`);
    } else {
      alert(`${activeMissions.length} missões desabilitadas com sucesso.`);
    }
    fetchMissions();
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
        <button onClick={handleDisableAll} style={{ padding: '4px 12px', cursor: 'pointer', background: '#7f1d1d', border: 'none', borderRadius: 4, color: '#fca5a5' }}>⏸ Desabilitar Todas</button>
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
          const draftMessages = draftMessagesFor(mission);
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
                        onClick={(e) => { e.stopPropagation(); handleEdit(mission); }}
                        style={{ background: '#0f172a', border: '1px solid #3b82f6', borderRadius: 4, padding: '4px 12px', color: '#bfdbfe', cursor: 'pointer', fontSize: 12 }}>
                        ✏️ Editar
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
                  {mission.status === 'failed' && (
                    <button
                      onClick={(e) => { e.stopPropagation(); handleRetry(mission.id); }}
                      style={{ background: '#f59e0b', border: 'none', borderRadius: 4, padding: '4px 12px', color: '#111827', cursor: 'pointer', fontSize: 12, fontWeight: 700 }}>
                      ↻ Retry
                    </button>
                  )}
                </div>
              </div>

              {mission.goal && (
                <p style={{ margin: 0, fontSize: 12, color: '#94a3b8' }}>🗒️ {mission.goal}</p>
              )}

              {draftMessages.length > 0 && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 10px', background: '#13161f', borderRadius: 4, borderLeft: '3px solid #3b82f6' }}>
                  {draftMessages.map((message, index) => (
                    <div key={`${mission.id}-${index}`} style={{ fontSize: 13, color: '#cbd5e1', fontStyle: 'italic' }}>
                      💬 {draftMessages.length > 1 ? `${index + 1}. ` : ''}"{message}"
                    </div>
                  ))}
                </div>
              )}

              <div style={{ display: 'flex', gap: 16, fontSize: 11, color: '#64748b' }}>
                <span>📱 {mission.contact_jid}</span>
                {meta.contact_phone_e164 && <span>📞 {meta.contact_phone_e164}</span>}
                {meta.mode && <span>⚙️ {meta.mode}</span>}
                <span>🕐 {mission.last_activity_at ? new Date(mission.last_activity_at).toLocaleString('pt-BR') : '-'}</span>
                {mission.next_action_type && <span>▶️ {mission.next_action_type}</span>}
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
