import { useState } from 'react';
import type { WindowMode, ChatMode } from '../types';

export const useAgentState = () => {
  const [isLoading, setIsLoading] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [currentIteration, setCurrentIteration] = useState(0);
  const [maxIterations, setMaxIterations] = useState(10);
  const [windowMode, setWindowMode] = useState<WindowMode>('tooltip');
  const [selectedModel, setSelectedModel] = useState('llama-3.3-70b-versatile');
  const [selectedProvider, setSelectedProvider] = useState('groq');
  const [chatMode, setChatMode] = useState<ChatMode>('agent');
  const [startupNotification, setStartupNotification] = useState<string | null>(null);

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
