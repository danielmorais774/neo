import { useCallback, useEffect, useState } from 'react';

// Aba Serviços — Service Gateway (docs/service-gateway-dsl-plan.md §8, F2).
// Backend: neo-core/api/routes/connections.py.
//
// Três coisas moram aqui:
//  1. Conexões  — host + auth POR REFERÊNCIA + política (deny>confirm>allow).
//  2. Cofre     — credenciais cifradas (Fernet). O segredo é WRITE-ONLY: entra
//                 no cadastro e nunca volta; a tela só mostra rótulo/hint.
//  3. Auditoria — o que o Neo chamou, com que decisão de política e status.
//
// Conexões vindas do .env (ex.: homeassistant) são read-only aqui de propósito:
// quem manda nelas é a env do neo-core.
const API = '/api/v1/gateway';

type Effect = 'allow' | 'deny' | 'confirm';
type AuthType = 'none' | 'bearer' | 'basic' | 'header' | 'query_param';

interface PolicyRule {
  effect: Effect;
  methods: string[];
  path: string;
  description?: string | null;
}

interface Connection {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  auth_type: AuthType;
  auth_config: Record<string, unknown>;
  credential_ref: string | null;
  timeout_s: number;
  verify_tls: boolean;
  allow_http: boolean;
  default_read: Effect;
  default_write: Effect;
  policies: PolicyRule[];
  docs_url: string | null;
  openapi_spec: string | null;
  openapi_ref: string | null;
  enabled: boolean;
  managed_by: 'env' | 'panel';
  editable: boolean;
}

interface Credential {
  id: string;
  label: string;
  kind: string;
  description: string | null;
  hint: string | null;
  ref: string;
  last_used_at: string | null;
}

interface AuditEntry {
  id: string;
  ts: string;
  source: string;
  connection_id: string;
  method: string;
  path: string;
  policy_decision: string;
  policy_reason: string | null;
  status: number | null;
  duration_ms: number | null;
  error: string | null;
}

const COLORS = {
  bg: '#0f1117',
  card: '#13161f',
  border: '#1e2235',
  accent: '#7c3aed',
  accentSoft: '#a78bfa',
  muted: '#64748b',
  text: '#e2e8f0',
  danger: '#ef4444',
  ok: '#22c55e',
  warn: '#f59e0b',
};

const EFFECT_COLOR: Record<Effect, string> = {
  allow: COLORS.ok,
  confirm: COLORS.warn,
  deny: COLORS.danger,
};

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'];

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${res.status}`);
  return data as T;
}

const Badge = ({ children, color = COLORS.muted }: { children: React.ReactNode; color?: string }) => (
  <span
    style={{
      fontSize: 10,
      fontWeight: 600,
      textTransform: 'uppercase',
      letterSpacing: 0.4,
      color,
      border: `1px solid ${color}44`,
      background: `${color}18`,
      borderRadius: 4,
      padding: '2px 6px',
      whiteSpace: 'nowrap',
    }}
  >
    {children}
  </span>
);

const Button = ({
  children,
  onClick,
  variant = 'ghost',
  disabled,
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  title?: string;
}) => {
  const styles = {
    primary: { background: COLORS.accent, color: '#fff', border: `1px solid ${COLORS.accent}` },
    ghost: { background: 'transparent', color: COLORS.text, border: `1px solid ${COLORS.border}` },
    danger: { background: 'transparent', color: COLORS.danger, border: `1px solid ${COLORS.danger}55` },
  }[variant];
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        ...styles,
        borderRadius: 6,
        padding: '7px 14px',
        fontSize: 13,
        fontWeight: 500,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
      }}
    >
      {children}
    </button>
  );
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  background: COLORS.bg,
  border: `1px solid ${COLORS.border}`,
  borderRadius: 6,
  color: COLORS.text,
  padding: '8px 10px',
  fontSize: 13,
  fontFamily: 'inherit',
  boxSizing: 'border-box',
};

const labelStyle: React.CSSProperties = {
  fontSize: 11,
  color: COLORS.muted,
  textTransform: 'uppercase',
  letterSpacing: 0.4,
  marginBottom: 4,
  display: 'block',
};

const emptyDraft = (): Connection => ({
  id: '',
  name: '',
  provider: 'generic',
  base_url: '',
  auth_type: 'none',
  auth_config: {},
  credential_ref: null,
  timeout_s: 15,
  verify_tls: true,
  allow_http: false,
  default_read: 'allow',
  default_write: 'confirm',
  policies: [],
  docs_url: null,
  openapi_spec: null,
  openapi_ref: null,
  enabled: true,
  managed_by: 'panel',
  editable: true,
});

export default function ServicesPanel() {
  const [tab, setTab] = useState<'conexoes' | 'cofre' | 'auditoria'>('conexoes');
  const [connections, setConnections] = useState<Connection[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [draft, setDraft] = useState<Connection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [credsUnavailable, setCredsUnavailable] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const data = await api<{ connections: Connection[] }>(`${API}/connections`);
      setConnections(data.connections);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
    try {
      const data = await api<{ credentials: Credential[] }>(`${API}/credentials`);
      setCredentials(data.credentials);
      setCredsUnavailable(null);
    } catch (e) {
      // 503 = sem NEO_CRED_KEY. Não é erro da tela: é estado do servidor.
      setCredsUnavailable(String(e instanceof Error ? e.message : e));
    }
  }, []);

  const loadAudit = useCallback(async () => {
    try {
      const data = await api<{ entries: AuditEntry[] }>(`${API}/audit?limit=100`);
      setAudit(data.entries);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (tab === 'auditoria') void loadAudit();
  }, [tab, loadAudit]);

  const save = async () => {
    if (!draft) return;
    setError(null);
    try {
      await api(`${API}/connections/${draft.id}`, { method: 'PUT', body: JSON.stringify(draft) });
      setNotice(`Conexão '${draft.id}' salva.`);
      setDraft(null);
      await load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (slug: string) => {
    if (!confirm(`Apagar a conexão '${slug}'? O Neo perde acesso a esse serviço.`)) return;
    try {
      await api(`${API}/connections/${slug}`, { method: 'DELETE' });
      await load();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const test = async (slug: string) => {
    setTesting(slug);
    setNotice(null);
    setError(null);
    try {
      const r = await api<{ ok: boolean; status?: number; detail?: string; duration_ms?: number }>(
        `${API}/connections/${slug}/test`,
        { method: 'POST', body: JSON.stringify({ path: '/' }) },
      );
      if (r.ok) setNotice(`'${slug}' respondeu ${r.status} em ${r.duration_ms}ms.`);
      else setError(`'${slug}': ${r.detail ?? 'falhou'}`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setTesting(null);
    }
  };

  return (
    <div style={{ padding: 24, color: COLORS.text, fontSize: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <h1 style={{ fontSize: 20, margin: 0 }}>Serviços</h1>
        <Badge color={COLORS.accentSoft}>{connections.length} conexões</Badge>
        <div style={{ flex: 1 }} />
        <Button onClick={() => void load()}>Recarregar</Button>
        {tab === 'conexoes' && (
          <Button variant="primary" onClick={() => setDraft(emptyDraft())}>
            Nova conexão
          </Button>
        )}
      </div>

      <p style={{ color: COLORS.muted, fontSize: 12, marginTop: 0, marginBottom: 16, maxWidth: 780 }}>
        O Neo chama estes serviços por HTTP sem nunca ver a credencial: ele informa só o
        serviço e o path, e o gateway resolve host e autenticação. A política decide o que
        é leitura livre, o que pede confirmação no chat e o que é bloqueado.
      </p>

      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        {(['conexoes', 'cofre', 'auditoria'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              background: tab === t ? COLORS.accent : 'transparent',
              color: tab === t ? '#fff' : COLORS.muted,
              border: `1px solid ${tab === t ? COLORS.accent : COLORS.border}`,
              borderRadius: 6,
              padding: '6px 14px',
              fontSize: 13,
              cursor: 'pointer',
            }}
          >
            {t === 'conexoes' ? 'Conexões' : t === 'cofre' ? 'Cofre' : 'Auditoria'}
          </button>
        ))}
      </div>

      {error && (
        <div style={{ background: `${COLORS.danger}18`, border: `1px solid ${COLORS.danger}55`, color: COLORS.danger, borderRadius: 6, padding: '8px 12px', marginBottom: 12, fontSize: 13 }}>
          {error}
        </div>
      )}
      {notice && (
        <div style={{ background: `${COLORS.ok}18`, border: `1px solid ${COLORS.ok}55`, color: COLORS.ok, borderRadius: 6, padding: '8px 12px', marginBottom: 12, fontSize: 13 }}>
          {notice}
        </div>
      )}

      {tab === 'conexoes' && (
        <ConnectionsTab
          connections={connections}
          credentials={credentials}
          draft={draft}
          setDraft={setDraft}
          save={save}
          remove={remove}
          test={test}
          testing={testing}
        />
      )}
      {tab === 'cofre' && (
        <VaultTab
          credentials={credentials}
          unavailable={credsUnavailable}
          reload={load}
          setError={setError}
          setNotice={setNotice}
        />
      )}
      {tab === 'auditoria' && <AuditTab entries={audit} />}
    </div>
  );
}

// ── Conexões ────────────────────────────────────────────────────────────────

function ConnectionsTab({
  connections,
  credentials,
  draft,
  setDraft,
  save,
  remove,
  test,
  testing,
}: {
  connections: Connection[];
  credentials: Credential[];
  draft: Connection | null;
  setDraft: (c: Connection | null) => void;
  save: () => void;
  remove: (slug: string) => void;
  test: (slug: string) => void;
  testing: string | null;
}) {
  if (draft) {
    return (
      <ConnectionForm
        draft={draft}
        setDraft={setDraft}
        credentials={credentials}
        onSave={save}
        onCancel={() => setDraft(null)}
      />
    );
  }
  if (!connections.length) {
    return <Empty>Nenhuma conexão. Crie uma para o Neo alcançar um serviço novo.</Empty>;
  }
  return (
    <div style={{ display: 'grid', gap: 10 }}>
      {connections.map((c) => (
        <div key={c.id} style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <strong style={{ fontSize: 14 }}>{c.name || c.id}</strong>
            <code style={{ color: COLORS.accentSoft, fontSize: 12 }}>{c.id}</code>
            {!c.enabled && <Badge color={COLORS.muted}>desabilitada</Badge>}
            {c.managed_by === 'env' && <Badge color={COLORS.accentSoft}>via .env</Badge>}
            <Badge color={EFFECT_COLOR[c.default_read]}>ler: {c.default_read}</Badge>
            <Badge color={EFFECT_COLOR[c.default_write]}>escrever: {c.default_write}</Badge>
            {!c.verify_tls && <Badge color={COLORS.warn}>TLS off</Badge>}
            {c.openapi_spec && <Badge color={COLORS.accentSoft}>openapi</Badge>}
            <div style={{ flex: 1 }} />
            <Button onClick={() => test(c.id)} disabled={testing === c.id}>
              {testing === c.id ? 'testando…' : 'Testar'}
            </Button>
            {c.editable && <Button onClick={() => setDraft(c)}>Editar</Button>}
            {c.editable && <Button variant="danger" onClick={() => remove(c.id)}>Apagar</Button>}
          </div>
          <div style={{ color: COLORS.muted, fontSize: 12, marginTop: 6 }}>
            {c.base_url} · auth: {c.auth_type}
            {c.credential_ref ? ` (${c.credential_ref})` : ''} · {c.policies.length} regra(s)
          </div>
          {c.managed_by === 'env' && (
            <div style={{ color: COLORS.muted, fontSize: 11, marginTop: 6, fontStyle: 'italic' }}>
              Vem das variáveis de ambiente do neo-core — edite no .env, não aqui.
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ConnectionForm({
  draft,
  setDraft,
  credentials,
  onSave,
  onCancel,
}: {
  draft: Connection;
  setDraft: (c: Connection) => void;
  credentials: Credential[];
  onSave: () => void;
  onCancel: () => void;
}) {
  const set = (patch: Partial<Connection>) => setDraft({ ...draft, ...patch });
  const setRule = (i: number, patch: Partial<PolicyRule>) => {
    const policies = [...draft.policies];
    policies[i] = { ...policies[i], ...patch };
    set({ policies });
  };
  const needsCredential = draft.auth_type !== 'none';

  return (
    <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 18, maxWidth: 820 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div>
          <label style={labelStyle}>Slug (id)</label>
          <input
            style={inputStyle}
            value={draft.id}
            placeholder="github-personal"
            onChange={(e) => set({ id: e.target.value.toLowerCase() })}
          />
        </div>
        <div>
          <label style={labelStyle}>Nome</label>
          <input style={inputStyle} value={draft.name} placeholder="GitHub (pessoal)" onChange={(e) => set({ name: e.target.value })} />
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <label style={labelStyle}>Base URL (o Neo nunca informa host — vem daqui)</label>
        <input style={inputStyle} value={draft.base_url} placeholder="https://api.github.com" onChange={(e) => set({ base_url: e.target.value })} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
        <div>
          <label style={labelStyle}>Autenticação</label>
          <select style={inputStyle} value={draft.auth_type} onChange={(e) => set({ auth_type: e.target.value as AuthType })}>
            <option value="none">nenhuma</option>
            <option value="bearer">Bearer token</option>
            <option value="basic">Basic (usuário + senha)</option>
            <option value="header">Header custom (ex.: X-Api-Key)</option>
            <option value="query_param">Query param (desaconselhado)</option>
          </select>
        </div>
        <div>
          <label style={labelStyle}>Credencial (do cofre)</label>
          <select
            style={inputStyle}
            value={draft.credential_ref ?? ''}
            disabled={!needsCredential}
            onChange={(e) => set({ credential_ref: e.target.value || null })}
          >
            <option value="">— nenhuma —</option>
            {credentials.map((c) => (
              <option key={c.id} value={c.ref}>
                {c.label} {c.hint ? `(…${c.hint})` : ''}
              </option>
            ))}
          </select>
        </div>
      </div>

      {draft.auth_type === 'basic' && (
        <div style={{ marginTop: 12 }}>
          <label style={labelStyle}>Usuário (a senha é a credencial do cofre)</label>
          <input
            style={inputStyle}
            value={String(draft.auth_config?.username ?? '')}
            onChange={(e) => set({ auth_config: { ...draft.auth_config, username: e.target.value } })}
          />
        </div>
      )}
      {draft.auth_type === 'header' && (
        <div style={{ marginTop: 12 }}>
          <label style={labelStyle}>Nome do header</label>
          <input
            style={inputStyle}
            value={String(draft.auth_config?.header ?? 'X-Api-Key')}
            onChange={(e) => set({ auth_config: { ...draft.auth_config, header: e.target.value } })}
          />
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
        <div>
          <label style={labelStyle}>Leitura (GET/HEAD) por padrão</label>
          <select style={inputStyle} value={draft.default_read} onChange={(e) => set({ default_read: e.target.value as Effect })}>
            <option value="allow">liberada</option>
            <option value="confirm">pede confirmação</option>
            <option value="deny">bloqueada</option>
          </select>
        </div>
        <div>
          <label style={labelStyle}>Escrita (POST/PUT/…) por padrão</label>
          <select style={inputStyle} value={draft.default_write} onChange={(e) => set({ default_write: e.target.value as Effect })}>
            <option value="confirm">pede confirmação (recomendado)</option>
            <option value="allow">liberada</option>
            <option value="deny">bloqueada</option>
          </select>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 18, marginTop: 12, fontSize: 13 }}>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
          habilitada
        </label>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <input type="checkbox" checked={draft.verify_tls} onChange={(e) => set({ verify_tls: e.target.checked })} />
          verificar TLS
        </label>
        <label style={{ display: 'flex', gap: 6, alignItems: 'center' }} title="Marque só para serviço interno em http://">
          <input type="checkbox" checked={draft.allow_http} onChange={(e) => set({ allow_http: e.target.checked })} />
          permitir HTTP
        </label>
      </div>

      <div style={{ marginTop: 18 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <label style={{ ...labelStyle, marginBottom: 0 }}>Regras de política</label>
          <Badge color={COLORS.muted}>deny &gt; confirm &gt; allow</Badge>
          <div style={{ flex: 1 }} />
          <Button
            onClick={() => set({ policies: [...draft.policies, { effect: 'allow', methods: ['GET'], path: '/*' }] })}
          >
            + regra
          </Button>
        </div>
        <div style={{ color: COLORS.muted, fontSize: 11, margin: '6px 0 10px' }}>
          Um deny em qualquer posição vence qualquer allow. Sem regra que case, vale o padrão acima.
        </div>
        {draft.policies.map((r, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center' }}>
            <select
              style={{ ...inputStyle, width: 110 }}
              value={r.effect}
              onChange={(e) => setRule(i, { effect: e.target.value as Effect })}
            >
              <option value="allow">allow</option>
              <option value="confirm">confirm</option>
              <option value="deny">deny</option>
            </select>
            <select
              style={{ ...inputStyle, width: 110 }}
              value={r.methods[0] ?? '*'}
              onChange={(e) => setRule(i, { methods: [e.target.value] })}
            >
              <option value="*">qualquer</option>
              {METHODS.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
            <input
              style={{ ...inputStyle, flex: 1, fontFamily: 'ui-monospace, monospace' }}
              value={r.path}
              placeholder="/api/services/light/*"
              onChange={(e) => setRule(i, { path: e.target.value })}
            />
            <Button variant="danger" onClick={() => set({ policies: draft.policies.filter((_, j) => j !== i) })}>
              ×
            </Button>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 12 }}>
        <label style={labelStyle}>Documentação (URL) — o Neo consulta para montar as chamadas</label>
        <input style={inputStyle} value={draft.docs_url ?? ''} onChange={(e) => set({ docs_url: e.target.value || null })} />
      </div>

      <div style={{ marginTop: 12 }}>
        <label style={labelStyle}>
          OpenAPI / Swagger (cole o JSON ou YAML) — opcional
        </label>
        <div style={{ color: COLORS.muted, fontSize: 11, marginBottom: 6 }}>
          Com a spec aqui, o Neo descobre sozinho os endpoints deste serviço: ele busca as
          operações relevantes ao pedido em vez de você criar uma tool para cada uma. A spec
          nunca vai inteira para o modelo — só as operações que casam com a intenção.
        </div>
        <textarea
          style={{ ...inputStyle, minHeight: 120, fontFamily: 'ui-monospace, monospace', fontSize: 11 }}
          value={draft.openapi_spec ?? ''}
          placeholder='{"openapi": "3.0.0", "paths": {...}}'
          onChange={(e) => set({ openapi_spec: e.target.value || null, openapi_ref: e.target.value ? 'upload' : null })}
        />
      </div>

      <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
        <Button variant="primary" onClick={onSave} disabled={!draft.id || !draft.base_url}>
          Salvar
        </Button>
        <Button onClick={onCancel}>Cancelar</Button>
      </div>
    </div>
  );
}

// ── Cofre ───────────────────────────────────────────────────────────────────

function VaultTab({
  credentials,
  unavailable,
  reload,
  setError,
  setNotice,
}: {
  credentials: Credential[];
  unavailable: string | null;
  reload: () => Promise<void>;
  setError: (s: string | null) => void;
  setNotice: (s: string | null) => void;
}) {
  const [label, setLabel] = useState('');
  const [secret, setSecret] = useState('');
  const [kind, setKind] = useState('token');
  const [description, setDescription] = useState('');

  if (unavailable) {
    return (
      <Empty>
        Cofre indisponível. Defina <code style={{ color: COLORS.accentSoft }}>NEO_CRED_KEY</code> na
        env do neo-core e reinicie o container. Sem cofre, só conexões com credencial vinda do
        .env (<code style={{ color: COLORS.accentSoft }}>env://</code>) funcionam.
      </Empty>
    );
  }

  const create = async () => {
    setError(null);
    try {
      await api(`${API}/credentials`, {
        method: 'POST',
        body: JSON.stringify({ label, secret, kind, description: description || null }),
      });
      setNotice(`Credencial '${label}' guardada (cifrada).`);
      setLabel('');
      setSecret('');
      setDescription('');
      await reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (c: Credential) => {
    if (!confirm(`Apagar a credencial '${c.label}'?`)) return;
    try {
      await api(`${API}/credentials/${c.id}`, { method: 'DELETE' });
      await reload();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  return (
    <div style={{ display: 'grid', gap: 16, maxWidth: 820 }}>
      <div style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 18 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>Guardar credencial</div>
        <div style={{ color: COLORS.muted, fontSize: 12, marginBottom: 14 }}>
          O valor é cifrado com a NEO_CRED_KEY (que vive só na env da VPS) e{' '}
          <strong style={{ color: COLORS.text }}>nunca mais é exibido</strong> — nem aqui, nem para
          o Neo. Para trocar, cadastre de novo por cima.
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
          <div>
            <label style={labelStyle}>Rótulo</label>
            <input style={inputStyle} value={label} placeholder="github-personal" onChange={(e) => setLabel(e.target.value)} />
          </div>
          <div>
            <label style={labelStyle}>Tipo</label>
            <select style={inputStyle} value={kind} onChange={(e) => setKind(e.target.value)}>
              {['token', 'api_key', 'password', 'basic', 'cookie', 'cert', 'other'].map((k) => (
                <option key={k} value={k}>{k}</option>
              ))}
            </select>
          </div>
        </div>
        <div style={{ marginTop: 12 }}>
          <label style={labelStyle}>Segredo</label>
          <input
            style={{ ...inputStyle, fontFamily: 'ui-monospace, monospace' }}
            type="password"
            value={secret}
            placeholder="ghp_…"
            onChange={(e) => setSecret(e.target.value)}
          />
        </div>
        <div style={{ marginTop: 12 }}>
          <label style={labelStyle}>Descrição (opcional)</label>
          <input style={inputStyle} value={description} onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div style={{ marginTop: 14 }}>
          <Button variant="primary" onClick={create} disabled={!label || !secret}>
            Guardar
          </Button>
        </div>
      </div>

      {credentials.length === 0 ? (
        <Empty>Nenhuma credencial no cofre.</Empty>
      ) : (
        <div style={{ display: 'grid', gap: 8 }}>
          {credentials.map((c) => (
            <div key={c.id} style={{ background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 8, padding: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
              <strong style={{ fontSize: 13 }}>{c.label}</strong>
              <Badge>{c.kind}</Badge>
              {c.hint && <code style={{ color: COLORS.muted, fontSize: 12 }}>…{c.hint}</code>}
              <span style={{ color: COLORS.muted, fontSize: 12 }}>{c.description}</span>
              <div style={{ flex: 1 }} />
              <code style={{ color: COLORS.accentSoft, fontSize: 11 }}>{c.ref}</code>
              <Button variant="danger" onClick={() => remove(c)}>Apagar</Button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Auditoria ───────────────────────────────────────────────────────────────

const DECISION_COLOR: Record<string, string> = {
  allow: COLORS.ok,
  confirmed: COLORS.ok,
  deny: COLORS.danger,
  error: COLORS.danger,
  confirm_pending: COLORS.warn,
};

function AuditTab({ entries }: { entries: AuditEntry[] }) {
  if (!entries.length) return <Empty>Nenhuma chamada registrada ainda.</Empty>;
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ color: COLORS.muted, textAlign: 'left' }}>
            {['quando', 'serviço', 'chamada', 'decisão', 'status', 'ms', 'origem'].map((h) => (
              <th key={h} style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, fontWeight: 500 }}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {entries.map((e) => (
            <tr key={e.id}>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, color: COLORS.muted, whiteSpace: 'nowrap' }}>
                {new Date(e.ts).toLocaleString('pt-BR')}
              </td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}` }}>{e.connection_id}</td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, fontFamily: 'ui-monospace, monospace' }}>
                {e.method} {e.path}
              </td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}` }}>
                <Badge color={DECISION_COLOR[e.policy_decision] ?? COLORS.muted}>{e.policy_decision}</Badge>
                {e.policy_reason && <div style={{ color: COLORS.muted, fontSize: 11, marginTop: 2 }}>{e.policy_reason}</div>}
                {e.error && <div style={{ color: COLORS.danger, fontSize: 11, marginTop: 2 }}>{e.error}</div>}
              </td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, color: e.status && e.status < 400 ? COLORS.ok : COLORS.danger }}>
                {e.status ?? '—'}
              </td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, color: COLORS.muted }}>{e.duration_ms ?? '—'}</td>
              <td style={{ padding: '6px 8px', borderBottom: `1px solid ${COLORS.border}`, color: COLORS.muted }}>{e.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const Empty = ({ children }: { children: React.ReactNode }) => (
  <div style={{ background: COLORS.card, border: `1px dashed ${COLORS.border}`, borderRadius: 8, padding: 24, color: COLORS.muted, fontSize: 13, textAlign: 'center' }}>
    {children}
  </div>
);
