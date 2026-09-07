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
  status: 'pending' | 'approved' | 'rejected' | 'executed';
  output?: string;
  requestId?: string;
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

export type MessageType = 'text' | 'thinking' | 'plan' | 'plan_update' | 'step' | 'command' | 'mission' | 'result' | 'error' | 'iteration';

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
}
