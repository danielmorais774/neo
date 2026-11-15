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
