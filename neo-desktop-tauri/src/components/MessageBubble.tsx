import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import 'highlight.js/styles/github-dark.css';
import { useState } from 'react';
import type { Message, PlanStep } from '../types';
import PlanTracker from './PlanTracker';
import './MessageBubble.css';

interface MessageBubbleProps {
  message: Message;
  onPermissionResponse: (messageId: string, requestId: string, approved: boolean) => void;
  onMissionResponse?: (missionId: string, approved: boolean, editedText?: string) => void;
}

function MessageBubble({ message, onPermissionResponse, onMissionResponse }: MessageBubbleProps) {
  const [responding, setResponding] = useState(false);

  const getStatusLabel = (status: string) => {
    switch (status) {
      case 'pending': return 'Aguardando permissão';
      case 'approved': return 'Aprovado';
      case 'rejected': return 'Rejeitado';
      case 'executed': return 'Executado';
      default: return status;
    }
  };

  const getThinkingIcon = (type?: string) => {
    switch (type) {
      case 'planning': return '🧠';
      case 'analyzing': return '🔍';
      case 'deciding': return '🤔';
      case 'executing': return '⚡';
      case 'error_handling': return '🔧';
      case 'success': return '✅';
      case 'retry': return '🔄';
      default: return '💭';
    }
  };

  const getThinkingLabel = (type?: string) => {
    switch (type) {
      case 'planning': return 'Planejando';
      case 'analyzing': return 'Analisando';
      case 'deciding': return 'Decidindo';
      case 'executing': return 'Executando';
      case 'error_handling': return 'Tratando Erro';
      case 'success': return 'Sucesso';
      case 'retry': return 'Tentando Novamente';
      default: return 'Pensando';
    }
  };

  const getStepStatusIcon = (status?: string) => {
    switch (status) {
      case 'pending': return '⏳';
      case 'running': return '🔄';
      case 'success': return '✅';
      case 'error': return '❌';
      case 'skipped': return '⏭️';
      default: return '•';
    }
  };

  // Renderizar mensagem de pensamento (mais sutil)
  if (message.messageType === 'thinking' && message.thinking) {
    return (
      <div className="thinking-indicator">
        <div className="thinking-spinner">
          <div className="spinner"></div>
        </div>
        <div className="thinking-text-inline">
          <span className="thinking-label">{getThinkingLabel(message.thinking.type)}</span>
          {message.thinking.content && (
            <span className="thinking-detail">— {message.thinking.content}</span>
          )}
        </div>
      </div>
    );
  }

  // Renderizar plan tracker (real-time updates from neo-core)
  if (message.messageType === 'plan_update' && message.plan) {
    return (
      <div className="message assistant plan-message">
        <div className="message-avatar">📋</div>
        <div className="message-content plan-content">
          <PlanTracker plan={message.plan} />
        </div>
      </div>
    );
  }

  // Renderizar plano de execução
  if (message.messageType === 'plan' && message.plan) {
    return (
      <div className="message assistant plan-message">
        <div className="message-avatar">📋</div>
        <div className="message-content plan-content">
          <div className="plan-header">
            <span className="plan-title">📋 Plano de Execução</span>
          </div>
          <div className="plan-goal">
            <strong>🎯 Objetivo:</strong> {message.plan.goal}
          </div>
          <div className="plan-reasoning">
            <strong>💡 Análise:</strong> {message.plan.reasoning}
          </div>
          <div className="plan-steps">
            <strong>📝 Etapas:</strong>
            <ol className="steps-list">
              {message.plan.steps.map((step: PlanStep) => (
                <li key={step.id} className={`plan-step status-${step.status}`}>
                  <span className="step-icon">{getStepStatusIcon(step.status)}</span>
                  <span className="step-title">{step.title}</span>
                  {step.description && (
                    <span className="step-description"> - {step.description}</span>
                  )}
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    );
  }

  // Renderizar step em execução
  if (message.messageType === 'step' && message.step) {
    const step = message.step;
    return (
      <div className={`message assistant step-message status-${step.status}`}>
        <div className="message-avatar step-avatar">
          {getStepStatusIcon(step.status)}
        </div>
        <div className="message-content step-content">
          <div className="step-header">
            <span className="step-title">{step.title}</span>
            <span className={`step-status-badge ${step.status}`}>
              {step.status}
            </span>
          </div>
          {step.description && (
            <div className="step-description-text">{step.description}</div>
          )}
          {step.command && (
            <div className="step-command">
              <code>{step.command}</code>
            </div>
          )}
          {step.output && (
            <div className="step-output">
              <div className="output-label">✓ Resultado:</div>
              <pre>{step.output}</pre>
            </div>
          )}
          {step.error && (
            <div className="step-error">
              <div className="error-label">✗ Erro:</div>
              <pre>{step.error}</pre>
            </div>
          )}
        </div>
      </div>
    );
  }

  // Renderizar iteração
  if (message.messageType === 'iteration') {
    return (
      <div className="message assistant iteration-message">
        <div className="iteration-badge">
          🔄 Iteração {(message as any).iteration?.current || '?'}/{(message as any).iteration?.max || '?'}
        </div>
      </div>
    );
  }

  // Renderizar missão WhatsApp pendente de aprovação
  if (message.messageType === 'mission' && message.mission) {
    const mission = message.mission;
    const draftMessages = mission.draftMessages?.length ? mission.draftMessages : [mission.draftText];
    return (
      <div className="message assistant mission-message">
        <div className="message-avatar">📱</div>
        <div className="message-body">
          {message.content && (
            <div className="mission-intro">{message.content}</div>
          )}
          <div className="message-content">
            <div className="mission-header">
              <span className="mission-icon">📲</span>
              <span className="mission-title">Aprovação de Mensagem WhatsApp</span>
            </div>
            <div className="mission-info">
              <div className="mission-contact">
                <span className="mission-label">Para:</span>
                <span className="mission-value">{mission.contactLabel}</span>
                {mission.contactPhone && (
                  <span className="mission-phone">{mission.contactPhone}</span>
                )}
              </div>
              <div className="mission-draft">
                <span className="mission-label">{draftMessages.length > 1 ? 'Mensagens:' : 'Mensagem:'}</span>
                <span className="mission-draft-text">
                  {draftMessages.map((draft, index) => (
                    <span key={index} style={{ display: 'block' }}>
                      {draftMessages.length > 1 ? `${index + 1}. ` : ''}"{draft}"
                    </span>
                  ))}
                </span>
              </div>
            </div>
            {mission.status === 'pending' && (
              <div className="mission-actions">
                <button
                  className="btn-reject-inline"
                  onClick={() => onMissionResponse?.(mission.missionId, false)}
                >
                  ✕ Cancelar
                </button>
                <button
                  className="btn-approve-inline"
                  onClick={() => onMissionResponse?.(mission.missionId, true)}
                >
                  ✓ Aprovar e Enviar
                </button>
              </div>
            )}
            {mission.status !== 'pending' && (
              <div className="mission-status">
                {mission.status === 'approved' && <span className="status-approved">✅ Aprovada</span>}
                {mission.status === 'rejected' && <span className="status-rejected">❌ Rejeitada</span>}
                {mission.status === 'cancelled' && <span className="status-cancelled">⚠️ Cancelada</span>}
              </div>
            )}
          </div>
        </div>
      </div>
    );
  }

  const [detailsOpen, setDetailsOpen] = useState(false);

  const renderDetails = () => {
    const detailEntries = [
      ...(message.details ? Object.entries(message.details) : []),
      ...(message.command ? Object.entries({ command: message.command }) : [])
    ];

    if (detailEntries.length === 0) return null;

    return (
      <div className="message-details-panel">
        {detailEntries.map(([key, value]) => (
          <div key={key} className="detail-row">
            <span className="detail-key">{key}:</span>
            <span className="detail-value">
              {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
            </span>
          </div>
        ))}
      </div>
    );
  };

  const hasDetails = Boolean(message.details || message.command);

  // Renderizar mensagem normal (texto, comando, resultado, erro)
  return (
    <div className={`message ${message.role}`}>
      <div className="message-avatar">
        {message.role === 'user' ? (
          <span className="avatar-letter">U</span>
        ) : message.messageType === 'error' ? '❌' : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="3" y="11" width="18" height="10" rx="2" />
            <circle cx="9" cy="16" r="1" fill="currentColor" />
            <circle cx="15" cy="16" r="1" fill="currentColor" />
            <path d="M8 11V7a4 4 0 0 1 8 0v4" />
          </svg>
        )}
      </div>
      <div className="message-body">
        <div className="message-content">
          <div className="message-actions-top">
            {hasDetails && (
              <button
                className="message-details-button"
                onClick={() => setDetailsOpen(open => !open)}
                title="Ver detalhes da mensagem"
              >
                🧪🧪🧪
              </button>
            )}
          </div>
          <div 
            className={`message-text ${message.messageType === 'error' ? 'error-text' : ''}`}
          >
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              rehypePlugins={[rehypeHighlight]}
            >
              {message.content}
            </ReactMarkdown>
          </div>
          
          {message.command && (
            <div className="command-box">
              <div className="command-header">
                <span>💻 Comando Terminal</span>
                <div className="command-status">
                  <span className={`status-dot ${message.command.status}`}></span>
                  <span>{getStatusLabel(message.command.status)}</span>
                </div>
              </div>
              
              {message.command.explanation && (
                <div className="command-explanation">
                  💡 {message.command.explanation}
                </div>
              )}
              
              <div className="command-code-wrapper">
                <div className="command-code">
                  {message.command.cmd}
                </div>
              </div>
              
              {message.command.status === 'pending' && (
                <div className="command-actions">
                  <button 
                    className="btn-reject-inline"
                    disabled={responding}
                    onClick={() => {
                      setResponding(true);
                      onPermissionResponse(message.id, message.command!.requestId || '', false);
                    }}
                  >
                    ✕ Rejeitar
                  </button>
                  <button 
                    className="btn-approve-inline"
                    disabled={responding}
                    onClick={() => {
                      setResponding(true);
                      onPermissionResponse(message.id, message.command!.requestId || '', true);
                    }}
                  >
                    {responding ? '⏳ Aguardando...' : '✓ Aprovar e Executar'}
                  </button>
                </div>
              )}
              
              {message.command.output && (
                <div className="command-output">
                  <div className="output-label">Saída:</div>
                  {message.command.output}
                </div>
              )}
            </div>
          )}
        </div>
        {detailsOpen && renderDetails()}
        <div className="message-time">
          {message.timestamp.toLocaleTimeString('pt-BR', { 
            hour: '2-digit', 
            minute: '2-digit' 
          })}
        </div>
      </div>
    </div>
  );
}

export default MessageBubble;
