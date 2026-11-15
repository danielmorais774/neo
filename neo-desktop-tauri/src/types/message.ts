export interface Thinking {
  id: string;
  type: 'planning' | 'analyzing' | 'deciding' | 'executing' | 'error_handling' | 'success' | 'retry';
  content: string;
  timestamp: Date;
}

export interface PlanStep {
  id: string;
  title: string;
  description: string;
  status: 'pending' | 'running' | 'success' | 'error' | 'skipped' | 'completed' | 'failed';
  tool?: string;
  command?: string;
  output?: string;
  error?: string;
  resultSummary?: string;
}

export interface ExecutionPlan {
  id?: string;
  goal: string;
  steps: PlanStep[];
  reasoning: string;
  status?: 'active' | 'completed' | 'failed';
  parentPlanId?: string;
  subPlans?: ExecutionPlan[];
}

export interface CommandInfo {
  cmd: string;
  explanation: string;
  /** `auto`: a política do cliente liberou sem perguntar (services/command_policy.rs). */
  status: 'pending' | 'approved' | 'rejected' | 'executed' | 'auto';
  output?: string;
  requestId?: string;
  /** Por que rodou sozinho — só vem com `status: 'auto'`. */
  autoReason?: string;
}

export interface MissionInfo {
  missionId: string;
  contactLabel: string;
  contactJid: string;
  contactPhone?: string;
  draftText: string;
  draftMessages?: string[];
  status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  backendStatus?: string;
  nextActionType?: string | null;
  nextActionParams?: Record<string, any>;
  resultSummary?: string | null;
}

export interface MessageDetails {
  durationMs?: number;
  eventType?: string;
  [key: string]: any;
}

// Arquivo que o Neo gerou/catalogou nesta resposta (ex: um .zip via a tool
// file_save) — mesma forma {id, name, sizeBytes} que o neo-panel usa
// (AttachmentRef). Baixado via o comando Tauri download_generated_file, que
// chama GET /api/v1/files/{id}/download no neo-core e salva com diálogo nativo.
export interface GeneratedFile {
  id: string;
  name: string;
  sizeBytes: number;
}

// Alteração (ou pedido de leitura) de arquivo feita no disco do usuário pelo
// desktop — services/file_ops.rs emite `file_change` a cada mudança de estado.
// `cardId` é estável; `changeId` vira o id do backup quando grava (Desfazer usa ele).
export interface FileChangeInfo {
  cardId?: string;
  changeId: string;
  path: string;
  status: 'pending' | 'applied' | 'rejected' | 'failed' | 'undone';
  kind?: 'write' | 'read';
  engine?: string;
  reason?: string | null;
  summary?: string | null;
  created?: boolean;
  linesAdded?: number;
  linesRemoved?: number;
  diff?: string;
}

// Tarefa em segundo plano presa a este balão: a que a resposta abriu (claude_task)
// ou cujo resultado ela entrega. Vem do core (AgentResponse.jobs, histórico e a
// notificação da entrega); o balão mostra o botão do processo (ClaudeNoBalao).
export interface JobRef {
  id: string;
  kind: string;
}

export type MessageType = 'text' | 'thinking' | 'plan' | 'plan_update' | 'step' | 'command' | 'mission' | 'result' | 'error' | 'iteration' | 'file_change';

export interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  messageType: MessageType;
  thinking?: Thinking;
  plan?: ExecutionPlan;
  step?: PlanStep;
  iteration?: { current: number; max: number };
  command?: CommandInfo;
  mission?: MissionInfo;
  details?: MessageDetails;
  files?: GeneratedFile[];
  jobs?: JobRef[];
  fileChange?: FileChangeInfo;
}
