// useLiveMode — orquestra o Modo Live (voz) do desktop.
//
// Estados: idle → listening → thinking → speaking → listening … (loop)
//   listening: capturando o microfone (neo-voice STT por /ws/panel).
//   thinking:  o turno foi enviado ao agente (gRPC) e estamos aguardando a resposta.
//   speaking:  reproduzindo o TTS da resposta final (mic pausado — meio-duplex).
//
// O texto final transcrito reaproveita o MESMO caminho do input de texto
// (onSendMessage → execute_with_loop). A resposta final do agente é capturada
// pelo evento `agent-event` ("complete") e falada de volta via neo-voice TTS.

import { useCallback, useEffect, useRef, useState } from 'react';
import { LiveVoiceSession } from '../lib/liveVoiceService';
import { AgentService } from '../lib';
import type { VoiceOverride } from '../lib/tauriApi';

export type LiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

interface UseLiveModeArgs {
  // `mode` marca a origem do turno; o Live envia "live" para o neo-core responder
  // em texto falado/compacto (sem markdown). Ver useLiveMode → handleFinalTranscript.
  onSendMessage: (text: string, mode?: string) => void;
}

interface UseLiveModeResult {
  isLive: boolean;
  liveState: LiveState;
  partialText: string;
  spokenText: string;
  micLevel: number;
  liveError: string | null;
  toggleLive: () => void;
  /** Interrompe a fala (TTS) ou o turno em andamento e volta a ouvir. */
  interrupt: () => void;
}

/** Remove o marcador [MISSION:{...}] e símbolos de markdown antes de falar. */
function cleanForSpeech(raw: string): string {
  return (raw || '')
    .replace(/\[MISSION:\{.*\}\]/gs, '')
    .replace(/```[\s\S]*?```/g, ' ') // blocos de código não são falados
    .replace(/[*_`#>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

export function useLiveMode({ onSendMessage }: UseLiveModeArgs): UseLiveModeResult {
  const [isLive, setIsLive] = useState(false);
  const [liveState, setLiveState] = useState<LiveState>('idle');
  const [partialText, setPartialText] = useState('');
  const [spokenText, setSpokenText] = useState('');
  const [micLevel, setMicLevel] = useState(0);
  const [liveError, setLiveError] = useState<string | null>(null);

  const sessionRef = useRef<LiveVoiceSession | null>(null);
  const isLiveRef = useRef(false);
  const stateRef = useRef<LiveState>('idle');

  // Mantém o callback de envio sempre atual (o usuário pode trocar
  // modelo/provider durante o Live; a sessão captura este ref, não o valor).
  const onSendMessageRef = useRef(onSendMessage);
  useEffect(() => {
    onSendMessageRef.current = onSendMessage;
  }, [onSendMessage]);

  const setState = useCallback((s: LiveState) => {
    stateRef.current = s;
    setLiveState(s);
  }, []);

  const resumeListening = useCallback(() => {
    if (!isLiveRef.current) return;
    setPartialText('');
    setSpokenText('');
    sessionRef.current?.resumeMic();
    setState('listening');
  }, [setState]);

  // Resposta final do agente → fala de volta (TTS), depois volta a ouvir.
  // `voice` (Pattern A) vem do evento `complete` do neo-core e define a voz/engine.
  const speakAnswer = useCallback(
    async (text: string, voice?: VoiceOverride | null) => {
      if (!isLiveRef.current) return;
      const raw = (text || '').trim();
      const clean = cleanForSpeech(text);
      if (!clean) {
        // Nada falável após remover markdown. NÃO suma mudo (o "pisca"): loga a
        // causa (resposta vazia vs. só-markdown) e, se houver texto cru, mostra-o
        // como legenda por um instante antes de voltar a ouvir.
        if (raw) {
          console.warn('[useLiveMode] resposta sem texto falável após limpeza; exibindo cru:', raw);
          setSpokenText(raw);
          await delay(2500);
        } else {
          console.warn('[useLiveMode] evento complete sem message (vazio) — nada a falar/mostrar');
        }
        resumeListening();
        return;
      }
      setSpokenText(clean);
      setState('speaking');
      try {
        await sessionRef.current?.speak(clean, voice);
      } catch (e) {
        // TTS falhou (ex.: engine offline): mantém a legenda visível e sinaliza,
        // em vez do flash mudo — resumeListening() limparia o texto na hora.
        console.warn('[useLiveMode] TTS falhou; mantendo legenda visível:', e);
        setSpokenText(clean);
        setLiveError('Falha ao falar a resposta (TTS). Texto exibido como legenda.');
        await delay(2500);
      }
      resumeListening();
    },
    [resumeListening, setState],
  );

  // Fala final transcrita → entra no pipeline de texto existente.
  const handleFinalTranscript = useCallback(
    (text: string) => {
      if (!isLiveRef.current || !text.trim()) return;
      // só dispara um novo turno se estávamos ouvindo (evita re-entrância)
      if (stateRef.current !== 'listening') return;
      sessionRef.current?.pauseMic();
      setPartialText('');
      setState('thinking');
      // "live" → neo-core responde em texto falado/compacto (sem markdown).
      onSendMessageRef.current(text.trim(), 'live');
    },
    [setState],
  );

  // Assina os eventos do agente para capturar a resposta final (e erros).
  useEffect(() => {
    if (!isLive) return;
    const unlisten = window.electronAPI?.onAgentEvent((event) => {
      if (!isLiveRef.current) return;
      if (stateRef.current !== 'thinking') return; // só reagimos ao turno em voo
      if (event.type === 'complete') {
        if (event.data?.stopped) {
          resumeListening();
          return;
        }
        const message = String(event.data?.message ?? '');
        // Diagnóstico do "pisca": resposta final sem texto vinda do core/transporte.
        if (!message.trim()) {
          console.warn('[useLiveMode] evento complete sem message:', event.data);
        }
        void speakAnswer(message, event.data?.voice as VoiceOverride | null);
      } else if (event.type === 'error') {
        resumeListening();
      }
    });
    return () => unlisten?.();
  }, [isLive, speakAnswer, resumeListening]);

  const stop = useCallback(() => {
    isLiveRef.current = false;
    sessionRef.current?.dispose();
    sessionRef.current = null;
    setIsLive(false);
    setState('idle');
    setPartialText('');
    setSpokenText('');
    setMicLevel(0);
  }, [setState]);

  const start = useCallback(async () => {
    setLiveError(null);
    let cfg;
    try {
      cfg = await window.electronAPI?.getVoiceConfig();
    } catch (e) {
      setLiveError('Não foi possível ler a config de voz: ' + errMsg(e));
      return;
    }
    if (!cfg) {
      setLiveError('Config de voz indisponível.');
      return;
    }

    const session = new LiveVoiceSession(cfg, {
      onPartial: (t) => setPartialText(t),
      onFinal: (t) => handleFinalTranscript(t),
      onLevel: (l) => setMicLevel(l),
      onError: (m) => console.warn('[useLiveMode]', m),
    });
    sessionRef.current = session;
    isLiveRef.current = true;
    setIsLive(true);
    setState('listening');

    try {
      await session.startListening();
    } catch (e) {
      setLiveError(errMsg(e) || 'Falha ao iniciar o microfone.');
      stop();
    }
  }, [handleFinalTranscript, setState, stop]);

  const toggleLive = useCallback(() => {
    if (isLiveRef.current) stop();
    else void start();
  }, [start, stop]);

  // Interrupção por voz ("pare") ou externa: corta o que está em andamento.
  //  - speaking → cancelSpeak() resolve o speak() pendente; o próprio speakAnswer
  //    chama resumeListening() em seguida.
  //  - thinking → cancela o turno (emite complete{stopped} → resumeListening()).
  const interrupt = useCallback(() => {
    // DIAG (barge-in "pare"): mostra se o evento chegou no overlay webview e em
    // que estado — o interrupt só age em speaking/thinking. Remover após depurar.
    console.warn(
      `[useLiveMode] interrupt() chamado — isLive=${isLiveRef.current} state=${stateRef.current}`,
    );
    if (!isLiveRef.current) return;
    if (stateRef.current === 'speaking') {
      sessionRef.current?.cancelSpeak();
    } else if (stateRef.current === 'thinking') {
      AgentService.stopExecution();
    }
  }, []);

  // Limpeza ao desmontar.
  useEffect(() => {
    return () => {
      isLiveRef.current = false;
      sessionRef.current?.dispose();
      sessionRef.current = null;
    };
  }, []);

  return { isLive, liveState, partialText, spokenText, micLevel, liveError, toggleLive, interrupt };
}
