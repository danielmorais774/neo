import { useEffect, useMemo, useState } from 'react';

interface ProviderInfo {
  name: string;
  available: boolean;
  models: string[];
  suggested: string | null;
}

interface ModelPair {
  provider: string;
  model: string;
}

// Classifier endpoint (/api/v1/intent-config) — single role, its own shape.
interface IntentConfig {
  current: ModelPair;
  providers: ProviderInfo[];
  default: ModelPair;
}

// Multi-role endpoint (/api/v1/model-config) — memory planner + research.
interface RoleInfo {
  key: string;
  label: string;
  description: string;
  current: ModelPair;
  default: ModelPair;
  /** O slot precisa LER IMAGEM (o olho do navegador local). */
  vision?: boolean;
  /** Providers que ESTE slot aceita; vazio/ausente = todos. */
  only_providers?: string[];
}

interface ModelConfig {
  roles: RoleInfo[];
  providers: ProviderInfo[];
  /** Só os modelos com visão, por provider. Usado quando `role.vision`. */
  vision_models?: Record<string, string[]>;
}

const CUSTOM_MODEL = '__custom__';

/**
 * Lista de providers que um slot pode receber.
 *
 * Duas restrições, independentes: `vision` corta os modelos que não leem imagem
 * e `only_providers` corta os providers que aquele subsistema sequer alcança —
 * o `browser_agent` roda em outro container, com uma única chave, então "Groq"
 * ali seria um 404 que só apareceria dias depois, num job assíncrono.
 */
const providersFor = (
  role: RoleInfo,
  all: ProviderInfo[],
  vision: ProviderInfo[],
): ProviderInfo[] => {
  const list = role.vision ? vision : all;
  const only = role.only_providers;
  return only && only.length > 0 ? list.filter((p) => only.includes(p.name)) : list;
};

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

/**
 * One provider+model picker bound to a single "role" (classifier, memory
 * planner, research volume/writer). Self-contained: owns its edit state,
 * re-syncs when the persisted `current` changes, and delegates persistence to
 * `onSave` (each role has its own endpoint/shape).
 */
const RolePicker = ({
  title,
  description,
  providers,
  current,
  onSave,
  successLabel,
  visionOnly = false,
}: {
  title: string;
  description: string;
  providers: ProviderInfo[];
  current: ModelPair;
  onSave: (provider: string, model: string) => Promise<ModelPair>;
  successLabel: (c: ModelPair) => string;
  /** Só avisa: a lista já vem filtrada de fora (`providers`). */
  visionOnly?: boolean;
}) => {
  const [provider, setProvider] = useState(current.provider);
  const [modelChoice, setModelChoice] = useState(''); // a model id or CUSTOM_MODEL
  const [customModel, setCustomModel] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Sync edit state from the persisted config; decide known-model vs "custom".
  const applyPair = (prov: string, model: string) => {
    setProvider(prov);
    const known = providers.find((p) => p.name === prov)?.models ?? [];
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

  // Re-sync only when the persisted values actually change (after a save or a
  // parent refetch) — never mid-edit, so the user's unsaved choice survives.
  useEffect(() => {
    applyPair(current.provider, current.model);
    setSuccess(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current.provider, current.model]);

  const providerInfo = useMemo(
    () => providers.find((p) => p.name === provider),
    [providers, provider],
  );

  const onProviderChange = (next: string) => {
    setSuccess(null);
    setProvider(next);
    const info = providers.find((p) => p.name === next);
    const models = info?.models ?? [];
    const preferred =
      info?.suggested && models.includes(info.suggested) ? info.suggested : models[0];
    if (preferred) {
      setModelChoice(preferred);
      setCustomModel('');
    } else {
      setModelChoice(CUSTOM_MODEL);
      setCustomModel(info?.suggested ?? '');
    }
  };

  const resolvedModel = modelChoice === CUSTOM_MODEL ? customModel.trim() : modelChoice;

  const isDirty = provider !== current.provider || resolvedModel !== current.model;
  const canSave = !!provider && !!resolvedModel && !!providerInfo?.available && isDirty && !saving;

  const save = async () => {
    if (!resolvedModel) {
      setError('Informe um modelo');
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const saved = await onSave(provider, resolvedModel);
      applyPair(saved.provider, saved.model);
      setSuccess(successLabel(saved));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Falha ao salvar');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="neo-card">
      <h3 className="neo-h3">{title}</h3>
      <p className="neo-desc">{description}</p>

      <div className="neo-status">
        <span>Ativo agora:</span>
        <span className="neo-status-value">
          {current.provider} · {current.model}
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
            {providers.map((p) => (
              <option key={p.name} value={p.name}>
                {PROVIDER_LABELS[p.name] ?? p.name}
                {p.available ? '' : ' — não configurado'}
              </option>
            ))}
          </select>
          <span className="neo-chevron">▼</span>
        </div>
        {providerInfo && !providerInfo.available && (
          <div className="neo-warn is-amber" style={{ marginTop: 10 }}>
            Este provider não tem API key configurada no neo-core. Defina a variável de
            ambiente correspondente para poder usá-lo.
          </div>
        )}
      </div>

      <div className="neo-field">
        <label className="neo-field-label">Modelo</label>
        <div className="neo-select-wrap">
          <select
            className="neo-select"
            value={modelChoice}
            onChange={(e) => {
              setModelChoice(e.target.value);
              setSuccess(null);
            }}
          >
            {(providerInfo?.models ?? []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
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
            onChange={(e) => {
              setCustomModel(e.target.value);
              setSuccess(null);
            }}
          />
        )}
        {visionOnly && (
          <div className="neo-note" style={{ marginTop: 8 }}>
            Só modelos que leem imagem aparecem aqui. O DeepSeek não lê (a API responde
            400 <em>“does not support image”</em>) e escolhê-lo desligaria a visão sem
            avisar.
          </div>
        )}
      </div>

      <div className="neo-actions">
        <button className="neo-btn-primary" onClick={save} disabled={!canSave}>
          {saving ? 'Salvando...' : 'Salvar'}
        </button>
        {isDirty && providerInfo?.available && <span className="neo-note">Alteração não salva</span>}
      </div>

      {error && <div className="neo-error">Erro: {error}</div>}
      {success && <div className="neo-ok">✓ {success}</div>}
    </div>
  );
};

const ModelosTab = ({ reloadToken }: { reloadToken: number }) => {
  const [intent, setIntent] = useState<IntentConfig | null>(null);
  const [models, setModels] = useState<ModelConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = async () => {
    setLoading(true);
    setError(null);
    try {
      const [intentRes, modelsRes] = await Promise.all([
        fetch('/api/v1/intent-config'),
        fetch('/api/v1/model-config'),
      ]);
      if (!intentRes.ok) {
        setError(`HTTP ${intentRes.status}`);
        return;
      }
      setIntent(await intentRes.json());
      // /model-config is newer — degrade gracefully if the core predates it.
      setModels(modelsRes.ok ? await modelsRes.json() : null);
    } catch {
      setError('Falha ao conectar no neo-core');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void fetchAll();
  }, [reloadToken]);

  // Catálogo dos slots que precisam ENXERGAR. Um provider sem nenhum modelo de
  // visão some da lista em vez de aparecer vazio: oferecer "DeepSeek" para o
  // olho do navegador é oferecer uma escolha que cega o subsistema em silêncio.
  const visionProviders = useMemo<ProviderInfo[]>(() => {
    const byProvider = models?.vision_models;
    if (!models) return [];
    // Core antigo, sem `vision_models` no payload: melhor a lista inteira do que
    // um dropdown vazio — a rota ainda barra o modelo cego no save.
    if (!byProvider) return models.providers;
    return models.providers
      .map((p) => ({ ...p, models: byProvider[p.name] ?? [] }))
      .filter((p) => p.models.length > 0);
  }, [models]);

  // Persist a classifier change and reflect the new `current` locally.
  const saveIntent = async (provider: string, model: string): Promise<ModelPair> => {
    const res = await fetch('/api/v1/intent-config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data?.detail ?? `HTTP ${res.status}`);
    setIntent(data);
    return data.current as ModelPair;
  };

  // Persist a role change (memory planner / research) and reflect it locally.
  const saveRole = (roleKey: string) => async (provider: string, model: string): Promise<ModelPair> => {
    const res = await fetch(`/api/v1/model-config/${roleKey}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    });
    const data: ModelConfig = await res.json();
    if (!res.ok) throw new Error((data as { detail?: string })?.detail ?? `HTTP ${res.status}`);
    setModels(data);
    const updated = data.roles.find((r) => r.key === roleKey);
    if (!updated) throw new Error('Role não encontrado na resposta');
    return updated.current;
  };

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Modelos de LLM por subsistema</h2>
        <p className="neo-desc">
          Escolha qual provider e modelo cada subsistema do neo-core usa. As mudanças
          valem para as próximas mensagens/pesquisas, sem reiniciar o neo-core.
        </p>

        {loading ? (
          <div className="neo-card">
            <div className="neo-muted">Carregando...</div>
          </div>
        ) : !intent ? (
          <div className="neo-card">
            <div className="neo-error">{error ?? 'Indisponível'}</div>
          </div>
        ) : (
          <>
            <RolePicker
              title="Classificador de intenção (Intent Router)"
              description="Define qual LLM classifica cada mensagem em rota de execução. Padrão: DeepSeek V4 Flash."
              providers={intent.providers}
              current={intent.current}
              onSave={saveIntent}
              successLabel={(c) => `Classificador de intenção agora usa ${c.provider} · ${c.model}`}
            />

            {models?.roles.map((role) => (
              <RolePicker
                key={role.key}
                title={role.label}
                description={role.description}
                providers={providersFor(role, models.providers, visionProviders)}
                current={role.current}
                onSave={saveRole(role.key)}
                successLabel={(c) => `${role.label} agora usa ${c.provider} · ${c.model}`}
                visionOnly={role.vision}
              />
            ))}

            {error && <div className="neo-error">Erro: {error}</div>}
          </>
        )}
      </div>
    </div>
  );
};

export default ModelosTab;
