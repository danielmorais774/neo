// Quem está em ação na conversa aberta (painel "Agentes em ação", F4 do plano
// desktop-chat-tres-colunas). Junta a frase de progresso do turno com as tarefas
// em background da conversa — ver lib/agentActivity.ts para o porquê e o limite.
//
// Escuta `onAgentEvent` por conta própria: o barramento do tauriApi aceita vários
// ouvintes, e o handler do useAgentEvents (o chat) fica intocado.

import { useEffect, useMemo, useState } from 'react';
import { AGENT_FOR_JOB_KIND, AGENT_NAME, agentForPhrase, type AgentId } from '../lib/agentActivity';
import { useActiveSession } from '../lib/activeSession';
import { useJobs } from './useJobs';

export type AgentActivityState = 'busy' | 'queued' | 'waiting';

export interface AgentActivity {
  key: string;
  agent: AgentId;
  name: string;
  /** O passo de agora: a frase do turno ou a linha de `preview` da tarefa. */
  step: string;
  state: AgentActivityState;
}

interface TurnStep {
  agent: AgentId;
  step: string;
}

export function useAgentActivity(): AgentActivity[] {
  const [turn, setTurn] = useState<TurnStep | null>(null);
  const currentSessionId = useActiveSession();
  const { jobs } = useJobs();

  useEffect(() => {
    const clear = () => setTurn(null);
    const stop = window.electronAPI?.onAgentEvent((event) => {
      if (event.type === 'thinking') {
        const content = String(event.data?.content ?? '').trim();
        const agent = agentForPhrase(content);
        // Frase que não casa (o modelo pensando, um "Processando…") não muda quem
        // está em ação: o passo anterior ainda é o último trabalho de verdade.
        if (agent) setTurn({ agent, step: content });
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

  return useMemo(() => {
    const byAgent = new Map<AgentId, AgentActivity>();
    if (turn) {
      byAgent.set(turn.agent, {
        key: `turn:${turn.agent}`,
        agent: turn.agent,
        name: AGENT_NAME[turn.agent],
        step: turn.step,
        state: 'busy',
      });
    }
    // Tarefa da conversa em andamento: o agente dela fica em ação até ela acabar.
    // A tarefa vence a frase do turno (é o trabalho que continua depois dele), e a
    // primeira da lista vence as outras do mesmo agente (o core manda ativas primeiro).
    for (const job of jobs) {
      if (!currentSessionId || job.session_id !== currentSessionId) continue;
      if (job.status !== 'running' && job.status !== 'queued' && job.status !== 'waiting') continue;
      const agent = AGENT_FOR_JOB_KIND[job.kind];
      if (!agent) continue;
      const existing = byAgent.get(agent);
      if (existing && existing.key.startsWith('job:')) continue;
      byAgent.set(agent, {
        key: `job:${job.id}`,
        agent,
        name: AGENT_NAME[agent],
        step: job.preview || job.title,
        state: job.status === 'waiting' ? 'waiting' : job.status === 'queued' ? 'queued' : 'busy',
      });
    }
    return [...byAgent.values()];
  }, [turn, jobs, currentSessionId]);
}
