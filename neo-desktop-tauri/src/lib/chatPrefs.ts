// Preferências do seletor do chat (provider / modelo / modo), persistidas por
// origem no localStorage — mesmo mecanismo do tema (themes/themeStorage) e do
// device de áudio (lib/audioDevices).
//
// Antes disto o estado nascia hardcoded em useAgentState ('openai'/'gpt-4.1'/
// 'agent') a cada boot: escolher um modelo valia só até fechar o app.
//
// Toda leitura é VALIDADA contra o catálogo (lib/models). Um provider que sumiu
// da lista, ou um modelo que não pertence mais ao provider salvo, cai no
// default — senão o GlassSelect mostraria o primeiro item da lista enquanto o
// estado (e o que vai pro neo-core) apontava pra outro.

import type { ChatMode } from '../types';
import type { ModelType, ProviderType } from './models';
import { DEFAULT_PROVIDER, defaultModelFor, isModelOfProvider, isProvider } from './models';

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

export function readStoredProvider(): ProviderType {
  const stored = read(PROVIDER_KEY);
  return isProvider(stored) ? stored : DEFAULT_PROVIDER;
}

export function storeProvider(provider: string): void {
  write(PROVIDER_KEY, provider);
}

/** O modelo só é aceito se ainda pertencer ao provider passado. */
export function readStoredModel(provider: string): ModelType {
  const stored = read(MODEL_KEY);
  return isModelOfProvider(provider, stored) ? stored! : defaultModelFor(provider);
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
