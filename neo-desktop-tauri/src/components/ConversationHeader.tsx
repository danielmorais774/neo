// Topo do centro no chat largo: o título da conversa aberta (da lista de sessões)
// e, quando ela tem tarefa em segundo plano, um selo que abre a coluna direita.
// Fica FORA do ChatPage, como as colunas: atualizar isto não re-renderiza o chat.

import { useMemo } from 'react';
import { useActiveSession } from '../lib/activeSession';
import { OPEN_STATUSES } from '../lib/jobs';
import { useJobs } from '../hooks/useJobs';
import { useSessionsList } from '../hooks/useSessionsList';

interface ConversationHeaderProps {
  /** Mostra a coluna "Segundo plano" (o selo de tarefas leva até ela). */
  onShowJobs: () => void;
}

export function ConversationHeader({ onShowJobs }: ConversationHeaderProps) {
  const currentSessionId = useActiveSession();
  const { sessions, status } = useSessionsList();
  const { jobs } = useJobs();

  const session = sessions.find((s) => s.session_id === currentSessionId);
  const title = session
    ? session.title || 'Conversa sem título'
    : status === 'ready'
      ? 'Nova conversa'
      : '';

  const { open, waiting } = useMemo(() => {
    const here = jobs.filter((job) => job.session_id === currentSessionId && OPEN_STATUSES.has(job.status));
    return { open: here.length, waiting: here.filter((job) => job.status === 'waiting').length };
  }, [jobs, currentSessionId]);

  return (
    <div className="chat-center-header">
      <h1 className="chat-center-title" title={title}>
        {title}
      </h1>
      {open > 0 && (
        <button
          type="button"
          className={`chat-center-chip${waiting ? ' waiting' : ''}`}
          onClick={onShowJobs}
          title="Ver no Segundo plano"
        >
          <span className="chat-center-chip-dot" />
          {waiting
            ? `${waiting === 1 ? '1 tarefa esperando' : `${waiting} tarefas esperando`} você`
            : `${open} em segundo plano`}
        </button>
      )}
    </div>
  );
}

export default ConversationHeader;
