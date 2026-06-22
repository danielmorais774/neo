// useWakeWord — Estágio 1 do Modo Live: escuta a palavra-gatilho local.
//
// Liga/desliga o sidecar de wake word (que roda LOCAL no Mac, offline, via
// openWakeWord) e, quando o backend emite `wake-detected`, chama `onWake` —
// onde o ChatPage acende o Modo Live (Estágio 2, useLiveMode). O reconhecimento
// é acústico (não transcreve): o áudio só vai pro neo-voice DEPOIS de acordar.
//
// O sidecar já aplica cooldown; aqui há um guard extra em JS para não disparar
// dois turnos por um único "Ei Neo".

import { useCallback, useEffect, useRef, useState } from 'react';
import type { WakeDetected } from '../lib/tauriApi';

const WAKE_ENABLED_KEY = 'neo.wake.enabled';
const WAKE_ENABLED_EVENT = 'neo-wake-enabled-changed';

/**
 * Flag persistida (localStorage) que liga/desliga o wake word, compartilhada
 * entre o SettingsModal (onde o usuário ativa) e o ChatPage (que dirige o
 * sidecar). Sincroniza entre componentes via CustomEvent — sem prop drilling.
 */
export function useWakeEnabled(): [boolean, (next: boolean) => void] {
  const [enabled, setEnabledState] = useState<boolean>(
    () => localStorage.getItem(WAKE_ENABLED_KEY) === '1',
  );

  useEffect(() => {
    const sync = () => setEnabledState(localStorage.getItem(WAKE_ENABLED_KEY) === '1');
    window.addEventListener(WAKE_ENABLED_EVENT, sync);
    window.addEventListener('storage', sync); // outra janela/aba
    return () => {
      window.removeEventListener(WAKE_ENABLED_EVENT, sync);
      window.removeEventListener('storage', sync);
    };
  }, []);

  const setEnabled = useCallback((next: boolean) => {
    localStorage.setItem(WAKE_ENABLED_KEY, next ? '1' : '0');
    setEnabledState(next);
    window.dispatchEvent(new Event(WAKE_ENABLED_EVENT));
  }, []);

  return [enabled, setEnabled];
}

interface UseWakeWordArgs {
  /** Liga o detector (sobe o sidecar). Persistência fica a cargo do chamador. */
  enabled: boolean;
  /** Chamado quando a palavra-gatilho é reconhecida. */
  onWake: (detected: WakeDetected) => void;
  /** Janela mínima entre dois disparos aceitos, em ms (default 2500). */
  cooldownMs?: number;
}

interface UseWakeWordResult {
  /** Última detecção (para feedback de UI). */
  lastDetected: WakeDetected | null;
  /** Mensagem de erro do sidecar (ex.: python/venv ausente), se houver. */
  wakeError: string | null;
}

export function useWakeWord({ enabled, onWake, cooldownMs = 2500 }: UseWakeWordArgs): UseWakeWordResult {
  const [lastDetected, setLastDetected] = useState<WakeDetected | null>(null);
  const [wakeError, setWakeError] = useState<string | null>(null);

  // Mantém o callback atual sem re-assinar o listener a cada render.
  const onWakeRef = useRef(onWake);
  useEffect(() => {
    onWakeRef.current = onWake;
  }, [onWake]);

  const lastFireRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;

    let disposed = false;

    window.electronAPI
      ?.wakeStart()
      .then(() => {
        if (!disposed) setWakeError(null);
      })
      .catch((e) => {
        if (!disposed) setWakeError(e instanceof Error ? e.message : String(e));
      });

    const unlistenWake = window.electronAPI?.onWakeDetected((detected) => {
      const now = Date.now();
      if (now - lastFireRef.current < cooldownMs) return; // guard anti-duplo-disparo
      lastFireRef.current = now;
      setLastDetected(detected);
      onWakeRef.current(detected);
    });

    return () => {
      disposed = true;
      unlistenWake?.();
      // Para o sidecar ao desligar/desmontar — libera o microfone (ponto laranja).
      window.electronAPI?.wakeStop().catch(() => {
        /* noop */
      });
    };
  }, [enabled, cooldownMs]);

  return { lastDetected, wakeError };
}
