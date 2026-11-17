import { useCallback, useEffect, useState } from 'react';

// Aba Arquivos — catálogo do File Store (docs/file-store-plan.md, F3).
// Backend: neo-core/api/routes/files.py. Lista/gestão exigem panel JWT (o
// window.fetch de main.tsx já injeta o Bearer em toda chamada /api/*);
// download é rota aberta de propósito (mesmo nível do chat, §14.1 do plano) —
// por isso é um <a href> puro, não passa pelo helper `api()`.
const FILES_API = '/api/v1/files';

type Retention = 'temp' | 'perm';

interface FileEntry {
  id: string;
  name: string;
  mime: string | null;
  size_bytes: number;
  sha256: string;
  origin: string;
  retention: Retention;
  status: string;
  session_id: string | null;
  expires_at: string | null;
  created_at: string;
  last_accessed_at: string | null;
  access_count: number;
  meta: Record<string, unknown>;
}

interface Usage {
  bytes_used: number;
  quota_bytes: number;
  percent: number;
  temp: number;
  perm: number;
  expired: number;
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

const ORIGIN_LABEL: Record<string, string> = {
  'upload-desktop': 'Anexo desktop',
  'upload-panel': 'Upload painel',
  whatsapp: 'WhatsApp',
  url: 'URL',
  workbench: 'Sandbox',
  research: 'Pesquisa',
  job: 'Job',
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data?.detail?.message ?? data?.detail ?? `HTTP ${res.status}`);
  return data as T;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' });
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
  href,
  variant = 'ghost',
  disabled,
  title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  href?: string;
  variant?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  title?: string;
}) => {
  const styles = {
    primary: { background: COLORS.accent, color: '#fff', border: `1px solid ${COLORS.accent}` },
    ghost: { background: 'transparent', color: COLORS.text, border: `1px solid ${COLORS.border}` },
    danger: { background: 'transparent', color: COLORS.danger, border: `1px solid ${COLORS.danger}55` },
  }[variant];
  const style: React.CSSProperties = {
    ...styles,
    borderRadius: 6,
    padding: '5px 11px',
    fontSize: 12,
    fontWeight: 500,
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.45 : 1,
    textDecoration: 'none',
    display: 'inline-block',
    whiteSpace: 'nowrap',
  };
  if (href && !disabled) {
    return (
      <a href={href} title={title} style={style} download>
        {children}
      </a>
    );
  }
  return (
    <button onClick={onClick} disabled={disabled} title={title} style={style}>
      {children}
    </button>
  );
};

export default function FilesPanel() {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [retentionFilter, setRetentionFilter] = useState<'' | Retention>('');
  const [originFilter, setOriginFilter] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (q.trim()) params.set('q', q.trim());
      if (retentionFilter) params.set('retention', retentionFilter);
      if (originFilter) params.set('origin', originFilter);
      const [filesRes, usageRes] = await Promise.all([
        api<{ files: FileEntry[]; count: number }>(`${FILES_API}?${params}`),
        api<Usage>(`${FILES_API}/usage`),
      ]);
      setFiles(filesRes.files);
      setUsage(usageRes);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [q, retentionFilter, originFilter]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const flash = (msg: string) => {
    setNotice(msg);
    setTimeout(() => setNotice(null), 3000);
  };

  const toggleRetention = async (file: FileEntry) => {
    const next: Retention = file.retention === 'perm' ? 'temp' : 'perm';
    try {
      await api(`${FILES_API}/${file.id}`, { method: 'PATCH', body: JSON.stringify({ retention: next }) });
      await refresh();
      flash(`'${file.name}' agora é ${next === 'perm' ? 'permanente' : 'temporário'}.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (file: FileEntry) => {
    if (!window.confirm(`Apagar '${file.name}' do catálogo? Não dá pra desfazer.`)) return;
    try {
      await api(`${FILES_API}/${file.id}`, { method: 'DELETE' });
      await refresh();
      flash(`'${file.name}' apagado.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16, padding: 24, overflow: 'auto', flex: 1, boxSizing: 'border-box', color: COLORS.text }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
        <div>
          <h2 style={{ margin: 0, fontSize: 18 }}>Arquivos</h2>
          <p style={{ margin: '4px 0 0', fontSize: 12.5, color: COLORS.muted, maxWidth: 560 }}>
            Catálogo de arquivos do Neo — o que você anexou e o que ele salvou (relatórios, jobs, código gerado) para você baixar.
          </p>
        </div>
        <Button onClick={refresh} disabled={loading}>{loading ? 'Carregando…' : 'Atualizar'}</Button>
      </div>

      {notice && (
        <div style={{ background: `${COLORS.ok}18`, border: `1px solid ${COLORS.ok}44`, color: COLORS.ok, borderRadius: 8, padding: '8px 12px', fontSize: 12.5 }}>
          {notice}
        </div>
      )}
      {error && (
        <div style={{ background: `${COLORS.danger}18`, border: `1px solid ${COLORS.danger}44`, color: COLORS.danger, borderRadius: 8, padding: '8px 12px', fontSize: 12.5 }}>
          {error}
        </div>
      )}

      {usage && (
        <div style={{ display: 'flex', gap: 16, alignItems: 'center', background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 10, padding: '12px 16px' }}>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, color: COLORS.muted, marginBottom: 6 }}>
              <span>{formatBytes(usage.bytes_used)} usados de {formatBytes(usage.quota_bytes)}</span>
              <span>{usage.percent}%</span>
            </div>
            <div style={{ height: 6, borderRadius: 3, background: COLORS.bg, overflow: 'hidden' }}>
              <div
                style={{
                  height: '100%',
                  width: `${Math.min(100, usage.percent)}%`,
                  background: usage.percent > 90 ? COLORS.danger : COLORS.accent,
                }}
              />
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <Badge color={COLORS.ok}>{usage.perm} perm</Badge>
            <Badge color={COLORS.warn}>{usage.temp} temp</Badge>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar por nome…"
          style={{
            flex: 1, background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 6,
            color: COLORS.text, padding: '7px 10px', fontSize: 13, fontFamily: 'inherit', boxSizing: 'border-box',
          }}
        />
        <select
          value={retentionFilter}
          onChange={(e) => setRetentionFilter(e.target.value as '' | Retention)}
          style={{
            background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 6,
            color: COLORS.text, padding: '7px 10px', fontSize: 13, cursor: 'pointer',
          }}
        >
          <option value="">Toda retenção</option>
          <option value="perm">Permanente</option>
          <option value="temp">Temporário</option>
        </select>
        <select
          value={originFilter}
          onChange={(e) => setOriginFilter(e.target.value)}
          style={{
            background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 6,
            color: COLORS.text, padding: '7px 10px', fontSize: 13, cursor: 'pointer',
          }}
        >
          <option value="">Toda origem</option>
          {Object.entries(ORIGIN_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </div>

      {!loading && files.length === 0 && !error && (
        <div style={{ textAlign: 'center', color: COLORS.muted, padding: '40px 0', fontSize: 13 }}>
          Nenhum arquivo no catálogo. Anexe algo no chat ou peça pro Neo salvar um resultado.
        </div>
      )}

      {files.length > 0 && (
        <div style={{ border: `1px solid ${COLORS.border}`, borderRadius: 10, overflow: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12.5 }}>
            <thead>
              <tr style={{ background: COLORS.card, textAlign: 'left', color: COLORS.muted }}>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Nome</th>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Tamanho</th>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Origem</th>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Retenção</th>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Criado</th>
                <th style={{ padding: '9px 12px', fontWeight: 500 }}>Ações</th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.id} style={{ borderTop: `1px solid ${COLORS.border}` }}>
                  <td style={{ padding: '9px 12px', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.name}>
                    {f.name}
                  </td>
                  <td style={{ padding: '9px 12px', color: COLORS.muted, whiteSpace: 'nowrap' }}>{formatBytes(f.size_bytes)}</td>
                  <td style={{ padding: '9px 12px' }}>
                    <Badge>{ORIGIN_LABEL[f.origin] ?? f.origin}</Badge>
                  </td>
                  <td style={{ padding: '9px 12px' }}>
                    <Badge color={f.retention === 'perm' ? COLORS.ok : COLORS.warn}>
                      {f.retention === 'perm' ? 'perm' : 'temp'}
                    </Badge>
                  </td>
                  <td style={{ padding: '9px 12px', color: COLORS.muted, whiteSpace: 'nowrap' }}>{formatDate(f.created_at)}</td>
                  <td style={{ padding: '9px 12px' }}>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <Button href={`${FILES_API}/${f.id}/download`} title="Baixar">Baixar</Button>
                      <Button onClick={() => toggleRetention(f)} title="Alternar retenção">
                        {f.retention === 'perm' ? 'Tornar temp' : 'Tornar perm'}
                      </Button>
                      <Button variant="danger" onClick={() => remove(f)} title="Apagar do catálogo">Excluir</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
