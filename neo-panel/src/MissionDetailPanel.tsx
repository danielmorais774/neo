import { useEffect, useMemo, useState } from 'react';

interface MissionDetailPanelProps {
  missionId: string;
  onClose: () => void;
}

interface MissionDetail {
  id: string;
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

interface MessageRow {
  id: string;
  mission_id?: string | null;
  session_id?: string | null;
  role: string;
  content: string;
  tool_name: string | null;
  tool_call_id: string | null;
  created_at: string | null;
  extra_metadata?: Record<string, any> | null;
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

type TabKey = 'overview' | 'timeline' | 'messages' | 'notifications' | 'pipeline_logs';

interface MissionPipelineLog {
  exists: boolean;
  content: string;
  path: string;
  updated_at: number | null;
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

const fmt = (iso: string | null) =>
  iso ? new Date(iso).toLocaleString('pt-BR') : '-';

function missionCode(id: string): string {
  return id ? `MIS-${id.slice(0, 8).toUpperCase()}` : 'MIS-';
}

function draftMessagesFor(mission: MissionDetail): string[] {
  const meta = mission.extra_metadata || {};
  const value = mission.next_action_params?.messages || meta.draft_messages;
  if (Array.isArray(value)) {
    return value.map(v => String(v).trim()).filter(Boolean);
  }
  const fallback = mission.next_action_params?.text || meta.draft_text || '';
  return fallback ? [fallback] : [];
}

export default function MissionDetailPanel({ missionId, onClose }: MissionDetailPanelProps) {
  const [tab, setTab] = useState<TabKey>('timeline');
  const [mission, setMission] = useState<MissionDetail | null>(null);
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [notifications, setNotifications] = useState<NotificationRow[]>([]);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [pipelineLog, setPipelineLog] = useState<MissionPipelineLog | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingPipelineLog, setLoadingPipelineLog] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadPipelineLog = async () => {
    setLoadingPipelineLog(true);
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/v1/missions/${missionId}/pipeline-log`);
      setPipelineLog(res.ok ? await res.json() : null);
    } finally {
      setLoadingPipelineLog(false);
    }
  };

  const load = async () => {
    try {
      const base = `http://127.0.0.1:8000/api/v1/missions/${missionId}`;
      const [mRes, msgRes, notifRes, tlRes, logRes] = await Promise.all([
        fetch(base),
        fetch(`${base}/messages?limit=500`),
        fetch(`${base}/notifications`),
        fetch(`${base}/timeline`),
        fetch(`${base}/pipeline-log`),
      ]);
      if (!mRes.ok) throw new Error(`Mission ${mRes.status}`);
      setMission(await mRes.json());
      setMessages(msgRes.ok ? await msgRes.json() : []);
      setNotifications(notifRes.ok ? await notifRes.json() : []);
      setTimeline(tlRes.ok ? await tlRes.json() : []);
      setPipelineLog(logRes.ok ? await logRes.json() : null);
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
              {(['timeline', 'overview', 'messages', 'notifications', 'pipeline_logs'] as TabKey[]).map(k => (
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
                  {k === 'pipeline_logs' && 'Logs Pipeline'}
                </button>
              ))}
            </div>

            <div style={contentStyle}>
              {tab === 'overview' && <OverviewTab mission={mission} />}
              {tab === 'timeline' && <TimelineTab events={timeline} />}
              {tab === 'messages' && <MessagesTab messages={messages} />}
              {tab === 'notifications' && <NotificationsTab notifications={notifications} />}
              {tab === 'pipeline_logs' && (
                <PipelineLogsTab
                  pipelineLog={pipelineLog}
                  loading={loadingPipelineLog}
                  onRefresh={loadPipelineLog}
                />
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function OverviewTab({ mission }: { mission: MissionDetail }) {
  const meta = mission.extra_metadata || {};
  const draftMessages = draftMessagesFor(mission);
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
      <Field label="Session ID">
        <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', color: '#93c5fd' }}>
          {mission.session_id}
        </span>
      </Field>
      <Field label="Parent Session ID">
        <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', color: '#93c5fd' }}>
          {mission.parent_session_id || '-'}
        </span>
      </Field>
      <Field label="Objetivo">{mission.goal || '-'}</Field>
      <Field label="Contato">
        {mission.contact_jid}
        {meta.contact_phone_e164 && ` (${meta.contact_phone_e164})`}
      </Field>
      <Field label="Modo">{meta.mode || '-'}</Field>
      <Field label="Canal">{mission.channel}</Field>
      <Field label="Próxima ação">{mission.next_action_type || '-'}</Field>
      {mission.next_action_type && (
        <Field label="Parâmetros da próxima ação">
          <pre style={jsonBoxStyle}>{JSON.stringify(mission.next_action_params || {}, null, 2)}</pre>
        </Field>
      )}
      <Field label="Criada em">{fmt(mission.created_at)}</Field>
      <Field label="Última atividade">{fmt(mission.last_activity_at)}</Field>
      <Field label="Concluída em">{fmt(mission.completed_at)}</Field>
      {draftMessages.length > 0 && (
        <Field label={draftMessages.length > 1 ? "Mensagens rascunho" : "Mensagem rascunho"}>
          <div style={draftBoxStyle}>
            {draftMessages.map((message, index) => (
              <div key={index}>
                {draftMessages.length > 1 ? `${index + 1}. ` : ''}"{message}"
              </div>
            ))}
          </div>
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
      {messages.map(m => {
        const meta = m.extra_metadata || {};
        const senderLabel = m.role === 'user'
          ? `👤 ${meta.contact_label || meta.contact_jid || 'Destinatário'}`
          : m.role === 'assistant'
            ? '🤖 Neo'
            : m.role;
        return (
          <div key={m.id} style={{
            padding: '8px 12px',
            background: m.role === 'user' ? '#1f2a44' : '#1e2235',
            border: '1px solid #2d3355',
            borderRadius: 6,
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: '#64748b', marginBottom: 4 }}>
              <span style={{ fontWeight: 600 }}>
                {senderLabel}
                {m.tool_name && ` · ${m.tool_name}`}
                {m.mission_id && ' · missão vinculada'}
              </span>
              <span>{fmt(m.created_at)}</span>
            </div>
            <div style={{ fontSize: 13, color: '#cbd5e1', whiteSpace: 'pre-wrap' }}>{m.content}</div>
          </div>
        );
      })}
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

function PipelineLogsTab({
  pipelineLog,
  loading,
  onRefresh,
}: {
  pipelineLog: MissionPipelineLog | null;
  loading: boolean;
  onRefresh: () => void;
}) {
  const [previewEnabled, setPreviewEnabled] = useState(true);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}>
        <div style={{ fontSize: 12, color: '#64748b' }}>
          {pipelineLog?.updated_at
            ? `Atualizado: ${new Date(pipelineLog.updated_at * 1000).toLocaleString('pt-BR')}`
            : 'Sem arquivo de log para esta missão'}
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={() => setPreviewEnabled(v => !v)} style={closeBtnStyle}>
            {previewEnabled ? 'Preview MD: ON' : 'Preview MD: OFF'}
          </button>
          <button onClick={onRefresh} style={closeBtnStyle}>
            {loading ? 'Recarregando...' : 'Recarregar arquivo'}
          </button>
        </div>
      </div>

      {pipelineLog?.exists ? (
        previewEnabled
          ? <SimpleMarkdown text={pipelineLog.content} />
          : <pre style={pipelineLogBoxStyle}>{pipelineLog.content}</pre>
      ) : (
        <p style={{ color: '#64748b' }}>Arquivo ainda não foi gerado.</p>
      )}
    </div>
  );
}

function SimpleMarkdown({ text }: { text: string }) {
  const rendered = useMemo(() => {
    const lines = text.split('\n');
    const elements: React.ReactNode[] = [];
    let key = 0;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      if (line.startsWith('```')) {
        const language = line.slice(3).trim();
        const codeLines: string[] = [];
        i += 1;
        while (i < lines.length && !lines[i].startsWith('```')) {
          codeLines.push(lines[i]);
          i += 1;
        }
        elements.push(
          <div key={key++} style={markdownCodeBlockWrapStyle}>
            {language && <div style={markdownCodeLangStyle}>{language}</div>}
            <pre style={markdownCodeBlockStyle}>{codeLines.join('\n') || ' '}</pre>
          </div>
        );
        continue;
      }

      if (/^---+$/.test(line.trim())) {
        elements.push(<hr key={key++} style={{ border: 'none', borderTop: '1px solid #2d3355', margin: '12px 0' }} />);
        continue;
      }

      const hMatch = line.match(/^(#{1,4})\s+(.+)$/);
      if (hMatch) {
        const level = hMatch[1].length;
        const sizes = [19, 16, 14, 13];
        const colors = ['#e2e8f0', '#93c5fd', '#67e8f9', '#94a3b8'];
        elements.push(
          <div key={key++} style={{ fontSize: sizes[level - 1], fontWeight: 700, color: colors[level - 1], marginTop: level <= 2 ? 14 : 8, marginBottom: 5 }}>
            {inlineFormat(line.replace(/^#{1,4}\s+/, ''))}
          </div>
        );
        continue;
      }

      if (/^\s*[-*]\s+/.test(line)) {
        const content = line.replace(/^\s*[-*]\s+/, '');
        elements.push(
          <div key={key++} style={{ display: 'flex', gap: 6, fontSize: 12, color: '#cbd5e1', lineHeight: 1.7 }}>
            <span style={{ color: '#64748b' }}>•</span>
            <span>{inlineFormat(content)}</span>
          </div>
        );
        continue;
      }

      if (!line.trim()) {
        elements.push(<div key={key++} style={{ height: 8 }} />);
        continue;
      }

      elements.push(
        <div key={key++} style={{ fontSize: 12, color: '#cbd5e1', lineHeight: 1.7 }}>
          {inlineFormat(line)}
        </div>
      );
    }

    return elements;
  }, [text]);

  return <div style={pipelineLogMarkdownBoxStyle}>{rendered}</div>;
}

function inlineFormat(text: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let k = 0;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    if (match[2]) {
      parts.push(<strong key={k++} style={{ color: '#e2e8f0', fontWeight: 700 }}>{match[2]}</strong>);
    } else if (match[3]) {
      parts.push(<em key={k++} style={{ color: '#a5b4fc' }}>{match[3]}</em>);
    } else if (match[4]) {
      parts.push(
        <code key={k++} style={{ background: '#111827', color: '#c4b5fd', padding: '1px 5px', borderRadius: 3, fontSize: '0.9em' }}>
          {match[4]}
        </code>
      );
    }
    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts.length > 0 ? <>{parts}</> : text;
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

const jsonBoxStyle: React.CSSProperties = {
  margin: 0,
  padding: '8px 10px',
  background: '#0b0f1a',
  border: '1px solid #263149',
  borderRadius: 4,
  color: '#cbd5e1',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontSize: 11,
  lineHeight: 1.45,
};

const pipelineLogBoxStyle: React.CSSProperties = {
  margin: 0,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  fontSize: 12,
  lineHeight: 1.45,
  color: '#cbd5e1',
  background: '#0b0f1a',
  border: '1px solid #2d3355',
  borderRadius: 6,
  padding: '12px 14px',
  maxHeight: '70vh',
  overflow: 'auto',
};

const pipelineLogMarkdownBoxStyle: React.CSSProperties = {
  background: '#0b0f1a',
  border: '1px solid #2d3355',
  borderRadius: 6,
  padding: '12px 14px',
  maxHeight: '70vh',
  overflow: 'auto',
};

const markdownCodeBlockWrapStyle: React.CSSProperties = {
  margin: '10px 0',
  border: '1px solid #263149',
  borderRadius: 6,
  background: '#080d16',
  overflow: 'hidden',
};

const markdownCodeLangStyle: React.CSSProperties = {
  padding: '6px 10px',
  borderBottom: '1px solid #263149',
  color: '#93c5fd',
  background: '#111827',
  fontSize: 11,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
};

const markdownCodeBlockStyle: React.CSSProperties = {
  margin: 0,
  padding: '10px 12px',
  color: '#dbeafe',
  whiteSpace: 'pre',
  overflowX: 'auto',
  fontSize: 12,
  lineHeight: 1.55,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
};
