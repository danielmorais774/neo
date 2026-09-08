// Catálogo de providers/modelos oferecidos no seletor do chat.
//
// Vive fora do Chat.tsx porque a preferência do usuário é PERSISTIDA
// (lib/chatPrefs): ao subir o app precisamos validar o que veio do
// localStorage contra este mesmo catálogo — um modelo que saiu da lista não
// pode voltar como valor selecionado (o GlassSelect exibiria o primeiro item
// e o estado ficaria apontando pra outro).

export type ProviderType =
  | 'openai'
  | 'groq'
  | 'grok'
  | 'deepseek'
  | 'anthropic'
  | 'gemini'
  | 'ollama'
  | 'openrouter';

export type ModelType = string;

export interface CatalogOption<T extends string> {
  value: T;
  label: string;
  description: string;
}

export const PROVIDER_OPTIONS: CatalogOption<ProviderType>[] = [
  { value: 'openai',    label: 'OpenAI',       description: 'GPT-4o, GPT-4.1...' },
  { value: 'groq',      label: 'Groq',         description: 'Llama-3.3, ultra-rápido' },
  { value: 'grok',      label: 'Grok (xAI)',   description: 'Grok-3-mini, Grok-2...' },
  { value: 'deepseek',  label: 'DeepSeek',     description: 'V4 Flash, V4 Pro...' },
  { value: 'anthropic', label: 'Anthropic',    description: 'Claude 3...' },
  { value: 'gemini',    label: 'Gemini',       description: 'Gemini 2.5 Pro...' },
  { value: 'ollama',    label: 'Ollama',       description: 'Local (Gemma4...)' },
  { value: 'openrouter', label: 'OpenRouter',  description: 'Grok 4.5, DeepSeek V4 Flash...' },
];

export const MODELS_BY_PROVIDER: Record<ProviderType, CatalogOption<ModelType>[]> = {
  openai: [
    { value: 'gpt-4.1',       label: 'GPT-4.1',        description: 'Mais capaz' },
    { value: 'gpt-4.1-mini',  label: 'GPT-4.1 Mini',   description: 'Rápido' },
    { value: 'gpt-4.1-nano',  label: 'GPT-4.1 Nano',   description: 'Mais rápido' },
    { value: 'gpt-4o',        label: 'GPT-4o',          description: 'Equilíbrio' },
    { value: 'gpt-4o-mini',   label: 'GPT-4o-mini',     description: 'Econômico' },
  ],
  groq: [
    { value: 'llama-3.3-70b-versatile', label: 'Llama-3.3 70B',     description: 'Padrão (free)' },
    { value: 'llama-3.1-8b-instant',    label: 'Llama-3.1 8B Fast', description: 'Ultra-rápido (free)' },
    { value: 'llama3-70b-8192',         label: 'Llama3 70B 8k',     description: 'Contexto longo (free)' },
    { value: 'mixtral-8x7b-32768',      label: 'Mixtral 8x7B',      description: '32k tokens (free)' },
    { value: 'gemma2-9b-it',            label: 'Gemma2 9B',         description: 'Google (free)' },
  ],
  grok: [
    { value: 'grok-3-mini-fast', label: 'Grok-3 Mini Fast', description: 'Mais rápido (free)' },
    { value: 'grok-3-mini',      label: 'Grok-3 Mini',      description: 'Rápido (free)' },
    { value: 'grok-2-1212',      label: 'Grok-2',           description: 'Completo (free)' },
    { value: 'grok-3',           label: 'Grok-3',           description: 'Mais capaz (pago)' },
  ],
  deepseek: [
    { value: 'deepseek-v4-flash', label: 'DeepSeek V4 Flash', description: 'Mais rápido' },
    { value: 'deepseek-v4-pro',   label: 'DeepSeek V4 Pro',   description: 'Mais capaz' },
    { value: 'deepseek-chat',     label: 'DeepSeek Chat',     description: 'Uso geral' },
    { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner', description: 'Raciocínio lento' },
  ],
  anthropic: [
    { value: 'claude-3-5-sonnet-20241022', label: 'Claude 3.5 Sonnet', description: 'Mais capaz' },
    { value: 'claude-3-haiku-20240307',    label: 'Claude 3 Haiku',   description: 'Rápido' },
  ],
  gemini: [
    { value: 'gemini-2.5-pro-preview-03-25', label: 'Gemini 2.5 Pro',     description: 'Avançado' },
    { value: 'gemini-2.0-flash',             label: 'Gemini 2.0 Flash',   description: 'Rápido' },
    { value: 'gemini-2.0-flash-lite',        label: 'Gemini Flash Lite',  description: 'Leve' },
  ],
  ollama: [
    { value: 'gemma4:e2b', label: 'Gemma4 2B (Local)', description: 'Ultra-leve' },
    { value: 'gemma4:12b', label: 'Gemma4 12B (Local)', description: 'Local' },
    { value: 'phi4',       label: 'Phi-4 (Local)',      description: 'Local' },
  ],
  openrouter: [
    { value: 'x-ai/grok-4.5',                label: 'Grok 4.5',      description: 'Mais capaz (raciocínio)' },
    { value: 'x-ai/grok-4.20',               label: 'Grok 4.20',     description: 'Equilíbrio, 2M contexto' },
    { value: 'google/gemini-3.1-flash-lite', label: 'Gemini 3.1 Flash Lite', description: 'Econômico' },
    // O sufixo `@baidu` fixa o provedor de infra do gateway (o neo-core o
    // consome e não manda para a API) — sem ele o mesmo modelo sai por outro
    // provedor, com outro preço e outro teto de saída.
    //
    // As DUAS entradas existem de propósito, e a escolha é preço x latência:
    // o pin garante 1M de contexto pelo preço sondado do Baidu, mas o pool dele
    // é compartilhado e satura (429 `rate-limited upstream`, medido na
    // session-d94dc42f: 19 chamadas falhas e ~40s de backoff num turno só).
    // Sem o pin o gateway roteia para qualquer endpoint livre — responde na
    // hora, aceita `tool_choice=required` e cobra o que o endpoint da vez cobrar.
    { value: 'deepseek/deepseek-v4-flash@baidu', label: 'DeepSeek V4 Flash (Baidu)', description: '1M contexto, barato — pool pode saturar' },
    { value: 'deepseek/deepseek-v4-flash',       label: 'DeepSeek V4 Flash (auto)',  description: 'Sem pin: qualquer endpoint livre, preço varia' },
  ],
};

export const DEFAULT_PROVIDER: ProviderType = 'openai';

export function isProvider(value: unknown): value is ProviderType {
  return typeof value === 'string' && value in MODELS_BY_PROVIDER;
}

export function modelsFor(provider: string): CatalogOption<ModelType>[] {
  return isProvider(provider)
    ? MODELS_BY_PROVIDER[provider]
    : MODELS_BY_PROVIDER[DEFAULT_PROVIDER];
}

/** Primeiro modelo do provider — o default quando não há preferência válida. */
export function defaultModelFor(provider: string): ModelType {
  return modelsFor(provider)[0].value;
}

export function isModelOfProvider(provider: string, model: unknown): boolean {
  return typeof model === 'string' && modelsFor(provider).some((m) => m.value === model);
}
