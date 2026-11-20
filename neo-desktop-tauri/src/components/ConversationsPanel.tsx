// Coluna esquerda do chat largo, em cima: as conversas, com busca e grupos por
// dia. Trocar de conversa e "Novo chat" disparam os mesmos eventos da gaveta e da
// barra (`neo-load-session` / `neo-new-chat`); quem troca continua sendo o ChatPage.

import { useCallback, useMemo, useState } from 'react';
import { SessionsList, type SessionGroup } from './SessionsList';
import { parseUtc, type SessionWithHumanizedTime } from '../hooks/useSessionManagement';
import { refreshSessions, useSessionsList } from '../hooks/useSessionsList';
import { useJobs } from '../hooks/useJobs';
import { openConversation, startNewChat, useActiveSession } from '../lib/activeSession';
import { countOpenJobsBySession } from '../lib/jobs';
import './ConversationsPanel.css';

const DAY_MS = 86_400_000;
const GROUP_ORDER = ['Hoje', 'Ontem', 'Esta semana', 'Mais antigas'] as const;

/** Busca sem acento e sem caixa: "reuniao" acha "Reunião". */
function fold(text: string): string {
  return text.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function groupByDay(sessions: SessionWithHumanizedTime[]): SessionGroup[] {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const startToday = today.getTime();
  const startYesterday = startToday - DAY_MS;
  const startWeek = startToday - 6 * DAY_MS;

  const buckets = new Map<string, SessionWithHumanizedTime[]>(GROUP_ORDER.map((label) => [label, []]));
  for (const session of sessions) {
    const when = session.last_activity_at ? parseUtc(session.last_activity_at).getTime() : Number.NaN;
    const label = Number.isNaN(when)
      ? 'Mais antigas'
      : when >= startToday
        ? 'Hoje'
        : when >= startYesterday
          ? 'Ontem'
          : when >= startWeek
            ? 'Esta semana'
            : 'Mais antigas';
    buckets.get(label)?.push(session);
  }
  return GROUP_ORDER.map((label) => ({ label, sessions: buckets.get(label) ?? [] }));
}

export function ConversationsPanel() {
  const { sessions, status, error } = useSessionsList();
  const { jobs } = useJobs();
  const currentSessionId = useActiveSession();
  const [query, setQuery] = useState('');

  const openBySession = useMemo(() => countOpenJobsBySession(jobs), [jobs]);

  const groups = useMemo(() => {
    const needle = fold(query.trim());
    const matching = needle ? sessions.filter((s) => fold(s.title || '').includes(needle)) : sessions;
    const grouped = groupByDay(matching);
    // Conversa nova ainda não está na lista do core (entra quando a 1ª resposta
    // fecha o turno): aparece no topo, destacada, para a coluna não parecer
    // apontar para a conversa errada.
    if (!needle && currentSessionId && status === 'ready' && !sessions.some((s) => s.session_id === currentSessionId)) {
      grouped[0].sessions = [
        {
          session_id: currentSessionId,
          title: 'Nova conversa',
          message_count: 0,
          last_activity_at: null,
          humanizedTime: 'agora',
        },
        ...grouped[0].sessions,
      ];
    }
    return grouped;
  }, [sessions, query, currentSessionId, status]);

  const renderBadge = useCallback(
    (sessionId: string) => {
      const open = openBySession.get(sessionId);
      if (!open) return null;
      // Esperando você pesa mais que rodando: é a que pede um gesto.
      if (open.waiting) {
        return (
          <span className="conv-badge conv-badge-waiting" title="Uma tarefa desta conversa parou esperando você">
            <span className="conv-badge-dot" />
            esperando
          </span>
        );
      }
      return (
        <span className="conv-badge" title="Tarefas desta conversa em segundo plano">
          <span className="conv-badge-dot" />
          {open.running} rodando
        </span>
      );
    },
    [openBySession],
  );

  return (
    <section className="conv-panel" aria-label="Conversas">
      <div className="conv-head">
        <div className="conv-head-row">
          <h2>Conversas</h2>
          <button type="button" className="conv-new" onClick={startNewChat}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" aria-hidden="true">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
            Novo chat
          </button>
        </div>
        <label className="conv-search">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setQuery('');
            }}
            placeholder="Buscar conversas"
            aria-label="Buscar conversas"
          />
        </label>
      </div>

      <SessionsList
        className="conv-list"
        groups={groups}
        isLoading={(status === 'loading' || status === 'idle') && !sessions.length}
        error={status === 'error' ? error : null}
        currentSessionId={currentSessionId}
        onSelect={openConversation}
        onRetry={() => void refreshSessions()}
        emptyLabel={query.trim() ? 'Nenhuma conversa com esse título' : 'Nenhuma conversa ainda'}
        renderBadge={renderBadge}
      />
    </section>
  );
}

export default ConversationsPanel;
