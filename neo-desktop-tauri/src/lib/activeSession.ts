// A conversa aberta no chat, para quem mora FORA do ChatPage (as colunas do chat
// largo). O ChatPage continua dono do `sessionId`; ele só publica aqui cada troca
// (novo chat, sessão antiga, rotação do core via `session_changed`).
//
// Store de módulo em vez de contexto: as colunas são irmãs do ChatPage no App, e
// nada do que elas leem pode fazer o chat re-renderizar (a lista é virtualizada e
// o memo depende de callbacks estáveis — neo-desktop-chat-virtualization).

import { useSyncExternalStore } from 'react';

let current = '';
let primed = false;
const listeners = new Set<() => void>();

export function setActiveSession(sessionId: string): void {
  if (!sessionId || sessionId === current) return;
  current = sessionId;
  listeners.forEach((notify) => notify());
}

export function getActiveSession(): string {
  return current;
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  // Uma coluna pode montar antes de o ChatPage publicar: pergunta ao Rust uma vez.
  if (!primed) {
    primed = true;
    window.electronAPI
      ?.getSessionId()
      .then((id) => {
        if (!current && id) setActiveSession(id);
      })
      .catch(() => {
        /* sem id, nada fica destacado até o ChatPage publicar */
      });
  }
  return () => {
    listeners.delete(notify);
  };
}

export function useActiveSession(): string {
  return useSyncExternalStore(subscribe, getActiveSession);
}

/** Abre uma conversa pelo MESMO evento da gaveta (o ChatPage é quem troca). A que
 *  já está aberta não é reaberta: o handler pararia o turno em andamento. */
export function openConversation(sessionId: string): void {
  if (!sessionId || sessionId === current) return;
  window.dispatchEvent(new CustomEvent('neo-load-session', { detail: { sessionId } }));
}

/** O mesmo "Novo chat" da barra do topo. */
export function startNewChat(): void {
  window.dispatchEvent(new CustomEvent('neo-new-chat'));
}
