import { useEffect, useMemo, useState } from 'react';

interface ProviderInfo {
  name: string;
  available: boolean;
  models: string[];
  suggested: string | null;
}

interface IntentConfig {
  current: { provider: string; model: string };
  providers: ProviderInfo[];
  default: { provider: string; model: string };
}

const CUSTOM_MODEL = '__custom__';

const PROVIDER_LABELS: Record<string, string> = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  groq: 'Groq',
  gemini: 'Google Gemini',
  grok: 'xAI Grok',
  anthropic: 'Anthropic',
  openrouter: 'OpenRouter',
  ollama: 'Ollama (local)',
};

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

const card: React.CSSProperties = {
  background: '#13161f',
  border: '1px solid #1e2235',
  borderRadius: 8,
  padding: 20,
  maxWidth: 640,
};

const label: React.CSSProperties = {
  color: '#94a3b8',
  fontSize: 12,
  fontWeight: 600,
  textTransform: 'uppercase',
  letterSpacing: 0.5,
  marginBottom: 6,
  display: 'block',
};

const select: React.CSSProperties = {
  width: '100%',
  background: '#1e2235',
  border: '1px solid #2d3348',
  borderRadius: 6,
  padding: '10px 12px',
  color: '#e2e8f0',
  fontSize: 14,
};

const SettingsPanel = () => {
  const [config, setConfig] = useState<IntentConfig | null>(null);
  const [provider, setProvider] = useState('');
  const [modelChoice, setModelChoice] = useState(''); // a model id or CUSTOM_MODEL
  const [customModel, setCustomModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // --- Voz (TTS): catálogo vem do neo-voice (proxy /neo-voice); persiste no neo-core ---
  const [voiceCur, setVoiceCur] = useState<VoiceCurrent | null>(null);
  const [vEngine, setVEngine] = useState('');
  const [vVoice, setVVoice] = useState('');
  const [vList, setVList] = useState<{ id: string; label: string; clone: boolean }[]>([]);
  const [vSaving, setVSaving] = useState(false);
  const [vError, setVError] = useState<string | null>(null);
  const [vSuccess, setVSuccess] = useState<string | null>(null);

  useEffect(() => {
    void fetchConfig();
    void fetchVoiceConfig();
  }, []);

  const fetchConfig = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch('/api/v1/intent-config');
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      const data: IntentConfig = await res.json();
      setConfig(data);
      applyConfig(data, data.current.provider, data.current.model);
    } catch {
      setError('Falha ao conectar no neo-core');
    } finally {
      setLoading(false);
    }
  };

  // Set the provider + decide whether the model is a known one or "custom".
  const applyConfig = (data: IntentConfig, prov: string, model: string) => {
    setProvider(prov);
    const known = data.providers.find((p) => p.name === prov)?.models ?? [];
    if (model && known.includes(model)) {
      setModelChoice(model);
      setCustomModel('');
    } else if (model) {
      setModelChoice(CUSTOM_MODEL);
      setCustomModel(model);
    } else {
      setModelChoice(known[0] ?? CUSTOM_MODEL);
      setCustomModel('');
    }
  };

  const providerInfo = useMemo(
    () => config?.providers.find((p) => p.name === provider),
    [config, provider],
  );

  const onProviderChange = (next: string) => {
    setSuccess(null);
    setProvider(next);
    const info = config?.providers.find((p) => p.name === next);
    const models = info?.models ?? [];
    const preferred = info?.suggested && models.includes(info.suggested)
      ? info.suggested
      : models[0];
    if (preferred) {
      setModelChoice(preferred);
      setCustomModel('');
    } else {
      setModelChoice(CUSTOM_MODEL);
      setCustomModel(info?.suggested ?? '');
    }
  };

  const resolvedModel = modelChoice === CUSTOM_MODEL ? customModel.trim() : modelChoice;

  const isDirty =
    !!config &&
    (provider !== config.current.provider || resolvedModel !== config.current.model);

  const canSave =
    !!provider &&
    !!resolvedModel &&
    !!providerInfo?.available &&
    isDirty &&
    !saving;

  const save = async () => {
    if (!resolvedModel) {
      setError('Informe um modelo');
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const res = await fetch('/api/v1/intent-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider, model: resolvedModel }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data?.detail ?? `HTTP ${res.status}`);
        return;
      }
      setConfig(data);
      applyConfig(data, data.current.provider, data.current.model);
      setSuccess(`Classificador de intenção agora usa ${data.current.provider} · ${data.current.model}`);
    } catch {
      setError('Falha ao salvar');
    } finally {
      setSaving(false);
    }
  };

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
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', overflow: 'auto' }}>
      {/* Header */}
      <div style={{
        padding: '12px 16px',
        borderBottom: '1px solid #1e2235',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexShrink: 0,
      }}>
        <span style={{ color: '#a78bfa', fontWeight: 600, fontSize: 14 }}>
          ⚙️ Configurações
        </span>
        <button
          onClick={() => { void fetchConfig(); void fetchVoiceConfig(); }}
          style={{
            background: '#1e2235',
            border: '1px solid #2d3348',
            borderRadius: 4,
            padding: '4px 12px',
            color: '#e2e8f0',
            fontSize: 12,
            cursor: 'pointer',
          }}
        >
          Recarregar
        </button>
      </div>

      <div style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 20 }}>
        <div>
          <h2 style={{ color: '#e2e8f0', fontSize: 18, margin: '0 0 4px' }}>
            Classificador de intenção (Intent Router)
          </h2>
          <p style={{ color: '#64748b', fontSize: 13, margin: 0, maxWidth: 640 }}>
            Define qual provider e modelo de LLM classificam cada mensagem em rota de
            execução. Padrão: DeepSeek V4 Flash. A mudança vale para as próximas
            mensagens, sem reiniciar o neo-core.
          </p>
        </div>

        {loading ? (
          <div style={{ color: '#64748b' }}>Carregando...</div>
        ) : !config ? (
          <div style={{ color: '#ef4444' }}>{error ?? 'Indisponível'}</div>
        ) : (
          <div style={card}>
            {/* Active */}
            <div style={{ marginBottom: 20, fontSize: 13, color: '#94a3b8' }}>
              Ativo agora:{' '}
              <span style={{ color: '#34d399', fontWeight: 600 }}>
                {config.current.provider} · {config.current.model}
              </span>
            </div>

            {/* Provider */}
            <div style={{ marginBottom: 16 }}>
              <label style={label}>Provider</label>
              <select
                style={select}
                value={provider}
                onChange={(e) => onProviderChange(e.target.value)}
              >
                {config.providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {(PROVIDER_LABELS[p.name] ?? p.name)}{p.available ? '' : ' — não configurado'}
                  </option>
                ))}
              </select>
              {providerInfo && !providerInfo.available && (
                <div style={{ color: '#f59e0b', fontSize: 12, marginTop: 6 }}>
                  ⚠️ Este provider não tem API key configurada no neo-core. Defina a
                  variável de ambiente correspondente para poder usá-lo.
                </div>
              )}
            </div>

            {/* Model */}
            <div style={{ marginBottom: 20 }}>
              <label style={label}>Modelo</label>
              <select
                style={select}
                value={modelChoice}
                onChange={(e) => { setModelChoice(e.target.value); setSuccess(null); }}
              >
                {(providerInfo?.models ?? []).map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
                <option value={CUSTOM_MODEL}>Outro (digitar)...</option>
              </select>
              {modelChoice === CUSTOM_MODEL && (
                <input
                  style={{ ...select, marginTop: 8 }}
                  placeholder="id do modelo (ex: gpt-4o-mini)"
                  value={customModel}
                  onChange={(e) => { setCustomModel(e.target.value); setSuccess(null); }}
                />
              )}
            </div>

            {/* Save */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <button
                onClick={save}
                disabled={!canSave}
                style={{
                  background: canSave ? '#7c3aed' : '#2d3348',
                  border: 'none',
                  borderRadius: 6,
                  padding: '10px 20px',
                  color: canSave ? 'white' : '#64748b',
                  fontSize: 14,
                  fontWeight: 600,
                  cursor: canSave ? 'pointer' : 'not-allowed',
                }}
              >
                {saving ? 'Salvando...' : 'Salvar'}
              </button>
              {isDirty && providerInfo?.available && (
                <span style={{ color: '#64748b', fontSize: 12 }}>
                  Alteração não salva
                </span>
              )}
            </div>

            {error && (
              <div style={{ color: '#ef4444', fontSize: 13, marginTop: 14 }}>Erro: {error}</div>
            )}
            {success && (
              <div style={{ color: '#34d399', fontSize: 13, marginTop: 14 }}>✓ {success}</div>
            )}
          </div>
        )}

        {/* ── Voz (TTS) ── */}
        <div>
          <h2 style={{ color: '#e2e8f0', fontSize: 18, margin: '0 0 4px' }}>Voz do neo (TTS)</h2>
          <p style={{ color: '#64748b', fontSize: 13, margin: 0, maxWidth: 640 }}>
            Motor e voz que o neo usa pra falar. Vale para as próximas respostas (lido por
            requisição). As vozes vêm do neo-voice; o neo-core precisa alcançá-lo pra listar.
          </p>
        </div>
        <div style={card}>
          <div style={{ marginBottom: 20, fontSize: 13, color: '#94a3b8' }}>
            Voz atual:{' '}
            <span style={{ color: '#34d399', fontWeight: 600 }}>
              {voiceCur
                ? `${ENGINE_LABELS[voiceCur.engine] ?? voiceCur.engine} · ${voiceCur.label ?? voiceCur.voice ?? '—'}`
                : '—'}
            </span>
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={label}>Motor</label>
            <select style={select} value={vEngine} onChange={(e) => onVEngineChange(e.target.value)}>
              {VOICE_ENGINES.map((eng) => (
                <option key={eng} value={eng}>{ENGINE_LABELS[eng] ?? eng}</option>
              ))}
            </select>
          </div>

          <div style={{ marginBottom: 20 }}>
            <label style={label}>Voz</label>
            <select
              style={select}
              value={vVoice}
              onChange={(e) => { setVVoice(e.target.value); setVSuccess(null); }}
            >
              {vList.length === 0 && <option value="">(engine offline / sem vozes)</option>}
              {vList.map((v) => (
                <option key={v.id} value={v.id}>{v.label}{v.clone ? ' 🎤' : ''}</option>
              ))}
            </select>
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <button
              onClick={saveVoice}
              disabled={!vCanSave}
              style={{
                background: vCanSave ? '#7c3aed' : '#2d3348',
                border: 'none', borderRadius: 6, padding: '10px 20px',
                color: vCanSave ? 'white' : '#64748b', fontSize: 14, fontWeight: 600,
                cursor: vCanSave ? 'pointer' : 'not-allowed',
              }}
            >
              {vSaving ? 'Salvando...' : 'Salvar voz'}
            </button>
            {vDirty && <span style={{ color: '#64748b', fontSize: 12 }}>Alteração não salva</span>}
          </div>

          {vError && <div style={{ color: '#ef4444', fontSize: 13, marginTop: 14 }}>Erro: {vError}</div>}
          {vSuccess && <div style={{ color: '#34d399', fontSize: 13, marginTop: 14 }}>✓ {vSuccess}</div>}
        </div>
      </div>
    </div>
  );
};

export default SettingsPanel;
