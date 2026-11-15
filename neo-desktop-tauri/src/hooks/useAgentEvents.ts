import { useEffect } from 'react';
import type { Message, AgentEvent, ExecutionPlan, FileChangeInfo } from '../types';
import { newMessageId } from '../lib/messageId';

interface UseAgentEventsProps {
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  setIsExecuting: (value: boolean) => void;
  setCurrentIteration: (value: number) => void;
  setMaxIterations: (value: number) => void;
  /** O core rotacionou a sessão: quem mostra algo da conversa precisa saber. */
  setSessionId: (sessionId: string) => void;
}

export const useAgentEvents = ({
  setMessages,
  setIsExecuting,
  setCurrentIteration,
  setMaxIterations,
  setSessionId
}: UseAgentEventsProps) => {
  useEffect(() => {
    const handleAgentEvent = (event: AgentEvent) => {
      console.log('[useAgentEvents] Agent event:', event.type, event.data);
      
      switch (event.type) {
        case 'session_changed':
          if (event.data.sessionId) {
            void window.electronAPI?.resetAgent(event.data.sessionId);
            setSessionId(event.data.sessionId);
          }
          break;

        case 'thinking':
          // Remover thinking anterior e adicionar novo (mantém apenas o mais recente)
          setMessages(prev => {
            const filtered = prev.filter(m => m.messageType !== 'thinking');
            return [...filtered, {
              id: newMessageId(),
              role: 'assistant',
              content: event.data.content,
              messageType: 'thinking',
              thinking: event.data,
              timestamp: new Date()
            }];
          });
          break;

        case 'assistant_message':
          // Persistent reply that precedes a client actuation (e.g. the witty answer
          // before a music play). Keep it as its own chat bubble — the "🎵 …"
          // confirmation arrives later as a separate message. Drop any thinking bubble.
          setMessages(prev => {
            const filtered = prev.filter(m => m.messageType !== 'thinking');
            return [...filtered, {
              id: newMessageId(),
              role: 'assistant',
              content: event.data.content,
              messageType: 'text',
              timestamp: new Date(),
              files: event.data.files,
              jobs: event.data.jobs ?? undefined,
            }];
          });
          break;

        case 'plan':
          setMessages(prev => [...prev, {
            id: newMessageId(),
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
              id: newMessageId(),
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
            id: newMessageId(),
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

        case 'file_change': {
          const change = event.data as FileChangeInfo;
          if (!change?.changeId) break;
          const key = change.cardId || change.changeId;
          setMessages(prev => {
            const idx = prev.findIndex(m =>
              m.fileChange &&
              ((m.fileChange.cardId || m.fileChange.changeId) === key ||
                m.fileChange.changeId === change.changeId)
            );
            if (idx >= 0) {
              const updated = [...prev];
              const current = updated[idx].fileChange!;
              // O evento de "desfeito" só traz id/caminho/status: o diff já exibido fica.
              updated[idx] = {
                ...updated[idx],
                fileChange: { ...current, ...change, diff: change.diff ?? current.diff },
              };
              return updated;
            }
            return [...prev, {
              id: newMessageId(),
              role: 'assistant',
              content: '',
              messageType: 'file_change',
              fileChange: change,
              timestamp: new Date(),
            }];
          });
          break;
        }

        case 'command_request':
          setMessages(prev => [...prev, {
            id: newMessageId(),
            role: 'assistant',
            content: `Comando: ${event.data.explanation}`,
            messageType: 'command',
            // requestId carries the command_id back to respond_to_command on approve/reject.
            command: { ...event.data, requestId: event.data.id },
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
                id: newMessageId(),
                role: 'assistant',
                content: '⏹ Execução interrompida pelo usuário',
                messageType: 'text',
                timestamp: new Date(),
                details
              }];
            } else {
              const rawContent = event.data.message || 'Tarefa concluída!';
              // Parse [MISSION:...] marker to attach mission metadata for approval buttons
              const missionMatch = rawContent.match(/\[MISSION:(\{.*\})\]/);
              const cleanContent = rawContent.replace(/\[MISSION:\{.*\}\]/, '').trim();
              let missionPayload: any = null;
              if (missionMatch?.[1]) {
                try {
                  missionPayload = JSON.parse(missionMatch[1]);
                } catch (error) {
                  console.error('[useAgentEvents] Failed to parse mission marker:', error, missionMatch[1]);
                }
              }
              const parsedMission = missionPayload ? {
                missionId: missionPayload.missionId,
                contactLabel: missionPayload.contactLabel,
                contactJid: missionPayload.contactJid,
                contactPhone: missionPayload.contactPhone,
                draftText: missionPayload.draftText,
                draftMessages: Array.isArray(missionPayload.draftMessages) ? missionPayload.draftMessages : undefined,
                status: 'pending' as const,
              } : undefined;
              const msgType = missionMatch ? 'mission' : (event.data.success ? 'result' : 'error');
              return [...filtered, {
                id: newMessageId(),
                role: 'assistant',
                content: cleanContent || rawContent,
                messageType: msgType,
                timestamp: new Date(),
                details,
                files: event.data.files,
                // O Rust manda `null` quando a resposta não abriu tarefa.
                jobs: event.data.jobs ?? undefined,
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
              id: newMessageId(),
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
  }, [setMessages, setIsExecuting, setCurrentIteration, setMaxIterations, setSessionId]);
};
