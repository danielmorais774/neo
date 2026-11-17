import { useCallback, useEffect, useRef, useState } from 'react';
import type { WakeDetected } from '../lib/tauriApi';

/*
 * Teste de Wake — monitor ao vivo do score do detector (openWakeWord).
 *
 * O sidecar emite `wake-score` p/ cada frame tipo-wake (>= 0.25, inclui
 * near-misses abaixo do limiar); o Rust repassa e aqui a gente AGRUPA frames
 * próximos numa "fala" e mostra o PICO de cada uma, marcando as que dispararam
 * de verdade (`wake-detected`). Serve p/ afinar limiar e a regra de disparo:
 * fale "ei neo" e "ei" e veja os dois grupos de score se separarem.
 */

interface Utterance {
  id: number;
  peak: number;
  fired: boolean; // disparou de verdade ao vivo (regra atual: limiar + frames)
  ts: number;
}

const GAP_MS = 420; // silêncio que fecha uma "fala"
const DECAY_MS = 260; // sem score por isso => medidor volta a 0
const MAX_ROWS = 24;

const barColor = (v: number, thr: number) =>
  v >= thr ? '#34d399' : v >= thr - 0.12 ? '#fcd34d' : 'rgba(255,255,255,0.35)';

const WakeTestPanel = () => {
  const [current, setCurrent] = useState(0);
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [realThr, setRealThr] = useState(0.6);
  const [testThr, setTestThr] = useState(0.6);
  const [running, setRunning] = useState(false);
  const [testing, setTesting] = useState(false);

  const curRef = useRef<{ peak: number; lastTs: number; fired: boolean } | null>(null);
  const lastScoreTsRef = useRef(0);
  const idRef = useRef(1);

  const finalize = useCallback(() => {
    const u = curRef.current;
    if (!u) return;
    curRef.current = null;
    setUtterances((prev) => [{ id: idRef.current++, peak: u.peak, fired: u.fired, ts: Date.now() }, ...prev].slice(0, MAX_ROWS));
  }, []);

  // limiar real (só p/ referência; mudá-lo de verdade exige reiniciar o sidecar)
  useEffect(() => {
    window.electronAPI
      ?.wakeStatus?.()
      .then((s) => {
        if (typeof s?.threshold === 'number') {
          setRealThr(s.threshold);
          setTestThr(s.threshold);
        }
        setRunning(!!s?.running);
      })
      .catch(() => {});
  }, []);

  // assinaturas dos eventos — só enquanto o teste está ativo
  useEffect(() => {
    if (!testing) return;
    const offScore = window.electronAPI?.onWakeScore?.((d: WakeDetected) => {
      const now = Date.now();
      lastScoreTsRef.current = now;
      setCurrent(d.score);
      const cur = curRef.current;
      if (!cur || now - cur.lastTs > GAP_MS) {
        if (cur) finalize();
        curRef.current = { peak: d.score, lastTs: now, fired: false };
      } else {
        cur.peak = Math.max(cur.peak, d.score);
        cur.lastTs = now;
      }
    });
    // Em modo teste o disparo vem como `wake-test-fire` (não aciona o Live).
    const offFire = window.electronAPI?.onWakeTestFire?.((d: WakeDetected) => {
      const now = Date.now();
      if (curRef.current) {
        curRef.current.fired = true;
        curRef.current.peak = Math.max(curRef.current.peak, d.score);
        curRef.current.lastTs = now;
      } else {
        curRef.current = { peak: d.score, lastTs: now, fired: true };
      }
    });
    return () => {
      offScore?.();
      offFire?.();
    };
  }, [testing, finalize]);

  // timer: fecha falas ociosas e faz o medidor decair (só testando)
  useEffect(() => {
    if (!testing) return;
    const t = window.setInterval(() => {
      const now = Date.now();
      if (curRef.current && now - curRef.current.lastTs > GAP_MS) finalize();
      if (now - lastScoreTsRef.current > DECAY_MS) setCurrent((c) => (c === 0 ? c : 0));
    }, 120);
    return () => window.clearInterval(t);
  }, [testing, finalize]);

  // segurança: ao desmontar (fechar Configurações), desliga o modo teste —
  // senão o wake fica suprimido e o Live nunca mais acionaria.
  useEffect(() => {
    return () => {
      window.electronAPI?.wakeSetTestMode?.(false).catch(() => {});
    };
  }, []);

  const startTest = useCallback(() => {
    setUtterances([]);
    curRef.current = null;
    window.electronAPI?.wakeSetTestMode?.(true).catch(() => {});
    setTesting(true);
  }, []);

  const stopTest = useCallback(() => {
    window.electronAPI?.wakeSetTestMode?.(false).catch(() => {});
    setTesting(false);
    setCurrent(0);
  }, []);

  const firedLive = utterances.filter((u) => u.fired).length;
  const wouldPass = utterances.filter((u) => u.peak >= testThr).length;

  const surface = 'rgba(255,255,255,0.05)';
  const border = 'var(--border, rgba(255,255,255,0.12))';
  const textDim = 'rgba(255,255,255,0.6)';

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12, marginTop: 12 }}>
      <div style={{ fontSize: 12, color: textDim, lineHeight: 1.5 }}>
        Aperte <strong style={{ color: '#fff' }}>Iniciar teste</strong> e fale{' '}
        <strong style={{ color: '#fff' }}>"ei neo"</strong> e <strong style={{ color: '#fff' }}>"ei"</strong> algumas
        vezes. Cada fala tipo-wake vira uma linha com seu <strong style={{ color: '#fff' }}>pico</strong> de score.
        Enquanto o teste está ligado, o disparo <strong style={{ color: '#fff' }}>não aciona o Modo Live</strong> — igual
        testar o microfone no Discord.{' '}
        {running ? '' : '⚠️ O detector parece desligado — ative a palavra de despertar acima.'}
      </div>

      {/* botão iniciar / parar teste */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <button
          type="button"
          onClick={testing ? stopTest : startTest}
          style={{
            padding: '10px 20px',
            borderRadius: 10,
            border: `1px solid ${testing ? 'rgba(248,113,113,0.5)' : 'rgba(52,211,153,0.5)'}`,
            background: testing ? 'rgba(248,113,113,0.14)' : 'rgba(52,211,153,0.14)',
            color: testing ? '#fca5a5' : '#86efac',
            fontSize: 14,
            fontWeight: 700,
            fontFamily: 'inherit',
            cursor: 'pointer',
          }}
        >
          {testing ? '■ Parar teste' : '● Iniciar teste'}
        </button>
        {testing && (
          <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: '#fcd34d' }}>
            <span style={{ width: 9, height: 9, borderRadius: '50%', background: '#fcd34d', animation: 'neoBlink 1s infinite' }} />
            modo teste — o Live está mudo
          </span>
        )}
      </div>

      {/* medidor ao vivo */}
      <div style={{ border: `1px solid ${border}`, background: surface, borderRadius: 12, padding: 14 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 8 }}>
          <span style={{ fontSize: 12, color: textDim }}>score ao vivo</span>
          <span style={{ fontSize: 26, fontWeight: 700, color: '#fff', fontVariantNumeric: 'tabular-nums' }}>
            {current.toFixed(3)}
          </span>
        </div>
        <div style={{ position: 'relative', height: 12, background: 'rgba(255,255,255,0.08)', borderRadius: 6, overflow: 'hidden' }}>
          <div
            style={{
              height: '100%',
              width: `${Math.round(Math.min(1, current) * 100)}%`,
              background: barColor(current, testThr),
              transition: 'width .06s linear, background .1s',
            }}
          />
          {/* marca do limiar de teste */}
          <div style={{ position: 'absolute', top: -3, bottom: -3, left: `${testThr * 100}%`, width: 2, background: '#fff', opacity: 0.7 }} />
        </div>
      </div>

      {/* limiar de teste (cliente) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ fontSize: 12, color: textDim, whiteSpace: 'nowrap' }}>limiar de teste</span>
        <input
          type="range"
          min={0.2}
          max={0.9}
          step={0.01}
          value={testThr}
          onChange={(e) => setTestThr(parseFloat(e.target.value))}
          style={{ flex: 1, accentColor: '#34d399' }}
        />
        <span style={{ fontSize: 14, fontWeight: 700, color: '#fff', width: 44, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
          {testThr.toFixed(2)}
        </span>
      </div>
      <div style={{ fontSize: 11, color: textDim, marginTop: -6 }}>
        Limiar REAL em uso: <strong style={{ color: '#fff' }}>{realThr.toFixed(2)}</strong> · regra atual: 1 frame ≥ limiar.
        O slider aqui é só p/ achar o valor — mudar de verdade exige reiniciar o app com{' '}
        <code>NEO_WAKE_THRESHOLD</code>.
      </div>

      {/* resumo */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: textDim }}>
          {utterances.length} falas · <strong style={{ color: '#86efac' }}>{firedLive}</strong> dispararam ao vivo ·{' '}
          <strong style={{ color: '#fff' }}>{wouldPass}</strong> passariam de {testThr.toFixed(2)}
        </span>
        <button
          type="button"
          onClick={() => setUtterances([])}
          style={{
            marginLeft: 'auto',
            padding: '4px 10px',
            borderRadius: 8,
            border: `1px solid ${border}`,
            background: 'rgba(255,255,255,0.05)',
            color: 'var(--text, #fff)',
            fontSize: 12,
            cursor: 'pointer',
          }}
        >
          Limpar
        </button>
      </div>

      {/* lista de falas */}
      <div style={{ border: `1px solid ${border}`, borderRadius: 12, overflow: 'hidden', maxHeight: 300, overflowY: 'auto' }}>
        {utterances.length === 0 ? (
          <div style={{ padding: '18px 14px', fontSize: 12, color: textDim, textAlign: 'center' }}>
            {testing ? 'Aguardando… fale "ei neo" perto do mic.' : 'Aperte "Iniciar teste" para começar.'}
          </div>
        ) : (
          utterances.map((u) => {
            const pass = u.peak >= testThr;
            return (
              <div
                key={u.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '8px 12px',
                  borderBottom: `1px solid ${border}`,
                  background: u.fired ? 'rgba(52,211,153,0.06)' : 'transparent',
                }}
              >
                <span style={{ fontSize: 15, fontWeight: 700, color: '#fff', width: 52, fontVariantNumeric: 'tabular-nums' }}>
                  {u.peak.toFixed(3)}
                </span>
                <div style={{ flex: 1, height: 8, background: 'rgba(255,255,255,0.08)', borderRadius: 4, overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${Math.round(Math.min(1, u.peak) * 100)}%`, background: barColor(u.peak, testThr) }} />
                </div>
                {u.fired && (
                  <span style={{ fontSize: 11, fontWeight: 700, color: '#0b0b10', background: '#34d399', borderRadius: 999, padding: '2px 8px' }}>
                    ⚡ disparou
                  </span>
                )}
                <span style={{ fontSize: 11, fontWeight: 600, color: pass ? '#86efac' : '#fca5a5', width: 92, textAlign: 'right' }}>
                  {pass ? 'passa' : 'não passa'} {testThr.toFixed(2)}
                </span>
              </div>
            );
          })
        )}
      </div>

      <style>{`@keyframes neoBlink { 0%,100% { opacity: 1 } 50% { opacity: .25 } }`}</style>
    </div>
  );
};

export default WakeTestPanel;
