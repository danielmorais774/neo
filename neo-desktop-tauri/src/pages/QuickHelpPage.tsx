import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './QuickHelpPage.css';

type State = 'idle' | 'loading' | 'streaming' | 'answered' | 'error';

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

  return (
    <div className="qh-root">
      <form className="qh-bar" onSubmit={handleSubmit}>
        <span className="qh-drag-handle" onMouseDown={handleDragStart} title="Arraste para mover">
          <svg width="10" height="16" viewBox="0 0 10 16" fill="currentColor">
            <circle cx="2" cy="3"  r="1.2"/><circle cx="8" cy="3"  r="1.2"/>
            <circle cx="2" cy="8"  r="1.2"/><circle cx="8" cy="8"  r="1.2"/>
            <circle cx="2" cy="13" r="1.2"/><circle cx="8" cy="13" r="1.2"/>
          </svg>
        </span>
        <span className="qh-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
        </span>
        <input
          ref={inputRef}
          className="qh-input"
          type="text"
          placeholder="Como posso ajudar? (Enter para enviar, Esc para fechar)"
          value={question}
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={handleKeyDown}
          autoComplete="off"
          spellCheck={false}
        />
        {state === 'loading' && <span className="qh-spinner" aria-label="carregando" />}
        {state === 'streaming' && <span className="qh-cursor-blink" />}
        {(state === 'answered' || state === 'error') && (
          <button type="button" className="qh-clear" onClick={handleClear} title="Nova pergunta">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        )}
      </form>

      {(state === 'streaming' || state === 'answered' || state === 'error') && (
        <div className={`qh-answer ${state === 'error' ? 'qh-answer--error' : ''}`}>
          <div className="qh-answer-scroll">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown>
            {isAnswering && <span className="qh-typing-dot" />}
          </div>
        </div>
      )}

      <div className="qh-hint">
        <kbd>Enter</kbd> enviar &nbsp;·&nbsp; <kbd>Esc</kbd> fechar &nbsp;·&nbsp; <kbd>Ctrl+Shift+Space</kbd> abrir/fechar
      </div>
    </div>
  );
}
