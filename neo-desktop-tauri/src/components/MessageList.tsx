import { memo, useLayoutEffect, useRef, useState } from 'react';
import type { Message } from '../types';
import MessageBubble from './MessageBubble';
import { ESTIMATED_SLOT_HEIGHT, MessageVirtualizer } from '../lib/messageVirtualizer';

// Lista virtualizada do chat — o mecanismo está em lib/messageVirtualizer.ts.
//
// É um componente memoizado à parte do Chat de propósito: antes, cada tecla no
// input e cada nível de microfone do Live re-renderizavam TODAS as bolhas
// (ReactMarkdown + highlight em cada uma). Aqui só entra render quando muda a
// lista ou o indicador de digitando.

/** Últimas bolhas que já nascem montadas: é o que aparece no primeiro frame ao
 * abrir uma conversa (e toda mensagem nova cai aqui). O resto nasce como slot
 * vazio e os observadores montam o que estiver perto da tela. */
const MOUNTED_TAIL = 10;

type PermissionHandler = (messageId: string, requestId: string, approved: boolean) => void;
type MissionHandler = (missionId: string, approved: boolean, editedText?: string) => void;

interface MessageSlotProps {
  message: Message;
  virtualizer: MessageVirtualizer;
  startMounted: boolean;
  onPermissionResponse: PermissionHandler;
  onMissionResponse: MissionHandler;
}

interface SlotView {
  mounted: boolean;
  /** Altura do slot vazio (a última medida da bolha, ou a estimativa). */
  height: number;
  /** Só anima a entrada da bolha na primeira vez. Remontar ao rolar de volta
   * com o slideIn parecia mensagem chegando de novo. */
  animate: boolean;
}

const MessageSlot = memo(function MessageSlot({
  message,
  virtualizer,
  startMounted,
  onPermissionResponse,
  onMissionResponse,
}: MessageSlotProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<SlotView>(() => ({
    mounted: startMounted,
    height: ESTIMATED_SLOT_HEIGHT,
    animate: startMounted,
  }));

  const id = message.id;
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    return virtualizer.register(el, id, (mounted, height) => {
      setView((prev) => {
        if (prev.mounted === mounted) return prev;
        return mounted
          ? { ...prev, mounted: true }
          : { mounted: false, height, animate: false };
      });
    });
  }, [virtualizer, id]);

  return (
    <div
      ref={ref}
      className={view.animate ? 'message-slot' : 'message-slot message-slot--static'}
      style={view.mounted ? undefined : { height: view.height }}
    >
      {view.mounted && (
        <MessageBubble
          message={message}
          onPermissionResponse={onPermissionResponse}
          onMissionResponse={onMissionResponse}
        />
      )}
    </div>
  );
});

interface MessageListProps {
  messages: Message[];
  showTyping: boolean;
  onPermissionResponse: PermissionHandler;
  onMissionResponse: MissionHandler;
}

function MessageList({ messages, showTyping, onPermissionResponse, onMissionResponse }: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [virtualizer] = useState(() => new MessageVirtualizer());

  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    return virtualizer.attach(el);
  }, [virtualizer]);

  // A primeira mensagem só troca quando a lista inteira é outra conversa.
  const firstId = messages[0]?.id;
  const lastFirstIdRef = useRef(firstId);

  // Auto-scroll pro fim ANTES do paint (useLayoutEffect) e de forma INSTANTÂNEA.
  // O conteúdo muda de altura a cada evento do agente (bolhas de "thinking" que
  // entram/saem, e no `complete` todas as thinking somem e o balão de resposta
  // entra). Se o scroll rodasse depois do paint (useEffect) e com `smooth`, o
  // navegador pintava primeiro o frame com o conteúdo já mexido e o scroll no
  // lugar antigo e só então o smooth corria atrás — parecia a janela "encolhendo
  // e se reajustando". Aqui conteúdo e scroll caem no mesmo frame. Só pinamos se
  // o usuário já está no fim (senão ele não conseguiria rolar pra cima e ler).
  // Crescimento DEPOIS do render (streaming, imagem carregando, a lista
  // encolhendo quando a barra do Live aparece) é seguido pelo ResizeObserver.
  useLayoutEffect(() => {
    if (lastFirstIdRef.current !== firstId) {
      lastFirstIdRef.current = firstId;
      virtualizer.pinToBottom();
      return;
    }
    virtualizer.followIfPinned();
  }, [virtualizer, messages, showTyping, firstId]);

  const tailStart = messages.length - MOUNTED_TAIL;

  return (
    <div className="messages-container" ref={containerRef} onScroll={virtualizer.handleScroll}>
      {messages.map((message, index) => (
        <MessageSlot
          key={message.id}
          message={message}
          virtualizer={virtualizer}
          startMounted={index >= tailStart}
          onPermissionResponse={onPermissionResponse}
          onMissionResponse={onMissionResponse}
        />
      ))}
      {showTyping && (
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
    </div>
  );
}

export default memo(MessageList);
