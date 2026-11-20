// A lista de conversas, usada pela gaveta do modo compacto (SessionsSidebar) e
// pela coluna Conversas do chat largo (ConversationsPanel). As classes são as da
// gaveta (SessionsSidebar.css); a coluna só acrescenta grupos e o selo.

import type { ReactNode } from 'react';
import type { SessionWithHumanizedTime } from '../hooks/useSessionManagement';

export interface SessionGroup {
  label: string;
  sessions: SessionWithHumanizedTime[];
}

interface SessionsListProps {
  /** Lista corrida (gaveta)… */
  sessions?: SessionWithHumanizedTime[];
  /** …ou em grupos com cabeçalho (coluna: Hoje / Ontem / Esta semana…). */
  groups?: SessionGroup[];
  isLoading: boolean;
  error: string | null;
  currentSessionId: string;
  onSelect: (sessionId: string) => void;
  onRetry: () => void;
  emptyLabel?: string;
  /** Selo ao lado do título (coluna: tarefas desta conversa ainda abertas). */
  renderBadge?: (sessionId: string) => ReactNode;
  className?: string;
}

export function SessionsList({
  sessions,
  groups,
  isLoading,
  error,
  currentSessionId,
  onSelect,
  onRetry,
  emptyLabel = 'Nenhuma conversa anterior',
  renderBadge,
  className,
}: SessionsListProps) {
  const isEmpty = groups ? groups.every((group) => group.sessions.length === 0) : !sessions?.length;

  const renderItem = (session: SessionWithHumanizedTime) => {
    const isCurrent = session.session_id === currentSessionId;
    const title = <span className="session-title">{session.title || 'Conversa sem título'}</span>;
    const badge = renderBadge?.(session.session_id);
    return (
      <button
        key={session.session_id}
        type="button"
        className={`session-item${isCurrent ? ' current' : ''}`}
        onClick={() => onSelect(session.session_id)}
        title={session.title ?? undefined}
        aria-current={isCurrent || undefined}
      >
        {badge ? (
          <span className="session-title-row">
            {title}
            {badge}
          </span>
        ) : (
          title
        )}
        <span className="session-meta">
          {session.humanizedTime}
          {session.message_count > 0 && ` · ${session.message_count} msgs`}
          {isCurrent && ' · atual'}
        </span>
      </button>
    );
  };

  return (
    <div className={`sessions-list${className ? ` ${className}` : ''}`}>
      {isLoading ? (
        <div className="sessions-state">Carregando…</div>
      ) : error ? (
        <div className="sessions-state sessions-state-error">
          Não foi possível carregar as sessões.
          <button className="sessions-retry" onClick={onRetry}>Tentar de novo</button>
        </div>
      ) : isEmpty ? (
        <div className="sessions-state">{emptyLabel}</div>
      ) : groups ? (
        groups.map((group) =>
          group.sessions.length ? (
            <div key={group.label} className="sessions-group" role="group" aria-label={group.label}>
              <div className="sessions-group-label" aria-hidden="true">{group.label}</div>
              {group.sessions.map(renderItem)}
            </div>
          ) : null,
        )
      ) : (
        sessions?.map(renderItem)
      )}
    </div>
  );
}

export default SessionsList;
