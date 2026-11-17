// Atalhos de teclado das janelas de documento do Neo: o caderno da memória de
// trabalho (⌘⇧M) e as tarefas em background (⌘⇧J).
//
// Até aqui as duas só abriam por Configurações (⚙) → "Abrir o caderno" /
// "Abrir tarefas": três cliques para olhar o que o Neo anotou no turno anterior,
// que é justamente a consulta que se faz no meio da conversa.
//
// Registrado no `main.tsx`, antes do React montar, e não dentro de um componente:
// o `index.html` é a MESMA página das três janelas (chat, `?mode=working-memory`
// e `?mode=jobs`), então um registro só faz o atalho valer em qualquer uma delas
// — é o que permite pular de uma janela para a outra sem passar pelo mouse. Fica
// fora a janela de notificação, que tem entrada própria (`notification.html`):
// é um toast, não um lugar onde alguém digita atalho.
//
// **Não é atalho global de sistema.** O `tauri_plugin_global_shortcut` está
// registrado no `lib.rs` (hoje sem uso) e daria a tecla mesmo com o Neo em
// segundo plano; aqui o listener é do documento, então ele só responde com uma
// janela do Neo em foco na tela — o escopo que foi pedido.
import { electronAPI } from './tauriApi';

interface ViewerShortcut {
  /** Tecla FÍSICA (`event.code`), nunca `event.key`: com ⇧ segurado o `key` vem
   *  'M' maiúsculo, e em layout não-QWERTY o caractere muda de lugar. */
  code: string;
  /** Só para a mensagem de erro no console. */
  what: string;
  open: () => Promise<void>;
}

const SHORTCUTS: ViewerShortcut[] = [
  {
    code: 'KeyM',
    what: 'o caderno',
    open: () => electronAPI.openWorkingMemoryWindow(),
  },
  {
    code: 'KeyJ',
    what: 'as tarefas',
    open: () => electronAPI.openJobsWindow(),
  },
];

/** Combos como a interface os escreve. Ficam aqui junto do `code` que os
 *  implementa: dica e atalho que discordam viram bug que ninguém vê no compilador. */
export const VIEWER_SHORTCUT_HINTS = {
  workingMemory: '⌘⇧M',
  jobs: '⌘⇧J',
} as const;

/** Flag na `window`, não no módulo: com o HMR do `tauri dev` este arquivo é
 *  reexecutado a cada salvamento, e um módulo novo não lembra do listener que o
 *  anterior deixou — cada salvamento somaria um. */
declare global {
  interface Window {
    __neoViewerShortcuts?: boolean;
  }
}

export function registerViewerShortcuts(): void {
  if (typeof window === 'undefined') return;
  if (window.__neoViewerShortcuts) return;
  window.__neoViewerShortcuts = true;

  window.addEventListener('keydown', (event) => {
    if (!event.metaKey || !event.shiftKey || event.altKey || event.ctrlKey) return;
    // Tecla segurada repete o keydown: sem isto seria um `invoke` por repetição,
    // cada um refocando a mesma janela.
    if (event.repeat) return;

    const hit = SHORTCUTS.find((shortcut) => shortcut.code === event.code);
    if (!hit) return;

    event.preventDefault();
    hit.open().catch((err) => {
      console.error(`[Neo] Falha ao abrir ${hit.what} pelo atalho:`, err);
    });
  });
}
