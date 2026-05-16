import { useCallback, useState, useEffect } from 'react';
import Chat from '../components/Chat';
import PasswordModal from '../components/PasswordModal';
import type { Message } from '../types';
import { 
  useMessages, 
  useAgentState, 
  useAgentEvents,
  useAppStartup 
} from '../hooks';
import {
  HistoryService,
  CommandService,
  AgentService,
  WindowService
} from '../lib';

export const ChatPage = () => {
  const {
    messages,
    setMessages,
    addMessage,
    clearMessages,
    loadMessages
  } = useMessages();

  const {
    isLoading,
    isExecuting,
    setIsExecuting,
    currentIteration,
    setCurrentIteration,
    maxIterations,
    setMaxIterations,
    windowMode,
    setWindowMode,
    selectedModel,
    setSelectedModel,
    selectedProvider,
    setSelectedProvider,
    chatMode,
    setChatMode,
    startupNotification,
    setStartupNotification
  } = useAgentState();

  // Hook para carregar histórico e configurar startup
  const loadHistory = useCallback(async () => {
    const history = await HistoryService.loadHistory();
    if (history.length > 0) {
      loadMessages(history);
    }
  }, [loadMessages]);

  useAppStartup({
    setWindowMode,
    setStartupNotification,
    loadHistory
  });

  // Password prompt state
  const [passwordPrompt, setPasswordPrompt] = useState<string | null>(null);

  useEffect(() => {
    window.electronAPI?.onPasswordRequest((prompt: string) => {
      setPasswordPrompt(prompt);
    });
  }, []);

  useEffect(() => {
    HistoryService.saveHistory(messages);
  }, [messages]);

  useEffect(() => {
    const unlisten = window.electronAPI?.onNotificationAction?.((event) => {
      const payload = event.action_payload || {};
      const missionId = String((payload as any).mission_id || (payload as any).missionId || '').trim();
      if (!missionId) return;

      const missionStatus =
        event.action_type === 'approve' || event.action_type === 'retry'
          ? 'approved'
          : event.action_type === 'cancel'
            ? 'cancelled'
            : 'pending';

      setMessages(prev =>
        prev.map(msg =>
          msg.mission?.missionId === missionId && msg.mission.status === 'pending'
            ? { ...msg, mission: { ...msg.mission, status: missionStatus as any } }
            : msg
        )
      );
    });

    return () => {
      unlisten?.();
    };
  }, [setMessages]);

  useEffect(() => {
    const unlisten = window.electronAPI?.onNotification?.((notification: any) => {
      const payload = notification.payload || {};
      const inboundText = String(payload.inbound_text || payload.inboundText || '').trim();
      if (!inboundText) return;

      const notificationId = String(notification.id || `${Date.now()}`);
      const missionId = String(
        notification.mission_id || notification.missionId || payload.mission_id || payload.missionId || ''
      ).trim();
      const contactLabel = String(payload.contact_label || payload.contactLabel || 'Contato');
      const contactJid = String(payload.contact_jid || payload.contactJid || '');
      const contactPhone = String(payload.contact_phone_e164 || payload.contactPhone || '');
      const draftText = String(payload.draft_text || payload.draftText || '').trim();
      const draftMessages = Array.isArray(payload.draft_messages)
        ? payload.draft_messages.map(String).filter(Boolean)
        : Array.isArray(payload.draftMessages)
          ? payload.draftMessages.map(String).filter(Boolean)
          : draftText
            ? [draftText]
            : [];

      setMessages(prev => {
        if (prev.some(msg => msg.details?.notificationId === notificationId)) {
          return prev;
        }

        const inboundMessage: Message = {
          id: `${notificationId}-inbound`,
          role: 'assistant',
          content: `${contactLabel} respondeu: "${inboundText}"`,
          messageType: 'text',
          timestamp: new Date(),
          details: {
            eventType: 'whatsapp_inbound',
            notificationId,
            missionId,
            contactLabel,
            contactJid,
            contactPhone,
            inboundText,
          },
        };

        if (!missionId || draftMessages.length === 0) {
          return [...prev, inboundMessage];
        }

        const replyCard: Message = {
          id: `${notificationId}-reply-card`,
          role: 'assistant',
          content: `${contactLabel} respondeu. Rascunho pronto, aguardando sua aprovação para enviar.`,
          messageType: 'mission',
          timestamp: new Date(),
          mission: {
            missionId,
            contactLabel,
            contactJid,
            contactPhone,
            draftText: draftText || draftMessages[0],
            draftMessages,
            status: 'pending',
            backendStatus: 'awaiting_confirmation',
            nextActionType: 'send_whatsapp',
          },
          details: {
            eventType: 'whatsapp_reply_draft',
            notificationId,
            inboundText,
          },
        };

        return [...prev, inboundMessage, replyCard];
      });
    });

    return () => {
      unlisten?.();
    };
  }, [setMessages]);

  const handlePasswordSubmit = (password: string) => {
    window.electronAPI?.respondToPassword(password);
    setPasswordPrompt(null);
  };

  const handlePasswordCancel = () => {
    window.electronAPI?.respondToPassword(null);
    setPasswordPrompt(null);
  };

  // Hook para eventos do agente
  useAgentEvents({
    setMessages,
    setIsExecuting,
    setCurrentIteration,
    setMaxIterations
  });

  // Handlers
  const handleSendMessage = async (content: string) => {
    addMessage({
      role: 'user',
      content,
      messageType: 'text'
    });

    setIsExecuting(true);
    setCurrentIteration(0);
    
    try {
      await AgentService.executeWithLoop(content, selectedModel, chatMode, selectedProvider);
    } catch (error: any) {
      console.error('Erro:', error);
      addMessage({
        role: 'assistant',
        content: `❌ Erro: ${error.message || 'Falha ao processar mensagem'}`,
        messageType: 'error'
      });
      setIsExecuting(false);
    }
  };

  const handleStop = () => {
    AgentService.stopExecution();
  };

  const handlePermissionResponse = async (messageId: string, requestId: string, approved: boolean) => {
    const message = messages.find(m => m.id === messageId);
    if (!message?.command) return;

    setMessages(prev => 
      prev.map(msg => 
        msg.id === messageId && msg.command
          ? {
              ...msg,
              command: {
                ...msg.command,
                status: approved ? 'approved' : 'rejected'
              }
            }
          : msg
      )
    );
    
    CommandService.respondToPermission(requestId, approved);
  };

  const handleMissionResponse = async (missionId: string, approved: boolean, editedText?: string) => {
    const tempId = `sending-${missionId}-${Date.now()}`;

    const sendingMsg: Message = {
      id: tempId,
      role: 'assistant',
      content: 'enviando...',
      messageType: 'text',
      timestamp: new Date(),
      details: { eventType: 'sending_indicator' },
    };

    setMessages(prev => [...prev, sendingMsg]);

    setMessages(prev =>
      prev.map(msg =>
        msg.mission?.missionId === missionId && msg.mission.status === 'pending'
          ? { ...msg, mission: { ...msg.mission, status: approved ? 'approved' : 'rejected' } }
          : msg
      )
    );

    try {
      const command = approved ? (editedText ? 'edit' : 'approve') : 'cancel';
      const payload = editedText
        ? { command, edited_text: editedText }
        : { command };
      const url = `http://localhost:8000/api/v1/missions/${missionId}/command`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      await fetch(`http://localhost:8000/api/v1/missions/${missionId}/notifications/ack`, {
        method: 'POST',
      }).catch((ackErr) => {
        console.warn('[ChatPage] notification ack by mission failed:', ackErr);
      });

      setMessages(prev => prev.filter(msg => msg.id !== tempId));

      if (data.display_message) {
        addMessage({
          role: 'assistant',
          content: data.display_message,
          messageType: 'text',
        });
      }

      if (data.is_auto_reply && data.sent_text) {
        addMessage({
          role: 'assistant',
          content: `Responder para o contato: "${data.sent_text}"`,
          messageType: 'text',
        });
      }
    } catch (e) {
      console.error('[ChatPage] handleMissionResponse failed:', e);
      setMessages(prev => prev.filter(msg => msg.id !== tempId));
    }
  };

  const handleNewChat = async () => {
    if (isExecuting) {
      handleStop();
    }
    // Gera novo sessionId no renderer e passa diretamente para o agente
    const newSessionId = crypto.randomUUID();
    await window.electronAPI?.resetAgent(newSessionId);
    await HistoryService.clearHistory();
    clearMessages();
    setCurrentIteration(0);
  };

  const handleClearHistory = async () => {
    await HistoryService.clearHistory();
    clearMessages();
  };

  return (
    <>
      {startupNotification && (
        <div className="startup-banner" role="status">
          <span>{startupNotification}</span>
          <button className="startup-close" onClick={() => setStartupNotification(null)}>✕</button>
        </div>
      )}

      {passwordPrompt && (
        <PasswordModal
          prompt={passwordPrompt}
          onSubmit={handlePasswordSubmit}
          onCancel={handlePasswordCancel}
        />
      )}

      <Chat
        messages={messages}
        isLoading={isLoading}
        isExecuting={isExecuting}
        currentIteration={currentIteration}
        maxIterations={maxIterations}
        onSendMessage={handleSendMessage}
        onClearHistory={handleClearHistory}
        onNewChat={handleNewChat}
        onStop={handleStop}
        onPermissionResponse={handlePermissionResponse}
        onMissionResponse={handleMissionResponse}
        onModelChange={setSelectedModel}
        onModeChange={setChatMode}
        onProviderChange={setSelectedProvider}
        selectedModel={selectedModel}
        selectedMode={chatMode}
        selectedProvider={selectedProvider}
        windowMode={windowMode}
        initialInput={`manda uma mensagem para minha irmã avisando que vou chegar mais tarde`}
      />
    </>
  );
};
