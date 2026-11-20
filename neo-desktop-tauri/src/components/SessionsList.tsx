// A lista de conversas, usada pela gaveta do modo compacto (SessionsSidebar) e
// pela coluna Conversas do chat largo (ConversationsPanel). As classes são as da
// gaveta (SessionsSidebar.css); a coluna só acrescenta grupos e o selo.
//
// Botão direito num item abre o menu de "Apagar conversa" (SessionDelete.tsx). O
// que vem depois de apagar mora aqui, igual nas duas vistas: a conversa sai da
// lista das colunas e, se era a aberta, o chat vai para uma conversa nova.

import { useCallback, useState, type MouseEvent, type ReactNode } from 'react';
import type { SessionWithHumanizedTime } from '../hooks/useSessionManagement';
import { forgetSession } from '../hooks/useSessionsList';
import { startNewChat } from '../lib/activeSession';
import { DeleteSessionDialog, SessionContextMenu } from './SessionDelete';

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
  /** Depois de apagar (a gaveta recarrega a lista dela, que não é a das colunas). */
  onDeleted?: (sessionId: string) => void;
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
  onDeleted,
  className,
}: SessionsListProps) {
  const [menu, setMenu] = useState<{ session: SessionWithHumanizedTime; x: number; y: number } | null>(null);
  const [toDelete, setToDelete] = useState<SessionWithHumanizedTime | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);

  const openMenu = (event: MouseEvent<HTMLButtonElement>, session: SessionWithHumanizedTime) => {
    event.preventDefault();
    // "Nova conversa" ainda não existe no core (entra no fim do 1º turno).
    if (session.message_count === 0) return;
    let { clientX: x, clientY: y } = event;
    // Pela tecla de menu / Shift+F10 o evento vem sem ponteiro: abre sob o item.
    if (x === 0 && y === 0) {
      const box = event.currentTarget.getBoundingClientRect();
      x = box.left + 16;
      y = box.bottom;
    }
    setMenu({ session, x, y });
  };

  const handleDeleted = (sessionId: string) => {
    setToDelete(null);
    forgetSession(sessionId);
    if (sessionId === currentSessionId) startNewChat();
    onDeleted?.(sessionId);
  };

  const isEmpty = groups ? groups.every((group) => group.sessions.length === 0) : !sessions?.length;

  const renderItem = (session: SessionWithHumanizedTime) => {
    const isCurrent = session.session_id === currentSessionId;
    const title = <span className="session-title">{session.title || 'Conversa sem título'}</span>;
    const badge = renderBadge?.(session.session_id);
    return (
      <button
        key={session.session_id}
        type="button"
        className={`session-item${isCurrent ? ' current' : ''}${menu?.session.session_id === session.session_id ? ' menu-open' : ''}`}
        onClick={() => onSelect(session.session_id)}
        onContextMenu={(event) => openMenu(event, session)}
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

      {menu && (
        <SessionContextMenu
          x={menu.x}
          y={menu.y}
          onClose={closeMenu}
          onDelete={() => {
            setToDelete(menu.session);
            setMenu(null);
          }}
        />
      )}
      {toDelete && (
        <DeleteSessionDialog
          session={toDelete}
          isCurrent={toDelete.session_id === currentSessionId}
          onCancel={() => setToDelete(null)}
          onDeleted={handleDeleted}
        />
      )}
    </div>
  );
}

export default SessionsList;
