// Apagar conversa do histórico: botão direito num item da lista → menu → "Apagar
// conversa" → confirmação → DELETE no core (sessão + mensagens no Postgres).
//
// Menu e diálogo vão num portal no <body>: a coluna Conversas tem overflow (o
// menu seria cortado) e o App.css força `position: relative` nos filhos de .app.
// O Escape é pego na CAPTURA da janela e parado ali, senão também fecharia a
// gaveta de sessões, que escuta o Escape na própria janela.

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import type { SessionWithHumanizedTime } from '../hooks/useSessionManagement';
import './SessionDelete.css';

const EDGE = 8;

function TrashIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </svg>
  );
}

/** Escape na captura da janela, parado ali (ver o topo do arquivo). */
function useEscape(onEscape: () => void): void {
  const handler = useRef(onEscape);
  useLayoutEffect(() => {
    handler.current = onEscape;
  });
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      event.preventDefault();
      handler.current();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, []);
}

interface SessionContextMenuProps {
  x: number;
  y: number;
  onDelete: () => void;
  onClose: () => void;
}

export function SessionContextMenu({ x, y, onDelete, onClose }: SessionContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });

  // Perto da borda direita/de baixo o menu abre para o outro lado.
  useLayoutEffect(() => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    setPos({
      left: Math.max(EDGE, Math.min(x, window.innerWidth - box.width - EDGE)),
      top: Math.max(EDGE, Math.min(y, window.innerHeight - box.height - EDGE)),
    });
  }, [x, y]);

  useEscape(onClose);

  useEffect(() => {
    ref.current?.querySelector('button')?.focus();
    // Clique (esquerdo ou direito) fora fecha. Um botão direito em OUTRO item
    // fecha este e o handler do item abre o novo no mesmo lote do React.
    const outside = (event: Event) => {
      if (ref.current?.contains(event.target as Node)) return;
      onClose();
    };
    window.addEventListener('mousedown', outside, true);
    window.addEventListener('contextmenu', outside, true);
    window.addEventListener('blur', onClose);
    window.addEventListener('resize', onClose);
    document.addEventListener('scroll', onClose, true);
    return () => {
      window.removeEventListener('mousedown', outside, true);
      window.removeEventListener('contextmenu', outside, true);
      window.removeEventListener('blur', onClose);
      window.removeEventListener('resize', onClose);
      document.removeEventListener('scroll', onClose, true);
    };
  }, [onClose]);

  return createPortal(
    <div
      ref={ref}
      className="session-menu"
      role="menu"
      aria-label="Ações da conversa"
      style={pos}
      onContextMenu={(event) => event.preventDefault()}
    >
      <button type="button" role="menuitem" className="session-menu-item session-menu-danger" onClick={onDelete}>
        <TrashIcon />
        Apagar conversa
      </button>
    </div>,
    document.body,
  );
}

interface DeleteSessionDialogProps {
  session: SessionWithHumanizedTime;
  isCurrent: boolean;
  onCancel: () => void;
  onDeleted: (sessionId: string) => void;
}

export function DeleteSessionDialog({ session, isCurrent, onCancel, onDeleted }: DeleteSessionDialogProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // Foco no Cancelar: um Enter distraído não apaga nada.
  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  useEscape(() => {
    if (!busy) onCancel();
  });

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const api = window.electronAPI;
      if (!api?.deleteSession) throw new Error('Sem a ponte do app (rodando fora do Tauri?)');
      await api.deleteSession(session.session_id);
      onDeleted(session.session_id);
    } catch (err) {
      // O core manda a frase pronta (ex.: o 409 de tarefa em andamento).
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const title = session.title || 'Conversa sem título';
  const count = session.message_count;

  return createPortal(
    <div
      className="session-dialog-overlay"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        className="session-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="session-dialog-heading"
        aria-describedby="session-dialog-body"
      >
        <h3 id="session-dialog-heading">Apagar conversa?</h3>
        <p className="session-dialog-name" title={title}>{title}</p>
        <div id="session-dialog-body" className="session-dialog-body">
          <p>
            {count === 1 ? 'A mensagem desta conversa será apagada' : `As ${count} mensagens desta conversa serão apagadas`}
            {' '}do Neo. Não dá para desfazer.
          </p>
          {isCurrent && <p>É a conversa aberta agora: o chat passa para uma conversa nova.</p>}
        </div>
        {error && (
          <p className="session-dialog-error" role="alert">
            {error}
          </p>
        )}
        <div className="session-dialog-actions">
          <button ref={cancelRef} type="button" className="session-dialog-cancel" onClick={onCancel} disabled={busy}>
            Cancelar
          </button>
          <button type="button" className="session-dialog-delete" onClick={() => void confirm()} disabled={busy}>
            <TrashIcon />
            {busy ? 'Apagando…' : 'Apagar'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
