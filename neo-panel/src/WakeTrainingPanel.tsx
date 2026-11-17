import { useCallback, useEffect, useRef, useState } from 'react';
import Screen from './ui/Screen';

/*
 * Aba "Wake · Treino" — captura amostras REAIS de voz p/ o wake "ei neo".
 *
 * Dois modos:
 *  - POSITIVOS: você lê "ei neo" variando distância/jeito. Ancora o RECALL.
 *  - NEGATIVOS: você lê "ei" (sozinho/frases) + confusores. Ensina a NÃO acordar.
 *
 * O guia é um TELEPROMPTER: uma lista de frases prontas p/ ler, cada uma com a
 * distância/entonação; a linha atual acende e rola sozinha conforme o tempo.
 *
 * Grava no browser (getUserMedia→MediaRecorder), reamostra p/ 16 kHz mono via
 * OfflineAudioContext, encoda WAV PCM16 e baixa. Nada vai a servidor.
 */

type Mode = 'neg' | 'pos';
type Dist = 'perto' | 'normal' | 'longe' | 'costas';

interface Line {
  say: string;
  note: string;
  dist: Dist;
}

const DIST_META: Record<Dist, { label: string; color: string }> = {
  perto: { label: 'PERTO ~20cm', color: 'var(--neo-green)' },
  normal: { label: 'BRAÇO ~60cm', color: 'var(--neo-violet-light)' },
  longe: { label: 'LONGE 2–3m', color: 'var(--neo-rose)' },
  costas: { label: 'DE LADO/COSTAS', color: '#e0a458' },
};

const POS_SCRIPT: Line[] = [
  { say: 'Ei Neo', note: 'tom normal', dist: 'perto' },
  { say: 'Ei Neo', note: 'mais forte', dist: 'perto' },
  { say: 'Ei Neo', note: 'mais baixo, quase sussurro', dist: 'perto' },
  { say: 'Ei Neo, que horas são?', note: 'tom normal', dist: 'perto' },
  { say: 'Ei Neo, toca música', note: 'tom normal', dist: 'perto' },
  { say: 'Ei Neo', note: 'tom normal', dist: 'normal' },
  { say: 'Ei Neo', note: 'mais agudo', dist: 'normal' },
  { say: 'Ei Neo', note: 'mais grave', dist: 'normal' },
  { say: 'Ei Neo', note: 'bem rápido', dist: 'normal' },
  { say: 'Ei Neo', note: 'devagar, arrastando', dist: 'normal' },
  { say: 'Ei Neo, liga a luz da sala', note: 'tom normal', dist: 'normal' },
  { say: 'Ei Neo, como está o tempo?', note: 'tom normal', dist: 'normal' },
  { say: 'Ei Neo', note: 'projetando a voz', dist: 'longe' },
  { say: 'Ei Neo', note: 'tom normal', dist: 'longe' },
  { say: 'Ei Neo', note: 'mais forte', dist: 'longe' },
  { say: 'Ei Neo, aumenta o volume', note: 'projetando', dist: 'longe' },
  { say: 'Ei Neo, pausa', note: 'tom normal', dist: 'longe' },
  { say: 'Ei Neo', note: 'virado de lado', dist: 'costas' },
  { say: 'Ei Neo', note: 'de costas pro mic', dist: 'costas' },
  { say: 'Ei Neo, manda mensagem', note: 'tom normal', dist: 'normal' },
  { say: 'Ei Neo', note: 'perto, bem rápido', dist: 'perto' },
  { say: 'Ei Neo', note: 'natural, do seu jeito', dist: 'normal' },
];

const NEG_SCRIPT: Line[] = [
  { say: 'Ei', note: 'tom normal', dist: 'perto' },
  { say: 'Ei', note: 'mais forte', dist: 'perto' },
  { say: 'Ei', note: 'tom normal', dist: 'normal' },
  { say: 'Ei, vem cá', note: 'tom normal', dist: 'normal' },
  { say: 'Ei, olha isso', note: 'tom normal', dist: 'normal' },
  { say: 'Ei, espera', note: 'tom normal', dist: 'perto' },
  { say: 'Ei, cara', note: 'tom normal', dist: 'normal' },
  { say: 'Ei', note: 'projetando a voz', dist: 'longe' },
  { say: 'Ei… ei… ei', note: 'repetido, ritmos diferentes', dist: 'normal' },
  { say: 'Né o', note: 'confusor — normal', dist: 'perto' },
  { say: 'Meio', note: 'confusor — normal', dist: 'normal' },
  { say: 'Ênio', note: 'confusor — normal', dist: 'normal' },
  { say: 'Vídeo', note: 'confusor — normal', dist: 'perto' },
  { say: 'Espontâneo', note: 'confusor — normal', dist: 'normal' },
  { say: 'Simultâneo', note: 'confusor — normal', dist: 'normal' },
  { say: 'Que horas são?', note: 'fala comum', dist: 'normal' },
  { say: 'Liga a luz da sala', note: 'fala comum', dist: 'normal' },
  { say: 'Tudo bem com você?', note: 'fala comum', dist: 'longe' },
  { say: 'Vamos ao mercado depois', note: 'fala comum', dist: 'normal' },
  { say: 'E o resto, como ficou?', note: 'fala comum', dist: 'perto' },
];

const TOTAL = 100;

const MODE_META: Record<Mode, { label: string; file: string; icon: string; script: Line[] }> = {
  pos: { label: 'Positivos ("ei neo")', file: 'ei_neo_pos_real.wav', icon: '✅', script: POS_SCRIPT },
  neg: { label: 'Negativos ("ei")', file: 'ei_neg_real.wav', icon: '🚫', script: NEG_SCRIPT },
};

type Status = 'idle' | 'recording' | 'processing' | 'done' | 'error';

function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeStr = (o: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i));
  };
  writeStr(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeStr(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    o += 2;
  }
  return new Blob([view], { type: 'audio/wav' });
}

async function blobToWav16kMono(blob: Blob): Promise<Blob> {
  const arrayBuf = await blob.arrayBuffer();
  const AC: typeof AudioContext =
    window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const ac = new AC();
  const decoded = await ac.decodeAudioData(arrayBuf);
  await ac.close();

  const frames = Math.max(1, Math.ceil(decoded.duration * 16000));
  const OAC: typeof OfflineAudioContext =
    window.OfflineAudioContext ||
    (window as unknown as { webkitOfflineAudioContext: typeof OfflineAudioContext }).webkitOfflineAudioContext;
  const offline = new OAC(1, frames, 16000);
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start();
  const rendered = await offline.startRendering();
  return encodeWav(rendered.getChannelData(0), 16000);
}

const fmt = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;

const WakeTrainingPanel = () => {
  const [mode, setMode] = useState<Mode>('pos');
  const [status, setStatus] = useState<Status>('idle');
  const [elapsed, setElapsed] = useState(0);
  const [level, setLevel] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [wavUrl, setWavUrl] = useState<string | null>(null);
  const [wavBytes, setWavBytes] = useState(0);

  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const acRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const timerRef = useRef<number | null>(null);
  const startTsRef = useRef<number>(0);
  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);

  const script = MODE_META[mode].script;
  const N = script.length;
  const slot = TOTAL / N;
  const activeIdx = status === 'recording' ? Math.min(N - 1, Math.floor(elapsed / slot)) : -1;

  // teleprompter: rola a linha ativa p/ dentro da vista
  useEffect(() => {
    if (activeIdx >= 0) rowRefs.current[activeIdx]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [activeIdx]);

  const cleanupLive = useCallback(() => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    if (timerRef.current != null) clearInterval(timerRef.current);
    rafRef.current = null;
    timerRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (acRef.current && acRef.current.state !== 'closed') acRef.current.close();
    acRef.current = null;
    analyserRef.current = null;
  }, []);

  const stop = useCallback(() => {
    if (recRef.current && recRef.current.state !== 'inactive') recRef.current.stop();
  }, []);

  const start = useCallback(async () => {
    setError(null);
    if (wavUrl) {
      URL.revokeObjectURL(wavUrl);
      setWavUrl(null);
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
      });
      streamRef.current = stream;

      const AC: typeof AudioContext =
        window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ac = new AC();
      acRef.current = ac;
      const source = ac.createMediaStreamSource(stream);
      const analyser = ac.createAnalyser();
      analyser.fftSize = 1024;
      source.connect(analyser);
      analyserRef.current = analyser;
      const buf = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        setLevel(Math.min(1, Math.sqrt(sum / buf.length) * 3));
        rafRef.current = requestAnimationFrame(tick);
      };
      tick();

      chunksRef.current = [];
      const rec = new MediaRecorder(stream);
      recRef.current = rec;
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      rec.onstop = async () => {
        cleanupLive();
        setLevel(0);
        setStatus('processing');
        try {
          const raw = new Blob(chunksRef.current, { type: chunksRef.current[0]?.type || 'audio/webm' });
          const wav = await blobToWav16kMono(raw);
          setWavBytes(wav.size);
          setWavUrl(URL.createObjectURL(wav));
          setStatus('done');
        } catch (err) {
          setError(`Falha ao processar o áudio: ${(err as Error).message}`);
          setStatus('error');
        }
      };
      rec.start();

      startTsRef.current = Date.now();
      setElapsed(0);
      setStatus('recording');
      timerRef.current = window.setInterval(() => {
        const el = (Date.now() - startTsRef.current) / 1000;
        setElapsed(el);
        if (el >= TOTAL) stop();
      }, 100);
    } catch (err) {
      setError(
        `Não consegui acessar o microfone: ${(err as Error).message}. ` +
          `Confira a permissão de mic do navegador (o painel precisa rodar em localhost/HTTPS).`,
      );
      setStatus('error');
    }
  }, [wavUrl, cleanupLive, stop]);

  const reset = useCallback(() => {
    if (wavUrl) URL.revokeObjectURL(wavUrl);
    setWavUrl(null);
    setWavBytes(0);
    setElapsed(0);
    setStatus('idle');
    setError(null);
  }, [wavUrl]);

  const switchMode = useCallback(
    (m: Mode) => {
      if (status === 'recording' || status === 'processing') return;
      reset();
      setMode(m);
    },
    [status, reset],
  );

  useEffect(() => () => cleanupLive(), [cleanupLive]);

  const pct = Math.min(100, (elapsed / TOTAL) * 100);
  const busy = status === 'recording' || status === 'processing';
  const active = activeIdx >= 0 ? script[activeIdx] : null;

  return (
    <Screen icon="mic" title="Wake · Treino">
      <div style={{ maxWidth: 880, display: 'flex', flexDirection: 'column', gap: 16 }}>
        {/* Seletor de modo */}
        <div style={{ display: 'flex', gap: 8 }}>
          {(['pos', 'neg'] as Mode[]).map((m) => {
            const on = mode === m;
            return (
              <button
                key={m}
                onClick={() => switchMode(m)}
                disabled={busy}
                style={{
                  flex: 1,
                  padding: '10px 14px',
                  borderRadius: 10,
                  border: `1px solid ${on ? 'var(--neo-violet)' : 'var(--neo-hairline)'}`,
                  background: on ? 'var(--neo-violet-edge)' : 'var(--neo-surface)',
                  boxShadow: on ? '0 0 16px rgba(139, 92, 246, 0.22)' : 'none',
                  color: on ? 'var(--neo-text)' : 'var(--neo-text-muted)',
                  fontSize: 13.5,
                  fontWeight: 600,
                  fontFamily: 'inherit',
                  cursor: busy ? 'not-allowed' : 'pointer',
                  opacity: busy && !on ? 0.5 : 1,
                }}
              >
                {MODE_META[m].icon} {MODE_META[m].label}
              </button>
            );
          })}
        </div>

        {/* Explicação por modo */}
        <div
          style={{
            border: '1px solid var(--neo-hairline)',
            background: 'var(--neo-surface)',
            borderRadius: 12,
            padding: '12px 16px',
            color: 'var(--neo-text-muted)',
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          {mode === 'pos' ? (
            <>
              <strong style={{ color: 'var(--neo-text)' }}>Positivos ("ei neo").</strong> Leia cada linha em voz alta,
              respeitando a <strong>distância</strong> e o <strong>jeito</strong> indicados. A linha acende sozinha; siga
              o ritmo. Baixa como <code>{MODE_META.pos.file}</code>.
            </>
          ) : (
            <>
              <strong style={{ color: 'var(--neo-text)' }}>Negativos ("ei").</strong> Leia cada linha respeitando
              distância/jeito. <strong>Nunca diga "ei neo" aqui</strong> — é só "ei", confusores e fala comum. Baixa como{' '}
              <code>{MODE_META.neg.file}</code>.
            </>
          )}
        </div>

        {/* Timer + nível compacto */}
        <div
          style={{
            border: '1px solid var(--neo-hairline)',
            background: 'var(--neo-surface)',
            borderRadius: 12,
            padding: '12px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 16,
          }}
        >
          <div style={{ fontSize: 26, fontWeight: 700, color: 'var(--neo-text)', fontVariantNumeric: 'tabular-nums', minWidth: 120 }}>
            {fmt(elapsed)} <span style={{ fontSize: 14, color: 'var(--neo-text-faint)' }}>/ {fmt(TOTAL)}</span>
          </div>
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div style={{ height: 6, background: 'var(--neo-hairline)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${pct}%`, background: 'var(--neo-violet)', transition: 'width .1s linear' }} />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 10, color: 'var(--neo-text-faint)', width: 34 }}>nível</span>
              <div style={{ flex: 1, height: 8, background: 'var(--neo-hairline)', borderRadius: 4, overflow: 'hidden' }}>
                <div
                  style={{
                    height: '100%',
                    width: `${Math.round(level * 100)}%`,
                    background: level > 0.85 ? 'var(--neo-rose)' : 'var(--neo-green)',
                    transition: 'width .05s linear',
                  }}
                />
              </div>
            </div>
          </div>
          {status === 'recording' && (
            <span style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--neo-rose)', fontSize: 12, fontWeight: 600 }}>
              <span style={{ width: 10, height: 10, borderRadius: '50%', background: 'var(--neo-rose)', animation: 'neoPulse 1s infinite' }} />
              GRAVANDO
            </span>
          )}
        </div>

        {/* "Lendo agora" — destaque grande da linha ativa */}
        {active && (
          <div
            style={{
              border: '1px solid var(--neo-violet)',
              background: 'var(--neo-violet-edge)',
              boxShadow: '0 0 22px rgba(139, 92, 246, 0.25)',
              borderRadius: 12,
              padding: '16px 18px',
              display: 'flex',
              alignItems: 'center',
              gap: 16,
            }}
          >
            <span
              style={{
                fontSize: 11,
                fontWeight: 700,
                letterSpacing: 0.4,
                color: DIST_META[active.dist].color,
                border: `1px solid ${DIST_META[active.dist].color}`,
                borderRadius: 999,
                padding: '3px 10px',
                whiteSpace: 'nowrap',
              }}
            >
              {DIST_META[active.dist].label}
            </span>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 26, fontWeight: 700, color: 'var(--neo-text)', lineHeight: 1.15 }}>“{active.say}”</div>
              <div style={{ fontSize: 13, color: 'var(--neo-text-muted)', marginTop: 3 }}>{active.note}</div>
            </div>
            <span style={{ fontSize: 13, color: 'var(--neo-text-faint)', fontVariantNumeric: 'tabular-nums' }}>
              {activeIdx + 1}/{N}
            </span>
          </div>
        )}

        {/* Roteiro completo (teleprompter) */}
        <div
          style={{
            border: '1px solid var(--neo-hairline)',
            background: 'var(--neo-surface)',
            borderRadius: 12,
            maxHeight: 300,
            overflowY: 'auto',
          }}
        >
          {script.map((line, i) => {
            const isActive = i === activeIdx;
            const isDone = activeIdx >= 0 && i < activeIdx;
            return (
              <div
                key={i}
                ref={(el) => {
                  rowRefs.current[i] = el;
                }}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '9px 14px',
                  borderBottom: i < N - 1 ? '1px solid var(--neo-hairline)' : 'none',
                  background: isActive ? 'var(--neo-violet-edge)' : 'transparent',
                  opacity: isDone ? 0.4 : 1,
                  transition: 'all .15s',
                }}
              >
                <span style={{ fontSize: 11, color: 'var(--neo-text-faint)', width: 26, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {i + 1}
                </span>
                <span
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    letterSpacing: 0.3,
                    color: DIST_META[line.dist].color,
                    border: `1px solid ${DIST_META[line.dist].color}`,
                    borderRadius: 999,
                    padding: '2px 8px',
                    whiteSpace: 'nowrap',
                    minWidth: 118,
                    textAlign: 'center',
                  }}
                >
                  {DIST_META[line.dist].label}
                </span>
                <span style={{ flex: 1, fontSize: 15, fontWeight: isActive ? 700 : 500, color: 'var(--neo-text)' }}>
                  “{line.say}”
                </span>
                <span style={{ fontSize: 12, color: 'var(--neo-text-muted)', whiteSpace: 'nowrap' }}>{line.note}</span>
              </div>
            );
          })}
        </div>

        {/* Controles */}
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {(status === 'idle' || status === 'error') && (
            <button className="neo-btn-primary" onClick={start}>
              Começar a gravar
            </button>
          )}
          {status === 'recording' && (
            <button className="neo-btn-ghost" onClick={stop}>
              Parar agora
            </button>
          )}
          {status === 'processing' && <span style={{ color: 'var(--neo-text-muted)', fontSize: 13 }}>Processando áudio…</span>}
          {status === 'done' && wavUrl && (
            <>
              <a className="neo-btn-primary" href={wavUrl} download={MODE_META[mode].file} style={{ textDecoration: 'none' }}>
                Baixar {MODE_META[mode].file}
              </a>
              <audio src={wavUrl} controls style={{ height: 34 }} />
              <button className="neo-btn-ghost" onClick={reset}>
                Gravar de novo
              </button>
              <span style={{ fontSize: 12, color: 'var(--neo-text-faint)' }}>
                {(wavBytes / 1024).toFixed(0)} KB · 16 kHz mono
              </span>
            </>
          )}
        </div>

        {status === 'done' && (
          <div style={{ fontSize: 12.5, color: 'var(--neo-text-muted)', lineHeight: 1.5 }}>
            Pronto — o arquivo caiu em <code>~/Downloads/{MODE_META[mode].file}</code>. Me avise que eu levo pro PC gamer
            e retreino.
          </div>
        )}

        {error && (
          <div style={{ color: 'var(--neo-rose)', fontSize: 13, border: '1px solid var(--neo-rose)', borderRadius: 10, padding: '10px 12px' }}>
            {error}
          </div>
        )}
      </div>

      <style>{`@keyframes neoPulse { 0%,100% { opacity: 1 } 50% { opacity: .25 } }`}</style>
    </Screen>
  );
};

export default WakeTrainingPanel;
