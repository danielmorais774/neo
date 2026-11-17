import { useEffect } from 'react';

interface UseAppStartupProps {
  setWindowMode: (mode: 'tooltip' | 'agent') => void;
  setStartupNotification: (msg: string | null) => void;
  loadHistory: () => Promise<void>;
}

export const useAppStartup = ({
  setWindowMode,
  setStartupNotification,
  loadHistory
}: UseAppStartupProps) => {
  useEffect(() => {
    // Carregar histórico
    loadHistory();
    
    // Obter modo da janela
    window.electronAPI?.getWindowMode().then(setWindowMode);

    // Listener para evento de startup
    window.electronAPI?.onAppStarted((payload: any) => {
      const msg = payload?.message || 'Neo Desktop iniciado';
      setStartupNotification(msg);
      console.log('[useAppStartup] app-started:', payload);
    });
  }, [setWindowMode, setStartupNotification, loadHistory]);
};
