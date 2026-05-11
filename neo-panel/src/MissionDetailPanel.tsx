import { useEffect, useState } from 'react';

interface MissionDetailPanelProps {
  missionId: string;
  onClose: () => void;
}

interface MissionDetail {
  id: string;
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

interface MessageRow {
  id: string;
  role: string;
  content: string;
  tool_name: string | null;
  tool_call_id: string | null;
  created_at: string | null;
}

interface NotificationRow {
  id: string;
  kind: string;
  title: string;
  body: string;
  payload: Record<string, any>;
  created_at: string | null;
  delivered_at: string | null;
  acked_at: string | null;
}

interface TimelineEvent {
  type: 'message' | 'notification' | 'milestone';
  kind?: string;
  role?: string;
  label: string;
  detail?: string | null;
  tool_name?: string | null;
  at: string | null;
}

type TabKey = 'overview' | 'timeline' | 'messages' | 'notifications';

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

const fmt = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('pt-BR') : '-';

function missionCode(id: string): string {
  return id ? `MIS-${id.slice(0, 8).toUpperCase()}` : 'MIS-';
}

export default function MissionDetailPanel({ missionId, onClose }: MissionDetailPanelProps) {
  const [tab, setTab] = useState<TabKey>('timeline');
  const [mission, setMission] = useState<MissionDetail | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [notifications, setNotifications] = useState<NotificationRow[]>([]);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      const base = `http://localhost:8000/api/v1/missions/${missionId}`;
      const [mRes, msgRes, notifRes, tlRes] = await Promise.all([
        fetch(base),
        fetch(`${base}/messages?limit=500`),
        fetch(`${base}/notifications`),
        fetch(`${base}/timeline`),
      ]);
      if (!mRes.ok) throw new Error(`Mission ${mRes.status}`);
      setMission(await mRes.json());
      setMessages(msgRes.ok ? await msgRes.json() : []);
      setNotifications(notifRes.ok ? await notifRes.json() : []);
      setTimeline(tlRes.ok ? await tlRes.json() : []);
      setError(null);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [missionId]);

  return (
    <div style={overlayStyle} onClick={onClose}>
      <div style={drawerStyle} onClick={e => e.stopPropagation()}>
        <header style={headerStyle}>
          <div>
            <div style={{ fontSize: 11, color: '#64748b', marginBottom: 2 }}>MISSÃO</div>
            <div
              title={mission?.id || missionId}
              style={{
                color: '#93c5fd',
                fontSize: 11,
                fontWeight: 700,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
                marginBottom: 4,
              }}
            >
              {missionCode(mission?.id || missionId)}
            </div>
            <h2 style={{ margin: 0, fontSize: 16 }}>
              {mission?.title || mission?.contact_jid || missionId.slice(0, 8)}
            </h2>
          </div>
          <button onClick={onClose} style={closeBtnStyle} aria-label="Fechar">✕</button>
        </header>

        {loading && <p style={{ padding: 20 }}>Carregando...</p>}
        {error && <p style={{ padding: 20, color: '#ef4444' }}>Erro: {error}</p>}

        {!loading && mission && (
          <>
            <div style={tabsStyle}>
              {(['timeline', 'overview', 'messages', 'notifications'] as TabKey[]).map(k => (
                <button
                  key={k}
                  onClick={() => setTab(k)}
                  style={{
                    ...tabBtnStyle,
                    ...(tab === k ? tabActiveStyle : {}),
                  }}
                >
                  {k === 'overview' && 'Visão geral'}
                  {k === 'timeline' && `Timeline (${timeline.length})`}
                  {k === 'messages' && `Mensagens (${messages.length})`}
                  {k === 'notifications' && `Notificações (${notifications.length})`}
                </button>
              ))}
            </div>

            <div style={contentStyle}>
              {tab === 'overview' && <OverviewTab mission={mission} />}
              {tab === 'timeline' && <TimelineTab events={timeline} />}
              {tab === 'messages' && <MessagesTab messages={messages} />}
              {tab === 'notifications' && <NotificationsTab notifications={notifications} />}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function OverviewTab({ mission }: { mission: MissionDetail }) {
  const meta = mission.extra_metadata || {};
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Field label="Status">
        <span style={{
          background: STATUS_COLORS[mission.status] || '#64748b',
          borderRadius: 4,
          padding: '2px 10px',
          fontSize: 12,
          fontWeight: 600,
          color: '#fff',
        }}>
          {STATUS_LABELS[mission.status] || mission.status}
        </span>
      </Field>
      <Field label="Código">{missionCode(mission.id)}</Field>
      <Field label="ID da missão">
        <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', color: '#93c5fd' }}>
          {mission.id}
        </span>
      </Field>
      <Field label="Objetivo">{mission.goal || '-'}</Field>
      <Field label="Contato">
        {mission.contact_jid}
        {meta.contact_phone_e164 && ` (${meta.contact_phone_e164})`}
      </Field>
      <Field label="Modo">{meta.mode || '-'}</Field>
      <Field label="Canal">{mission.channel}</Field>
      <Field label="Criada em">{fmt(mission.created_at)}</Field>
      <Field label="Última atividade">{fmt(mission.last_activity_at)}</Field>
      <Field label="Concluída em">{fmt(mission.completed_at)}</Field>
      {meta.draft_text && (
        <Field label="Mensagem rascunho">
          <div style={draftBoxStyle}>"{meta.draft_text}"</div>
        </Field>
      )}
      {mission.result_summary && (
        <Field label="Resumo">{mission.result_summary}</Field>
      )}
    </div>
  );
}

function TimelineTab({ events }: { events: TimelineEvent[] }) {
  if (events.length === 0) return <p style={{ color: '#64748b' }}>Sem eventos.</p>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {events.map((e, idx) => {
        const color =
          e.type === 'milestone' ? '#3b82f6'
          : e.type === 'notification' ? '#f59e0b'
          : e.role === 'user' ? '#22c55e'
          : '#8b5cf6';
        const icon =
          e.type === 'milestone' ? '🏁'
          : e.type === 'notification' ? '🔔'
          : e.role === 'user' ? '👤'
          : e.tool_name ? '🛠️'
          : '🤖';
        return (
          <div key={idx} style={{
            display: 'grid',
            gridTemplateColumns: '20px 130px 1fr',
            gap: 10,
            padding: '8px 10px',
            background: '#13161f',
            border: `1px solid ${color}33`,
            borderLeft: `3px solid ${color}`,
            borderRadius: 6,
          }}>
            <div style={{ fontSize: 14 }}>{icon}</div>
            <div style={{ fontSize: 11, color: '#64748b' }}>{fmt(e.at)}</div>
            <div style={{ fontSize: 12 }}>
              <div style={{ fontWeight: 600, color: '#e2e8f0' }}>{e.label}</div>
              {e.detail && (
                <div style={{ marginTop: 2, color: '#94a3b8', whiteSpace: 'pre-wrap' }}>
                  {e.detail}
                </div>
              )}
              {e.tool_name && (
                <div style={{ marginTop: 2, fontSize: 11, color: '#64748b' }}>
                  tool: {e.tool_name}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function MessagesTab({ messages }: { messages: MessageRow[] }) {
  if (messages.length === 0) return <p style={{ color: '#64748b' }}>Sem mensagens.</p>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {messages.map(m => (
        <div key={m.id} style={{
          padding: '8px 12px',
          background: m.role === 'user' ? '#1f2a44' : '#1e2235',
          border: '1px solid #2d3355',
          borderRadius: 6,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#64748b', marginBottom: 4 }}>
            <span style={{ fontWeight: 600 }}>
              {m.role === 'user' ? '👤 Usuário' : m.role === 'assistant' ? '🤖 Assistente' : m.role}
              {m.tool_name && ` · ${m.tool_name}`}
            </span>
            <span>{fmt(m.created_at)}</span>
          </div>
          <div style={{ fontSize: 13, color: '#cbd5e1', whiteSpace: 'pre-wrap' }}>{m.content}</div>
        </div>
      ))}
    </div>
  );
}

function NotificationsTab({ notifications }: { notifications: NotificationRow[] }) {
  if (notifications.length === 0) return <p style={{ color: '#64748b' }}>Sem notificações.</p>;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {notifications.map(n => (
        <div key={n.id} style={{
          padding: '10px 12px',
          background: '#1e2235',
          border: '1px solid #2d3355',
          borderRadius: 6,
        }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#64748b', marginBottom: 4 }}>
            <span style={{ fontWeight: 600 }}>🔔 {n.kind}</span>
            <span>{fmt(n.created_at)}</span>
          </div>
          <div style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0' }}>{n.title}</div>
          <div style={{ fontSize: 12, marginTop: 2, color: '#94a3b8', whiteSpace: 'pre-wrap' }}>{n.body}</div>
          <div style={{ fontSize: 10, color: '#64748b', marginTop: 6 }}>
            entregue: {fmt(n.delivered_at)} · acked: {fmt(n.acked_at)}
          </div>
        </div>
      ))}
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
  width: 'min(720px, 100%)',
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

const tabsStyle: React.CSSProperties = {
  display: 'flex',
  gap: 4,
  padding: '8px 16px',
  borderBottom: '1px solid #2d3355',
};

const tabBtnStyle: React.CSSProperties = {
  background: 'transparent',
  border: 'none',
  color: '#64748b',
  padding: '6px 12px',
  cursor: 'pointer',
  fontSize: 12,
  borderRadius: 4,
};

const tabActiveStyle: React.CSSProperties = {
  background: '#1e2235',
  color: '#e2e8f0',
};

const contentStyle: React.CSSProperties = {
  flex: 1,
  overflowY: 'auto',
  padding: '16px 20px',
};

const draftBoxStyle: React.CSSProperties = {
  padding: '8px 12px',
  background: '#13161f',
  borderLeft: '3px solid #3b82f6',
  borderRadius: 4,
  fontStyle: 'italic',
  color: '#cbd5e1',
};
