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
    setMessages(prev =>
      prev.map(msg =>
        msg.mission?.missionId === missionId
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
      await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    } catch (e) {
      console.error('[ChatPage] handleMissionResponse failed:', e);
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
