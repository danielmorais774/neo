// Agentes e canais da conversa aberta (seção de baixo da coluna esquerda, F4 do
// plano desktop-chat-tres-colunas). Junta a frase de progresso do turno com as
// tarefas em background da conversa — ver lib/agentActivity.ts para o porquê e o
// limite.
//
// Quem está trabalhando vem primeiro. Depois, quem JÁ trabalhou nesta conversa:
// os agentes que o turno usou desde que o app abriu e as tarefas da conversa que
// terminaram. A seção não some quando ninguém está em ação.
//
// Escuta `onAgentEvent` por conta própria: o barramento do tauriApi aceita vários
// ouvintes, e o handler do useAgentEvents (o chat) fica intocado.

import { useEffect, useMemo, useState } from 'react';
import {
  AGENT_FOR_JOB_KIND,
  AGENT_NAME,
  agentForPhrase,
  type AgentId,
  type PhraseMatch,
} from '../lib/agentActivity';
import { getActiveSession, useActiveSession } from '../lib/activeSession';
import { parseJobTime, statusLabel } from '../lib/jobs';
import { useJobs } from './useJobs';

export type AgentActivityState = 'busy' | 'queued' | 'waiting' | 'idle';

export interface AgentActivity {
  key: string;
  agent: AgentId;
  name: string;
  /** Agora: a frase do turno ou o `preview` da tarefa. Parado: o último passo. */
  step: string;
  state: AgentActivityState;
  /** Selo de quem está parado: "feito" (turno) ou o status da tarefa. */
  label?: string;
  /** Quando parou (ms), para ordenar e mostrar "há X". */
  at?: number;
}

const OPEN_JOB = new Set(['running', 'queued', 'waiting']);
const MAX_IDLE = 6;

// Quem o turno usou em cada conversa NESTA abertura do app: o turno não deixa
// rastro estruturado que o desktop consiga reler depois. Em módulo para
// sobreviver a esconder e mostrar a coluna.
const usedBySession = new Map<string, Map<AgentId, { step: string; at: number }>>();

function remember(sessionId: string, match: PhraseMatch): void {
  if (!sessionId) return;
  const used = usedBySession.get(sessionId) ?? new Map();
  used.set(match.agent, { step: match.step, at: Date.now() });
  usedBySession.set(sessionId, used);
}

export function useAgentActivity(): AgentActivity[] {
  const [turn, setTurn] = useState<PhraseMatch | null>(null);
  const currentSessionId = useActiveSession();
  const { jobs } = useJobs();

  useEffect(() => {
    const clear = () => setTurn(null);
    const stop = window.electronAPI?.onAgentEvent((event) => {
      if (event.type === 'thinking') {
        const match = agentForPhrase(String(event.data?.content ?? ''));
        // Frase que não casa (o modelo pensando, um "Processando…") não muda quem
        // está em ação: o passo anterior ainda é o último trabalho de verdade.
        if (!match) return;
        remember(String(event.data?.sessionId || getActiveSession()), match);
        setTurn(match);
      } else if (event.type === 'complete' || event.type === 'error') {
        clear();
      }
    });
    // Trocar de conversa ou abrir uma nova encerra o turno da tela.
    window.addEventListener('neo-new-chat', clear);
    window.addEventListener('neo-load-session', clear);
    return () => {
      stop?.();
      window.removeEventListener('neo-new-chat', clear);
      window.removeEventListener('neo-load-session', clear);
    };
  }, []);

  // `turn` muda junto com cada `remember`, então entra nas dependências por ele.
  return useMemo(() => {
    const active = new Map<AgentId, AgentActivity>();
    if (turn) {
      active.set(turn.agent, {
        key: `turn:${turn.agent}`,
        agent: turn.agent,
        name: AGENT_NAME[turn.agent],
        step: turn.step,
        state: 'busy',
      });
    }
    const here = currentSessionId ? jobs.filter((job) => job.session_id === currentSessionId) : [];
    // Tarefa da conversa em andamento: o agente dela fica em ação até ela acabar.
    // A tarefa vence a frase do turno (é o trabalho que continua depois dele), e a
    // primeira da lista vence as outras do mesmo agente (o core manda ativas primeiro).
    for (const job of here) {
      if (!OPEN_JOB.has(job.status)) continue;
      const agent = AGENT_FOR_JOB_KIND[job.kind];
      if (!agent || active.get(agent)?.key.startsWith('job:')) continue;
      active.set(agent, {
        key: `job:${job.id}`,
        agent,
        name: AGENT_NAME[agent],
        step: job.preview || job.title,
        state: job.status === 'waiting' ? 'waiting' : job.status === 'queued' ? 'queued' : 'busy',
      });
    }

    const idle = new Map<AgentId, AgentActivity>();
    for (const [agent, info] of usedBySession.get(currentSessionId) ?? []) {
      if (active.has(agent)) continue;
      idle.set(agent, {
        key: `used:${agent}`,
        agent,
        name: AGENT_NAME[agent],
        step: info.step,
        state: 'idle',
        label: 'feito',
        at: info.at,
      });
    }
    for (const job of here) {
      if (OPEN_JOB.has(job.status)) continue;
      const agent = AGENT_FOR_JOB_KIND[job.kind];
      if (!agent || active.has(agent)) continue;
      const at = parseJobTime(job.finished_at ?? job.created_at)?.getTime() ?? 0;
      const previous = idle.get(agent);
      if (previous && (previous.at ?? 0) >= at) continue;
      idle.set(agent, {
        key: `done:${job.id}`,
        agent,
        name: AGENT_NAME[agent],
        step: job.title,
        state: 'idle',
        label: statusLabel(job.status),
        at,
      });
    }

    const recent = [...idle.values()].sort((a, b) => (b.at ?? 0) - (a.at ?? 0)).slice(0, MAX_IDLE);
    return [...active.values(), ...recent];
  }, [turn, jobs, currentSessionId]);
}
