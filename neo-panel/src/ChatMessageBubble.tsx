import { useState, type ComponentPropsWithoutRef } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github-dark.css';
import type { ChatMessage } from './lib/chatTypes';
import { CORE_BASE } from './lib/chatApi';

interface ChatMessageBubbleProps {
  message: ChatMessage;
  onCommandResponse: (commandId: string, command: string, approved: boolean) => void;
  onMissionResponse: (missionId: string, approved: boolean, editedText?: string) => void;
}

const STEP_ICON: Record<string, string> = {
  pending: '○',
  running: '◐',
  success: '✓',
  completed: '✓',
  error: '✕',
  failed: '✕',
  skipped: '⏭',
};

const LANG_EXT: Record<string, string> = {
  javascript: 'js', js: 'js', jsx: 'jsx',
  typescript: 'ts', ts: 'ts', tsx: 'tsx',
  python: 'py', py: 'py',
  bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh',
  json: 'json', html: 'html', xml: 'xml',
  css: 'css', scss: 'scss', sql: 'sql',
  go: 'go', rust: 'rs', rs: 'rs',
  java: 'java', c: 'c', cpp: 'cpp', 'c++': 'cpp',
  ruby: 'rb', php: 'php',
  yaml: 'yml', yml: 'yml',
  markdown: 'md', md: 'md',
  dockerfile: 'dockerfile',
  plaintext: 'txt', text: 'txt',
};

function humanFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function extractCodeText(node: unknown): string {
  if (node == null) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(extractCodeText).join('');
  if (typeof node === 'object' && node !== null && 'props' in node) {
    return extractCodeText((node as { props?: { children?: unknown } }).props?.children);
  }
  return '';
}

function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<'pre'>) {
  const [copied, setCopied] = useState(false);
  const codeChild = Array.isArray(children) ? children[0] : children;
  const className: string =
    (codeChild && typeof codeChild === 'object' && 'props' in codeChild
      ? (codeChild as { props?: { className?: string } }).props?.className
      : undefined) || '';
  const language = /language-([\w+-]+)/.exec(className)?.[1] || '';
  const text = extractCodeText(children);

  const handleCopy = () => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const handleDownload = () => {
    const ext = LANG_EXT[language.toLowerCase()] || 'txt';
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `codigo.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="chat-code-block">
      <div className="chat-code-header">
        <span className="chat-code-lang">{language || 'texto'}</span>
        <div className="chat-code-actions">
          <button
            type="button"
            className={`chat-code-btn ${copied ? 'is-copied' : ''}`}
            onClick={handleCopy}
          >
            {copied ? '✓ Copiado' : '⧉ Copiar'}
          </button>
          <button type="button" className="chat-code-btn" onClick={handleDownload}>
            ⬇ Baixar
          </button>
        </div>
      </div>
      <pre {...props}>{children}</pre>
    </div>
  );
}

function ChatMessageBubble({ message, onCommandResponse, onMissionResponse }: ChatMessageBubbleProps) {
  const [responded, setResponded] = useState(false);
  const time = message.timestamp.toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });

  if (message.messageType === 'thinking') {
    return (
      <div className="chat-thinking">
        <span className="chat-thinking-dots"><span /><span /><span /></span>
        <span className="chat-thinking-text">{message.content || 'Pensando…'}</span>
      </div>
    );
  }

  if (message.messageType === 'plan_update' && message.plan) {
    const plan = message.plan;
    const done = plan.steps.filter((s) => s.status === 'success' || s.status === 'completed').length;
    return (
      <div className="chat-row assistant">
        <div className="chat-avatar">📋</div>
        <div className="chat-bubble chat-plan">
          <div className="chat-plan-head">
            <span>{plan.goal}</span>
            <span className="chat-plan-progress">{done}/{plan.steps.length}</span>
          </div>
          <ul className="chat-plan-steps">
            {plan.steps.map((step) => (
              <li key={step.id} className={`chat-plan-step is-${step.status}`}>
                <span className="chat-plan-step-icon">{STEP_ICON[step.status] || '•'}</span>
                <span className="chat-plan-step-desc">{step.description}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    );
  }

  if (message.messageType === 'mission' && message.mission) {
    const mission = message.mission;
    const drafts = mission.draftMessages?.length ? mission.draftMessages : [mission.draftText];

    const handleEdit = () => {
      const edited = window.prompt('Editar mensagem antes de enviar:', drafts.join('\n'));
      if (edited === null) return;
      const normalized = edited.trim();
      if (!normalized) return;
      setResponded(true);
      onMissionResponse(mission.missionId, true, normalized);
    };

    return (
      <div className="chat-row assistant">
        <div className="chat-avatar">📱</div>
        <div className="chat-bubble chat-mission">
          {message.content && <div className="chat-mission-intro">{message.content}</div>}
          <div className="chat-mission-title">Mensagem para {mission.contactLabel}:</div>
          {drafts.map((draft, i) => (
            <div key={i} className="chat-mission-draft">
              {drafts.length > 1 ? `${i + 1}. ` : ''}"{draft}"
            </div>
          ))}
          {mission.status === 'pending' && !responded ? (
            <div className="chat-actions-row">
              <button
                className="chat-btn-approve"
                onClick={() => { setResponded(true); onMissionResponse(mission.missionId, true); }}
              >
                Enviar
              </button>
              <button className="chat-btn-ghost-sm" onClick={handleEdit}>Editar</button>
            </div>
          ) : (
            <div className="chat-mission-status">
              {mission.status === 'approved' && 'Enviado ✅'}
              {mission.status === 'rejected' && '❌ Rejeitada'}
              {mission.status === 'cancelled' && '⚠️ Cancelada'}
              {mission.status === 'pending' && responded && 'Enviando…'}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (message.messageType === 'command' && message.command) {
    const command = message.command;
    const statusLabel: Record<string, string> = {
      pending: 'Aguardando aprovação',
      approved: 'Aprovado',
      rejected: 'Rejeitado',
    };
    return (
      <div className="chat-row assistant">
        <div className="chat-avatar">💻</div>
        <div className="chat-bubble chat-command">
          <div className="chat-command-head">
            <span>Comando de sistema</span>
            <span className={`chat-command-badge is-${command.status}`}>{statusLabel[command.status]}</span>
          </div>
          {command.explanation && <div className="chat-command-explanation">{command.explanation}</div>}
          <pre className="chat-command-code">{command.cmd}</pre>
          {command.status === 'pending' && !responded && (
            <div className="chat-actions-row">
              <button
                className="chat-btn-reject"
                onClick={() => { setResponded(true); onCommandResponse(command.id, command.cmd, false); }}
              >
                ✕ Rejeitar
              </button>
              <button
                className="chat-btn-approve"
                onClick={() => { setResponded(true); onCommandResponse(command.id, command.cmd, true); }}
              >
                ✓ Aprovar
              </button>
            </div>
          )}
          {command.status === 'pending' && (
            <div className="chat-command-hint">
              Execução remota via Painel ainda não está disponível — aprovar aqui só registra a
              decisão. Para executar de fato, use o neo-desktop-tauri.
            </div>
          )}
        </div>
      </div>
    );
  }

  const isUser = message.role === 'user';
  return (
    <div className={`chat-row ${isUser ? 'user' : 'assistant'}`}>
      <div className="chat-avatar">{isUser ? 'U' : '🤖'}</div>
      <div className={`chat-bubble ${message.messageType === 'error' ? 'is-error' : ''}`}>
        <div className="chat-markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeHighlight]}
            components={{ pre: CodeBlock }}
          >
            {message.content}
          </ReactMarkdown>
        </div>
        {message.attachments && message.attachments.length > 0 && (
          <div className="chat-attachment-list">
            {message.attachments.map((a) => (
              <span key={a.id} className="chat-attachment-tag">📎 {a.name}</span>
            ))}
          </div>
        )}
        {message.files && message.files.length > 0 && (
          <div className="chat-attachment-list">
            {message.files.map((f) => (
              <a
                key={f.id}
                className="chat-file-download"
                href={`${CORE_BASE}/api/v1/files/${f.id}/download`}
                download
                title={`Baixar ${f.name}`}
              >
                ⬇ {f.name}
                <span className="chat-file-download-size">{humanFileSize(f.sizeBytes)}</span>
              </a>
            ))}
          </div>
        )}
        <div className="chat-bubble-time">{time}</div>
      </div>
    </div>
  );
}

export default ChatMessageBubble;
