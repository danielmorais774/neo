export interface PlanStepInfo {
  id: string;
  index: number;
  description: string;
  toolName?: string;
  status: string;
  resultSummary?: string;
  error?: string;
}

export interface PlanInfo {
  planId: string;
  goal: string;
  currentStep: number;
  status: string;
  steps: PlanStepInfo[];
}

export interface CommandInfo {
  id: string;
  cmd: string;
  explanation: string;
  isBackground: boolean;
  status: 'pending' | 'approved' | 'rejected';
}

export interface MissionInfo {
  missionId: string;
  contactLabel: string;
  contactJid: string;
  contactPhone?: string;
  draftText: string;
  draftMessages?: string[];
  status: 'pending' | 'approved' | 'rejected' | 'cancelled';
}

export interface AttachmentRef {
  id: string;
  name: string;
  sizeBytes: number;
}

export type MessageType = 'text' | 'thinking' | 'plan_update' | 'command' | 'mission' | 'result' | 'error';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp: Date;
  messageType: MessageType;
  plan?: PlanInfo;
  command?: CommandInfo;
  mission?: MissionInfo;
  /** O que o USUÁRIO anexou nesta mensagem (echo, tag estática). */
  attachments?: AttachmentRef[];
  /** O que o NEO salvou no catálogo neste turno — chip com botão de download. */
  files?: AttachmentRef[];
}

export type ChatMode = 'agent' | 'plan' | 'ask';

// Catálogo do seletor de provider/modelo — vem do neo-core
// (`GET /api/v1/desktop/model-catalog`, editado em Configurações → Providers).
// Antes era um enum copiado do desktop, e os dois já tinham divergido do core.
export interface CatalogModel {
  id: string;
  label: string;
  description: string;
  vision: boolean;
}

export interface CatalogProvider {
  name: string;
  label: string;
  description: string;
  models: CatalogModel[];
}

export interface ModelCatalog {
  providers: CatalogProvider[];
  /** O par que vale quando não há escolha (o DEFAULT_PROVIDER do core). */
  default: { provider: string; model: string } | null;
}

function randomId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

/** Mensagem local, ainda sem passar pelo servidor (ex.: eco do que o usuário mandou). */
export function makeMessage(partial: Omit<ChatMessage, 'id' | 'timestamp'>): ChatMessage {
  return { ...partial, id: randomId(), timestamp: new Date() };
}

/** Extrai o marcador `[MISSION:{...}]` que o agente embute na resposta final
 * quando um rascunho de WhatsApp precisa de aprovação — mesmo parsing do
 * neo-desktop-tauri (useAgentEvents.ts). */
export function parseMissionMarker(rawContent: string): { content: string; mission?: MissionInfo } {
  const match = rawContent.match(/\[MISSION:(\{.*\})\]/);
  const content = rawContent.replace(/\[MISSION:\{.*\}\]/, '').trim();
  if (!match?.[1]) return { content: content || rawContent };
  try {
    const payload = JSON.parse(match[1]);
    return {
      content,
      mission: {
        missionId: payload.missionId,
        contactLabel: payload.contactLabel,
        contactJid: payload.contactJid,
        contactPhone: payload.contactPhone,
        draftText: payload.draftText,
        draftMessages: Array.isArray(payload.draftMessages) ? payload.draftMessages : undefined,
        status: 'pending',
      },
    };
  } catch (err) {
    console.error('[chatTypes] marcador de missão inválido:', err);
    return { content: content || rawContent };
  }
}

/** Mensagens vindas de `GET /desktop/session/{id}/history` já chegam no
 * formato certo (role/content/messageType/timestamp) — só normaliza a data. */
export function normalizeHistoryMessage(raw: any): ChatMessage {
  return {
    id: raw.id ?? randomId(),
    role: raw.role ?? 'assistant',
    content: raw.content ?? '',
    messageType: raw.messageType || 'text',
    timestamp: raw.timestamp ? new Date(raw.timestamp) : new Date(),
    mission: raw.mission,
    files: raw.files,
  };
}
