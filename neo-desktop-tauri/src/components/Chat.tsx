import { useCallback, useState, useRef, useEffect, useLayoutEffect } from 'react';
import type { Message } from '../types';
import type { LiveState } from '../hooks/useLiveMode';
import type { ModelCatalog, ModelType, ProviderType } from '../lib/models';
import { modelOptions, providerOptions } from '../lib/models';
import MessageList from './MessageList';
import GlassSelect from './GlassSelect';
import './Chat.css';

interface ChatProps {
  messages: Message[];
  isLoading: boolean;
  isExecuting: boolean;
  currentIteration?: number;
  maxIterations?: number;
  // `attachmentIds` são ids do File Store (neo-core) já enviados; o conteúdo do
  // arquivo não passa por aqui.
  onSendMessage: (
    message: string,
    attachmentIds?: string[],
    /** caracteres desta mensagem que vieram do clipboard (ver §21 do
     *  release-request-plan: colagem = conteúdo de terceiro) */
    pastedChars?: number
  ) => void;
  onClearHistory: () => void;
  onNewChat: () => void;
  onStop: () => void;
  onPermissionResponse: (messageId: string, requestId: string, approved: boolean) => void;
  onMissionResponse: (missionId: string, approved: boolean, editedText?: string) => void;
  onModelChange: (model: string) => void;
  onModeChange: (mode: 'agent' | 'plan' | 'ask') => void;
  onProviderChange: (provider: string) => void;
  selectedModel: string;
  selectedMode: 'agent' | 'plan' | 'ask';
  selectedProvider: string;
  /** Lista do seletor, do neo-core (null = nunca carregou e não há cache). */
  modelCatalog: ModelCatalog | null;
  windowMode: 'tooltip' | 'agent';
  initialInput?: string;
  // Modo Live (voz)
  isLive?: boolean;
  liveState?: LiveState;
  livePartialText?: string;
  liveMicLevel?: number;
  liveError?: string | null;
  onToggleLive?: () => void;
}

// Um arquivo escolhido no picker, do clique até virar id no File Store.
interface AttachmentChip {
  key: string;
  name: string;
  sizeBytes: number;
  status: 'uploading' | 'ready' | 'error';
  id?: string;
  error?: string;
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const LIVE_LABELS: Record<LiveState, string> = {
  idle: '',
  listening: 'Ouvindo…',
  thinking: 'Pensando…',
  speaking: 'Falando…',
};

type ChatMode = 'agent' | 'plan' | 'ask';

function Chat({ 
  messages, 
  isLoading, 
  isExecuting,
  currentIteration = 0,
  maxIterations = 10,
  onSendMessage, 
  onNewChat, 
  onStop,
  onPermissionResponse,
  onMissionResponse,
  onModelChange, 
  onModeChange,
  onProviderChange,
  selectedModel,
  selectedMode,
  selectedProvider,
  modelCatalog,
  windowMode,
  initialInput,
  isLive = false,
  liveState = 'idle',
  livePartialText = '',
  liveMicLevel = 0,
  liveError = null,
  onToggleLive,
}: ChatProps) {
  const [input, setInput] = useState(initialInput || '');
  const [attachments, setAttachments] = useState<AttachmentChip[]>([]);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const resizeInput = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;

    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, []);

  // O auto-scroll pro fim (e a virtualização da lista) mora no MessageList.

  useEffect(() => {
    // If the latest message is from the assistant, return focus to the input
    const last = messages[messages.length - 1];
    if (last && last.role === 'assistant' && !isLoading && !isExecuting) {
      const el = inputRef.current;
      if (el) {
        el.focus();
        // move caret to the end so user can continue typing immediately
        const len = el.value.length;
        try { el.setSelectionRange(len, len); } catch { /* noop */ }
      }
    }
  }, [messages, isLoading, isExecuting]);

  useEffect(() => {
    inputRef.current?.focus();
    resizeInput();
  }, [resizeInput]);

  // Must run BEFORE paint (useLayoutEffect, not useEffect): when a message is
  // sent, `input` clears in the same render that swaps welcome->messages and
  // mounts the agent-progress bar. If the textarea shrink lagged a frame
  // behind (as a regular effect / rAF), the browser painted the tall empty
  // box first, then visibly snapped it (and the message list height with it)
  // one frame later — looked like the window "readjusting" itself.
  useLayoutEffect(() => {
    resizeInput();
  }, [input, resizeInput]);

  // When initialInput changes (e.g. user clicked "Responder" on a notification
  // toast), prefill the textarea so the seed text is visible immediately.
  useEffect(() => {
    if (!initialInput) return;

    let focusFrame: number | undefined;
    const inputFrame = requestAnimationFrame(() => {
      setInput(initialInput);
      focusFrame = requestAnimationFrame(() => {
        const el = inputRef.current;
        if (el) {
          resizeInput();
          el.focus();
          const len = el.value.length;
          try { el.setSelectionRange(len, len); } catch { /* noop */ }
        }
      });
    });

    return () => {
      cancelAnimationFrame(inputFrame);
      if (focusFrame) cancelAnimationFrame(focusFrame);
    };
  }, [initialInput, resizeInput]);

  // Listen for new-chat event from titlebar
  useEffect(() => {
    const handler = () => { onNewChat(); inputRef.current?.focus(); };
    window.addEventListener('neo-new-chat', handler);
    return () => window.removeEventListener('neo-new-chat', handler);
  }, [onNewChat]);

  // Anexo: picker nativo → upload pro File Store (cada arquivo sobe sozinho, o
  // chip mostra o progresso). O envio da mensagem carrega só os ids.
  const isUploading = attachments.some((a) => a.status === 'uploading');

  const handlePickAttachments = useCallback(async () => {
    let paths: string[] = [];
    try {
      paths = (await window.electronAPI?.pickAttachmentFiles()) ?? [];
    } catch (err: any) {
      console.error('[Chat] picker falhou:', err);
      return;
    }
    for (const path of paths) {
      const key = `${path}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      const name = path.split('/').pop() || path;
      setAttachments((prev) => [...prev, { key, name, sizeBytes: 0, status: 'uploading' }]);
      window.electronAPI
        ?.uploadAttachment(path)
        .then((meta) =>
          setAttachments((prev) =>
            prev.map((a) =>
              a.key === key
                ? { ...a, status: 'ready', id: meta.id, name: meta.name, sizeBytes: meta.size_bytes }
                : a
            )
          )
        )
        .catch((err: any) =>
          setAttachments((prev) =>
            prev.map((a) =>
              a.key === key
                ? { ...a, status: 'error', error: String(err?.message ?? err) }
                : a
            )
          )
        );
    }
  }, []);

  const removeAttachment = useCallback((key: string) => {
    // Só tira da mensagem — o arquivo fica no catálogo como temp e o GC limpa.
    setAttachments((prev) => prev.filter((a) => a.key !== key));
  }, []);

  // Trechos COLADOS no rascunho atual. Quem sabe que um texto é de terceiro é o
  // cliente: o core só vê a mensagem final, e adivinhar por formato erraria nos
  // dois sentidos. Guardamos os pedaços (não só o total) para não contar o que
  // ele colou e depois apagou — no envio só vale o que ainda está no texto.
  // Ver neo-core/docs/release-request-plan.md §21.
  const pastedChunksRef = useRef<string[]>([]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const texto = e.clipboardData?.getData('text') ?? '';
    // Abaixo de 24 caracteres é link curto/palavra/comando — ruído para esta conta.
    if (texto.trim().length >= 24) {
      pastedChunksRef.current = [...pastedChunksRef.current.slice(-19), texto];
    }
  }, []);

  const pastedCharsNoTexto = (texto: string): number =>
    pastedChunksRef.current
      .filter((pedaco) => texto.includes(pedaco))
      .reduce((total, pedaco) => total + pedaco.length, 0);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (input.trim() && !isLoading && !isExecuting && !isUploading) {
      const ids = attachments.filter((a) => a.status === 'ready' && a.id).map((a) => a.id!);
      const colados = pastedCharsNoTexto(input);
      onSendMessage(input.trim(), ids.length ? ids : undefined, colados || undefined);
      pastedChunksRef.current = [];
      setInput('');
      setAttachments([]);
      // Textarea shrink is handled by the useLayoutEffect on [input] below,
      // synchronously before the next paint — no need to schedule it here.
      // keep focus in the textarea so user can continue typing
      inputRef.current?.focus();
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <div className={`chat-container ${windowMode}`}>
      {messages.length === 0 ? (
        <div className="messages-container">
          <div className="welcome-message">
            <div className="welcome-icon">🤖</div>
            <h2>Olá! Sou o Neo</h2>
            <p>Seu assistente pessoal de IA para automação do sistema.</p>
            <p className="welcome-subtitle">Agora com execução em ciclos - vou tentar até conseguir!</p>
            <div className="suggestions">
              <button onClick={() => { onSendMessage('Crie uma chave SSH no meu computador'); inputRef.current?.focus(); }}>
                🔐 Criar chave SSH
              </button>
              <button onClick={() => { onSendMessage('Mostre informações do sistema'); inputRef.current?.focus(); }}>
                💻 Info do Sistema
              </button>
              <button onClick={() => { onSendMessage('Liste os arquivos da pasta atual'); inputRef.current?.focus(); }}>
                📁 Listar Arquivos
              </button>
            </div>
          </div>
        </div>
      ) : (
        <MessageList
          messages={messages}
          showTyping={isLoading && !isExecuting}
          onPermissionResponse={onPermissionResponse}
          onMissionResponse={onMissionResponse}
        />
      )}

      <form className="input-container" onSubmit={handleSubmit}>
        {isLive && (
          <div className={`live-bar live-${liveState}`} role="status">
            <span className="live-dot" />
            <span className="live-label">{LIVE_LABELS[liveState]}</span>
            {liveState === 'listening' && (
              <span className="live-meter">
                <span
                  className="live-meter-fill"
                  style={{ width: `${Math.min(100, Math.round(liveMicLevel * 100))}%` }}
                />
              </span>
            )}
            {livePartialText && <span className="live-partial">{livePartialText}</span>}
          </div>
        )}
        {liveError && !isLive && (
          <div className="live-bar live-error" role="alert">
            <span className="live-label">🎙️ {liveError}</span>
          </div>
        )}
        {attachments.length > 0 && (
          <div className="attachment-chips">
            {attachments.map((a) => (
              <span
                key={a.key}
                className={`attachment-chip attachment-${a.status}`}
                title={a.status === 'error' ? a.error : a.name}
              >
                {a.status === 'uploading' && <span className="attachment-spinner" />}
                <span className="attachment-name">{a.name}</span>
                {a.status === 'ready' && (
                  <span className="attachment-size">{humanSize(a.sizeBytes)}</span>
                )}
                {a.status === 'error' && <span className="attachment-size">falhou</span>}
                <button
                  type="button"
                  className="attachment-remove"
                  onClick={() => removeAttachment(a.key)}
                  title="Remover anexo"
                >
                  ✕
                </button>
              </span>
            ))}
          </div>
        )}
        <div className="input-wrapper">
          <button
            type="button"
            className="btn-attachment"
            title="Anexar arquivo"
            onClick={handlePickAttachments}
            disabled={isLoading || isExecuting}
          >
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            placeholder={isExecuting ? "Agente em execução..." : "Digite sua mensagem..."}
            rows={1}
            disabled={isLoading || isExecuting}
          />
          <button
            type="button"
            className={`btn-mic${isLive ? ' live-active' : ''}`}
            title={isLive ? 'Encerrar Modo Live' : 'Modo Live (voz)'}
            aria-pressed={isLive}
            onClick={() => onToggleLive?.()}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
          </button>
          {isExecuting ? (
            <button type="button" className="btn-stop-inline" onClick={onStop}>
              ⏹
            </button>
          ) : (
            <button
              type="submit"
              className="btn-send"
              // Enquanto um anexo sobe, enviar mandaria a mensagem SEM o
              // manifesto do arquivo — o Neo responderia às cegas.
              disabled={!input.trim() || isLoading || isUploading}
              title={isUploading ? 'Aguarde o anexo terminar de subir…' : undefined}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M22 2L11 13M22 2L15 22L11 13L2 9L22 2Z" />
              </svg>
            </button>
          )}
        </div>
        <div className="input-controls">
          <GlassSelect<ChatMode>
            ariaLabel="Modo do chat"
            value={selectedMode}
            disabled={isExecuting}
            width={112}
            onChange={(mode) => onModeChange(mode)}
            options={[
              { value: 'agent', label: 'Agent', description: 'Executa tarefas' },
              { value: 'plan', label: 'Plan', description: 'Planeja antes' },
              { value: 'ask', label: 'Ask', description: 'Só responde' }
            ]}
          />

          {/* Sem catálogo (primeira abertura sem o core) mostra só o par salvo,
              travado: não há lista para escolher, e sem core não há chat. */}
          <GlassSelect<ProviderType>
            ariaLabel="Provider"
            value={selectedProvider as ProviderType}
            disabled={isExecuting || !modelCatalog}
            width={130}
            onChange={(provider) => onProviderChange(provider)}
            options={
              modelCatalog
                ? providerOptions(modelCatalog)
                : [{ value: selectedProvider, label: selectedProvider || '—', description: '' }]
            }
          />

          <GlassSelect<ModelType>
            ariaLabel="Modelo"
            value={selectedModel as ModelType}
            disabled={isExecuting || !modelCatalog}
            width={160}
            onChange={(model) => onModelChange(model)}
            options={
              modelCatalog
                ? modelOptions(modelCatalog, selectedProvider)
                : [{ value: selectedModel, label: selectedModel || '—', description: '' }]
            }
          />
        </div>
      </form>
    </div>
  );
}

export default Chat;
