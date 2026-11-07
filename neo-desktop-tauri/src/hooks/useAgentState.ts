import { useCallback, useState } from 'react';
import type { WindowMode, ChatMode } from '../types';
import {
  readStoredChatMode,
  readStoredModel,
  readStoredProvider,
  storeChatMode,
  storeModel,
  storeProvider,
} from '../lib/chatPrefs';
import { defaultModelFor, resolveSelection } from '../lib/models';
import { useModelCatalog } from './useModelCatalog';

export const useAgentState = () => {
  const [isLoading, setIsLoading] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [currentIteration, setCurrentIteration] = useState(0);
  const [maxIterations, setMaxIterations] = useState(10);
  const [windowMode, setWindowMode] = useState<WindowMode>('tooltip');
  // Provider/modelo/modo vêm do localStorage (lazy initializer: lê uma vez, no
  // mount) e voltam pra lá a cada troca — antes reiniciar o app jogava a
  // escolha fora.
  const [selectedProvider, setSelectedProviderState] = useState<string>(readStoredProvider);
  const [selectedModel, setSelectedModelState] = useState<string>(readStoredModel);
  // A lista do seletor vem do neo-core (cache primeiro, ver useModelCatalog).
  const modelCatalog = useModelCatalog();
  const [chatMode, setChatModeState] = useState<ChatMode>(readStoredChatMode);
  const [startupNotification, setStartupNotification] = useState<string | null>(null);

  const setSelectedModel = useCallback((model: string) => {
    setSelectedModelState(model);
    storeModel(model);
  }, []);

  // Trocar de provider troca o modelo junto (o Chat já fazia isso chamando
  // onModelChange logo depois). Fazemos aqui também para que o par salvo nunca
  // fique inconsistente caso alguém mude só o provider.
  const setSelectedProvider = useCallback((provider: string) => {
    const model = modelCatalog ? defaultModelFor(modelCatalog, provider) : '';
    setSelectedProviderState(provider);
    setSelectedModelState(model);
    storeProvider(provider);
    storeModel(model);
  }, [modelCatalog]);

  // O par salvo é corrigido contra o catálogo na LEITURA, não regravado: provider
  // que saiu da lista (desligado no painel, chave removida) ou modelo que não é
  // mais dele vira o default enquanto estiver fora — e a escolha salva volta
  // sozinha se ele voltar. Sem catálogo (boot sem rede e sem cache) vale o salvo.
  const effective = modelCatalog
    ? resolveSelection(modelCatalog, { provider: selectedProvider, model: selectedModel })
    : { provider: selectedProvider, model: selectedModel };

  const setChatMode = useCallback((mode: ChatMode) => {
    setChatModeState(mode);
    storeChatMode(mode);
  }, []);

  return {
    isLoading,
    setIsLoading,
    isExecuting,
    setIsExecuting,
    currentIteration,
    setCurrentIteration,
    maxIterations,
    setMaxIterations,
    windowMode,
    setWindowMode,
    selectedModel: effective.model,
    setSelectedModel,
    selectedProvider: effective.provider,
    setSelectedProvider,
    modelCatalog,
    chatMode,
    setChatMode,
    startupNotification,
    setStartupNotification
  };
};
