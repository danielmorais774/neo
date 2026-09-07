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
import { defaultModelFor } from '../lib/models';

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
  const [selectedModel, setSelectedModelState] = useState<string>(() =>
    readStoredModel(readStoredProvider())
  );
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
    const model = defaultModelFor(provider);
    setSelectedProviderState(provider);
    setSelectedModelState(model);
    storeProvider(provider);
    storeModel(model);
  }, []);

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
    selectedModel,
    setSelectedModel,
    selectedProvider,
    setSelectedProvider,
    chatMode,
    setChatMode,
    startupNotification,
    setStartupNotification
  };
};
