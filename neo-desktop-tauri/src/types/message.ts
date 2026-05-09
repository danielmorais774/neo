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
  status: 'pending' | 'approved' | 'rejected' | 'cancelled';
}

export interface MessageDetails {
  durationMs?: number;
  eventType?: string;
  [key: string]: any;
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
}
