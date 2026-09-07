import { useEffect } from 'react';
import { useSessionManagement } from '../hooks/useSessionManagement';
import './SessionsSidebar.css';

interface SessionsSidebarProps {
  open: boolean;
  onClose: () => void;
  onSelectSession: (sessionId: string) => void;
}

export function SessionsSidebar({ open, onClose, onSelectSession }: SessionsSidebarProps) {
  const { sessions, isLoading, error, currentSessionId, loadSessions } = useSessionManagement();

  // Recarrega a cada abertura: a lista vem do core e pode ter mudado desde a
  // última vez (inclusive por conversa feita no painel web ou no notch).
  useEffect(() => {
    if (open) loadSessions();
  }, [open, loadSessions]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      <div className="sessions-sidebar-overlay" onClick={onClose} />
      <aside className="sessions-sidebar" role="dialog" aria-label="Sessões anteriores">
        <div className="sessions-header">
          <h3>Sessões anteriores</h3>
          <button className="sessions-close-btn" onClick={onClose} title="Fechar" aria-label="Fechar">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="sessions-list">
          {isLoading ? (
            <div className="sessions-state">Carregando…</div>
          ) : error ? (
            <div className="sessions-state sessions-state-error">
              Não foi possível carregar as sessões.
              <button className="sessions-retry" onClick={loadSessions}>Tentar de novo</button>
            </div>
          ) : sessions.length === 0 ? (
            <div className="sessions-state">Nenhuma conversa anterior</div>
          ) : (
            sessions.map((session) => {
              const isCurrent = session.session_id === currentSessionId;
              return (
                <button
                  key={session.session_id}
                  type="button"
                  className={`session-item${isCurrent ? ' current' : ''}`}
                  onClick={() => onSelectSession(session.session_id)}
                  title={session.title ?? undefined}
                >
                  <span className="session-title">
                    {session.title || 'Conversa sem título'}
                  </span>
                  <span className="session-meta">
                    {session.humanizedTime}
                    {session.message_count > 0 && ` · ${session.message_count} msgs`}
                    {isCurrent && ' · atual'}
                  </span>
                </button>
              );
            })
          )}
        </div>
      </aside>
    </>
  );
}

export default SessionsSidebar;
