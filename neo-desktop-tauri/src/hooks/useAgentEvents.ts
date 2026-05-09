import { useEffect } from 'react';
import type { Message, AgentEvent, ExecutionPlan } from '../types';

interface UseAgentEventsProps {
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  setIsExecuting: (value: boolean) => void;
  setCurrentIteration: (value: number) => void;
  setMaxIterations: (value: number) => void;
}

export const useAgentEvents = ({
  setMessages,
  setIsExecuting,
  setCurrentIteration,
  setMaxIterations
}: UseAgentEventsProps) => {
  useEffect(() => {
    const handleAgentEvent = (event: AgentEvent) => {
      console.log('[useAgentEvents] Agent event:', event.type, event.data);
      
      switch (event.type) {
        case 'thinking':
          // Remover thinking anterior e adicionar novo (mantém apenas o mais recente)
          setMessages(prev => {
            const filtered = prev.filter(m => m.messageType !== 'thinking');
            return [...filtered, {
              id: Date.now().toString(),
              role: 'assistant',
              content: event.data.content,
              messageType: 'thinking',
              thinking: event.data,
              timestamp: new Date()
            }];
          });
          break;

        case 'plan':
          setMessages(prev => [...prev, {
            id: Date.now().toString(),
            role: 'assistant',
            content: `Plano: ${event.data.goal}`,
            messageType: 'plan',
            plan: event.data,
            timestamp: new Date()
          }]);
          break;

        case 'plan_update': {
          const update = event.data;
          setMessages(prev => {
            // If this is a sub-plan, find the parent plan message and nest it
            if (update.parentPlanId) {
              const parentIdx = prev.findIndex(
                m => (m.messageType === 'plan' || m.messageType === 'plan_update') && m.plan?.id === update.parentPlanId
              );
              if (parentIdx >= 0) {
                const updated = [...prev];
                const parentPlan = { ...updated[parentIdx].plan! };
                const subPlans = parentPlan.subPlans ? [...parentPlan.subPlans] : [];
                const existingIdx = subPlans.findIndex(sp => sp.id === update.planId);
                const subPlanData = {
                  id: update.planId,
                  goal: update.goal,
                  steps: (update.steps || []).map((s: any) => ({
                    id: s.id,
                    title: s.description,
                    description: s.description,
                    status: s.status,
                    tool: s.toolName,
                    resultSummary: s.resultSummary,
                    error: s.error,
                  })),
                  reasoning: '',
                  status: update.status,
                  parentPlanId: update.parentPlanId,
                };
                if (existingIdx >= 0) {
                  subPlans[existingIdx] = subPlanData;
                } else {
                  subPlans.push(subPlanData);
                }
                parentPlan.subPlans = subPlans;
                updated[parentIdx] = { ...updated[parentIdx], plan: parentPlan };
                return updated;
              }
            }

            // Find existing plan_update message for this plan or create new
            const existingIdx = prev.findIndex(
              m => (m.messageType === 'plan' || m.messageType === 'plan_update') && m.plan?.id === update.planId
            );
            const planData = {
              id: update.planId,
              goal: update.goal,
              steps: (update.steps || []).map((s: any) => ({
                id: s.id,
                title: s.description,
                description: s.description,
                status: s.status,
                tool: s.toolName,
                resultSummary: s.resultSummary,
                error: s.error,
              })),
              reasoning: '',
              status: update.status,
              parentPlanId: update.parentPlanId || undefined,
              subPlans: undefined as ExecutionPlan[] | undefined,
            };

            if (existingIdx >= 0) {
              const updated = [...prev];
              // Preserve sub-plans from the existing message
              planData.subPlans = updated[existingIdx].plan?.subPlans;
              updated[existingIdx] = {
                ...updated[existingIdx],
                messageType: 'plan_update',
                plan: planData as any,
              };
              return updated;
            }

            return [...prev, {
              id: Date.now().toString(),
              role: 'assistant' as const,
              content: `Plano: ${update.goal}`,
              messageType: 'plan_update' as const,
              plan: planData as any,
              timestamp: new Date(),
            }];
          });
          break;
        }

        case 'step_start':
          setMessages(prev => [...prev, {
            id: Date.now().toString(),
            role: 'assistant',
            content: event.data.title,
            messageType: 'step',
            step: { ...event.data, status: 'running' },
            timestamp: new Date()
          }]);
          break;

        case 'step_complete':
          // Atualizar o step existente ou adicionar novo
          setMessages(prev => {
            let lastStepIdx = -1;
            for (let i = prev.length - 1; i >= 0; i--) {
              if (prev[i].messageType === 'step' && prev[i].step?.id === event.data.id) {
                lastStepIdx = i;
                break;
              }
            }
            if (lastStepIdx >= 0) {
              const updated = [...prev];
              updated[lastStepIdx] = {
                ...updated[lastStepIdx],
                step: event.data
              };
              return updated;
            }
            return prev;
          });
          break;

        case 'iteration':
          setCurrentIteration(event.data.current);
          setMaxIterations(event.data.max);
          break;

        case 'command_request':
          setMessages(prev => [...prev, {
            id: Date.now().toString(),
            role: 'assistant',
            content: `Comando: ${event.data.explanation}`,
            messageType: 'command',
            command: event.data,
            timestamp: new Date()
          }]);
          break;

        case 'complete':
          setIsExecuting(false);
          setCurrentIteration(0);
          // Remover mensagens de thinking ao completar
          setMessages(prev => {
            const filtered = prev.filter(m => m.messageType !== 'thinking');
            const details = {
              eventType: 'complete',
              durationMs: event.data.durationMs,
              processInfo: event.data.processInfo,
            };
            if (event.data.stopped) {
              return [...filtered, {
                id: Date.now().toString(),
                role: 'assistant',
                content: '⏹ Execução interrompida pelo usuário',
                messageType: 'text',
                timestamp: new Date(),
                details
              }];
            } else {
              const rawContent = event.data.message || 'Tarefa concluída!';
              // Parse [MISSION:...] marker to attach mission metadata for approval buttons
              const missionMatch = rawContent.match(/\[MISSION:([^\]]+)\]/);
              const cleanContent = rawContent.replace(/\[MISSION:[^\]]+\]/, '').trim();
              const parsedMission = missionMatch ? {
                missionId: JSON.parse(missionMatch[1]).missionId,
                contactLabel: JSON.parse(missionMatch[1]).contactLabel,
                contactJid: JSON.parse(missionMatch[1]).contactJid,
                contactPhone: JSON.parse(missionMatch[1]).contactPhone,
                draftText: JSON.parse(missionMatch[1]).draftText,
                status: 'pending' as const,
              } : undefined;
              const msgType = missionMatch ? 'mission' : (event.data.success ? 'result' : 'error');
              return [...filtered, {
                id: Date.now().toString(),
                role: 'assistant',
                content: cleanContent || rawContent,
                messageType: msgType,
                timestamp: new Date(),
                details,
                ...(parsedMission ? { mission: parsedMission } : {}),
              }];
            }
          });
          break;

        case 'error':
          setIsExecuting(false);
          // Remover mensagens de thinking ao dar erro
          setMessages(prev => {
            const filtered = prev.filter(m => m.messageType !== 'thinking');
            return [...filtered, {
              id: Date.now().toString(),
              role: 'assistant',
              content: `❌ Erro: ${event.data.message}`,
              messageType: 'error',
              timestamp: new Date(),
              details: {
                eventType: 'error',
                durationMs: event.data.durationMs,
                error: event.data.message
              }
            }];
          });
          break;
      }
    };

    const unlistenAgent = window.electronAPI?.onAgentEvent(handleAgentEvent);
    const unlistenCommand = window.electronAPI?.onCommandRequest(() => {
      // Já tratado via onAgentEvent
    });

    return () => {
      unlistenAgent?.();
      unlistenCommand?.();
    };
  }, [setMessages, setIsExecuting, setCurrentIteration, setMaxIterations]);
};
