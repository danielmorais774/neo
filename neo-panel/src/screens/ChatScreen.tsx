import { useCallback, useEffect, useRef, useState } from 'react';
import Icon from '../ui/Icon';
import ChatMessageBubble from '../ChatMessageBubble';
import {
  streamChat,
  streamCommandResponse,
  loadModelCatalog,
  loadSessionHistory,
  uploadAttachment,
  postMissionCommand,
  type AgentStreamEvent,
} from '../lib/chatApi';
import {
  makeMessage,
  normalizeHistoryMessage,
  parseMissionMarker,
  type AttachmentRef,
  type ChatMessage,
  type ChatMode,
  type ModelCatalog,
} from '../lib/chatTypes';
import './ChatScreen.css';

const SESSION_STORAGE_KEY = 'neo-panel-chat-session-id';

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

function loadOrCreateSessionId(): string {
  const existing = localStorage.getItem(SESSION_STORAGE_KEY);
  if (existing) return existing;
  const created = crypto.randomUUID();
  localStorage.setItem(SESSION_STORAGE_KEY, created);
  return created;
}

const ChatScreen = () => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isExecuting, setIsExecuting] = useState(false);
  const [input, setInput] = useState('');
  const [attachments, setAttachments] = useState<AttachmentChip[]>([]);
  const [mode, setMode] = useState<ChatMode>('agent');
  // Provider/modelo começam vazios e recebem o default do core quando o
  // catálogo chega — a lista é a mesma do desktop (GET /desktop/model-catalog).
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [loadingHistory, setLoadingHistory] = useState(true);

  const sessionIdRef = useRef<string>(loadOrCreateSessionId());
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadModelCatalog()
      .then((data) => {
        if (cancelled) return;
        setCatalog(data);
        const fallback = data.default;
        if (fallback) {
          setProvider((current) => current || fallback.provider);
          setModel((current) => current || fallback.model);
        }
      })
      .catch((err) => console.error('[ChatScreen] catálogo de modelos indisponível:', err));
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    loadSessionHistory(sessionIdRef.current)
      .then((history) => {
        if (cancelled) return;
        if (history.length > 0) setMessages(history.map(normalizeHistoryMessage));
      })
      .finally(() => { if (!cancelled) setLoadingHistory(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const setActiveSessionId = useCallback((sid: string) => {
    if (!sid || sid === sessionIdRef.current) return;
    sessionIdRef.current = sid;
    localStorage.setItem(SESSION_STORAGE_KEY, sid);
  }, []);

  // Aplica um AgentStreamEvent do stream NDJSON ao timeline local. Espelha o
  // useAgentEvents.ts do neo-desktop-tauri (mesmos tipos de evento), menos
  // tool_request (execução client-side, sem equivalente no navegador).
  const applyEvent = useCallback((evt: AgentStreamEvent, terminalRef: { current: boolean }) => {
    switch (evt.type) {
      case 'session_changed':
        if (evt.data.sessionId) setActiveSessionId(evt.data.sessionId);
        break;

      case 'thinking':
        setMessages((prev) => {
          const filtered = prev.filter((m) => m.messageType !== 'thinking');
          return [...filtered, makeMessage({
            role: 'assistant',
            content: evt.data.content || '',
            messageType: 'thinking',
          })];
        });
        break;

      case 'assistant_message':
        setMessages((prev) => {
          const filtered = prev.filter((m) => m.messageType !== 'thinking');
          return [...filtered, makeMessage({
            role: 'assistant',
            content: evt.data.content || '',
            messageType: 'text',
            files: evt.data.files,
          })];
        });
        break;

      case 'plan_update':
        setMessages((prev) => {
          const idx = prev.findIndex((m) => m.messageType === 'plan_update' && m.plan?.planId === evt.data.planId);
          const planMsg = makeMessage({
            role: 'assistant',
            content: `Plano: ${evt.data.goal}`,
            messageType: 'plan_update',
            plan: {
              planId: evt.data.planId,
              goal: evt.data.goal,
              currentStep: evt.data.currentStep,
              status: evt.data.status,
              steps: evt.data.steps || [],
            },
          });
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = { ...updated[idx], plan: planMsg.plan };
            return updated;
          }
          return [...prev, planMsg];
        });
        break;

      case 'command_request':
        setMessages((prev) => [...prev, makeMessage({
          role: 'assistant',
          content: `Comando: ${evt.data.explanation || evt.data.cmd}`,
          messageType: 'command',
          command: {
            id: evt.data.id,
            cmd: evt.data.cmd,
            explanation: evt.data.explanation || '',
            isBackground: !!evt.data.isBackground,
            status: 'pending',
          },
        })]);
        break;

      case 'complete': {
        terminalRef.current = true;
        setIsExecuting(false);
        const raw = evt.data.message || 'Tarefa concluída!';
        const { content, mission } = parseMissionMarker(raw);
        setMessages((prev) => {
          const filtered = prev.filter((m) => m.messageType !== 'thinking');
          return [...filtered, makeMessage({
            role: 'assistant',
            content: content || raw,
            messageType: mission ? 'mission' : (evt.data.success ? 'result' : 'error'),
            mission,
            files: evt.data.files,
          })];
        });
        break;
      }

      case 'error':
        terminalRef.current = true;
        setIsExecuting(false);
        setMessages((prev) => {
          const filtered = prev.filter((m) => m.messageType !== 'thinking');
          return [...filtered, makeMessage({
            role: 'assistant',
            content: `❌ ${evt.data.message || 'Erro desconhecido'}`,
            messageType: 'error',
          })];
        });
        break;

      default:
        break;
    }
  }, [setActiveSessionId]);

  const handleSend = useCallback(async (text: string, attachmentRefs?: AttachmentRef[]) => {
    const trimmed = text.trim();
    if (!trimmed) return;

    const attachmentIds = attachmentRefs?.map((a) => a.id);
    setMessages((prev) => [...prev, makeMessage({
      role: 'user',
      content: trimmed,
      messageType: 'text',
      attachments: attachmentRefs,
    })]);
    setIsExecuting(true);

    const controller = new AbortController();
    abortRef.current = controller;
    const terminalRef = { current: false };

    try {
      await streamChat(
        { sessionId: sessionIdRef.current, message: trimmed, provider, model, attachments: attachmentIds },
        (evt) => applyEvent(evt, terminalRef),
        controller.signal,
      );
    } catch (err: any) {
      terminalRef.current = true;
      setMessages((prev) => [...prev, makeMessage({
        role: 'assistant',
        content: `❌ Erro: ${err?.message || 'Falha ao processar mensagem'}`,
        messageType: 'error',
      })]);
    } finally {
      if (!terminalRef.current) setIsExecuting(false);
      abortRef.current = null;
    }
  }, [applyEvent, provider, model]);

  const handleCommandResponse = useCallback(async (commandId: string, command: string, approved: boolean) => {
    setMessages((prev) => prev.map((m) => (
      m.command?.id === commandId
        ? { ...m, command: { ...m.command, status: approved ? 'approved' : 'rejected' } }
        : m
    )));

    setIsExecuting(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const terminalRef = { current: false };

    try {
      await streamCommandResponse(
        { sessionId: sessionIdRef.current, commandId, command, approved },
        (evt) => applyEvent(evt, terminalRef),
        controller.signal,
      );
    } catch (err: any) {
      terminalRef.current = true;
      setMessages((prev) => [...prev, makeMessage({
        role: 'assistant',
        content: `❌ Erro: ${err?.message || 'Falha ao processar aprovação'}`,
        messageType: 'error',
      })]);
    } finally {
      abortRef.current = null;
      // Aprovado não fecha o turno (precisaria de um executor local, que o
      // Painel não tem) — o stream termina sem `complete`. Avisa em vez de
      // deixar a UI travada em "Executando".
      if (!terminalRef.current) {
        if (approved) {
          setMessages((prev) => [...prev, makeMessage({
            role: 'assistant',
            content: 'Aprovado. A execução remota desse comando ainda não está disponível pelo Painel — use o neo-desktop-tauri para executar.',
            messageType: 'text',
          })]);
        }
        setIsExecuting(false);
      }
    }
  }, [applyEvent]);

  const handleMissionResponse = useCallback(async (missionId: string, approved: boolean, editedText?: string) => {
    setMessages((prev) => prev.map((m) => (
      m.mission?.missionId === missionId && m.mission.status === 'pending'
        ? { ...m, mission: { ...m.mission, status: approved ? 'approved' : 'rejected' } }
        : m
    )));
    try {
      await postMissionCommand(missionId, approved ? (editedText ? 'edit' : 'approve') : 'cancel', editedText);
    } catch (err) {
      console.error('[ChatScreen] falha ao responder missão:', err);
    }
  }, []);

  const handleNewChat = useCallback(() => {
    abortRef.current?.abort();
    const newSessionId = crypto.randomUUID();
    sessionIdRef.current = newSessionId;
    localStorage.setItem(SESSION_STORAGE_KEY, newSessionId);
    setMessages([]);
    setIsExecuting(false);
    setInput('');
    setAttachments([]);
  }, []);

  const isUploading = attachments.some((a) => a.status === 'uploading');

  const handlePickFiles = useCallback((files: FileList | null) => {
    if (!files || files.length === 0) return;
    for (const file of Array.from(files)) {
      const key = `${file.name}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
      setAttachments((prev) => [...prev, { key, name: file.name, sizeBytes: file.size, status: 'uploading' }]);
      uploadAttachment(file, sessionIdRef.current)
        .then((meta) => setAttachments((prev) => prev.map((a) => (
          a.key === key ? { ...a, status: 'ready', id: meta.id, name: meta.name, sizeBytes: meta.size_bytes } : a
        ))))
        .catch((err: any) => setAttachments((prev) => prev.map((a) => (
          a.key === key ? { ...a, status: 'error', error: String(err?.message ?? err) } : a
        ))));
    }
  }, []);

  const removeAttachment = useCallback((key: string) => {
    setAttachments((prev) => prev.filter((a) => a.key !== key));
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || isExecuting || isUploading) return;
    const ready = attachments
      .filter((a) => a.status === 'ready' && a.id)
      .map((a): AttachmentRef => ({ id: a.id!, name: a.name, sizeBytes: a.sizeBytes }));
    handleSend(input, ready.length ? ready : undefined);
    setInput('');
    setAttachments([]);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  const providerModels = catalog?.providers.find((p) => p.name === provider)?.models ?? [];

  return (
    <div className="neo-screen">
      <div className="neo-topbar">
        <div className="neo-topbar-title">
          <Icon name="robot" />
          <span>Chat</span>
        </div>
        <button className="neo-btn-ghost" onClick={handleNewChat} disabled={isExecuting}>
          Nova conversa
        </button>
      </div>

      <div className="chat-screen-body">
        <div className="chat-messages">
          {loadingHistory ? (
            <div className="chat-empty">Carregando conversa…</div>
          ) : messages.length === 0 ? (
            <div className="chat-empty">
              <div className="chat-empty-icon">🤖</div>
              <h2>Olá! Sou o Neo</h2>
              <p>Fale comigo direto por aqui — mesmo agente do neo-desktop-tauri.</p>
            </div>
          ) : (
            messages.map((message) => (
              <ChatMessageBubble
                key={message.id}
                message={message}
                onCommandResponse={handleCommandResponse}
                onMissionResponse={handleMissionResponse}
              />
            ))
          )}
          <div ref={messagesEndRef} />
        </div>

        <form className="chat-composer" onSubmit={handleSubmit}>
          {attachments.length > 0 && (
            <div className="chat-attachment-chips">
              {attachments.map((a) => (
                <span key={a.key} className={`chat-chip is-${a.status}`} title={a.status === 'error' ? a.error : a.name}>
                  {a.status === 'uploading' && <span className="chat-chip-spinner" />}
                  <span className="chat-chip-name">{a.name}</span>
                  {a.status === 'ready' && <span className="chat-chip-size">{humanSize(a.sizeBytes)}</span>}
                  {a.status === 'error' && <span className="chat-chip-size">falhou</span>}
                  <button type="button" className="chat-chip-remove" onClick={() => removeAttachment(a.key)}>✕</button>
                </span>
              ))}
            </div>
          )}

          <div className="chat-input-row">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => { handlePickFiles(e.target.files); e.target.value = ''; }}
            />
            <button
              type="button"
              className="chat-btn-icon"
              title="Anexar arquivo"
              disabled={isExecuting}
              onClick={() => fileInputRef.current?.click()}
            >
              📎
            </button>
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={isExecuting ? 'Neo está respondendo…' : 'Digite sua mensagem…'}
              rows={1}
              disabled={isExecuting}
            />
            <button
              type="submit"
              className="chat-btn-send"
              disabled={!input.trim() || isExecuting || isUploading}
              title={isUploading ? 'Aguarde o anexo terminar de subir…' : undefined}
            >
              ➤
            </button>
          </div>

          <div className="chat-controls-row">
            <select className="neo-select chat-select" value={mode} disabled={isExecuting} onChange={(e) => setMode(e.target.value as ChatMode)}>
              <option value="agent">Agent — executa tarefas</option>
              <option value="plan">Plan — planeja antes</option>
              <option value="ask">Ask — só responde</option>
            </select>
            <select
              className="neo-select chat-select"
              value={provider}
              disabled={isExecuting || !catalog}
              onChange={(e) => {
                const nextProvider = e.target.value;
                setProvider(nextProvider);
                const first = catalog?.providers.find((p) => p.name === nextProvider)?.models[0];
                if (first) setModel(first.id);
              }}
            >
              {(catalog?.providers ?? []).map((p) => (
                <option key={p.name} value={p.name}>
                  {p.description ? `${p.label} — ${p.description}` : p.label}
                </option>
              ))}
            </select>
            <select
              className="neo-select chat-select"
              value={model}
              disabled={isExecuting || !catalog}
              onChange={(e) => setModel(e.target.value)}
            >
              {providerModels.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.description ? `${m.label} — ${m.description}` : m.label}
                </option>
              ))}
            </select>
          </div>
        </form>
      </div>
    </div>
  );
};

export default ChatScreen;
