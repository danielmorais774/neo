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

import type { VoiceConfig, VoiceOverride } from './tauriApi';

const TARGET_SR = 16000;
const CHUNK = 1600; // 100ms @ 16kHz — frames PCM int16 enviados ao neo-voice

const WORKLET_SRC = `
class PCMCapture extends AudioWorkletProcessor {
  process(inputs){ const c=inputs[0][0]; if(c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor('pcm-capture', PCMCapture);`;

export interface LiveVoiceCallbacks {
  onPartial?: (text: string) => void;
  onFinal?: (text: string) => void;
  onLevel?: (level: number) => void; // VU do mic, 0..1
  onError?: (message: string) => void;
  onSttOpen?: () => void;
  onSttClose?: () => void;
}

interface SttServerMsg {
  type?: string;
  text?: string;
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

  constructor(cfg: VoiceConfig, cb: LiveVoiceCallbacks) {
    this.cfg = cfg;
    this.cb = cb;
  }

  // ---------------------------------------------------------------- STT ----
  async startListening(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Navegador sem getUserMedia (permissão de microfone indisponível).');
    }

    // 1) microfone
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      throw new Error('Permissão de microfone negada: ' + msg, { cause: e });
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
      else if (m.type === 'final') this.cb.onFinal?.((m.text || '').trim());
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
    s.connect(ctx.destination);
    const t = Math.max(ctx.currentTime + 0.03, this.ttsPlayhead);
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
  speak(text: string, override?: VoiceOverride | null): Promise<void> {
    const clean = (text || '').trim();
    if (!clean) return Promise.resolve();

    // neo-core manda `engine`; o neo-voice espera `backend`. Cai pra cfg quando ausente.
    const backend = override?.engine || this.cfg.backend;
    const voice = override?.voice ?? this.cfg.voice ?? undefined;
    const language = override?.language ?? this.cfg.language ?? undefined;

    return new Promise<void>((resolve, reject) => {
      const ctx = this.ttsEnsureCtx();
      if (ctx.state === 'suspended') ctx.resume();
      this.ttsPlayhead = ctx.currentTime;

      let settled = false;
      let doneSignaled = false;
      let sr = 24000;
      let headerSr: number | null = null;

      const finish = () => {
        if (settled) return;
        settled = true;
        this.ttsFinish = null;
        resolve();
      };
      this.ttsFinish = finish;
      const scheduleDrain = () => {
        const remainingMs = Math.max(0, (this.ttsPlayhead - ctx.currentTime) * 1000) + 80;
        setTimeout(finish, remainingMs);
      };

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
          else if (m.type === 'chunk') headerSr = m.sr || headerSr;
          else if (m.type === 'done') {
            doneSignaled = true;
            scheduleDrain();
            try {
              ws.close();
            } catch {
              /* noop */
            }
          } else if (m.type === 'error') {
            try {
              ws.close();
            } catch {
              /* noop */
            }
            if (!settled) {
              settled = true;
              this.ttsFinish = null;
              reject(new Error(m.error || 'Falha no TTS'));
            }
          }
        } else {
          const int16 = new Int16Array(ev.data as ArrayBuffer);
          this.ttsPlayChunk(int16, headerSr || sr);
        }
      };
      ws.onerror = () => {
        if (!settled && !doneSignaled) {
          settled = true;
          this.ttsFinish = null;
          reject(new Error('Erro de WebSocket no TTS.'));
        }
      };
      ws.onclose = () => {
        if (doneSignaled && !settled) scheduleDrain();
      };
    });
  }

  /** Interrompe a fala atual (fecha WS e para os buffers em reprodução). */
  cancelSpeak(): void {
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
