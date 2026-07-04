// WakeWordPanel — cadastro das frases-gatilho do wake word (few-shot pela voz).
//
// Duas seções, mesmo fluxo (gravar ≥4 amostras → "Gerar referência" → ativa):
//   • Ativação (role=wake): a frase ("Ei Neo") ACENDE o Modo Live.
//   • Interromper (role=stop): a frase ("pare") INTERROMPE a fala/o turno no
//     Modo Live (barge-in). Tudo persiste via comandos Tauri (wake_*).
// Ver sidecar/wake/README.md e services/wake.rs.

import { useCallback, useEffect, useState } from 'react';
import type { WakePhrase } from '../lib/tauriApi';
import { recordPcm16k } from '../lib/wakeRecorder';
import './WakeWordPanel.css';

const RECORD_MS = 1500; // janela de 1.5s do EfficientWord-Net

type Role = 'wake' | 'stop';

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

  // Grava uma amostra e a associa à frase `text` com o papel `role`.
  // Retorna `true` em sucesso (a seção limpa o input).
  const recordFor = useCallback(async (text: string, role: Role): Promise<boolean> => {
    const t = text.trim();
    if (!t) {
      setError('Dê um nome à frase antes de gravar.');
      return false;
    }
    setError(null);
    setRecording(true);
    try {
      const samples = await recordPcm16k(RECORD_MS);
      const updated = await window.electronAPI.wakeAddSample(t, samples, role);
      setPhrases((prev) => upsert(prev, updated));
      return true;
    } catch (e) {
      setError(errMsg(e));
      return false;
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
      <PhraseSection
        role="wake"
        title="Ativação — acende o Modo Live"
        placeholder='Nome da frase (ex.: "Ei Neo")'
        phrases={phrases.filter((p) => p.role !== 'stop')}
        recording={recording}
        busySlug={busySlug}
        onRecord={recordFor}
        onEnroll={enroll}
        onRemove={remove}
      />

      <PhraseSection
        role="stop"
        title="Interromper — cala o Neo no Modo Live"
        placeholder='Nome da frase (ex.: "Pare")'
        phrases={phrases.filter((p) => p.role === 'stop')}
        recording={recording}
        busySlug={busySlug}
        onRecord={recordFor}
        onEnroll={enroll}
        onRemove={remove}
      />

      {error && <p className="wake-error">{error}</p>}
    </div>
  );
}

interface PhraseSectionProps {
  role: Role;
  title: string;
  placeholder: string;
  phrases: WakePhrase[];
  recording: boolean;
  busySlug: string | null;
  onRecord: (text: string, role: Role) => Promise<boolean>;
  onEnroll: (slug: string) => void;
  onRemove: (slug: string) => void;
}

function PhraseSection({
  role,
  title,
  placeholder,
  phrases,
  recording,
  busySlug,
  onRecord,
  onEnroll,
  onRemove,
}: PhraseSectionProps) {
  const [label, setLabel] = useState('');

  const record = useCallback(
    async (text: string) => {
      const ok = await onRecord(text, role);
      if (ok) setLabel('');
    },
    [onRecord, role],
  );

  return (
    <div className="wake-section">
      <h4 className="wake-section-title">{title}</h4>
      <div className="wake-newrow">
        <input
          className="wake-input"
          type="text"
          placeholder={placeholder}
          value={label}
          disabled={recording}
          onChange={(e) => setLabel(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !recording) void record(label);
          }}
        />
        <button
          type="button"
          className={`wake-rec-btn ${recording ? 'recording' : ''}`}
          disabled={recording}
          onClick={() => void record(label)}
        >
          {recording ? '● Gravando…' : '🎙 Gravar amostra'}
        </button>
      </div>
      <p className="wake-hint">
        {recording
          ? 'Fale a frase agora (1,5s).'
          : 'Clique e fale a frase logo em seguida. Grave 4–6 amostras por frase.'}
      </p>

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
                    onClick={() => void record(p.label)}
                  >
                    🎙 +1
                  </button>
                  <button
                    type="button"
                    className="wake-mini primary"
                    disabled={!enough || busy}
                    title={enough ? 'Gerar a referência (ativa o gatilho)' : `Precisa de ${p.minSamples} amostras`}
                    onClick={() => onEnroll(p.slug)}
                  >
                    {busy ? 'Gerando…' : p.enrolled ? 'Regerar' : 'Gerar referência'}
                  </button>
                  <button
                    type="button"
                    className="wake-mini danger"
                    disabled={busy}
                    title="Apagar a frase"
                    onClick={() => onRemove(p.slug)}
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
