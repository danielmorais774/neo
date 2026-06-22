// VoiceEnginesPanel — liga/desliga os motores de STT/TTS que rodam na GPU do
// PC gamer, sem precisar abrir o SSH na mão. Cada linha mostra um motor com um
// status (online/offline, via /health) e botões Iniciar / Reiniciar / Parar,
// que disparam o engine_ctl*.ps1 correspondente por SSH (comandos no Rust).

import { useCallback, useEffect, useRef, useState } from 'react';
import { electronAPI, type VoiceEngine, type VoiceEngineAction } from '../lib/tauriApi';
import './VoiceEnginesPanel.css';

type EngineState = 'unknown' | 'up' | 'down';
type Busy = '' | VoiceEngineAction;

interface Row {
  engine: VoiceEngine;
  state: EngineState;
  busy: Busy;
}

interface LogEntry {
  key: string;
  label: string;
  text: string;
  error: boolean;
}

const STATE_LABEL: Record<EngineState, string> = {
  up: 'online',
  down: 'offline',
  unknown: '…',
};

export default function VoiceEnginesPanel() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [log, setLog] = useState<LogEntry | null>(null);

  // Espelho das linhas para o polling de health não recriar o setInterval.
  const rowsRef = useRef<Row[]>([]);
  rowsRef.current = rows;

  // Carrega a lista de motores uma vez.
  useEffect(() => {
    let alive = true;
    electronAPI
      .listVoiceEngines()
      .then((engines) => {
        if (!alive) return;
        setRows(engines.map((engine) => ({ engine, state: 'unknown', busy: '' })));
      })
      .catch((e) => {
        if (!alive) return;
        setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  const refreshHealth = useCallback(async () => {
    const current = rowsRef.current;
    if (current.length === 0) return;
    const results = await Promise.all(
      current.map(async (r) => {
        try {
          return [r.engine.key, await electronAPI.voiceEngineHealth(r.engine.key)] as const;
        } catch {
          return [r.engine.key, false] as const;
        }
      }),
    );
    const map = new Map<string, boolean>(results);
    setRows((prev) =>
      prev.map((r) =>
        map.has(r.engine.key) ? { ...r, state: map.get(r.engine.key) ? 'up' : 'down' } : r,
      ),
    );
  }, []);

  // Polling do status a cada 5s (e logo ao montar).
  useEffect(() => {
    if (rows.length === 0) return;
    void refreshHealth();
    const id = window.setInterval(() => void refreshHealth(), 5000);
    return () => window.clearInterval(id);
  }, [rows.length, refreshHealth]);

  const runAction = useCallback(
    async (engine: VoiceEngine, action: Exclude<VoiceEngineAction, 'status'>) => {
      setRows((prev) =>
        prev.map((r) => (r.engine.key === engine.key ? { ...r, busy: action } : r)),
      );
      setLog(null);
      try {
        const out = await electronAPI.voiceEngineAction(engine.key, action);
        setLog({ key: engine.key, label: engine.label, text: out, error: false });
      } catch (e) {
        setLog({
          key: engine.key,
          label: engine.label,
          text: e instanceof Error ? e.message : String(e),
          error: true,
        });
      } finally {
        setRows((prev) =>
          prev.map((r) => (r.engine.key === engine.key ? { ...r, busy: '' } : r)),
        );
        // Um `start` volta rápido, mas o modelo leva alguns segundos pra subir
        // e responder /health — re-checa agora e com atraso.
        void refreshHealth();
        window.setTimeout(() => void refreshHealth(), 4000);
        window.setTimeout(() => void refreshHealth(), 12000);
      }
    },
    [refreshHealth],
  );

  if (loadError) {
    return <p className="ve-error">Não foi possível listar os motores: {loadError}</p>;
  }

  return (
    <div className="ve-wrap">
      <div className="ve-list">
        {rows.map((r) => (
          <div className="ve-row" key={r.engine.key}>
            <span className={`ve-dot ve-dot-${r.state}`} aria-hidden="true" />
            <div className="ve-meta">
              <strong>{r.engine.label}</strong>
              <small>
                {r.engine.kind} · :{r.engine.port} · {STATE_LABEL[r.state]}
              </small>
            </div>
            <div className="ve-actions">
              <button
                type="button"
                className="ve-start"
                disabled={r.busy !== ''}
                title="Iniciar"
                onClick={() => void runAction(r.engine, 'start')}
              >
                {r.busy === 'start' ? '…' : 'Iniciar'}
              </button>
              <button
                type="button"
                disabled={r.busy !== ''}
                title="Reiniciar"
                aria-label="Reiniciar"
                onClick={() => void runAction(r.engine, 'restart')}
              >
                {r.busy === 'restart' ? '…' : '↻'}
              </button>
              <button
                type="button"
                className="ve-stop"
                disabled={r.busy !== ''}
                title="Parar"
                onClick={() => void runAction(r.engine, 'stop')}
              >
                {r.busy === 'stop' ? '…' : 'Parar'}
              </button>
            </div>
          </div>
        ))}
      </div>

      <p className="ve-hint">
        Os motores rodam na GPU do PC gamer. Após “Iniciar”, o modelo leva alguns segundos pra
        ficar online.
      </p>

      {log && (
        <pre className={`ve-log ${log.error ? 'error' : ''}`}>
          {log.label}
          {'\n'}
          {log.text}
        </pre>
      )}
    </div>
  );
}
