import { useEffect, useState } from 'react';
import './ProvidersTab.css';

// CRUD do catálogo de LLM (neo-core /api/v1/llm/*). É a fonte do seletor do chat
// (desktop e painel) e dos dropdowns da aba Modelos. Toda gravação devolve o
// catálogo inteiro já atualizado, e o core remonta os providers na hora — vale no
// próximo turno, sem reiniciar.

interface Adapter {
  key: string;
  label: string;
  needs_key: boolean;
  base_url: 'required' | 'optional' | 'none';
}

interface KeyInfo {
  /** db = gravada aqui · env = .env do core · none = não precisa · null = nenhuma */
  source: 'db' | 'env' | 'none' | null;
  hint: string | null;
  stored: boolean;
  broken: boolean;
  env_var: string | null;
}

interface Model {
  id: string | null;
  model_id: string;
  label: string;
  description: string;
  vision: boolean;
  in_selector: boolean;
  position: number;
  notes: string;
  in_use: string[];
}

interface Provider {
  name: string;
  label: string;
  adapter: string;
  adapter_label: string;
  base_url: string | null;
  enabled: boolean;
  default_model: string | null;
  position: number;
  available: boolean;
  key: KeyInfo;
  in_use: string[];
  models: Model[];
}

interface Catalog {
  source: 'db' | 'seed';
  error: string | null;
  cred_key: boolean;
  adapters: Adapter[];
  providers: Provider[];
}

interface ProbeResult {
  ok: boolean;
  model: string | null;
  latency_ms?: number;
  reply?: string;
  error?: string;
}

type Notice = { kind: 'ok' | 'error'; text: string } | null;

async function api<T = Catalog>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/v1/llm${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    throw new Error(
      typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : `HTTP ${res.status}`,
    );
  }
  return data as T;
}

function move<T>(list: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= list.length) return list;
  const next = [...list];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

function keyText(key: KeyInfo, adapter?: Adapter): string {
  if (key.broken) return 'chave gravada aqui NÃO decifra (NEO_CRED_KEY trocada?) — grave de novo';
  switch (key.source) {
    case 'db':
      return `gravada aqui${key.hint ? ` (…${key.hint})` : ''}`;
    case 'env':
      return `do .env do core (${key.env_var})`;
    case 'none':
      return 'não precisa de chave';
    default:
      return adapter?.needs_key === false ? 'não precisa de chave' : 'nenhuma — o provider não é registrado';
  }
}

const ProvidersTab = ({ reloadToken }: { reloadToken: number }) => {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // Remonta o card (e zera os formulários dele) depois de uma gravação que deu
  // certo: o que está na tela passa a ser o que o core gravou.
  const [revision, setRevision] = useState<Record<string, number>>({});
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setCatalog(await api('GET', '/catalog'));
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Falha ao conectar no neo-core');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [reloadToken]);

  /**
   * Roda uma gravação; o resultado aparece no card `scope`. Devolve a mensagem de
   * erro (null = deu certo) para quem tem formulário aberto mostrá-la ALI — no
   * topo do card ela ficava fora da tela de quem editava um modelo lá embaixo.
   */
  const run = async (
    scope: string,
    action: () => Promise<Catalog>,
    okText: string,
  ): Promise<string | null> => {
    setBusy(scope);
    setNotices((n) => ({ ...n, [scope]: null }));
    try {
      setCatalog(await action());
      setRevision((r) => ({ ...r, [scope]: (r[scope] ?? 0) + 1 }));
      setNotices((n) => ({ ...n, [scope]: { kind: 'ok', text: okText } }));
      return null;
    } catch (e) {
      const text = e instanceof Error ? e.message : 'Falha ao salvar';
      setNotices((n) => ({ ...n, [scope]: { kind: 'error', text } }));
      return text;
    } finally {
      setBusy(null);
    }
  };

  if (loading && !catalog) {
    return (
      <div className="neo-content">
        <div className="neo-card">
          <div className="neo-muted">Carregando...</div>
        </div>
      </div>
    );
  }
  if (!catalog) {
    return (
      <div className="neo-content">
        <div className="neo-card">
          <div className="neo-error">{loadError ?? 'Indisponível'}</div>
        </div>
      </div>
    );
  }

  const readOnly = catalog.source !== 'db';
  const providers = catalog.providers;
  const adapters = catalog.adapters;

  return (
    <div className="neo-content">
      <div className="neo-section">
        <h2 className="neo-h2">Providers e modelos</h2>
        <p className="neo-desc">
          O que o Neo sabe usar. É daqui que saem o seletor do chat (desktop e painel) e as listas
          da aba Modelos. Só aparece no chat o provider ativo, com chave e com modelo marcado
          “no seletor”.
        </p>

        {readOnly && (
          <div className="neo-warn is-amber">
            Somente leitura: {catalog.error ?? 'o catálogo ainda não foi carregado do banco.'} Até
            lá o core usa o catálogo embutido no código.
          </div>
        )}
        {!catalog.cred_key && (
          <div className="neo-warn is-amber">
            O core está sem <code>NEO_CRED_KEY</code>: dá para editar providers e modelos, mas não
            para gravar chaves (elas são cifradas com ela).
          </div>
        )}

        {!readOnly && (
          <NewProvider
            adapters={adapters}
            busy={busy === '__new__'}
            notice={notices.__new__ ?? null}
            revision={revision.__new__ ?? 0}
            onCreate={(data) =>
              run(
                '__new__',
                () => api('POST', '/providers', data),
                `Provider '${data.name}' criado. Agora grave a chave e cadastre os modelos.`,
              ).then((error) => {
                if (!error) setExpanded((x) => ({ ...x, [data.name]: true }));
              })
            }
          />
        )}

        {providers.map((p, index) => (
          <ProviderCard
            key={`${p.name}:${revision[p.name] ?? 0}`}
            provider={p}
            adapters={adapters}
            readOnly={readOnly}
            canStoreKey={catalog.cred_key}
            expanded={!!expanded[p.name]}
            busy={busy === p.name}
            notice={notices[p.name] ?? null}
            first={index === 0}
            last={index === providers.length - 1}
            onToggle={() => setExpanded((x) => ({ ...x, [p.name]: !x[p.name] }))}
            onMove={(delta) =>
              run(
                p.name,
                () =>
                  api('PUT', '/providers/order', {
                    ids: move(providers, index, delta).map((x) => x.name),
                  }),
                'Ordem salva.',
              )
            }
            run={(action, okText) => run(p.name, action, okText)}
          />
        ))}
      </div>
    </div>
  );
};

// ── Novo provider ────────────────────────────────────────────────────────────

const NewProvider = ({
  adapters,
  busy,
  notice,
  revision,
  onCreate,
}: {
  adapters: Adapter[];
  busy: boolean;
  notice: Notice;
  revision: number;
  onCreate: (data: { name: string; label: string; adapter: string; base_url: string | null }) => void;
}) => {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [label, setLabel] = useState('');
  const [adapter, setAdapter] = useState('openai_compatible');
  const [baseUrl, setBaseUrl] = useState('');
  const spec = adapters.find((a) => a.key === adapter);

  // Depois de criar, o formulário volta limpo e fechado.
  const [seen, setSeen] = useState(revision);
  if (seen !== revision) {
    setSeen(revision);
    setOpen(false);
    setName('');
    setLabel('');
    setBaseUrl('');
  }

  if (!open) {
    return (
      <div className="prov-new-bar">
        <button className="neo-btn-ghost" onClick={() => setOpen(true)}>
          + Novo provider
        </button>
        {notice && <div className={notice.kind === 'ok' ? 'neo-ok' : 'neo-error'}>{notice.text}</div>}
      </div>
    );
  }

  return (
    <div className="neo-card">
      <h3 className="neo-h3">Novo provider</h3>
      <div className="prov-grid">
        <div className="neo-field">
          <label className="neo-field-label">Nome (slug, não muda depois)</label>
          <input
            className="neo-input"
            placeholder="together"
            value={name}
            onChange={(e) => setName(e.target.value.toLowerCase())}
          />
        </div>
        <div className="neo-field">
          <label className="neo-field-label">Rótulo</label>
          <input className="neo-input" placeholder="Together AI" value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
      </div>
      <div className="neo-field">
        <label className="neo-field-label">Tipo (como o core fala com a API)</label>
        <div className="neo-select-wrap">
          <select className="neo-select" value={adapter} onChange={(e) => setAdapter(e.target.value)}>
            {adapters.map((a) => (
              <option key={a.key} value={a.key}>
                {a.label}
              </option>
            ))}
          </select>
          <span className="neo-chevron">▼</span>
        </div>
      </div>
      {spec && spec.base_url !== 'none' && (
        <div className="neo-field">
          <label className="neo-field-label">
            Base URL {spec.base_url === 'required' ? '(obrigatório)' : '(opcional)'}
          </label>
          <input
            className="neo-input"
            placeholder="https://api.exemplo.com/v1"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
        </div>
      )}
      <div className="neo-actions">
        <button
          className="neo-btn-primary"
          disabled={busy || !name.trim()}
          onClick={() => onCreate({ name: name.trim(), label: label.trim(), adapter, base_url: baseUrl.trim() || null })}
        >
          {busy ? 'Criando...' : 'Criar'}
        </button>
        <button className="neo-btn-ghost" onClick={() => setOpen(false)}>
          Cancelar
        </button>
      </div>
      {notice?.kind === 'error' && <div className="neo-error">Erro: {notice.text}</div>}
    </div>
  );
};

// ── Card de um provider ──────────────────────────────────────────────────────

const ProviderCard = ({
  provider: p,
  adapters,
  readOnly,
  canStoreKey,
  expanded,
  busy,
  notice,
  first,
  last,
  onToggle,
  onMove,
  run,
}: {
  provider: Provider;
  adapters: Adapter[];
  readOnly: boolean;
  canStoreKey: boolean;
  expanded: boolean;
  busy: boolean;
  notice: Notice;
  first: boolean;
  last: boolean;
  onToggle: () => void;
  onMove: (delta: number) => void;
  run: (action: () => Promise<Catalog>, okText: string) => Promise<string | null>;
}) => {
  const adapter = adapters.find((a) => a.key === p.adapter);
  const [label, setLabel] = useState(p.label);
  const [baseUrl, setBaseUrl] = useState(p.base_url ?? '');
  const [defaultModel, setDefaultModel] = useState(p.default_model ?? '');
  const [keyInput, setKeyInput] = useState('');
  const [probeModel, setProbeModel] = useState(p.default_model ?? p.models[0]?.model_id ?? '');
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);

  const path = `/providers/${encodeURIComponent(p.name)}`;
  const dirty =
    label !== p.label || baseUrl !== (p.base_url ?? '') || defaultModel !== (p.default_model ?? '');
  const shownInChat = p.models.filter((m) => m.in_selector).length;

  const status = !p.enabled
    ? { cls: 'is-off', text: 'desligado' }
    : p.available
      ? { cls: 'is-on', text: 'ativo' }
      : { cls: 'is-warn', text: 'sem chave' };

  const testKey = async () => {
    setProbing(true);
    setProbe(null);
    try {
      setProbe(await api<ProbeResult>('POST', `${path}/test`, { model: probeModel || null }));
    } catch (e) {
      setProbe({ ok: false, model: probeModel, error: e instanceof Error ? e.message : 'falhou' });
    } finally {
      setProbing(false);
    }
  };

  return (
    <div className={`neo-card prov-card ${p.enabled ? '' : 'is-disabled'}`}>
      <div className="prov-head">
        <button className="prov-title" onClick={onToggle} aria-expanded={expanded}>
          <span className="prov-caret">{expanded ? '▾' : '▸'}</span>
          <span className="prov-name">{p.label}</span>
          <span className="prov-sub">
            {p.name} · {p.adapter_label} · {p.models.length} modelos ({shownInChat} no chat)
          </span>
        </button>
        <span className={`prov-pill ${status.cls}`}>{status.text}</span>
        {!readOnly && (
          <span className="prov-order">
            <button className="prov-icon-btn" disabled={first || busy} onClick={() => onMove(-1)} title="Subir">
              ↑
            </button>
            <button className="prov-icon-btn" disabled={last || busy} onClick={() => onMove(1)} title="Descer">
              ↓
            </button>
          </span>
        )}
      </div>

      <div className="prov-keyline">
        Chave: <span className={p.key.broken ? 'neo-state-off' : 'neo-meta-value'}>{keyText(p.key, adapter)}</span>
        {p.in_use.length > 0 && <span className="prov-uses"> · em uso por: {p.in_use.join(', ')}</span>}
      </div>

      {notice && <div className={notice.kind === 'ok' ? 'neo-ok' : 'neo-error'}>{notice.text}</div>}

      {expanded && (
        <div className="prov-body">
          {/* Dados do provider */}
          <div className="prov-grid">
            <div className="neo-field">
              <label className="neo-field-label">Rótulo</label>
              <input className="neo-input" value={label} disabled={readOnly} onChange={(e) => setLabel(e.target.value)} />
            </div>
            <div className="neo-field">
              <label className="neo-field-label">Modelo padrão (chamadas sem modelo)</label>
              <div className="neo-select-wrap">
                <select
                  className="neo-select"
                  value={defaultModel}
                  disabled={readOnly}
                  onChange={(e) => setDefaultModel(e.target.value)}
                >
                  <option value="">— nenhum —</option>
                  {p.models.map((m) => (
                    <option key={m.model_id} value={m.model_id}>
                      {m.model_id}
                    </option>
                  ))}
                </select>
                <span className="neo-chevron">▼</span>
              </div>
            </div>
          </div>
          {adapter && adapter.base_url !== 'none' && (
            <div className="neo-field">
              <label className="neo-field-label">
                Base URL {adapter.base_url === 'required' ? '(obrigatório)' : '(vazio = padrão)'}
              </label>
              <input
                className="neo-input"
                value={baseUrl}
                disabled={readOnly}
                placeholder="https://…"
                onChange={(e) => setBaseUrl(e.target.value)}
              />
            </div>
          )}
          {!readOnly && (
            <div className="neo-actions">
              <button
                className="neo-btn-primary"
                disabled={!dirty || busy}
                onClick={() =>
                  void run(
                    () =>
                      api('PUT', path, {
                        label,
                        default_model: defaultModel || null,
                        ...(adapter && adapter.base_url !== 'none' ? { base_url: baseUrl.trim() || null } : {}),
                      }),
                    'Provider salvo.',
                  )
                }
              >
                {busy ? 'Salvando...' : 'Salvar'}
              </button>
              <button
                className={`neo-toggle ${p.enabled ? 'is-on' : 'is-off'}`}
                disabled={busy}
                onClick={() =>
                  void run(
                    () => api('PUT', path, { enabled: !p.enabled }),
                    p.enabled ? 'Provider desligado.' : 'Provider ligado.',
                  )
                }
              >
                {p.enabled ? '● LIGADO' : '○ DESLIGADO'}
              </button>
            </div>
          )}

          {/* Chave */}
          <h4 className="prov-h4">Chave de API</h4>
          {!readOnly && (
            <div className="prov-key-row">
              <input
                className="neo-input"
                type="password"
                autoComplete="off"
                placeholder={p.key.stored ? 'nova chave (substitui a gravada)' : 'cole a chave aqui'}
                value={keyInput}
                disabled={!canStoreKey}
                onChange={(e) => setKeyInput(e.target.value)}
              />
              <button
                className="neo-btn-primary"
                disabled={!keyInput.trim() || busy || !canStoreKey}
                onClick={() => void run(() => api('PUT', `${path}/key`, { api_key: keyInput }), 'Chave gravada.')}
              >
                Gravar
              </button>
              {p.key.stored && (
                <button
                  className="neo-btn-ghost"
                  disabled={busy}
                  onClick={() => {
                    if (
                      window.confirm(
                        p.key.env_var
                          ? `Remover a chave gravada aqui? Volta a valer a do .env (${p.key.env_var}), se houver.`
                          : 'Remover a chave gravada? O provider deixa de funcionar.',
                      )
                    )
                      void run(() => api('DELETE', `${path}/key`), 'Chave removida.');
                  }}
                >
                  Remover
                </button>
              )}
            </div>
          )}
          <div className="prov-key-row">
            <div className="neo-select-wrap prov-grow">
              <select className="neo-select" value={probeModel} onChange={(e) => setProbeModel(e.target.value)}>
                {p.models.map((m) => (
                  <option key={m.model_id} value={m.model_id}>
                    {m.model_id}
                  </option>
                ))}
              </select>
              <span className="neo-chevron">▼</span>
            </div>
            <button className="neo-btn-ghost" disabled={probing || !p.available} onClick={() => void testKey()}>
              {probing ? 'Testando...' : 'Testar'}
            </button>
          </div>
          <div className="neo-note">
            Uma chamada real e curta com a chave em uso — gasta centavos.
            {!p.available && ' Indisponível: o provider não está registrado (desligado ou sem chave).'}
          </div>
          {probe &&
            (probe.ok ? (
              <div className="neo-ok">
                ✓ {probe.model} respondeu em {probe.latency_ms} ms: “{probe.reply || '(vazio — pode ter gasto os tokens raciocinando)'}”
              </div>
            ) : (
              <div className="neo-error">✗ {probe.model ?? ''} {probe.error}</div>
            ))}

          {/* Modelos */}
          <h4 className="prov-h4">Modelos</h4>
          <div className="prov-models">
            {p.models.map((m, index) => (
              <ModelRow
                key={m.id ?? m.model_id}
                model={m}
                readOnly={readOnly}
                busy={busy}
                isDefault={m.model_id === p.default_model}
                first={index === 0}
                last={index === p.models.length - 1}
                onMove={(delta) =>
                  void run(
                    () =>
                      api('PUT', `${path}/models/order`, {
                        ids: move(p.models, index, delta).map((x) => x.id),
                      }),
                    'Ordem salva.',
                  )
                }
                onSave={(data) => run(() => api('PUT', `/models/${m.id}`, data), `Modelo '${data.model_id}' salvo.`)}
                onDelete={() => {
                  if (window.confirm(`Apagar o modelo '${m.model_id}'?`))
                    void run(() => api('DELETE', `/models/${m.id}`), `Modelo '${m.model_id}' apagado.`);
                }}
              />
            ))}
            {p.models.length === 0 && <div className="neo-muted">Nenhum modelo cadastrado.</div>}
          </div>
          {!readOnly && (
            <AddModel
              busy={busy}
              onAdd={(data) => run(() => api('POST', `${path}/models`, data), `Modelo '${data.model_id}' adicionado.`)}
            />
          )}

          {!readOnly && (
            <div className="prov-danger">
              <button
                className="neo-btn-danger"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(`Apagar o provider '${p.name}' e os ${p.models.length} modelos dele?`))
                    void run(() => api('DELETE', path), `Provider '${p.name}' apagado.`);
                }}
              >
                Apagar provider
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// ── Modelos ──────────────────────────────────────────────────────────────────

interface ModelForm {
  model_id: string;
  label: string;
  description: string;
  vision: boolean;
  in_selector: boolean;
  notes: string;
}

const formOf = (m: Model): ModelForm => ({
  model_id: m.model_id,
  label: m.label,
  description: m.description,
  vision: m.vision,
  in_selector: m.in_selector,
  notes: m.notes,
});

const ModelFields = ({ form, onChange }: { form: ModelForm; onChange: (f: ModelForm) => void }) => (
  <>
    <div className="prov-grid">
      <div className="neo-field">
        <label className="neo-field-label">Id do modelo (vai para a API)</label>
        <input
          className="neo-input"
          placeholder="vendor/modelo@pin"
          value={form.model_id}
          onChange={(e) => onChange({ ...form, model_id: e.target.value })}
        />
      </div>
      <div className="neo-field">
        <label className="neo-field-label">Rótulo no chat</label>
        <input className="neo-input" value={form.label} onChange={(e) => onChange({ ...form, label: e.target.value })} />
      </div>
    </div>
    <div className="neo-field">
      <label className="neo-field-label">Descrição curta</label>
      <input
        className="neo-input"
        placeholder="Rápido, barato…"
        value={form.description}
        onChange={(e) => onChange({ ...form, description: e.target.value })}
      />
    </div>
    <div className="prov-checks">
      <label>
        <input type="checkbox" checked={form.in_selector} onChange={(e) => onChange({ ...form, in_selector: e.target.checked })} />
        Aparece no seletor do chat
      </label>
      <label>
        <input type="checkbox" checked={form.vision} onChange={(e) => onChange({ ...form, vision: e.target.checked })} />
        Lê imagem
      </label>
    </div>
    <div className="neo-field">
      <label className="neo-field-label">Notas (por que existe, o que foi medido)</label>
      <textarea
        className="neo-input prov-notes"
        value={form.notes}
        onChange={(e) => onChange({ ...form, notes: e.target.value })}
      />
    </div>
  </>
);

const ModelRow = ({
  model: m,
  readOnly,
  busy,
  isDefault,
  first,
  last,
  onMove,
  onSave,
  onDelete,
}: {
  model: Model;
  readOnly: boolean;
  busy: boolean;
  isDefault: boolean;
  first: boolean;
  last: boolean;
  onMove: (delta: number) => void;
  onSave: (data: ModelForm) => Promise<string | null>;
  onDelete: () => void;
}) => {
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<ModelForm>(() => formOf(m));
  const [error, setError] = useState<string | null>(null);

  if (editing) {
    return (
      <div className="prov-model is-editing">
        <ModelFields form={form} onChange={setForm} />
        {m.in_use.length > 0 && form.model_id.trim() !== m.model_id && (
          <div className="neo-note">
            Em uso por {m.in_use.join(', ')} — o id novo vai junto para lá.
          </div>
        )}
        <div className="neo-actions">
          <button
            className="neo-btn-primary"
            disabled={busy || !form.model_id.trim()}
            onClick={() =>
              void onSave(form).then((err) => {
                setError(err);
                if (!err) setEditing(false);
              })
            }
          >
            Salvar
          </button>
          <button
            className="neo-btn-ghost"
            onClick={() => {
              setForm(formOf(m));
              setError(null);
              setEditing(false);
            }}
          >
            Cancelar
          </button>
        </div>
        {error && <div className="neo-error">Não salvou: {error}</div>}
      </div>
    );
  }

  return (
    <div className="prov-model">
      <div className="prov-model-main">
        <span className="prov-model-label">{m.label || m.model_id}</span>
        {m.label && m.label !== m.model_id && <code className="prov-model-id">{m.model_id}</code>}
        {m.description && <span className="prov-model-desc">{m.description}</span>}
        {(m.in_use.length > 0 || m.notes) && (
          <span className="prov-model-desc">
            {m.in_use.length > 0 && `em uso por: ${m.in_use.join(', ')}`}
            {m.in_use.length > 0 && m.notes && ' · '}
            {m.notes && <span title={m.notes}>📝 notas</span>}
          </span>
        )}
      </div>
      <span className="prov-tags">
        {isDefault && <span className="prov-tag">padrão</span>}
        {m.in_selector && <span className="prov-tag is-chat">chat</span>}
        {m.vision && <span className="prov-tag">visão</span>}
      </span>
      {!readOnly && (
        <span className="prov-order">
          <button className="prov-icon-btn" disabled={first || busy} onClick={() => onMove(-1)} title="Subir">
            ↑
          </button>
          <button className="prov-icon-btn" disabled={last || busy} onClick={() => onMove(1)} title="Descer">
            ↓
          </button>
          <button className="prov-icon-btn" disabled={busy} onClick={() => setEditing(true)} title="Editar">
            ✎
          </button>
          <button className="prov-icon-btn is-danger" disabled={busy} onClick={onDelete} title="Apagar">
            ✕
          </button>
        </span>
      )}
    </div>
  );
};

const EMPTY_MODEL: ModelForm = {
  model_id: '',
  label: '',
  description: '',
  vision: false,
  in_selector: true,
  notes: '',
};

const AddModel = ({ busy, onAdd }: { busy: boolean; onAdd: (data: ModelForm) => Promise<string | null> }) => {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<ModelForm>(EMPTY_MODEL);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button className="neo-btn-ghost prov-add" onClick={() => setOpen(true)}>
        + Adicionar modelo
      </button>
    );
  }
  return (
    <div className="prov-model is-editing">
      <ModelFields form={form} onChange={setForm} />
      <div className="neo-actions">
        <button
          className="neo-btn-primary"
          disabled={busy || !form.model_id.trim()}
          onClick={() =>
            void onAdd({ ...form, label: form.label.trim() || form.model_id.trim() }).then((err) => {
              setError(err);
              if (!err) {
                setForm(EMPTY_MODEL);
                setOpen(false);
              }
            })
          }
        >
          Adicionar
        </button>
        <button className="neo-btn-ghost" onClick={() => setOpen(false)}>
          Cancelar
        </button>
      </div>
      {error && <div className="neo-error">Não adicionou: {error}</div>}
    </div>
  );
};

export default ProvidersTab;
