// liveVoiceService — captura de microfone (STT) e síntese de voz (TTS) para o
// Modo Live, falando direto com o serviço neo-voice por WebSocket.
//
// Fluxo:
//   - startListening(): getUserMedia + AudioWorklet → PCM int16 16kHz → /ws/panel
//     (o neo-voice faz VAD/segmentação e devolve {type:partial|final}).
//   - speak(text): abre /ws/tts, recebe chunks PCM int16 24kHz e toca em sequência;
//     a Promise resolve quando o último buffer agendado termina de tocar.
//   - pauseMic()/resumeMic(): meio-duplex — o mic fica mudo enquanto o Neo fala.
//
// Código de captura/resampler e de playback portado de
// neo-voice/service/web/index.html (classe PCMCapture, makeResampler, ttsPlayChunk).

import type { VoiceConfig, VoiceOverride, SpeechSegmentOverride } from './tauriApi';
import { OutputRouter, getMicStream } from './audioDevices';

const TARGET_SR = 16000;
const CHUNK = 1600; // 100ms @ 16kHz — frames PCM int16 enviados ao neo-voice

// Watchdog do TTS (ver speakInner). O gatilho é SILÊNCIO AUDÍVEL, não tempo de
// síntese: só conta quando o áudio já agendado acabou de tocar. Um trecho que
// demora 15s p/ sintetizar não dispara nada enquanto houver áudio na frente —
// o que dispara é o servidor parar de mandar com o playhead na estaca zero.
const TTS_FIRST_AUDIO_TIMEOUT_MS = 60_000; // nada tocou desde o pedido
const TTS_STALL_MS = 15_000; // fala começou e secou no meio
const TTS_WATCHDOG_TICK_MS = 1_000;

const WORKLET_SRC = `
class PCMCapture extends AudioWorkletProcessor {
  process(inputs){ const c=inputs[0][0]; if(c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor('pcm-capture', PCMCapture);`;

/** Correção que o neo-voice aplicou na transcrição (stt_text.py). */
export interface SttFix {
  from: string;
  to: string;
  rule: string;
}

/** Metadados do final do STT — viram `input_source`/`asr_raw` no metadata do turno. */
export interface SttFinalMeta {
  /** texto ANTES da correção do nome ("Neil que hora são") */
  raw?: string;
  /** de onde veio a transcrição: parakeet-gpu / parakeet-cpu / openrouter */
  engine?: string;
  fixes?: SttFix[];
}

export interface LiveVoiceCallbacks {
  onPartial?: (text: string) => void;
  onFinal?: (text: string, meta?: SttFinalMeta) => void;
  onLevel?: (level: number) => void; // VU do mic, 0..1
  onError?: (message: string) => void;
  onSttOpen?: () => void;
  onSttClose?: () => void;
}

interface SttServerMsg {
  type?: string;
  text?: string;
  /** transcrição crua, antes do corretor do neo-voice (stt_text.py) */
  raw_text?: string;
  fixes?: SttFix[];
  asr_engine?: string;
  /** final vazio + dropped = eco do wake word, não é turno */
  dropped?: string;
}

interface TtsServerMsg {
  type?: string;
  sr?: number;
  error?: string;
}

type AudioCtxCtor = typeof AudioContext;

function newAudioContext(opts?: AudioContextOptions): AudioContext {
  const AC: AudioCtxCtor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext: AudioCtxCtor }).webkitAudioContext;
  return new AC(opts);
}

function makeResampler(inRate: number): (frame: Float32Array) => Float32Array {
  const ratio = inRate / TARGET_SR;
  if (Math.abs(ratio - 1) < 1e-6) return (f) => f;
  let buf = new Float32Array(0);
  let pos = 0;
  return (frame: Float32Array) => {
    const nb = new Float32Array(buf.length + frame.length);
    nb.set(buf, 0);
    nb.set(frame, buf.length);
    buf = nb;
    const out: number[] = [];
    while (Math.floor(pos) + 1 < buf.length) {
      const i0 = Math.floor(pos);
      const fr = pos - i0;
      out.push(buf[i0] * (1 - fr) + buf[i0 + 1] * fr);
      pos += ratio;
    }
    const c = Math.floor(pos);
    buf = buf.subarray(c);
    pos -= c;
    return Float32Array.from(out);
  };
}

function f2i(x: number): number {
  x = Math.max(-1, Math.min(1, x));
  return x < 0 ? x * 32768 : x * 32767;
}

export class LiveVoiceSession {
  private cfg: VoiceConfig;
  private cb: LiveVoiceCallbacks;

  // STT
  private sttWs: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private src: MediaStreamAudioSourceNode | null = null;
  private workletURL: string | null = null;
  private resample: (f: Float32Array) => Float32Array = (f) => f;
  private pend: Float32Array[] = [];
  private pendLen = 0;
  private micPaused = false;
  private disposed = false;

  // TTS
  private ttsCtx: AudioContext | null = null;
  private ttsPlayhead = 0;
  private ttsWs: WebSocket | null = null;
  private activeSources: AudioBufferSourceNode[] = [];
  private ttsFinish: (() => void) | null = null;
  // Destino onde as fontes de TTS se conectam. Começa no ctx.destination e o
  // OutputRouter troca p/ um MediaStreamDestination quando roteamos p/ um
  // alto-falante específico (WebKit). O router resolve antes do 1º chunk chegar
  // (há a ida-e-volta do WebSocket de TTS no meio), então não há glitch.
  private ttsSink: AudioNode | null = null;
  private ttsRouter: OutputRouter | null = null;
  // Bumped by cancelSpeak()/dispose() so an in-flight speakSegments() loop stops
  // before starting the next segment (each segment is its own speak()).
  private segRun = 0;

  constructor(cfg: VoiceConfig, cb: LiveVoiceCallbacks) {
    this.cfg = cfg;
    this.cb = cb;
  }

  // ---------------------------------------------------------------- STT ----
  async startListening(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Navegador sem getUserMedia (permissão de microfone indisponível).');
    }

    // 1) microfone (device + AEC seguem a rota de áudio atual — ver audioDevices)
    try {
      this.stream = await getMicStream();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      throw new Error('Não foi possível abrir o microfone: ' + msg, { cause: e });
    }
    if (this.disposed) {
      this.stopListening();
      return;
    }

    // 2) websocket STT
    const url = this.sttUrl();
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    this.sttWs = ws;
    ws.onopen = () => this.cb.onSttOpen?.();
    ws.onclose = () => this.cb.onSttClose?.();
    ws.onerror = () => this.cb.onError?.('Erro de WebSocket no STT.');
    ws.onmessage = (ev: MessageEvent) => {
      let m: SttServerMsg;
      try {
        m = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (m.type === 'partial') this.cb.onPartial?.(m.text || '');
      else if (m.type === 'final') {
        // final vazio (eco do wake word descartado no neo-voice) cai no guard de
        // texto vazio do useLiveMode — nenhum turno é aberto.
        this.cb.onFinal?.((m.text || '').trim(), {
          raw: m.raw_text,
          engine: m.asr_engine,
          fixes: m.fixes,
        });
      }
      // 'response' é ignorado de propósito: o turno é orquestrado pelo desktop
      // via gRPC (rode o neo-voice SEM NEO_CORE_URL p/ não duplicar).
    };

    // 3) pipeline de áudio (AudioContext @16k + AudioWorklet)
    this.ctx = newAudioContext({ sampleRate: TARGET_SR });
    this.workletURL = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
    await this.ctx.audioWorklet.addModule(this.workletURL);
    if (this.disposed) {
      this.stopListening();
      return;
    }
    this.resample = makeResampler(this.ctx.sampleRate);
    this.pend = [];
    this.pendLen = 0;
    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    this.node.port.onmessage = (e: MessageEvent) => this.onFrame(e.data as Float32Array);
    this.src.connect(this.node);
    this.node.connect(this.ctx.destination);
  }

  private sttUrl(): string {
    const base = this.cfg.wsBaseUrl.replace(/\/$/, '');
    const qs = this.cfg.apiKey ? `?api_key=${encodeURIComponent(this.cfg.apiKey)}` : '';
    return `${base}/ws/panel${qs}`;
  }

  private onFrame(f: Float32Array): void {
    // VU meter
    let s = 0;
    for (let i = 0; i < f.length; i++) s += f[i] * f[i];
    this.cb.onLevel?.(Math.min(1, Math.sqrt(s / f.length) * 3.2));

    if (this.micPaused) return; // meio-duplex: descarta enquanto o Neo fala

    const r = this.resample(f);
    this.pend.push(r);
    this.pendLen += r.length;
    while (this.pendLen >= CHUNK) {
      const out = new Int16Array(CHUNK);
      let filled = 0;
      while (filled < CHUNK) {
        const head = this.pend[0];
        const need = CHUNK - filled;
        if (head.length <= need) {
          for (let i = 0; i < head.length; i++) out[filled + i] = f2i(head[i]);
          filled += head.length;
          this.pend.shift();
        } else {
          for (let i = 0; i < need; i++) out[filled + i] = f2i(head[i]);
          this.pend[0] = head.subarray(need);
          filled += need;
        }
      }
      this.pendLen -= CHUNK;
      if (this.sttWs && this.sttWs.readyState === WebSocket.OPEN) {
        this.sttWs.send(out.buffer);
      }
    }
  }

  /** Pausa o envio de áudio e silencia o track (meio-duplex). */
  pauseMic(): void {
    this.micPaused = true;
    this.pend = [];
    this.pendLen = 0;
    this.stream?.getAudioTracks().forEach((t) => (t.enabled = false));
    this.cb.onLevel?.(0);
  }

  /** Religa a captura após o Neo terminar de falar. */
  resumeMic(): void {
    if (this.disposed) return;
    this.pend = [];
    this.pendLen = 0;
    this.stream?.getAudioTracks().forEach((t) => (t.enabled = true));
    this.micPaused = false;
  }

  /** Pede ao neo-voice para fechar a fala atual e transcrever o que houver. */
  flush(): void {
    if (this.sttWs && this.sttWs.readyState === WebSocket.OPEN) {
      try {
        this.sttWs.send('flush');
      } catch {
        /* noop */
      }
    }
  }

  stopListening(): void {
    try {
      this.node?.disconnect();
    } catch {
      /* noop */
    }
    try {
      this.src?.disconnect();
    } catch {
      /* noop */
    }
    try {
      this.stream?.getTracks().forEach((t) => t.stop());
    } catch {
      /* noop */
    }
    try {
      this.ctx?.close();
    } catch {
      /* noop */
    }
    if (this.workletURL) {
      URL.revokeObjectURL(this.workletURL);
      this.workletURL = null;
    }
    const ws = this.sttWs;
    if (ws) {
      try {
        ws.close();
      } catch {
        /* noop */
      }
    }
    this.node = null;
    this.src = null;
    this.stream = null;
    this.ctx = null;
    this.sttWs = null;
    this.pend = [];
    this.pendLen = 0;
  }

  // ---------------------------------------------------------------- TTS ----
  private ttsEnsureCtx(): AudioContext {
    if (!this.ttsCtx || this.ttsCtx.state === 'closed') {
      this.ttsCtx = newAudioContext();
      this.ttsSink = this.ttsCtx.destination; // padrão até o router resolver
      this.ttsRouter?.dispose();
      this.ttsRouter = new OutputRouter();
      // Roteia a fala p/ o alto-falante escolhido (best-effort). Async: as fontes
      // usam ttsSink, que passa a apontar p/ o destino roteado quando resolver.
      void this.ttsRouter.attach(this.ttsCtx).then((node) => {
        this.ttsSink = node;
      });
    }
    return this.ttsCtx;
  }

  private ttsPlayChunk(int16: Int16Array, sr: number): void {
    const ctx = this.ttsEnsureCtx();
    const f32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, sr);
    buf.copyToChannel(f32, 0);
    const s = ctx.createBufferSource();
    s.buffer = buf;
    s.connect(this.ttsSink ?? ctx.destination);
    // Folga adaptativa: com áudio ainda agendado à frente (primed), 30ms bastam
    // p/ emendar sem clique; no 1º buffer — e na retomada após um underrun — o
    // colchão sobe p/ 150ms, senão o WebKit descarta o começo (agendar colado no
    // "agora" logo depois do resume() era o que comia o início da fala).
    const now = ctx.currentTime;
    const primed = this.ttsPlayhead > now;
    const t = Math.max(now + (primed ? 0.03 : 0.15), this.ttsPlayhead);
    s.start(t);
    this.ttsPlayhead = t + buf.duration;
    this.activeSources.push(s);
    s.onended = () => {
      this.activeSources = this.activeSources.filter((x) => x !== s);
    };
  }

  /**
   * Sintetiza e toca `text`. Resolve quando o áudio termina de tocar.
   * Se cancelSpeak()/dispose() for chamado, resolve antecipadamente.
   *
   * `override` (voz resolvida pelo neo-core, Pattern A) tem prioridade sobre a
   * VoiceConfig estática para esta fala: define voz/engine/idioma do TTS.
   */
  async speak(text: string, override?: VoiceOverride | null): Promise<void> {
    const clean = (text || '').trim();
    if (!clean) return;

    // neo-core manda `engine`; o neo-voice espera `backend`. Cai pra cfg quando ausente.
    const backend = override?.engine || this.cfg.backend;
    const voice = override?.voice ?? this.cfg.voice ?? undefined;
    const language = override?.language ?? this.cfg.language ?? undefined;

    // resume() AGUARDADO antes de qualquer agendamento: agendar num contexto
    // ainda suspenso descartava o começo do 1º áudio no WebKit.
    const ctx = this.ttsEnsureCtx();
    if (ctx.state === 'suspended') {
      try {
        await ctx.resume();
      } catch {
        /* segue mesmo assim: melhor tentar tocar do que travar a fala */
      }
    }

    // Gate anti-eco: enquanto este TTS toca, a voz do Neo não pode acordar o
    // wake word do sidecar ("Ei Neo" sai da boca do próprio Neo o tempo todo).
    window.electronAPI?.wakeTtsState?.(true).catch(() => {});
    try {
      return await this.speakInner(clean, backend, voice, language, ctx);
    } finally {
      window.electronAPI?.wakeTtsState?.(false).catch(() => {});
    }
  }

  private speakInner(
    clean: string,
    backend: string | undefined,
    voice: string | undefined,
    language: string | undefined,
    ctx: AudioContext,
  ): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      this.ttsPlayhead = ctx.currentTime;

      let settled = false;
      let doneSignaled = false;
      let sr = 24000;
      let headerSr: number | null = null;
      let lastChunkAt = Date.now();
      let gotAudio = false;
      let watchdog: ReturnType<typeof setInterval> | null = null;

      const stopWatchdog = () => {
        if (watchdog !== null) {
          clearInterval(watchdog);
          watchdog = null;
        }
      };
      const finish = () => {
        if (settled) return;
        settled = true;
        stopWatchdog();
        this.ttsFinish = null;
        resolve();
      };
      const fail = (err: Error) => {
        if (settled) return;
        settled = true;
        stopWatchdog();
        this.ttsFinish = null;
        try {
          ws.close();
        } catch {
          /* noop */
        }
        reject(err);
      };
      this.ttsFinish = finish;
      const scheduleDrain = () => {
        stopWatchdog(); // 'done' recebido: o resto é só o playhead escoar
        const remainingMs = Math.max(0, (this.ttsPlayhead - ctx.currentTime) * 1000) + 80;
        setTimeout(finish, remainingMs);
      };

      // Watchdog: sem ele, um engine que para de responder no meio da resposta
      // deixava esta Promise pendurada P/ SEMPRE — e como o Modo Live só volta a
      // ouvir quando ela resolve, o mic ficava mudo e o gate anti-eco travava a
      // wake word: o usuário perdia até o "pare". Ver o incidente de 2026-07-20
      // (coqui 500 no 7º trecho, 18 re-tentativas, 4min de silêncio).
      watchdog = setInterval(() => {
        if (settled || doneSignaled) return;
        // ainda há áudio agendado tocando: não é silêncio, não é stall
        if (this.ttsPlayhead > ctx.currentTime) return;
        const quietMs = Date.now() - lastChunkAt;
        const limit = gotAudio ? TTS_STALL_MS : TTS_FIRST_AUDIO_TIMEOUT_MS;
        if (quietMs < limit) return;
        fail(
          new Error(
            gotAudio
              ? `TTS travou no meio da fala (${Math.round(quietMs / 1000)}s sem áudio).`
              : `TTS não respondeu (${Math.round(quietMs / 1000)}s sem áudio).`,
          ),
        );
      }, TTS_WATCHDOG_TICK_MS);

      const ws = new WebSocket(`${this.cfg.wsBaseUrl.replace(/\/$/, '')}/ws/tts`);
      ws.binaryType = 'arraybuffer';
      this.ttsWs = ws;

      ws.onopen = () =>
        ws.send(
          JSON.stringify({
            text: clean,
            voice,
            // sem language → cada engine usa seu default correto
            // (Qwen="Portuguese", Coqui="pt"); só envia se configurado.
            language,
            backend,
          }),
        );
      ws.onmessage = (ev: MessageEvent) => {
        if (typeof ev.data === 'string') {
          let m: TtsServerMsg;
          try {
            m = JSON.parse(ev.data);
          } catch {
            return;
          }
          if (m.type === 'start') sr = m.sr || 24000;
          else if (m.type === 'chunk') {
            headerSr = m.sr || headerSr;
            lastChunkAt = Date.now();
          } else if (m.type === 'done') {
            doneSignaled = true;
            scheduleDrain();
            try {
              ws.close();
            } catch {
              /* noop */
            }
          } else if (m.type === 'error') {
            fail(new Error(m.error || 'Falha no TTS'));
          }
        } else {
          const int16 = new Int16Array(ev.data as ArrayBuffer);
          lastChunkAt = Date.now();
          gotAudio = true;
          this.ttsPlayChunk(int16, headerSr || sr);
        }
      };
      ws.onerror = () => {
        if (!doneSignaled) fail(new Error('Erro de WebSocket no TTS.'));
      };
      ws.onclose = () => {
        if (doneSignaled && !settled) scheduleDrain();
      };
    });
  }

  /**
   * Fala uma sequência de segmentos (imitação de vozes), um após o outro,
   * trocando a voz por segmento. `seg.voice` tem prioridade; sem ele, usa
   * `defaultVoice` (a voz padrão do turno, Pattern A). Resolve quando o último
   * segmento termina; um cancelSpeak()/dispose() no meio interrompe a sequência.
   */
  async speakSegments(
    segments: SpeechSegmentOverride[],
    defaultVoice?: VoiceOverride | null,
  ): Promise<void> {
    const myRun = ++this.segRun;
    for (const seg of segments) {
      if (this.disposed || myRun !== this.segRun) return; // cancelado/substituído
      const text = (seg.text || '').trim();
      if (!text) continue;
      await this.speak(text, seg.voice ?? defaultVoice ?? null);
    }
  }

  /** Interrompe a fala atual (fecha WS e para os buffers em reprodução). */
  cancelSpeak(): void {
    this.segRun++; // aborta qualquer speakSegments() em andamento

    // resolve a Promise de speak() pendente para não deixá-la pendurada
    const finish = this.ttsFinish;
    this.ttsFinish = null;
    const ws = this.ttsWs;
    if (ws) {
      try {
        ws.close();
      } catch {
        /* noop */
      }
      this.ttsWs = null;
    }
    this.activeSources.forEach((s) => {
      try {
        s.stop();
      } catch {
        /* noop */
      }
    });
    this.activeSources = [];
    this.ttsRouter?.dispose();
    this.ttsRouter = null;
    this.ttsSink = null;
    if (this.ttsCtx) {
      try {
        this.ttsCtx.close();
      } catch {
        /* noop */
      }
      this.ttsCtx = null;
    }
    this.ttsPlayhead = 0;
    finish?.();
  }

  dispose(): void {
    this.disposed = true;
    this.cancelSpeak();
    this.stopListening();
  }
}
