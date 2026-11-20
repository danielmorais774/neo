// Em que largura a janela está, para o chat em três colunas
// (neo-core/docs/desktop-chat-tres-colunas-plan.md §3.1). A webview tem o
// tamanho da janela, então media query basta; o hook existe para o App não MONTAR
// o que não aparece (cada coluna consulta o core).
//
//   compact  < 960 px   → como sempre foi (a padrão é 560×720): gaveta de sessões.
//   medium   960–1279   → conversas + chat; "Segundo plano" abre por cima, no botão.
//   wide     ≥ 1280     → as três colunas.

import { useSyncExternalStore } from 'react';

export type WideLayout = 'compact' | 'medium' | 'wide';

export const MEDIUM_MIN_PX = 960;
export const WIDE_MIN_PX = 1280;

const MEDIUM_QUERY = `(min-width: ${MEDIUM_MIN_PX}px)`;
const WIDE_QUERY = `(min-width: ${WIDE_MIN_PX}px)`;

function getLayout(): WideLayout {
  if (typeof window.matchMedia !== 'function') return 'compact';
  if (window.matchMedia(WIDE_QUERY).matches) return 'wide';
  if (window.matchMedia(MEDIUM_QUERY).matches) return 'medium';
  return 'compact';
}

function subscribe(notify: () => void): () => void {
  if (typeof window.matchMedia !== 'function') return () => {};
  const queries = [window.matchMedia(MEDIUM_QUERY), window.matchMedia(WIDE_QUERY)];
  queries.forEach((query) => query.addEventListener('change', notify));
  return () => queries.forEach((query) => query.removeEventListener('change', notify));
}

export function useWideLayout(): WideLayout {
  return useSyncExternalStore(subscribe, getLayout);
}
