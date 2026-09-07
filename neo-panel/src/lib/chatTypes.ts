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

export type ProviderType =
  | 'openai' | 'groq' | 'grok' | 'deepseek' | 'anthropic' | 'gemini' | 'ollama' | 'openrouter';

export const PROVIDER_OPTIONS: { value: ProviderType; label: string }[] = [
  { value: 'openai', label: 'OpenAI — GPT-4o, GPT-4.1...' },
  { value: 'groq', label: 'Groq — Llama-3.3, ultra-rápido' },
  { value: 'grok', label: 'Grok (xAI) — Grok-3-mini, Grok-2...' },
  { value: 'deepseek', label: 'DeepSeek — V4 Flash, V4 Pro...' },
  { value: 'anthropic', label: 'Anthropic — Claude 3...' },
  { value: 'gemini', label: 'Gemini — Gemini 2.5 Pro...' },
  { value: 'ollama', label: 'Ollama — Local (Gemma4...)' },
  { value: 'openrouter', label: 'OpenRouter — Grok 4.5, DeepSeek V4 Flash...' },
];

export const MODELS_BY_PROVIDER: Record<ProviderType, { value: string; label: string }[]> = {
  openai: [
    { value: 'gpt-4.1', label: 'GPT-4.1 — mais capaz' },
    { value: 'gpt-4.1-mini', label: 'GPT-4.1 Mini — rápido' },
    { value: 'gpt-4.1-nano', label: 'GPT-4.1 Nano — mais rápido' },
    { value: 'gpt-4o', label: 'GPT-4o — equilíbrio' },
    { value: 'gpt-4o-mini', label: 'GPT-4o-mini — econômico' },
  ],
  groq: [
    { value: 'llama-3.3-70b-versatile', label: 'Llama-3.3 70B — padrão (free)' },
    { value: 'llama-3.1-8b-instant', label: 'Llama-3.1 8B Fast — ultra-rápido (free)' },
    { value: 'llama3-70b-8192', label: 'Llama3 70B 8k — contexto longo (free)' },
    { value: 'mixtral-8x7b-32768', label: 'Mixtral 8x7B — 32k tokens (free)' },
    { value: 'gemma2-9b-it', label: 'Gemma2 9B — Google (free)' },
  ],
  grok: [
    { value: 'grok-3-mini-fast', label: 'Grok-3 Mini Fast — mais rápido (free)' },
    { value: 'grok-3-mini', label: 'Grok-3 Mini — rápido (free)' },
    { value: 'grok-2-1212', label: 'Grok-2 — completo (free)' },
    { value: 'grok-3', label: 'Grok-3 — mais capaz (pago)' },
  ],
  deepseek: [
    { value: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash — mais rápido' },
    { value: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro — mais capaz' },
    { value: 'deepseek-chat', label: 'DeepSeek Chat — uso geral' },
    { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner — raciocínio lento' },
  ],
  anthropic: [
    { value: 'claude-3-5-sonnet-20241022', label: 'Claude 3.5 Sonnet — mais capaz' },
    { value: 'claude-3-haiku-20240307', label: 'Claude 3 Haiku — rápido' },
  ],
  gemini: [
    { value: 'gemini-2.5-pro-preview-03-25', label: 'Gemini 2.5 Pro — avançado' },
    { value: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash — rápido' },
    { value: 'gemini-2.0-flash-lite', label: 'Gemini Flash Lite — leve' },
  ],
  ollama: [
    { value: 'gemma4:e2b', label: 'Gemma4 2B (Local) — ultra-leve' },
    { value: 'gemma4:12b', label: 'Gemma4 12B (Local)' },
    { value: 'phi4', label: 'Phi-4 (Local)' },
  ],
  openrouter: [
    { value: 'x-ai/grok-4.5', label: 'Grok 4.5 — mais capaz (raciocínio)' },
    { value: 'x-ai/grok-4.20', label: 'Grok 4.20 — equilíbrio, 2M contexto' },
    { value: 'google/gemini-3.1-flash-lite', label: 'Gemini 3.1 Flash Lite — econômico' },
    // O sufixo `@baidu` fixa o provedor de infra do gateway (o neo-core o
    // consome e não manda para a API) — sem ele o mesmo modelo sai por outro
    // provedor, com outro preço e outro teto de saída.
    { value: 'deepseek/deepseek-v4-flash@baidu', label: 'DeepSeek V4 Flash (Baidu) — 1M contexto, barato' },
  ],
};

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
