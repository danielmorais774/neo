// wakeRecorder — grava uma amostra curta do microfone como PCM mono 16kHz int16,
// para cadastrar frases-gatilho (few-shot) do wake word.
//
// Captura via AudioContext @16k + AudioWorklet (mesmo esquema do liveVoiceService),
// com resampler de fallback caso o contexto não honre 16kHz. Retorna number[]
// (int16) pronto pra mandar no comando Tauri `wake_add_sample`.

import { micConstraints } from './audioDevices';

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

/**
 * Grava `durationMs` (default 1500 = a janela de 1.5s do EfficientWord-Net) e
 * devolve as amostras PCM int16 @16k como number[]. Lança se o mic for negado.
 */
export async function recordPcm16k(durationMs = 1500): Promise<number[]> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('Navegador sem getUserMedia (microfone indisponível).');
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: micConstraints({
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }),
  });

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
