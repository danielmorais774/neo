// Tarefas em background para as colunas do chat largo: UMA consulta só, lida pela
// coluna "Segundo plano", pelo selo das conversas, pelo título do centro e pelo
// painel de agentes.
//
// Mesmo ritmo do visor (JobsViewer): a cada 4 s enquanto houver tarefa ativa, a
// cada 20 s quando está tudo parado. A consulta só roda enquanto alguém está
// inscrito — escondeu as colunas, parou — e pausa com a janela oculta: cada volta
// vai até o core de produção pelo túnel.
//
// Store de módulo (não contexto) pelo mesmo motivo do lib/activeSession.ts: nada
// disto pode re-renderizar o ChatPage.

import { useSyncExternalStore } from 'react';
import type { JobSummary } from '../lib/tauriApi';

export interface JobsSnapshot {
  /** Ativas primeiro, depois as mais recentes (ordem do core). */
  jobs: JobSummary[];
  /** status → quantidade, sobre TODAS as tarefas do dono (não só a página). */
  counts: Record<string, number>;
  /** queued + running, de todas. */
  active: number;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string;
  /** Hora (ms) da última resposta: o "rodando há…" conta a partir dela, sem
   *  `Date.now()` no render. */
  fetchedAt: number;
  /** Sobe a cada resposta: quem mostra o fim do log de uma tarefa ativa relê nela. */
  version: number;
}

const FAST_POLL_MS = 4_000;
const SLOW_POLL_MS = 20_000;
/** A janela Tarefas pede 100; a coluna mostra menos e roda mais vezes. */
const JOBS_LIMIT = 50;

let snapshot: JobsSnapshot = {
  jobs: [],
  counts: {},
  active: 0,
  status: 'idle',
  error: '',
  fetchedAt: 0,
  version: 0,
};
const listeners = new Set<() => void>();
let timer: number | undefined;
let inflight: Promise<void> | null = null;
let again = false;
let stopAgentEvents: (() => void) | undefined;

function emit(next: JobsSnapshot): void {
  snapshot = next;
  listeners.forEach((notify) => notify());
}

function schedule(): void {
  window.clearTimeout(timer);
  timer = undefined;
  if (!listeners.size || document.hidden) return;
  timer = window.setTimeout(() => void refreshJobs(), snapshot.active > 0 ? FAST_POLL_MS : SLOW_POLL_MS);
}

/** Busca agora (e remarca o relógio). Pedido durante uma busca vira mais uma
 *  volta no fim dela: depois de um Cancelar, a resposta em voo pode ser velha. */
export function refreshJobs(): Promise<void> {
  if (inflight) {
    again = true;
    return inflight;
  }
  if (snapshot.status === 'idle') emit({ ...snapshot, status: 'loading' });
  inflight = (async () => {
    try {
      const page = await window.electronAPI?.listJobs('all', JOBS_LIMIT);
      emit({
        jobs: page?.jobs ?? [],
        counts: page?.counts ?? {},
        active: page?.active ?? 0,
        status: 'ready',
        error: '',
        fetchedAt: Date.now(),
        version: snapshot.version + 1,
      });
    } catch (err) {
      console.error('[Neo] Falha ao carregar as tarefas (colunas):', err);
      // Com lista na tela, mantém a lista: um soluço do túnel não apaga a coluna.
      emit({ ...snapshot, status: snapshot.jobs.length ? 'ready' : 'error', error: String(err) });
    } finally {
      inflight = null;
      if (again && listeners.size) {
        again = false;
        void refreshJobs();
      } else {
        again = false;
        schedule();
      }
    }
  })();
  return inflight;
}

function onVisibility(): void {
  if (document.hidden) {
    window.clearTimeout(timer);
    timer = undefined;
  } else if (listeners.size) {
    void refreshJobs();
  }
}

function start(): void {
  document.addEventListener('visibilitychange', onVisibility);
  // Fim de turno costuma ser quando uma tarefa nasce: não espera o relógio.
  stopAgentEvents = window.electronAPI?.onAgentEvent((event) => {
    if (event.type === 'complete') void refreshJobs();
  });
  void refreshJobs();
}

function stop(): void {
  document.removeEventListener('visibilitychange', onVisibility);
  stopAgentEvents?.();
  stopAgentEvents = undefined;
  window.clearTimeout(timer);
  timer = undefined;
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  if (listeners.size === 1) start();
  return () => {
    listeners.delete(notify);
    if (listeners.size === 0) stop();
  };
}

function getSnapshot(): JobsSnapshot {
  return snapshot;
}

export function useJobs(): JobsSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}
