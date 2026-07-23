import { useEffect, useState } from 'react';
import Icon from '../ui/Icon';

const ENGINE_LABELS: Record<string, string> = {
  qwen: 'Qwen3-TTS',
  coqui: 'Coqui XTTS',
  cosyvoice: 'CosyVoice3',
  f5: 'F5-TTS (PT-BR)',
  chatterbox: 'Chatterbox ML',
  dia: 'Nari Dia (EN)',
};

interface VoiceEntry { name: string; voice: string; engine: string; is_clone: boolean; desc: string; }
interface VoiceCurrent { engine: string; voice: string | null; language: string | null; label: string | null; }
interface VoiceConfigData { current: VoiceCurrent; voices: VoiceEntry[]; engines: string[]; }

interface FallbackState {
  enabled: boolean;        // o toggle (env default ou override do painel)
  available: boolean;      // toggle E chave OpenRouter presente
  openrouter_key: boolean;
  model: string;
  env_default: boolean;
  runtime_override: boolean | null;
  engine_down: boolean;
}

interface TtsFallbackState {
  enabled: boolean;        // toggle do fallback de TTS
  available: boolean;      // toggle E pacote kokoro carregavel
  kokoro_ok: boolean;
  voice: string;
  lang: string;
  env_default: boolean;
  runtime_override: boolean | null;
  engine_down: boolean;
}

// Hedge = trocar a voz pelo Kokoro quando o motor escolhido está DEMORANDO.
// Desligado, a voz escolhida é usada a todo custo (só a FALHA do motor derruba
// pro fallback — isso é o toggle de cima).
interface TtsHedgeState {
  enabled: boolean;
  env_default: boolean;
  runtime_override: boolean | null;
  first_audio_hedge_s: number;
}

const Override = ({ override, envDefault }: { override: boolean | null; envDefault: boolean }) => (
  <div className="neo-meta-faint">
    Override do painel:{' '}
    {override === null ? `— (usando env: ${envDefault ? 'on' : 'off'})` : (override ? 'on' : 'off')}
  </div>
);

const VozTab = ({ reloadToken }: { reloadToken: number }) => {
  // --- Voz (TTS): catálogo vem do neo-voice (proxy /neo-voice); persiste no neo-core ---
  const [voiceCur, setVoiceCur] = useState<VoiceCurrent | null>(null);
  const [vEngine, setVEngine] = useState('');
  const [vVoice, setVVoice] = useState('');
  const [vList, setVList] = useState<{ id: string; label: string; clone: boolean }[]>([]);
  const [vSaving, setVSaving] = useState(false);
  const [vError, setVError] = useState<string | null>(null);
  const [vSuccess, setVSuccess] = useState<string | null>(null);

  // --- Fallback de STT (OpenRouter): controle live via neo-voice (proxy /neo-voice) ---
  const [fb, setFb] = useState<FallbackState | null>(null);
  const [fbSaving, setFbSaving] = useState(false);
  const [fbError, setFbError] = useState<string | null>(null);

  // --- Fallback de TTS (Kokoro local) ---
  const [fbTts, setFbTts] = useState<TtsFallbackState | null>(null);
  const [fbTtsSaving, setFbTtsSaving] = useState(false);
  const [fbTtsError, setFbTtsError] = useState<string | null>(null);
  const [hedge, setHedge] = useState<TtsHedgeState | null>(null);
  const [hedgeSaving, setHedgeSaving] = useState(false);
  const [hedgeError, setHedgeError] = useState<string | null>(null);

  const VOICE_ENGINES = Object.keys(ENGINE_LABELS);

  const mapVoices = (arr: VoiceEntry[] | undefined): { id: string; label: string; clone: boolean }[] =>
    (arr ?? []).map((v) => {
      const desc = (v as unknown as { desc?: string }).desc ?? '';
      const native = (v as unknown as { native?: string }).native ?? '';
      const clone = native === 'clone' || desc.startsWith('🎤');
      const label = clone ? (desc.replace(/^🎤\s*clone:\s*/, '').trim() || v.name) : v.name;
      return { id: v.name, label, clone };
    });

  // catálogo direto do neo-voice (browser no Mac → proxy /neo-voice → :5002)
  const loadVoicesFor = async (engine: string, preferred?: string | null) => {
    if (!engine) { setVList([]); return; }
    try {
      const res = await fetch(`/neo-voice/voices?backend=${encodeURIComponent(engine)}`);
      if (!res.ok) { setVList([]); setVVoice(''); return; }
      const data = await res.json();
      const list = mapVoices(data.voices);
      setVList(list);
      const want = preferred ?? vVoice;
      setVVoice(list.find((x) => x.id === want)?.id ?? data.default ?? list[0]?.id ?? '');
    } catch {
      setVList([]); setVVoice('');
    }
  };

  const fetchVoiceConfig = async () => {
    try {
      const res = await fetch('/api/v1/voice-config');
      if (!res.ok) return;
      const data = (await res.json()) as VoiceConfigData;
      setVoiceCur(data.current);
      const eng = data.current.engine || VOICE_ENGINES[0];
      setVEngine(eng);
      void loadVoicesFor(eng, data.current.voice);
    } catch { /* neo-core indisponível */ }
  };

  const fetchFallback = async () => {
    try {
      const res = await fetch('/neo-voice/stt/fallback');
      if (!res.ok) { setFb(null); return; }
      setFb((await res.json()) as FallbackState);
    } catch { setFb(null); /* neo-voice indisponível */ }
  };

  const fetchTtsFallback = async () => {
    try {
      const res = await fetch('/neo-voice/tts/fallback');
      if (!res.ok) { setFbTts(null); return; }
      setFbTts((await res.json()) as TtsFallbackState);
    } catch { setFbTts(null); /* neo-voice indisponível */ }
  };

  const fetchTtsHedge = async () => {
    try {
      const res = await fetch('/neo-voice/tts/hedge');
      if (!res.ok) { setHedge(null); return; }
      setHedge((await res.json()) as TtsHedgeState);
    } catch { setHedge(null); /* neo-voice indisponível */ }
  };

  useEffect(() => {
    void fetchVoiceConfig();
    void fetchFallback();
    void fetchTtsFallback();
    void fetchTtsHedge();
  }, [reloadToken]);

  const toggleFallback = async (next: boolean) => {
    setFbSaving(true); setFbError(null);
    try {
      const res = await fetch('/neo-voice/stt/fallback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) { setFbError(`HTTP ${res.status}`); return; }
      setFb((await res.json()) as FallbackState);
    } catch { setFbError('Falha ao falar com o neo-voice'); }
    finally { setFbSaving(false); }
  };

  const toggleTtsFallback = async (next: boolean) => {
    setFbTtsSaving(true); setFbTtsError(null);
    try {
      const res = await fetch('/neo-voice/tts/fallback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) { setFbTtsError(`HTTP ${res.status}`); return; }
      setFbTts((await res.json()) as TtsFallbackState);
    } catch { setFbTtsError('Falha ao falar com o neo-voice'); }
    finally { setFbTtsSaving(false); }
  };

  const toggleTtsHedge = async (next: boolean) => {
    setHedgeSaving(true); setHedgeError(null);
    try {
      const res = await fetch('/neo-voice/tts/hedge', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (!res.ok) { setHedgeError(`HTTP ${res.status}`); return; }
      setHedge((await res.json()) as TtsHedgeState);
    } catch { setHedgeError('Falha ao falar com o neo-voice'); }
    finally { setHedgeSaving(false); }
  };

  const onVEngineChange = (next: string) => {
    setVSuccess(null);
    setVEngine(next);
    void loadVoicesFor(next, null);
  };

  const vDirty = !!voiceCur && (vEngine !== voiceCur.engine || vVoice !== (voiceCur.voice ?? ''));
  const vCanSave = !!vEngine && !!vVoice && vDirty && !vSaving;

  const saveVoice = async () => {
    if (!vEngine || !vVoice) return;
    setVSaving(true); setVError(null); setVSuccess(null);
    try {
      const entry = vList.find((x) => x.id === vVoice);
      const res = await fetch('/api/v1/voice-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          engine: vEngine,
          voice: vVoice,
          language: voiceCur?.language ?? 'pt',
          label: entry?.label ?? null,
        }),
      });
      const data = await res.json();
      if (!res.ok) { setVError(data?.detail ?? `HTTP ${res.status}`); return; }
      const c = (data as VoiceConfigData).current;
      setVoiceCur(c);
      setVSuccess(`Voz do neo: ${ENGINE_LABELS[c.engine] ?? c.engine} · ${c.label ?? c.voice}`);
    } catch { setVError('Falha ao salvar'); }
    finally { setVSaving(false); }
  };

  return (
    <div className="neo-content">
      {/* ── Voz (TTS) ── */}
      <div className="neo-section">
        <h2 className="neo-h2">Voz do neo (TTS)</h2>
        <p className="neo-desc">
          Motor e voz que o neo usa pra falar. Vale para as próximas respostas (lido por
          requisição). As vozes vêm do neo-voice; o neo-core precisa alcançá-lo pra listar.
        </p>

        <div className="neo-card">
          <div className="neo-status">
            <span>Voz atual:</span>
            <span className="neo-status-value">
              {voiceCur
                ? `${ENGINE_LABELS[voiceCur.engine] ?? voiceCur.engine} · ${voiceCur.label ?? voiceCur.voice ?? '—'}`
                : '—'}
            </span>
          </div>

          <div className="neo-field">
            <label className="neo-field-label">Motor</label>
            <div className="neo-select-wrap">
              <select className="neo-select" value={vEngine} onChange={(e) => onVEngineChange(e.target.value)}>
                {VOICE_ENGINES.map((eng) => (
                  <option key={eng} value={eng}>{ENGINE_LABELS[eng] ?? eng}</option>
                ))}
              </select>
              <span className="neo-chevron">▼</span>
            </div>
          </div>

          <div className="neo-field">
            <label className="neo-field-label">Voz</label>
            <div className="neo-select-wrap">
              <select
                className="neo-select"
                value={vVoice}
                onChange={(e) => { setVVoice(e.target.value); setVSuccess(null); }}
              >
                {vList.length === 0 && <option value="">(engine offline / sem vozes)</option>}
                {vList.map((v) => (
                  <option key={v.id} value={v.id}>{v.label}{v.clone ? ' 🎤' : ''}</option>
                ))}
              </select>
              <span className="neo-chevron">▼</span>
            </div>
          </div>

          <div className="neo-actions">
            <button className="neo-btn-primary" onClick={saveVoice} disabled={!vCanSave}>
              {vSaving ? 'Salvando...' : 'Salvar voz'}
            </button>
            {vDirty && <span className="neo-note">Alteração não salva</span>}
          </div>

          {vError && <div className="neo-error">Erro: {vError}</div>}
          {vSuccess && <div className="neo-ok">✓ {vSuccess}</div>}
        </div>
      </div>

      {/* ── Fallback de STT (OpenRouter) ── */}
      <div className="neo-section">
        <h2 className="neo-h2">STT — Fallback OpenRouter</h2>
        <p className="neo-desc">
          Por padrão o STT usa o Parakeet local (PC gamer). Se ele cair, o neo-voice pode
          transcrever pela nuvem (OpenRouter). Desligue aqui pra impedir qualquer uso do
          OpenRouter — mesmo com o PC gamer offline.
        </p>

        <div className="neo-card">
          {!fb ? (
            <div className="neo-warn">
              <Icon name="warn" size={14} />
              neo-voice indisponível (não foi possível ler o estado do fallback).
            </div>
          ) : (
            <>
              <div className="neo-toggle-row">
                <div className="neo-toggle-label">
                  Fallback OpenRouter:{' '}
                  <span className={fb.enabled ? 'neo-state-on' : 'neo-state-off'}>
                    {fb.enabled ? 'LIGADO' : 'DESLIGADO'}
                  </span>
                  {fb.enabled && fb.engine_down && fb.available && (
                    <span className="neo-state-live">● em uso agora (PC gamer offline)</span>
                  )}
                </div>
                <button
                  className={`neo-toggle ${fb.enabled ? 'is-on' : 'is-off'}`}
                  onClick={() => void toggleFallback(!fb.enabled)}
                  disabled={fbSaving}
                >
                  {fbSaving ? '...' : fb.enabled ? '● LIGADO' : '○ DESLIGADO'}
                </button>
              </div>

              <div className="neo-meta">
                <div>Modelo: <span className="neo-meta-value">{fb.model}</span></div>
                <div>
                  Chave OpenRouter:{' '}
                  <span className={fb.openrouter_key ? 'neo-state-on' : 'neo-state-off'}>
                    {fb.openrouter_key ? 'configurada' : 'ausente'}
                  </span>
                </div>
                <Override override={fb.runtime_override} envDefault={fb.env_default} />
              </div>

              {fb.enabled && !fb.openrouter_key && (
                <div className="neo-warn is-amber" style={{ marginTop: 14 }}>
                  <Icon name="warn" size={14} />
                  Ligado, mas sem chave: o fallback não funciona até definir OPENROUTER_API_KEY no neo-voice.
                </div>
              )}
              {fbError && <div className="neo-error">Erro: {fbError}</div>}
            </>
          )}
        </div>
      </div>

      {/* ── Fallback de TTS (Kokoro local) ── */}
      <div className="neo-section">
        <h2 className="neo-h2">TTS — Fallback Kokoro</h2>
        <p className="neo-desc">
          Por padrão o neo fala pelos motores do PC gamer (Qwen/Coqui/F5…). Se eles caírem,
          o neo-voice sintetiza localmente com o Kokoro (roda na CPU do container). Desligue
          aqui pra impedir o uso do Kokoro — mesmo com os motores offline.
        </p>

        <div className="neo-card">
          {!fbTts ? (
            <div className="neo-warn">
              <Icon name="warn" size={14} />
              neo-voice indisponível (não foi possível ler o estado do fallback de TTS).
            </div>
          ) : (
            <>
              <div className="neo-toggle-row">
                <div className="neo-toggle-label">
                  Fallback Kokoro:{' '}
                  <span className={fbTts.enabled ? 'neo-state-on' : 'neo-state-off'}>
                    {fbTts.enabled ? 'LIGADO' : 'DESLIGADO'}
                  </span>
                  {fbTts.enabled && fbTts.engine_down && fbTts.available && (
                    <span className="neo-state-live">● em uso agora (motores offline)</span>
                  )}
                </div>
                <button
                  className={`neo-toggle ${fbTts.enabled ? 'is-on' : 'is-off'}`}
                  onClick={() => void toggleTtsFallback(!fbTts.enabled)}
                  disabled={fbTtsSaving}
                >
                  {fbTtsSaving ? '...' : fbTts.enabled ? '● LIGADO' : '○ DESLIGADO'}
                </button>
              </div>

              <div className="neo-meta">
                <div>Voz: <span className="neo-meta-value">{fbTts.voice} ({fbTts.lang})</span></div>
                <div>
                  Kokoro:{' '}
                  <span className={fbTts.kokoro_ok ? 'neo-state-on' : 'neo-state-off'}>
                    {fbTts.kokoro_ok ? 'carregado' : 'indisponível'}
                  </span>
                </div>
                <Override override={fbTts.runtime_override} envDefault={fbTts.env_default} />
              </div>

              {fbTts.enabled && !fbTts.kokoro_ok && (
                <div className="neo-warn is-amber" style={{ marginTop: 14 }}>
                  <Icon name="warn" size={14} />
                  Ligado, mas o pacote kokoro não carregou no neo-voice.
                </div>
              )}
              {fbTtsError && <div className="neo-error">Erro: {fbTtsError}</div>}
            </>
          )}
        </div>
      </div>

      {/* ── Hedge de TTS (trocar a voz quando o motor demora) ── */}
      <div className="neo-section">
        <h2 className="neo-h2">TTS — Trocar a voz quando demora</h2>
        <p className="neo-desc">
          Ligado, se o motor escolhido não entregar o 1º áudio a tempo, o Kokoro corre em
          paralelo e fala primeiro — a resposta sai <strong>com outra voz</strong>. Desligue
          para usar a voz escolhida <strong>a todo custo</strong>: o Kokoro fica{' '}
          <strong>proibido</strong> (nem por lentidão, nem por erro, nem com o motor offline)
          e o neo insiste no motor escolhido até conseguir — com o motor fora do ar, ele fica
          em silêncio em vez de falar com outra voz. Isto <strong>ignora o fallback acima</strong>.
        </p>

        <div className="neo-card">
          {!hedge ? (
            <div className="neo-warn">
              <Icon name="warn" size={14} />
              neo-voice indisponível (não foi possível ler o estado do hedge).
            </div>
          ) : (
            <>
              <div className="neo-toggle-row">
                <div className="neo-toggle-label">
                  Troca por lentidão:{' '}
                  <span className={hedge.enabled ? 'neo-state-on' : 'neo-state-off'}>
                    {hedge.enabled ? 'LIGADA' : 'DESLIGADA'}
                  </span>
                  {!hedge.enabled && (
                    <span className="neo-state-live">● voz escolhida a todo custo</span>
                  )}
                </div>
                <button
                  className={`neo-toggle ${hedge.enabled ? 'is-on' : 'is-off'}`}
                  onClick={() => void toggleTtsHedge(!hedge.enabled)}
                  disabled={hedgeSaving}
                >
                  {hedgeSaving ? '...' : hedge.enabled ? '● LIGADA' : '○ DESLIGADA'}
                </button>
              </div>

              <div className="neo-meta">
                <div>
                  Prazo do 1º áudio:{' '}
                  <span className="neo-meta-value">{hedge.first_audio_hedge_s}s</span>{' '}
                  (mínimo; o neo-voice estica conforme o motor)
                </div>
                <Override override={hedge.runtime_override} envDefault={hedge.env_default} />
              </div>

              {!hedge.enabled && (
                <div className="neo-warn is-amber" style={{ marginTop: 14 }}>
                  <Icon name="warn" size={14} />
                  Sem a troca, um motor lento atrasa a fala — o neo acumula áudio antes de
                  começar pra não picotar. No Qwen (~1,9× tempo real) isso pode passar de 30s.
                </div>
              )}
              {hedgeError && <div className="neo-error">Erro: {hedgeError}</div>}
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default VozTab;
