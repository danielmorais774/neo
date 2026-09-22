import { useEffect, useState } from 'react';
import type { FileChangeInfo } from '../types';
import './FileChangeCard.css';

// Card de uma alteração (ou pedido de leitura) de arquivo feita pelo desktop —
// services/file_ops.rs. Dentro das pastas liberadas a alteração já chega
// "applied" com Desfazer; fora delas chega "pending" e só grava com o clique.

const STATUS_LABEL: Record<FileChangeInfo['status'], string> = {
  pending: 'Aguardando você',
  applied: 'Alterado',
  rejected: 'Recusado',
  failed: 'Não gravado',
  undone: 'Desfeito',
};

// Diff grande abre fechado: o card não pode empurrar a conversa pra fora da tela.
const DIFF_OPEN_MAX_LINES = 40;

function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, '').split('\n');
  return (
    <pre className="fc-diff">
      {lines.map((line, i) => {
        const kind = line.startsWith('@@')
          ? 'fc-hunk'
          : line.startsWith('+')
            ? 'fc-add'
            : line.startsWith('-')
              ? 'fc-del'
              : 'fc-ctx';
        return (
          <div key={i} className={kind}>
            {line || ' '}
          </div>
        );
      })}
    </pre>
  );
}

function FileChangeCard({ change }: { change: FileChangeInfo }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const diffLines = change.diff ? change.diff.replace(/\n$/, '').split('\n').length : 0;
  const [open, setOpen] = useState(diffLines > 0 && diffLines <= DIFF_OPEN_MAX_LINES);

  // A resposta do clique chega como um novo evento de status; aí o card destrava.
  useEffect(() => {
    setBusy(false);
  }, [change.status]);

  const isRead = change.kind === 'read';
  const name = change.path.split('/').pop() || change.path;
  const engine = change.engine === 'opencode' ? 'opencode' : 'Neo';
  const title = isRead
    ? '👁 Leitura de arquivo'
    : change.created
      ? '📄 Arquivo criado'
      : '✏️ Arquivo alterado';

  const respond = async (approved: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await window.electronAPI?.respondFileChange(change.changeId, approved);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  const undo = async () => {
    setBusy(true);
    setError(null);
    try {
      await window.electronAPI?.undoFileChange(change.changeId);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  };

  const reveal = () => {
    window.electronAPI?.revealLocalFile(change.path).catch(() => {
      // Arquivo apagado (ex.: criação desfeita) — nada a revelar.
    });
  };

  return (
    <div className={`fc-card fc-${change.status}`}>
      <div className="fc-header">
        <span className="fc-title">{title}</span>
        <span className={`fc-status fc-status-${change.status}`}>
          {STATUS_LABEL[change.status] ?? change.status}
        </span>
      </div>

      <button type="button" className="fc-path" title="Mostrar no Finder" onClick={reveal}>
        <span className="fc-name">{name}</span>
        <span className="fc-path-full">{change.path}</span>
      </button>

      {!isRead && (
        <div className="fc-meta">
          <span>por {engine}</span>
          {(change.linesAdded || change.linesRemoved) ? (
            <span>
              <span className="fc-plus">+{change.linesAdded ?? 0}</span>{' '}
              <span className="fc-minus">−{change.linesRemoved ?? 0}</span>
            </span>
          ) : null}
        </div>
      )}

      {change.reason && change.status === 'pending' && (
        <div className="fc-reason">⚠️ {change.reason}</div>
      )}
      {change.summary && <div className="fc-summary">💬 {change.summary}</div>}

      {change.diff && (
        <>
          <button type="button" className="fc-toggle" onClick={() => setOpen((o) => !o)}>
            {open ? 'Ocultar diff' : `Ver diff (${diffLines} linhas)`}
          </button>
          {open && <DiffView diff={change.diff} />}
        </>
      )}

      {change.status === 'pending' && (
        <div className="fc-actions">
          <button
            type="button"
            className="btn-reject-inline"
            disabled={busy}
            onClick={() => void respond(false)}
          >
            ✕ Rejeitar
          </button>
          <button
            type="button"
            className="btn-approve-inline"
            disabled={busy}
            onClick={() => void respond(true)}
          >
            {busy ? '⏳ Aguardando...' : isRead ? '✓ Liberar leitura' : '✓ Aprovar e gravar'}
          </button>
        </div>
      )}

      {change.status === 'applied' && (
        <div className="fc-actions">
          <button type="button" className="fc-undo" disabled={busy} onClick={() => void undo()}>
            {busy ? '⏳ Desfazendo...' : '↶ Desfazer'}
          </button>
        </div>
      )}

      {error && <div className="fc-error">{error}</div>}
    </div>
  );
}

export default FileChangeCard;
