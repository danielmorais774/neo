// Preferências do seletor do chat (provider / modelo / modo), persistidas por
// origem no localStorage — mesmo mecanismo do tema (themes/themeStorage) e do
// device de áudio (lib/audioDevices).
//
// Antes disto o estado nascia hardcoded em useAgentState ('openai'/'gpt-4.1'/
// 'agent') a cada boot: escolher um modelo valia só até fechar o app.
//
// Provider e modelo são lidos CRUS: o catálogo vem do neo-core e pode ainda não
// ter chegado no boot. Quem valida é o useAgentState, quando há catálogo
// (lib/models.resolveSelection) — validar contra lista nenhuma jogaria fora a
// escolha do usuário a cada abertura sem rede.

import type { ChatMode } from '../types';

const PROVIDER_KEY = 'neo.chat.provider';
const MODEL_KEY = 'neo.chat.model';
const MODE_KEY = 'neo.chat.mode';

const CHAT_MODES: ChatMode[] = ['agent', 'plan', 'ask'];

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // localStorage pode lançar (janela sem storage, storage bloqueado).
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* preferência é conveniência: falhar em salvar não pode derrubar o app */
  }
}

function remove(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* idem */
  }
}

export function readStoredProvider(): string {
  return read(PROVIDER_KEY) ?? '';
}

export function storeProvider(provider: string): void {
  write(PROVIDER_KEY, provider);
}

export function readStoredModel(): string {
  return read(MODEL_KEY) ?? '';
}

export function storeModel(model: string): void {
  write(MODEL_KEY, model);
}

export function readStoredChatMode(): ChatMode {
  const stored = read(MODE_KEY);
  return CHAT_MODES.includes(stored as ChatMode) ? (stored as ChatMode) : 'agent';
}

export function storeChatMode(mode: ChatMode): void {
  write(MODE_KEY, mode);
}

// Faixa do Claude (ClaudeBar.tsx): a caixa do diário aberta ou fechada fica
// como a pessoa deixou, entre uma tarefa e outra.
const CLAUDE_BOX_KEY = 'neo.chat.claudeBox';

export function readClaudeBoxOpen(): boolean {
  return read(CLAUDE_BOX_KEY) === 'open';
}

export function storeClaudeBoxOpen(open: boolean): void {
  write(CLAUDE_BOX_KEY, open ? 'open' : 'closed');
}

// Colunas do chat largo (App.tsx): cada uma fica aberta ou fechada como a pessoa
// deixou, entre uma abertura do app e outra. Nascem abertas. A direita só guarda
// a escolha da janela LARGA; na média ela abre por cima e fecha sozinha.
export type ChatColumn = 'left' | 'right';

const COLUMN_KEYS: Record<ChatColumn, string> = {
  left: 'neo.chat.columnLeft',
  right: 'neo.chat.columnRight',
};

export function readColumnOpen(column: ChatColumn): boolean {
  return read(COLUMN_KEYS[column]) !== 'closed';
}

export function storeColumnOpen(column: ChatColumn, open: boolean): void {
  write(COLUMN_KEYS[column], open ? 'open' : 'closed');
}

// Tamanhos arrastáveis do chat largo (ResizeHandle.tsx): largura de cada coluna e
// altura da seção "Agentes e canais". Sem valor salvo (ou depois do duplo clique
// na barra) vale o padrão da largura da janela.
const PANE_SIZE_KEYS = {
  left: 'neo.chat.columnLeftWidth',
  right: 'neo.chat.columnRightWidth',
  sessionPanel: 'neo.chat.sessionPanelHeight',
} as const;

export type ChatPane = keyof typeof PANE_SIZE_KEYS;

export function readPaneSize(pane: ChatPane): number | null {
  const stored = Number(read(PANE_SIZE_KEYS[pane]) ?? Number.NaN);
  return Number.isFinite(stored) && stored > 0 ? stored : null;
}

export function storePaneSize(pane: ChatPane, px: number | null): void {
  if (px === null) remove(PANE_SIZE_KEYS[pane]);
  else write(PANE_SIZE_KEYS[pane], String(Math.round(px)));
}

// Filtro da coluna "Segundo plano" (JobsColumn.tsx).
export type JobsColumnFilter = 'open' | 'session' | 'all';

const JOBS_FILTER_KEY = 'neo.chat.jobsFilter';
const JOBS_FILTERS: JobsColumnFilter[] = ['open', 'session', 'all'];

export function readJobsColumnFilter(): JobsColumnFilter {
  const stored = read(JOBS_FILTER_KEY);
  return JOBS_FILTERS.includes(stored as JobsColumnFilter) ? (stored as JobsColumnFilter) : 'open';
}

export function storeJobsColumnFilter(filter: JobsColumnFilter): void {
  write(JOBS_FILTER_KEY, filter);
}
