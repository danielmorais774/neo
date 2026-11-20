import { useEffect } from 'react';
import { useSessionManagement } from '../hooks/useSessionManagement';
import { SessionsList } from './SessionsList';
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

        <SessionsList
          sessions={sessions}
          isLoading={isLoading}
          error={error}
          currentSessionId={currentSessionId}
          onSelect={onSelectSession}
          onRetry={loadSessions}
        />
      </aside>
    </>
  );
}

export default SessionsSidebar;
