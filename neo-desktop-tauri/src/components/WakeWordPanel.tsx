// WakeWordPanel — cadastro das frases-gatilho do wake word (few-shot pela voz).
//
// Fluxo: digite um rótulo ("Ei Neo") → grave ≥4 amostras (clica e fala a frase)
// → "Gerar referência" (enroll) ativa o detector. Tudo persiste via comandos
// Tauri (wake_*). Ver sidecar/wake/README.md e services/wake.rs.

import { useCallback, useEffect, useState } from 'react';
import type { WakePhrase } from '../lib/tauriApi';
import { recordPcm16k } from '../lib/wakeRecorder';
import './WakeWordPanel.css';

const RECORD_MS = 1500; // janela de 1.5s do EfficientWord-Net

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function upsert(list: WakePhrase[], p: WakePhrase): WakePhrase[] {
  const i = list.findIndex((x) => x.slug === p.slug);
  if (i < 0) return [...list, p];
  const copy = [...list];
  copy[i] = p;
  return copy;
}

export default function WakeWordPanel() {
  const [phrases, setPhrases] = useState<WakePhrase[]>([]);
  const [label, setLabel] = useState('');
  const [recording, setRecording] = useState(false);
  const [busySlug, setBusySlug] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await window.electronAPI.wakePhrases();
        if (!cancelled) setPhrases(list);
      } catch (e) {
        if (!cancelled) setError(errMsg(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const recordFor = useCallback(async (text: string) => {
    const t = text.trim();
    if (!t) {
      setError('Dê um nome à frase antes de gravar.');
      return;
    }
    setError(null);
    setRecording(true);
    try {
      const samples = await recordPcm16k(RECORD_MS);
      const updated = await window.electronAPI.wakeAddSample(t, samples);
      setPhrases((prev) => upsert(prev, updated));
      setLabel('');
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setRecording(false);
    }
  }, []);

  const enroll = useCallback(async (slug: string) => {
    setError(null);
    setBusySlug(slug);
    try {
      const updated = await window.electronAPI.wakeEnroll(slug);
      setPhrases((prev) => upsert(prev, updated));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setBusySlug(null);
    }
  }, []);

  const remove = useCallback(async (slug: string) => {
    setError(null);
    try {
      await window.electronAPI.wakeDeletePhrase(slug);
      setPhrases((prev) => prev.filter((p) => p.slug !== slug));
    } catch (e) {
      setError(errMsg(e));
    }
  }, []);

  return (
    <div className="wake-panel">
      <div className="wake-newrow">
        <input
          className="wake-input"
          type="text"
          placeholder='Nome da frase (ex.: "Ei Neo")'
          value={label}
          disabled={recording}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !recording) void recordFor(label);
          }}
        />
        <button
          type="button"
          className={`wake-rec-btn ${recording ? 'recording' : ''}`}
          disabled={recording}
          onClick={() => void recordFor(label)}
        >
          {recording ? '● Gravando…' : '🎙 Gravar amostra'}
        </button>
      </div>
      <p className="wake-hint">
        {recording
          ? 'Fale a frase agora (1,5s).'
          : 'Clique e fale a frase logo em seguida. Grave 4–6 amostras por frase.'}
      </p>

      {error && <p className="wake-error">{error}</p>}

      {phrases.length === 0 ? (
        <p className="wake-empty">Nenhuma frase cadastrada ainda.</p>
      ) : (
        <ul className="wake-list">
          {phrases.map((p) => {
            const enough = p.sampleCount >= p.minSamples;
            const busy = busySlug === p.slug;
            return (
              <li key={p.slug} className="wake-item">
                <div className="wake-item-main">
                  <strong>{p.label}</strong>
                  <small>
                    {p.sampleCount}/{p.minSamples} amostras
                    {p.enrolled ? ' · ✓ cadastrada' : ''}
                  </small>
                </div>
                <div className="wake-item-actions">
                  <button
                    type="button"
                    className="wake-mini"
                    disabled={recording}
                    title="Gravar mais uma amostra desta frase"
                    onClick={() => void recordFor(p.label)}
                  >
                    🎙 +1
                  </button>
                  <button
                    type="button"
                    className="wake-mini primary"
                    disabled={!enough || busy}
                    title={enough ? 'Gerar a referência (ativa o gatilho)' : `Precisa de ${p.minSamples} amostras`}
                    onClick={() => void enroll(p.slug)}
                  >
                    {busy ? 'Gerando…' : p.enrolled ? 'Regerar' : 'Gerar referência'}
                  </button>
                  <button
                    type="button"
                    className="wake-mini danger"
                    disabled={busy}
                    title="Apagar a frase"
                    onClick={() => void remove(p.slug)}
                  >
                    ✕
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
