// wakeRecorder — grava uma amostra curta do microfone como PCM mono 16kHz int16,
// para cadastrar frases-gatilho (few-shot) do wake word.
//
// Captura via AudioContext @16k + AudioWorklet (mesmo esquema do liveVoiceService),
// com resampler de fallback caso o contexto não honre 16kHz. Retorna number[]
// (int16) pronto pra mandar no comando Tauri `wake_add_sample`.

import { getMicStream } from './audioDevices';

const TARGET_SR = 16000;

const WORKLET_SRC = `
class PCMCapture extends AudioWorkletProcessor {
  process(inputs){ const c=inputs[0][0]; if(c) this.port.postMessage(c.slice(0)); return true; }
}
registerProcessor('pcm-capture', PCMCapture);`;

function newAudioContext(opts?: AudioContextOptions): AudioContext {
  const AC =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
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

const delay = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Veredito de qualidade de uma amostra de cadastro. */
export interface SampleLevel {
  /** Maior amplitude absoluta, 0..1. */
  peak: number;
  /** Energia média (RMS), 0..1. */
  rms: number;
  /** Fração de amostras no teto (indício de saturação). */
  clipped: number;
  /** `false` = não serve como referência; ver `reason`. */
  ok: boolean;
  /** Serve, mas está no limite — vale regravar. */
  weak: boolean;
  reason: string;
}

// Calibrado com as amostras REAIS do cadastro (medição de 2026-07-20):
//   boas .............. pico 0.385–0.641  rms 0.040–0.044
//   fraca (funcionou) . pico 0.153        rms 0.014
//   ruins ............. pico 0.037        rms 0.003–0.004
//   muda .............. pico 0.001        rms 0.0003
// Os limiares ficam ENTRE a que funcionou e as ruins.
const MIN_PEAK = 0.08;
const MIN_RMS = 0.006;
const WEAK_PEAK = 0.2;
const WEAK_RMS = 0.015;
const MAX_CLIPPED = 0.005; // 0.5% das amostras no teto

/**
 * Mede o nível de uma amostra PCM int16 e diz se ela serve de referência.
 *
 * Existe porque o cadastro aceitava QUALQUER coisa: havia uma amostra de "ei neo"
 * em silêncio puro (pico 0.001) e três de "pare" quase mudas (pico 0.037) — e o
 * detector calcula a referência em cima disso, o que degrada todas as ativações
 * daquela frase. Amostra ruim é pior que amostra faltando.
 */
export function analyzeLevel(pcm: number[]): SampleLevel {
  if (pcm.length === 0) {
    return { peak: 0, rms: 0, clipped: 0, ok: false, weak: false, reason: 'Nada foi gravado.' };
  }
  let peakRaw = 0;
  let sumSq = 0;
  let atCeiling = 0;
  for (let i = 0; i < pcm.length; i++) {
    const a = Math.abs(pcm[i]);
    if (a > peakRaw) peakRaw = a;
    if (a >= 32700) atCeiling++;
    const n = pcm[i] / 32768;
    sumSq += n * n;
  }
  const peak = peakRaw / 32768;
  const rms = Math.sqrt(sumSq / pcm.length);
  const clipped = atCeiling / pcm.length;

  if (clipped > MAX_CLIPPED) {
    return {
      peak, rms, clipped, ok: false, weak: false,
      reason: 'Áudio saturado (muito perto do microfone ou ganho alto). Afaste-se um pouco e regrave.',
    };
  }
  if (peak < MIN_PEAK || rms < MIN_RMS) {
    return {
      peak, rms, clipped, ok: false, weak: false,
      reason:
        peak < 0.02
          ? 'Não captei áudio nenhum — confira se o microfone certo está selecionado.'
          : 'Áudio baixo demais para servir de referência. Fale mais perto/mais alto e regrave.',
    };
  }
  const weak = peak < WEAK_PEAK || rms < WEAK_RMS;
  return {
    peak, rms, clipped, ok: true, weak,
    reason: weak ? 'Gravou, mas o áudio ficou fraco — se puder, regrave mais perto.' : '',
  };
}

/**
 * Grava `durationMs` (default 1500 = a janela de 1.5s do EfficientWord-Net) e
 * devolve as amostras PCM int16 @16k como number[]. Lança se o mic for negado.
 */
export async function recordPcm16k(durationMs = 1500): Promise<number[]> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Navegador sem getUserMedia (microfone indisponível).');
  }
  // Mesmo mic (e mesma política de AEC) que o Modo Live usa na hora de escutar —
  // a referência few-shot tem que casar com o que o detector vai ouvir depois.
  const stream = await getMicStream();

  const ctx = newAudioContext({ sampleRate: TARGET_SR });
  const workletURL = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
  const chunks: Float32Array[] = [];
  let node: AudioWorkletNode | null = null;
  let src: MediaStreamAudioSourceNode | null = null;

  try {
    await ctx.audioWorklet.addModule(workletURL);
    const resample = makeResampler(ctx.sampleRate);
    src = ctx.createMediaStreamSource(stream);
    node = new AudioWorkletNode(ctx, 'pcm-capture');
    node.port.onmessage = (e: MessageEvent) => chunks.push(resample(e.data as Float32Array));
    src.connect(node);
    node.connect(ctx.destination); // worklet é silencioso (não copia input→output)
    await delay(durationMs);
  } finally {
    try {
      node?.disconnect();
    } catch {
      /* noop */
    }
    try {
      src?.disconnect();
    } catch {
      /* noop */
    }
    stream.getTracks().forEach((t) => t.stop());
    try {
      await ctx.close();
    } catch {
      /* noop */
    }
    URL.revokeObjectURL(workletURL);
  }

  const total = chunks.reduce((n, c) => n + c.length, 0);
  const out = new Array<number>(total);
  let o = 0;
  for (const c of chunks) {
    for (let i = 0; i < c.length; i++) {
      const x = Math.max(-1, Math.min(1, c[i]));
      out[o++] = Math.round(x < 0 ? x * 32768 : x * 32767);
    }
  }
  return out;
}
