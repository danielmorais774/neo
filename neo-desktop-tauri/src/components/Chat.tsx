import { useCallback, useState, useRef, useEffect } from 'react';
import type { Message } from '../types';
import MessageBubble from './MessageBubble';
import GlassSelect from './GlassSelect';
import './Chat.css';

interface ChatProps {
  messages: Message[];
  isLoading: boolean;
  isExecuting: boolean;
  currentIteration?: number;
  maxIterations?: number;
  onSendMessage: (message: string) => void;
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
  windowMode: 'tooltip' | 'agent';
  initialInput?: string;
}

type ChatMode = 'agent' | 'plan' | 'ask';
type ProviderType = 'openai' | 'groq' | 'grok' | 'deepseek' | 'anthropic' | 'gemini' | 'ollama';
type ModelType = string;

const PROVIDER_OPTIONS: { value: ProviderType; label: string; description: string }[] = [
  { value: 'openai',    label: 'OpenAI',       description: 'GPT-4o, GPT-4.1...' },
  { value: 'groq',      label: 'Groq',         description: 'Llama-3.3, ultra-rápido' },
  { value: 'grok',      label: 'Grok (xAI)',   description: 'Grok-3-mini, Grok-2...' },
  { value: 'deepseek',  label: 'DeepSeek',     description: 'deepseek-chat...' },
  { value: 'anthropic', label: 'Anthropic',    description: 'Claude 3...' },
  { value: 'gemini',    label: 'Gemini',       description: 'Gemini 2.5 Pro...' },
  { value: 'ollama',    label: 'Ollama',       description: 'Local (Gemma4...)' },
];

const MODELS_BY_PROVIDER: Record<ProviderType, { value: string; label: string; description: string }[]> = {
  openai: [
    { value: 'gpt-4.1',       label: 'GPT-4.1',        description: 'Mais capaz' },
    { value: 'gpt-4.1-mini',  label: 'GPT-4.1 Mini',   description: 'Rápido' },
    { value: 'gpt-4.1-nano',  label: 'GPT-4.1 Nano',   description: 'Mais rápido' },
    { value: 'gpt-4o',        label: 'GPT-4o',          description: 'Equilíbrio' },
    { value: 'gpt-4o-mini',   label: 'GPT-4o-mini',     description: 'Econômico' },
  ],
  groq: [
    { value: 'llama-3.3-70b-versatile', label: 'Llama-3.3 70B',     description: 'Padrão (free)' },
    { value: 'llama-3.1-8b-instant',    label: 'Llama-3.1 8B Fast', description: 'Ultra-rápido (free)' },
    { value: 'llama3-70b-8192',         label: 'Llama3 70B 8k',     description: 'Contexto longo (free)' },
    { value: 'mixtral-8x7b-32768',      label: 'Mixtral 8x7B',      description: '32k tokens (free)' },
    { value: 'gemma2-9b-it',            label: 'Gemma2 9B',         description: 'Google (free)' },
  ],
  grok: [
    { value: 'grok-3-mini-fast', label: 'Grok-3 Mini Fast', description: 'Mais rápido (free)' },
    { value: 'grok-3-mini',      label: 'Grok-3 Mini',      description: 'Rápido (free)' },
    { value: 'grok-2-1212',      label: 'Grok-2',           description: 'Completo (free)' },
    { value: 'grok-3',           label: 'Grok-3',           description: 'Mais capaz (pago)' },
  ],
  deepseek: [
    { value: 'deepseek-chat',     label: 'DeepSeek Chat',     description: 'Uso geral' },
    { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner', description: 'Raciocínio lento' },
  ],
  anthropic: [
    { value: 'claude-3-5-sonnet-20241022', label: 'Claude 3.5 Sonnet', description: 'Mais capaz' },
    { value: 'claude-3-haiku-20240307',    label: 'Claude 3 Haiku',   description: 'Rápido' },
  ],
  gemini: [
    { value: 'gemini-2.5-pro-preview-03-25', label: 'Gemini 2.5 Pro',     description: 'Avançado' },
    { value: 'gemini-2.0-flash',             label: 'Gemini 2.0 Flash',   description: 'Rápido' },
    { value: 'gemini-2.0-flash-lite',        label: 'Gemini Flash Lite',  description: 'Leve' },
  ],
  ollama: [
    { value: 'gemma4:e2b', label: 'Gemma4 2B (Local)', description: 'Ultra-leve' },
    { value: 'gemma4:12b', label: 'Gemma4 12B (Local)', description: 'Local' },
    { value: 'phi4',       label: 'Phi-4 (Local)',      description: 'Local' },
  ],
};

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
  windowMode,
  initialInput,
}: ChatProps) {
  const [input, setInput] = useState(initialInput || '');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const resizeInput = useCallback(() => {
    const el = inputRef.current;
    if (!el) return;

    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, []);

  useEffect(() => {
    // Always scroll to the end when messages change
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });

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

  useEffect(() => {
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

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (input.trim() && !isLoading && !isExecuting) {
      onSendMessage(input.trim());
      setInput('');
      requestAnimationFrame(resizeInput);
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

  const progressPercent = maxIterations > 0 ? (currentIteration / maxIterations) * 100 : 0;

  return (
    <div className={`chat-container ${windowMode}`}>
      {/* Barra de progresso do agente */}
      {isExecuting && (
        <div className="agent-progress">
          <span className="executing-badge">⚡ Executando</span>
          <div className="progress-bar-container">
            <div
              className="progress-bar"
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          <span className="progress-text">
            Iteração {currentIteration}/{maxIterations}
          </span>
          <button className="btn-stop" onClick={onStop}>
            ⏹ Parar
          </button>
        </div>
      )}

      <div className="messages-container">
        {messages.length === 0 ? (
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
        ) : (
          <>
            {messages.map((message) => (
              <MessageBubble
                key={message.id}
                message={message}
                onPermissionResponse={onPermissionResponse}
                onMissionResponse={onMissionResponse}
              />
            ))}
            {isLoading && !isExecuting && (
              <div className="message assistant">
                <div className="message-avatar">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <circle cx="9" cy="16" r="1" fill="currentColor" />
                    <circle cx="15" cy="16" r="1" fill="currentColor" />
                    <path d="M8 11V7a4 4 0 0 1 8 0v4" />
                  </svg>
                </div>
                <div className="message-body">
                  <div className="message-content">
                    <div className="message-text">
                      <div className="typing-indicator">
                        <span></span>
                        <span></span>
                        <span></span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </>
        )}
      </div>

      <form className="input-container" onSubmit={handleSubmit}>
        <div className="input-wrapper">
          <button type="button" className="btn-attachment" title="Anexar arquivo">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={isExecuting ? "Agente em execução..." : "Digite sua mensagem..."}
            rows={1}
            disabled={isLoading || isExecuting}
          />
          <button type="button" className="btn-mic" title="Gravação de voz">
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
            <button type="submit" className="btn-send" disabled={!input.trim() || isLoading}>
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

          <GlassSelect<ProviderType>
            ariaLabel="Provider"
            value={selectedProvider as ProviderType}
            disabled={isExecuting}
            width={130}
            onChange={(provider) => {
              onProviderChange(provider);
              const models = MODELS_BY_PROVIDER[provider];
              if (models?.length > 0) onModelChange(models[0].value);
            }}
            options={PROVIDER_OPTIONS}
          />

          <GlassSelect<ModelType>
            ariaLabel="Modelo"
            value={selectedModel as ModelType}
            disabled={isExecuting}
            width={160}
            onChange={(model) => onModelChange(model)}
            options={MODELS_BY_PROVIDER[selectedProvider as ProviderType] ?? MODELS_BY_PROVIDER.openai}
          />
        </div>
      </form>
    </div>
  );
}

export default Chat;
