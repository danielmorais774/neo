// Tipos para eventos e estado do agente

export interface AgentEvent {
  type: string;
  data: any;
}

export interface AgentState {
  isExecuting: boolean;
  currentIteration: number;
  maxIterations: number;
}

export type StepStatus = 'pending' | 'running' | 'success' | 'error' | 'skipped';
export type ExecutionStatus = 'idle' | 'planning' | 'executing' | 'waiting_permission' | 'completed' | 'stopped' | 'error';

// Callbacks para streaming de eventos do agente
export interface AgentCallbacks {
  onThinking: (thinking: any) => void;
  onPlanCreated: (plan: any) => void;
  onStepStart: (step: any) => void;
  onStepComplete: (step: any) => void;
  onCommandRequest: (command: any) => Promise<boolean>;
  onComplete: (result: string) => void;
  onError: (error: string) => void;
  onStopped: () => void;
}

// Configuração do agente
export interface AgentConfig {
  maxIterations: number;
  autoRetry: boolean;
  maxRetries: number;
  requirePermission: boolean;
}

