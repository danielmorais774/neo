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
import type { SttFinalMeta } from '../lib/liveVoiceService';
import { AgentService } from '../lib';
import { stripInlineImageMarkers } from '../lib/inlineImages';
import type { VoiceOverride, SpeechSegmentOverride } from '../lib/tauriApi';

export type LiveState = 'idle' | 'listening' | 'thinking' | 'speaking';

// Teto de segurança dos estados OCUPADOS (thinking/speaking), onde a contagem de
// ociosidade não corre. Espelha `stuckTimeout` do LiveSession (NeoNotchHost.swift):
// um turno travado — engine de TTS que para no meio, agente que nunca responde —
// não pode prender a sessão para sempre. Generoso de propósito: uma resposta longa
// de verdade passa de 2min de fala, então isto é backstop, não limite de duração.
const LIVE_STUCK_TIMEOUT_MS = 300_000;

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

interface UseLiveModeArgs {
  // `mode` marca a origem do turno; o Live envia "live" para o neo-core responder
  // em texto falado/compacto (sem markdown). Ver useLiveMode → handleFinalTranscript.
  // `voice` marca a ENTRADA como transcrição (input_source=voice no metadata).
  onSendMessage: (text: string, mode?: string, voice?: VoiceInputMeta) => void;
}

/** Origem de voz do turno — vira `input_source`/`asr_raw`/`asr_engine` no metadata. */
export interface VoiceInputMeta {
  source: 'voice';
  raw?: string;
  engine?: string;
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
  // `[[IMAGE:/caminho…]]` é instrução de RENDER pra bolha do chat; falado viraria
  // o caminho do arquivo lido letra por letra.
  return stripInlineImageMarkers(raw || '')
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
  // Último nível do mic sem passar por render: o worklet emite a cada frame de
  // áudio (centenas de vezes por segundo) e quem espelha isso na notch lê daqui
  // num ritmo fixo.
  const micLevelRef = useRef(0);

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

  // --- Auto-desligar por inatividade -------------------------------------
  // Sem isto o Modo Live ficava escutando para sempre. Espelha o LiveSession do
  // NeoNotchHost.swift: o relógio só corre em `listening` (é rearmado a cada
  // parcial do STT, ou seja, enquanto você fala ele não avança) e nos estados
  // ocupados vira um teto de travamento bem mais folgado.
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const idleTimeoutMsRef = useRef(60_000); // sobrescrito pelo VoiceConfig no start()
  // `stop` é definido mais abaixo (precisa de sessionRef/setState); o timer o
  // alcança por ref para não criar dependência circular entre os callbacks.
  const stopRef = useRef<() => void>(() => {});

  const clearIdleTimer = useCallback(() => {
    if (idleTimerRef.current !== null) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
  }, []);

  const armCloseTimer = useCallback(
    (ms: number, reason: string) => {
      clearIdleTimer();
      if (ms <= 0) return; // 0 = timeout desligado por config
      idleTimerRef.current = setTimeout(() => {
        idleTimerRef.current = null;
        if (!isLiveRef.current) return;
        console.info(`[useLiveMode] Modo Live desligado por ${reason} (${Math.round(ms / 1000)}s)`);
        stopRef.current();
      }, ms);
    },
    [clearIdleTimer],
  );

  const armIdleTimer = useCallback(
    () => armCloseTimer(idleTimeoutMsRef.current, 'inatividade'),
    [armCloseTimer],
  );
  const armStuckTimer = useCallback(
    () => armCloseTimer(LIVE_STUCK_TIMEOUT_MS, 'turno travado'),
    [armCloseTimer],
  );

  const resumeListening = useCallback(() => {
    if (!isLiveRef.current) return;
    setPartialText('');
    setSpokenText('');
    sessionRef.current?.resumeMic();
    setState('listening');
    armIdleTimer(); // voltou a escutar ocioso -> conta o tempo de novo
  }, [setState, armIdleTimer]);

  // Resposta final do agente → fala de volta (TTS), depois volta a ouvir.
  // `voice` (Pattern A) vem do evento `complete` do neo-core e define a voz/engine.
  const speakAnswer = useCallback(
    async (
      text: string,
      voice?: VoiceOverride | null,
      segments?: SpeechSegmentOverride[] | null,
    ) => {
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
      armStuckTimer(); // falando: ociosidade não vale, mas o teto de travamento sim
      try {
        // Imitação de vozes: se a resposta veio fatiada em segmentos (cada um com
        // sua voz), fala segmento a segmento; senão, mantém a fala única de sempre.
        const segs = (segments || [])
          .map((s) => ({ text: cleanForSpeech(s.text), voice: s.voice ?? null }))
          .filter((s) => s.text);
        if (segs.length) {
          await sessionRef.current?.speakSegments(segs, voice);
        } else {
          await sessionRef.current?.speak(clean, voice);
        }
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
    [resumeListening, setState, armStuckTimer],
  );

  // Fala final transcrita → entra no pipeline de texto existente.
  const handleFinalTranscript = useCallback(
    (text: string, meta?: SttFinalMeta) => {
      if (!isLiveRef.current || !text.trim()) return;
      // só dispara um novo turno se estávamos ouvindo (evita re-entrância)
      if (stateRef.current !== 'listening') return;
      sessionRef.current?.pauseMic();
      setPartialText('');
      setState('thinking');
      armStuckTimer(); // pensando: agente mudo não prende a sessão para sempre
      // "live" → neo-core responde em texto falado/compacto (sem markdown).
      // `voice` é OUTRA coisa: marca a ENTRADA como transcrição de áudio, para o
      // core tolerar erro de ASR (docs/voice-input-normalization-plan.md §4).
      onSendMessageRef.current(text.trim(), 'live', {
        source: 'voice',
        raw: meta?.raw,
        engine: meta?.engine,
      });
    },
    [setState, armStuckTimer],
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
        void speakAnswer(
          message,
          event.data?.voice as VoiceOverride | null,
          event.data?.segments as SpeechSegmentOverride[] | null,
        );
      } else if (event.type === 'error') {
        resumeListening();
      }
    });
    return () => unlisten?.();
  }, [isLive, speakAnswer, resumeListening]);

  // Mensagem proativa (ex.: um job em background terminou) → fala quando o Live
  // está ativo e ocioso entre turnos. Se estivermos no meio de um turno
  // (thinking/speaking), apenas ignora — a mensagem já entra no chat pela timeline.
  useEffect(() => {
    const handler = (e: Event) => {
      if (!isLiveRef.current) return;
      if (stateRef.current !== 'listening') return;
      const detail = (e as CustomEvent).detail || {};
      const text = String(detail.text || '').trim();
      if (!text) return;
      sessionRef.current?.pauseMic();
      void speakAnswer(
        text,
        (detail.voice as VoiceOverride) || null,
        (detail.segments as SpeechSegmentOverride[]) || null,
      );
    };
    window.addEventListener('neo:proactive-speak', handler as EventListener);
    return () => window.removeEventListener('neo:proactive-speak', handler as EventListener);
  }, [speakAnswer]);

  const stop = useCallback(() => {
    isLiveRef.current = false;
    clearIdleTimer();
    sessionRef.current?.dispose();
    sessionRef.current = null;
    setIsLive(false);
    setState('idle');
    setPartialText('');
    setSpokenText('');
    setMicLevel(0);
  }, [setState, clearIdleTimer]);

  // O timer de auto-desligar chama `stop` por aqui (ver armCloseTimer).
  useEffect(() => {
    stopRef.current = stop;
  }, [stop]);

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

    // Timeout de ociosidade servido pelo Rust (NEO_LIVE_IDLE_TIMEOUT_S). O mesmo
    // VoiceConfig leva o do notch (mais curto) ao NeoNotchHost.swift.
    idleTimeoutMsRef.current = Math.max(0, (cfg.liveIdleTimeoutS ?? 60) * 1000);

    const session = new LiveVoiceSession(cfg, {
      onPartial: (t) => {
        setPartialText(t);
        // Só rearma ESCUTANDO. O mic fica pausado enquanto o Neo pensa/fala, mas
        // um parcial atrasado (áudio já em trânsito no neo-voice) ainda pode
        // chegar depois — e trocaria o teto de travamento pelo prazo curto de
        // ociosidade, derrubando a sessão no meio de uma resposta longa.
        if (stateRef.current === 'listening') armIdleTimer();
      },
      onFinal: (t, meta) => handleFinalTranscript(t, meta),
      onLevel: (l) => {
        micLevelRef.current = l;
        setMicLevel(l);
      },
      onError: (m) => console.warn('[useLiveMode]', m),
    });
    sessionRef.current = session;
    isLiveRef.current = true;
    setIsLive(true);
    setState('listening');
    armIdleTimer();

    try {
      await session.startListening();
    } catch (e) {
      setLiveError(errMsg(e) || 'Falha ao iniciar o microfone.');
      stop();
    }
  }, [handleFinalTranscript, setState, stop, armIdleTimer]);

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

  // Espelha o Modo Live na ilha da notch (mic + stop em volta do recorte).
  // O chip nativo acende as asas sozinho; este efeito cobre o caminho oposto —
  // wake word com a janela em foco abre o Live AQUI, e sem o aviso a notch
  // continuaria escondida. Falha de IPC é irrelevante: é só apresentação.
  //
  // O NÍVEL vai junto, a 20 Hz: nesta sessão quem captura é o webview, então
  // sem ele o medidor da ilha ficava parado no piso enquanto o equalizador da
  // janela dançava com a voz — as duas superfícies pareciam desligadas uma da
  // outra. 20 Hz porque é o que um medidor de 3 barras consome; repassar os
  // ~375 Hz do worklet seria um invoke por frame de áudio.
  useEffect(() => {
    const push = (level: number) =>
      void window.electronAPI?.notchLiveUi(isLive, stateRef.current, level)?.catch(() => {});
    push(isLive ? micLevelRef.current : 0);
    if (!isLive) return;
    const id = setInterval(() => push(micLevelRef.current), 50);
    return () => clearInterval(id);
  }, [isLive, liveState]);

  // Limpeza ao desmontar.
  useEffect(() => {
    return () => {
      isLiveRef.current = false;
      if (idleTimerRef.current !== null) clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
      sessionRef.current?.dispose();
      sessionRef.current = null;
    };
  }, []);

  return { isLive, liveState, partialText, spokenText, micLevel, liveError, toggleLive, interrupt };
}
