import { useCallback, useState, useEffect, useRef } from 'react';
import Chat from '../components/Chat';
import LiveOverlay from '../components/LiveOverlay';
import PasswordModal from '../components/PasswordModal';
import type { Message } from '../types';
import {
  useMessages,
  useAgentState,
  useAgentEvents,
  useAppStartup,
  useLiveMode,
  useWakeWord,
  useWakeEnabled
} from '../hooks';
import type { VoiceInputMeta } from '../hooks/useLiveMode';
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

  // Abrir uma sessão anterior pelo sidebar. `resetAgent` aponta o AppState do
  // Rust pro sessionId escolhido; como `loadHistory` busca
  // /desktop/session/{atual}/history, ele já traz o histórico da sessão certa —
  // por isso a ordem (resetAgent ANTES de carregar) importa.
  useEffect(() => {
    const handleLoadSession = async (e: Event) => {
      const { sessionId } = (e as CustomEvent).detail || {};
      if (!sessionId) return;

      if (isExecuting) AgentService.stopExecution();

      try {
        await window.electronAPI?.resetAgent(sessionId);
        const history = await HistoryService.loadHistory();
        // Troca o conteúdo mesmo se vier vazio: manter as mensagens da conversa
        // anterior na tela faria parecer que a sessão escolhida é outra.
        loadMessages(history ?? []);
        setCurrentIteration(0);
      } catch (err) {
        console.error('[Neo] Falha ao abrir sessão:', err);
      }
    };

    window.addEventListener('neo-load-session', handleLoadSession);
    return () => window.removeEventListener('neo-load-session', handleLoadSession);
  }, [loadMessages, isExecuting, setCurrentIteration]);

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
      const kind = String(notification.kind || '').trim();
      if (!['confirm_reply', 'unsolicited_message', 'mission_completed'].includes(kind)) {
        return;
      }

      const payload = notification.payload || {};
      const inboundText = String(payload.inbound_text || payload.inboundText || '').trim();

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

        if (kind === 'mission_completed') {
          return prev.map(msg =>
            msg.mission?.missionId === missionId && msg.mission.status === 'pending'
              ? { ...msg, mission: { ...msg.mission, status: 'approved' as any } }
              : msg
          );
        }

        if (!inboundText) {
          return prev;
        }

        const inboundDedup = prev.some(
          msg =>
            msg.details?.eventType === 'whatsapp_inbound' &&
            msg.details?.missionId === missionId &&
            msg.details?.inboundText === inboundText
        );
        const inboundMessage: Message = {
          id: `${notificationId}-inbound`,
          role: 'assistant',
          content: `${contactLabel} respondeu:\n"${inboundText}"`,
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

        const withInbound = inboundDedup ? prev : [...prev, inboundMessage];
        if (kind !== 'confirm_reply' || !missionId || draftMessages.length === 0) {
          return withInbound;
        }

        const replyCardDedup = withInbound.some(
          msg =>
            msg.messageType === 'mission' &&
            msg.mission?.missionId === missionId &&
            msg.mission?.status === 'pending' &&
            msg.mission?.draftText === (draftText || draftMessages[0])
        );
        if (replyCardDedup) {
          return withInbound;
        }
        const replyCard: Message = {
          id: `${notificationId}-reply-card`,
          role: 'assistant',
          content: '',
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

        return [...withInbound, replyCard];
      });
    });

    return () => {
      unlisten?.();
    };
  }, [setMessages]);

  // Proactive chat messages from background jobs (workbench): the neo-core pushes a
  // kind="chat_message" notification when a job it ran for us finishes. Append it to
  // the timeline as a normal assistant bubble (deduped by notification id) and, if
  // Live mode is on, ask it to speak the text. See workbench-background-jobs-plan.md.
  useEffect(() => {
    const unlisten = window.electronAPI?.onNotification?.((notification: any) => {
      if (String(notification.kind || '').trim() !== 'chat_message') return;

      const payload = notification.payload || {};
      const text = String(
        payload.text || notification.body || notification.description || ''
      ).trim();
      if (!text) return;
      const notificationId = String(notification.id || `${Date.now()}`);
      const jobId = String(payload.job_id || payload.jobId || '').trim();

      setMessages(prev => {
        if (prev.some(msg => msg.details?.notificationId === notificationId)) {
          return prev;
        }
        const jobMessage: Message = {
          id: `${notificationId}-job`,
          role: 'assistant',
          content: text,
          messageType: 'text',
          timestamp: new Date(),
          details: {
            eventType: 'agent_job_result',
            notificationId,
            jobId,
          },
        };
        return [...prev, jobMessage];
      });

      // Best-effort: speak it when Live mode is active and idle between turns.
      try {
        window.dispatchEvent(
          new CustomEvent('neo:proactive-speak', { detail: { text } })
        );
      } catch {
        /* noop */
      }
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
  const handleSendMessage = async (
    content: string,
    modeOverride?: string,
    attachmentIds?: string[],
    voice?: VoiceInputMeta
  ) => {
    addMessage({
      role: 'user',
      content,
      messageType: 'text'
    });

    setIsExecuting(true);
    setCurrentIteration(0);

    // modeOverride vem do Live (="live") p/ resposta falada/compacta; senão usa chatMode.
    try {
      await AgentService.executeWithLoop(
        content,
        selectedModel,
        modeOverride ?? chatMode,
        selectedProvider,
        attachmentIds,
        voice
      );
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

  // Modo Live (voz): captura mic → STT (neo-voice) → mesmo pipeline de texto;
  // resposta final do agente é falada de volta via TTS (neo-voice).
  const { isLive, liveState, partialText, spokenText, micLevel, liveError, toggleLive, interrupt } = useLiveMode({
    // O Live não anexa arquivos; o 3º arg de handleSendMessage é `attachmentIds`,
    // então o `voice` entra na posição certa por este wrapper.
    onSendMessage: (text, mode, voice) => handleSendMessage(text, mode, undefined, voice),
  });

  // Wake word (Estágio 1): "Ei Neo" reconhecido localmente → acende o Modo Live.
  // Usa um ref de isLive p/ o callback não capturar um valor velho.
  const isLiveRef = useRef(isLive);
  useEffect(() => {
    isLiveRef.current = isLive;
  }, [isLive]);
  const [wakeEnabled] = useWakeEnabled();
  // A wake word "Ei Neo" é roteada pelo Rust conforme a janela: se ela está
  // VISÍVEL na tela, o Rust emite `wake-live-inapp` e abrimos o Modo Live DENTRO
  // do app (overlay, abaixo); se está escondida/minimizada, o Rust sobe o chip
  // nativo do notch. Por isso o onWake do sidecar aqui é no-op (a decisão mora
  // no Rust) — mantido só para não competir pelo microfone.
  const handleWake = useCallback(() => {
    /* no-op: o roteamento notch-vs-in-app é decidido no Rust (wake-detected) */
  }, []);
  // "pare": interrompe o overlay Live in-app (webview). O chip nativo trata a
  // mesma interrupção pelo caminho Rust; `interrupt()` é no-op se o overlay não
  // estiver ativo, então não há conflito entre as duas sessões.
  useWakeWord({ enabled: wakeEnabled, onWake: handleWake, onInterrupt: interrupt });

  // Janela visível + "Ei Neo" -> abre o Modo Live in-app. Guardado por isLiveRef
  // para não FECHAR uma sessão já aberta (toggleLive alterna) nem abrir em dobro.
  useEffect(() => {
    const unlisten = window.electronAPI?.onWakeLiveInApp(() => {
      if (!isLiveRef.current) toggleLive();
    });
    return () => unlisten?.();
  }, [toggleLive]);

  // O chip nativo fechou (X ou auto-desligar por inatividade) -> fecha também o
  // overlay in-app, se estiver aberto. Guardado por isLiveRef porque toggleLive
  // alterna: sem isso, um live_close com o overlay já fechado o ABRIRIA.
  useEffect(() => {
    const unlisten = window.electronAPI?.onLiveCloseRequested(() => {
      if (isLiveRef.current) toggleLive();
    });
    return () => unlisten?.();
  }, [toggleLive]);

  const handleStop = () => {
    AgentService.stopExecution();
  };

  // A aprovação é casada pelo requestId (o command_id do neo-core), NUNCA pelo id da
  // bolha: com ids colidindo, o find por messageId achava a bolha de thinking (sem
  // .command) e a função saía antes de enviar, deixando o botão travado em
  // "Aguardando..." e o core esperando até o timeout de 300s. O envio vem primeiro e
  // não depende de encontrar a bolha — o estado visual é consequência, não pré-requisito.
  const handlePermissionResponse = async (messageId: string, requestId: string, approved: boolean) => {
    if (!requestId) {
      console.error('[ChatPage] aprovação sem requestId — nada a enviar', { messageId, approved });
      return;
    }

    CommandService.respondToPermission(requestId, approved);

    setMessages(prev =>
      prev.map(msg =>
        msg.command?.requestId === requestId
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
  };

  const handleMissionResponse = async (missionId: string, approved: boolean, editedText?: string) => {
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
      const url = `http://127.0.0.1:8000/api/v1/missions/${missionId}/command`;
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      await fetch(`http://127.0.0.1:8000/api/v1/missions/${missionId}/notifications/ack`, {
        method: 'POST',
      }).catch((ackErr) => {
        console.warn('[ChatPage] notification ack by mission failed:', ackErr);
      });
    } catch (e) {
      console.error('[ChatPage] handleMissionResponse failed:', e);
    }
  };

  const handleNewChat = async () => {
    if (isExecuting) {
      handleStop();
    }
    // A sessão que está saindo não precisa ser registrada aqui: o neo-core já a
    // persistiu em conversation_session conforme as mensagens foram trocadas, e
    // é de lá que a lista de sessões é lida.
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
        // O Chat manda anexos no 2º arg; o Live usa esse mesmo slot para o mode
        // (useLiveMode chama handleSendMessage(texto, 'live')) — daí o wrapper.
        onSendMessage={(msg, attachmentIds) => handleSendMessage(msg, undefined, attachmentIds)}
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
        // initialInput={`manda uma mensagem para minha irmã avisando que vou chegar mais tarde`}
        initialInput={``}
        isLive={isLive}
        liveState={liveState}
        livePartialText={partialText}
        liveMicLevel={micLevel}
        liveError={liveError}
        onToggleLive={toggleLive}
      />

      {isLive && (
        <LiveOverlay
          liveState={liveState}
          partialText={partialText}
          spokenText={spokenText}
          micLevel={micLevel}
          onClose={toggleLive}
        />
      )}
    </>
  );
};
