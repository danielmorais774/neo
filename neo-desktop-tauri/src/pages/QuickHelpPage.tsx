import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './QuickHelpPage.css';

type State = 'idle' | 'loading' | 'streaming' | 'answered' | 'error';

type PreviewNotification = {
  id: string;
  icon: string;
  title: string;
  subtitle: string;
  time: string;
  badge?: string;
};

type PreviewTask = {
  id: string;
  title: string;
  progress: number;
  eta: string;
};

const PREVIEW_NOTIFICATIONS: PreviewNotification[] = [
  {
    id: 'summary',
    icon: '✦',
    title: 'Resumo do call pronto',
    subtitle: '12 itens · 4 ações sugeridas',
    time: 'agora',
  },
  {
    id: 'marina',
    icon: '💬',
    title: 'Marina Lopes',
    subtitle: 'Te envio o briefing em 10min',
    time: '2m',
  },
  {
    id: 'team',
    icon: '💬',
    title: 'Time · Acme',
    subtitle: 'Lucas: revisei a PR, pode mergear',
    time: '8m',
    badge: '3',
  },
];

const PREVIEW_TASKS: PreviewTask[] = [
  { id: 'task-1', title: 'Indexar Notion', progress: 72, eta: '1m' },
  { id: 'task-2', title: 'Transcrição · call.mp3', progress: 34, eta: '4m' },
  { id: 'task-3', title: 'Gerar 6 thumbs', progress: 12, eta: '7m' },
];

export function QuickHelpPage() {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [state, setState] = useState<State>('idle');
  const inputRef = useRef<HTMLInputElement>(null);
  const resizedRef = useRef(false);

  useEffect(() => {
    inputRef.current?.focus();

    window.electronAPI?.onQuickHelpChunk?.((chunk) => {
      if (!resizedRef.current) {
        resizedRef.current = true;
        window.electronAPI?.quickHelpResize?.(true);
      }
      setState('streaming');
      setAnswer(prev => prev + chunk);
    });

    window.electronAPI?.onQuickHelpDone?.(() => {
      setState('answered');
    });

    window.electronAPI?.onQuickHelpError?.((msg) => {
      setState('error');
      setAnswer(msg);
      if (!resizedRef.current) {
        resizedRef.current = true;
        window.electronAPI?.quickHelpResize?.(true);
      }
    });
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = question.trim();
    if (!q || state === 'loading' || state === 'streaming') return;

    resizedRef.current = false;
    setState('loading');
    setAnswer('');
    window.electronAPI?.quickHelpAskStream?.(q);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      window.electronAPI?.closeWindow?.();
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      handleSubmit(e as unknown as React.FormEvent);
    }
  };

  const handleClear = () => {
    setQuestion('');
    setAnswer('');
    setState('idle');
    resizedRef.current = false;
    window.electronAPI?.quickHelpResize?.(false);
    inputRef.current?.focus();
  };

  const handleDragStart = (e: React.MouseEvent) => {
    e.preventDefault();
    let lastX = e.screenX;
    let lastY = e.screenY;

    const onMove = (ev: MouseEvent) => {
      const dx = ev.screenX - lastX;
      const dy = ev.screenY - lastY;
      lastX = ev.screenX;
      lastY = ev.screenY;
      if (dx !== 0 || dy !== 0) window.electronAPI?.quickHelpMoveBy?.(dx, dy);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  };

  const isAnswering = state === 'streaming' || state === 'loading';
  const isIdle = state === 'idle' && !answer;

  return (
    <div className="qh-root">
      <div className="qh-bg-glow" aria-hidden="true" />

      <form className="qh-bar" onSubmit={handleSubmit}>
        <span className="qh-drag-handle" onMouseDown={handleDragStart} title="Arraste para mover">
          <svg width="10" height="16" viewBox="0 0 10 16" fill="currentColor">
            <circle cx="2" cy="3" r="1.2" /><circle cx="8" cy="3" r="1.2" />
            <circle cx="2" cy="8" r="1.2" /><circle cx="8" cy="8" r="1.2" />
            <circle cx="2" cy="13" r="1.2" /><circle cx="8" cy="13" r="1.2" />
          </svg>
        </span>

        <div className="qh-agent-dot" aria-hidden="true">◆</div>

        <input
          ref={inputRef}
          className="qh-input"
          type="text"
          placeholder="Olá, em que posso ajudar?"
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={handleKeyDown}
          autoComplete="off"
          spellCheck={false}
        />

        {state === 'loading' && <span className="qh-spinner" aria-label="carregando" />}
        {state === 'streaming' && <span className="qh-cursor-blink" />}

        {(state === 'answered' || state === 'error') ? (
          <button type="button" className="qh-clear" onClick={handleClear} title="Nova pergunta">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        ) : (
          <button type="submit" className="qh-mic-btn" title="Enviar">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M12 14a3 3 0 0 0 3-3V7a3 3 0 1 0-6 0v4a3 3 0 0 0 3 3Z" />
              <path d="M19 11a7 7 0 0 1-14 0" />
              <path d="M12 18v3" />
            </svg>
          </button>
        )}
      </form>

      {isIdle && (
        <div className="qh-preview">
          <section className="qh-preview-section">
            <header className="qh-preview-header">
              <span>Notificações</span>
              <strong>{PREVIEW_NOTIFICATIONS.length}</strong>
            </header>

            <div className="qh-preview-list">
              {PREVIEW_NOTIFICATIONS.map((item) => (
                <article key={item.id} className="qh-card">
                  <div className="qh-card-icon">{item.icon}</div>
                  <div className="qh-card-main">
                    <h4>{item.title}</h4>
                    <p>{item.subtitle}</p>
                  </div>
                  <div className="qh-card-meta">
                    <span>{item.time}</span>
                    {item.badge ? <em>{item.badge}</em> : null}
                  </div>
                </article>
              ))}
            </div>
          </section>

          <section className="qh-preview-section">
            <header className="qh-preview-header">
              <span>Filas de Processamento</span>
              <strong>{PREVIEW_TASKS.length} ativas</strong>
            </header>

            <div className="qh-queue">
              {PREVIEW_TASKS.map((task) => (
                <article key={task.id} className="qh-queue-item">
                  <div className="qh-queue-row">
                    <h4>{task.title}</h4>
                    <p>
                      <span>{task.progress}%</span>
                      <span> · {task.eta}</span>
                    </p>
                  </div>
                  <div className="qh-progress-track">
                    <div className="qh-progress-fill" style={{ width: `${task.progress}%` }} />
                  </div>
                </article>
              ))}
            </div>
          </section>
        </div>
      )}

      {(state === 'streaming' || state === 'answered' || state === 'error') && (
        <div className={`qh-answer ${state === 'error' ? 'qh-answer--error' : ''}`}>
          <div className="qh-answer-scroll">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
            {isAnswering && <span className="qh-typing-dot" />}
          </div>
        </div>
      )}

      <div className="qh-hint">
        <kbd>Enter</kbd> enviar · <kbd>Esc</kbd> fechar · <kbd>Ctrl+Shift+Space</kbd> abrir/fechar
      </div>
    </div>
  );
}
