import { useState, useCallback } from 'react';
import type { SessionInfo } from '../lib/tauriApi';

export type { SessionInfo };

export interface SessionWithHumanizedTime extends SessionInfo {
  humanizedTime: string;
}

/** O core grava `datetime.utcnow()` e serializa SEM sufixo de timezone
 *  ("2026-07-30T02:23:10.826889"). `new Date()` leria isso como hora LOCAL e o
 *  "há X" sairia deslocado pelo fuso (no -03 daria 3h a menos, chegando a
 *  mostrar tempo negativo em conversa recente). Daí o 'Z' explícito. */
function parseUtc(iso: string): Date {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

export function humanizeTime(iso: string | null): string {
  if (!iso) return '';

  const created = parseUtc(iso);
  if (Number.isNaN(created.getTime())) return '';

  const diffMs = Date.now() - created.getTime();
  const minutes = Math.floor(diffMs / 60000);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  // Relógios podem divergir alguns segundos entre o core e a máquina local;
  // sem isto, uma sessão recém-criada apareceria como "em 1 minuto".
  if (minutes < 1) return 'agora mesmo';
  if (minutes === 1) return 'há 1 minuto';
  if (minutes < 60) return `há ${minutes} minutos`;
  if (hours === 1) return 'há 1 hora';
  if (hours < 24) return `há ${hours} horas`;
  if (days === 1) return 'ontem';
  if (days < 7) return `há ${days} dias`;

  return created.toLocaleDateString('pt-BR');
}

export function useSessionManagement() {
  const [sessions, setSessions] = useState<SessionWithHumanizedTime[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  // Distingue "não tem conversa nenhuma" de "não consegui falar com o core" —
  // as duas viravam a mesma lista vazia e escondiam a falha real.
  const [error, setError] = useState<string | null>(null);
  const [currentSessionId, setCurrentSessionId] = useState<string>('');

  const loadSessions = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [list, current] = await Promise.all([
        window.electronAPI?.listSessions(),
        window.electronAPI?.getSessionId().catch(() => ''),
      ]);
      setCurrentSessionId(current || '');
      setSessions(
        (list ?? []).map((session) => ({
          ...session,
          humanizedTime: humanizeTime(session.last_activity_at),
        }))
      );
    } catch (err) {
      console.error('[Neo] Falha ao carregar sessões:', err);
      setError(String(err));
      setSessions([]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  return { sessions, isLoading, error, currentSessionId, loadSessions };
}
