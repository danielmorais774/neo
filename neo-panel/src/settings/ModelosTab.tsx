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

const ModelosTab = ({ reloadToken }: { reloadToken: number }) => {
  const [config, setConfig] = useState<IntentConfig | null>(null);
  const [provider, setProvider] = useState('');
  const [modelChoice, setModelChoice] = useState(''); // a model id or CUSTOM_MODEL
  const [customModel, setCustomModel] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

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

  useEffect(() => {
    void fetchConfig();
  }, [reloadToken]);

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

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Classificador de intenção (Intent Router)</h2>
        <p className="neo-desc">
          Define qual provider e modelo de LLM classificam cada mensagem em rota de
          execução. Padrão: DeepSeek V4 Flash. A mudança vale para as próximas
          mensagens, sem reiniciar o neo-core.
        </p>

        {loading ? (
          <div className="neo-card"><div className="neo-muted">Carregando...</div></div>
        ) : !config ? (
          <div className="neo-card"><div className="neo-error">{error ?? 'Indisponível'}</div></div>
        ) : (
          <div className="neo-card">
            <div className="neo-status">
              <span>Ativo agora:</span>
              <span className="neo-status-value">
                {config.current.provider} · {config.current.model}
              </span>
            </div>

            <div className="neo-field">
              <label className="neo-field-label">Provider</label>
              <div className="neo-select-wrap">
                <select
                  className="neo-select"
                  value={provider}
                  onChange={(e) => onProviderChange(e.target.value)}
                >
                  {config.providers.map((p) => (
                    <option key={p.name} value={p.name}>
                      {(PROVIDER_LABELS[p.name] ?? p.name)}{p.available ? '' : ' — não configurado'}
                    </option>
                  ))}
                </select>
                <span className="neo-chevron">▼</span>
              </div>
              {providerInfo && !providerInfo.available && (
                <div className="neo-warn is-amber" style={{ marginTop: 10 }}>
                  Este provider não tem API key configurada no neo-core. Defina a
                  variável de ambiente correspondente para poder usá-lo.
                </div>
              )}
            </div>

            <div className="neo-field">
              <label className="neo-field-label">Modelo</label>
              <div className="neo-select-wrap">
                <select
                  className="neo-select"
                  value={modelChoice}
                  onChange={(e) => { setModelChoice(e.target.value); setSuccess(null); }}
                >
                  {(providerInfo?.models ?? []).map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                  <option value={CUSTOM_MODEL}>Outro (digitar)...</option>
                </select>
                <span className="neo-chevron">▼</span>
              </div>
              {modelChoice === CUSTOM_MODEL && (
                <input
                  className="neo-input"
                  placeholder="id do modelo (ex: gpt-4o-mini)"
                  value={customModel}
                  onChange={(e) => { setCustomModel(e.target.value); setSuccess(null); }}
                />
              )}
            </div>

            <div className="neo-actions">
              <button className="neo-btn-primary" onClick={save} disabled={!canSave}>
                {saving ? 'Salvando...' : 'Salvar'}
              </button>
              {isDirty && providerInfo?.available && (
                <span className="neo-note">Alteração não salva</span>
              )}
            </div>

            {error && <div className="neo-error">Erro: {error}</div>}
            {success && <div className="neo-ok">✓ {success}</div>}
          </div>
        )}
      </div>
    </div>
  );
};

export default ModelosTab;
