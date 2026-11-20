// Lista de conversas para as colunas do chat largo (coluna Conversas e o título do
// centro). A gaveta do modo compacto continua no useSessionManagement, que busca
// a cada abertura; aqui a lista fica na tela, então ela se mantém sozinha:
// ao montar, no fim de cada turno (a conversa nova ganha título com a 1ª
// mensagem), quando o core troca de sessão e a cada 60 s.
//
// Só roda com alguém inscrito, como o useJobs.

import { useSyncExternalStore } from 'react';
import { humanizeTime, type SessionWithHumanizedTime } from './useSessionManagement';

export interface SessionsSnapshot {
  /** Mais recentes primeiro (ordem do core). */
  sessions: SessionWithHumanizedTime[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}

const REFRESH_MS = 60_000;
const SESSIONS_LIMIT = 100;

let snapshot: SessionsSnapshot = { sessions: [], status: 'idle', error: null };
const listeners = new Set<() => void>();
let timer: number | undefined;
let inflight: Promise<void> | null = null;
let stopAgentEvents: (() => void) | undefined;

function emit(next: SessionsSnapshot): void {
  snapshot = next;
  listeners.forEach((notify) => notify());
}

export function refreshSessions(): Promise<void> {
  if (inflight) return inflight;
  if (snapshot.status === 'idle' || snapshot.status === 'error') {
    emit({ ...snapshot, status: 'loading', error: null });
  }
  inflight = (async () => {
    try {
      const list = await window.electronAPI?.listSessions(SESSIONS_LIMIT);
      emit({
        sessions: (list ?? []).map((session) => ({
          ...session,
          humanizedTime: humanizeTime(session.last_activity_at),
        })),
        status: 'ready',
        error: null,
      });
    } catch (err) {
      console.error('[Neo] Falha ao carregar as conversas (colunas):', err);
      emit({ ...snapshot, status: snapshot.sessions.length ? 'ready' : 'error', error: String(err) });
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

/** Conversa apagada: some da lista já, sem esperar o core. Depois confere com o
 *  core — DEPOIS de qualquer busca em voo, que pode ter saído antes do DELETE e
 *  traria a conversa de volta. */
export function forgetSession(sessionId: string): void {
  emit({ ...snapshot, sessions: snapshot.sessions.filter((s) => s.session_id !== sessionId) });
  void (inflight ?? Promise.resolve()).then(() => refreshSessions());
}

function start(): void {
  stopAgentEvents = window.electronAPI?.onAgentEvent((event) => {
    if (event.type === 'complete' || event.type === 'session_changed') void refreshSessions();
  });
  timer = window.setInterval(() => {
    if (!document.hidden) void refreshSessions();
  }, REFRESH_MS);
  void refreshSessions();
}

function stop(): void {
  stopAgentEvents?.();
  stopAgentEvents = undefined;
  window.clearInterval(timer);
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

function getSnapshot(): SessionsSnapshot {
  return snapshot;
}

export function useSessionsList(): SessionsSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}
