import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { currentUserId } from './lib/auth';

/*
 * Tela "Dados" — CRUD GENÉRICO dos cadastros generativos.
 * Backend: neo-core/api/routes/datasets.py · plano: neo-core/docs/datasets-crud-plan.md
 *
 * Não há nada aqui específico de "produtos" ou "clientes": a lista, o
 * formulário e a validação saem todos do `schema` que o backend devolve. Um
 * cadastro novo aparece sozinho — é isso que faz o ciclo "descrevi a skill →
 * tenho tela" fechar sem código de frente.
 *
 * Regra de degradação (§5.1 do plano): tipo que o backend conhece e esta tela
 * não vira um input de texto. O backend evolui primeiro; a tela nunca quebra.
 */

const API = '/api/v1/datasets';

type FieldType =
  | 'texto' | 'texto_longo' | 'inteiro' | 'numero' | 'dinheiro' | 'booleano'
  | 'data' | 'data_hora' | 'opcao' | 'multi_opcao' | 'relacao' | 'arquivo' | 'json';

interface FieldDef {
  name: string;
  type: FieldType;
  label: string;
  required: boolean;
  max: number | null;
  options: string[];
  dataset: string | null;
  default: unknown;
  help: string;
}

interface DatasetSchema {
  version: number;
  fields: FieldDef[];
  list_columns: string[];
  search_fields: string[];
  order_by: string;
  label_field: string;
}

interface Dataset {
  slug: string;
  title: string;
  description: string;
  table_name: string;
  skill_name: string | null;
  enabled: boolean;
  schema: DatasetSchema;
  total_linhas?: number;
}

type Row = Record<string, unknown> & { id: string };

interface RowsPayload {
  cadastro: string;
  total: number;
  pagina: number;
  por_pagina: number;
  tem_mais: boolean;
  linhas: Row[];
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

const PAGE_SIZE = 25;

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error((data as { detail?: string })?.detail ?? `HTTP ${res.status}`);
  return data as T;
}

const withUser = (path: string, extra: Record<string, string | number | undefined> = {}) => {
  const dono = currentUserId();
  const params = new URLSearchParams(dono ? { user_id: dono } : {});
  for (const [k, v] of Object.entries(extra)) {
    if (v !== undefined && v !== '') params.set(k, String(v));
  }
  return `${path}?${params.toString()}`;
};

// ── formatação ───────────────────────────────────────────────────────────────

/** Centavos inteiros → "R$ 49,90". O banco guarda int; quem formata é a tela. */
const formatMoney = (cents: unknown): string => {
  const value = Number(cents ?? 0) / 100;
  return value.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
};

const formatCell = (field: FieldDef, value: unknown): string => {
  if (value === null || value === undefined || value === '') return '—';
  switch (field.type) {
    case 'dinheiro':
      return formatMoney(value);
    case 'booleano':
      return value ? 'Sim' : 'Não';
    case 'data':
      return new Date(`${String(value)}T00:00:00`).toLocaleDateString('pt-BR');
    case 'data_hora':
      return new Date(String(value)).toLocaleString('pt-BR');
    case 'multi_opcao':
      return Array.isArray(value) ? value.join(', ') : String(value);
    case 'json':
      return JSON.stringify(value);
    default:
      return String(value);
  }
};

/** Valor do banco → valor do input. Dinheiro vira "49,90" (o backend reparseia). */
const toInputValue = (field: FieldDef, value: unknown): string => {
  if (value === null || value === undefined) return '';
  if (field.type === 'dinheiro') return (Number(value) / 100).toFixed(2).replace('.', ',');
  if (field.type === 'data_hora') return String(value).slice(0, 16);
  if (field.type === 'json') return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return String(value);
};

// ── átomos ───────────────────────────────────────────────────────────────────

const Badge = ({ children, color = COLORS.muted }: { children: React.ReactNode; color?: string }) => (
  <span
    style={{
      fontSize: 10, fontWeight: 600, textTransform: 'uppercase', letterSpacing: 0.4,
      color, border: `1px solid ${color}55`, borderRadius: 4, padding: '2px 6px',
    }}
  >
    {children}
  </span>
);

const Button = ({
  children, onClick, kind = 'ghost', disabled, title,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  kind?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  title?: string;
}) => {
  const palette = {
    primary: { bg: COLORS.accent, fg: '#fff', border: COLORS.accent },
    ghost: { bg: 'transparent', fg: COLORS.text, border: COLORS.border },
    danger: { bg: 'transparent', fg: COLORS.danger, border: `${COLORS.danger}66` },
  }[kind];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        background: palette.bg, color: palette.fg, border: `1px solid ${palette.border}`,
        borderRadius: 6, padding: '6px 12px', fontSize: 12, fontWeight: 600,
        cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.45 : 1,
      }}
    >
      {children}
    </button>
  );
};

const inputStyle: React.CSSProperties = {
  width: '100%', background: COLORS.bg, color: COLORS.text,
  border: `1px solid ${COLORS.border}`, borderRadius: 6, padding: '8px 10px', fontSize: 13,
};

// ── formulário gerado pelo schema ────────────────────────────────────────────

const FieldInput = ({
  field, value, onChange,
}: {
  field: FieldDef;
  value: unknown;
  onChange: (v: unknown) => void;
}) => {
  const common = { style: inputStyle, id: `f-${field.name}` };

  switch (field.type) {
    case 'texto_longo':
      return (
        <textarea
          {...common}
          rows={4}
          value={toInputValue(field, value)}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case 'booleano':
      return (
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          <input
            type="checkbox"
            checked={Boolean(value)}
            onChange={(e) => onChange(e.target.checked)}
          />
          {value ? 'Sim' : 'Não'}
        </label>
      );
    case 'opcao':
      return (
        <select {...common} value={toInputValue(field, value)} onChange={(e) => onChange(e.target.value || null)}>
          <option value="">—</option>
          {field.options.map((o) => (
            <option key={o} value={o}>{o}</option>
          ))}
        </select>
      );
    case 'multi_opcao': {
      const selected: string[] = Array.isArray(value) ? (value as string[]) : [];
      return (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {field.options.map((o) => (
            <label key={o} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
              <input
                type="checkbox"
                checked={selected.includes(o)}
                onChange={(e) =>
                  onChange(e.target.checked ? [...selected, o] : selected.filter((s) => s !== o))
                }
              />
              {o}
            </label>
          ))}
        </div>
      );
    }
    case 'inteiro':
    case 'numero':
      return (
        <input
          {...common}
          type="number"
          step={field.type === 'inteiro' ? 1 : 'any'}
          value={toInputValue(field, value)}
          onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
        />
      );
    case 'dinheiro':
      // Texto, não number: o backend aceita "1.234,56" e é o formato que o
      // usuário digita. Number forçaria ponto decimal.
      return (
        <input
          {...common}
          inputMode="decimal"
          placeholder="49,90"
          value={toInputValue(field, value)}
          onChange={(e) => onChange(e.target.value)}
        />
      );
    case 'data':
      return (
        <input {...common} type="date" value={toInputValue(field, value)} onChange={(e) => onChange(e.target.value || null)} />
      );
    case 'data_hora':
      return (
        <input {...common} type="datetime-local" value={toInputValue(field, value)} onChange={(e) => onChange(e.target.value || null)} />
      );
    case 'json':
      return (
        <textarea {...common} rows={5} style={{ ...inputStyle, fontFamily: 'ui-monospace, monospace' }}
          value={toInputValue(field, value)} onChange={(e) => onChange(e.target.value)} />
      );
    default:
      // texto, relacao, arquivo e QUALQUER tipo que o backend passe a conhecer
      // antes desta tela: input simples em vez de tela quebrada.
      return (
        <input
          {...common}
          maxLength={field.max ?? undefined}
          placeholder={field.type === 'relacao' ? `id de ${field.dataset}` : field.type === 'arquivo' ? 'id do arquivo' : ''}
          value={toInputValue(field, value)}
          onChange={(e) => onChange(e.target.value || null)}
        />
      );
  }
};

const RowForm = ({
  dataset, row, onSaved, onCancel,
}: {
  dataset: Dataset;
  row: Row | null;
  onSaved: () => void;
  onCancel: () => void;
}) => {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const initial: Record<string, unknown> = {};
    for (const f of dataset.schema.fields) {
      initial[f.name] = row ? row[f.name] : f.default ?? (f.type === 'booleano' ? false : null);
    }
    setValues(initial);
    setError(null);
  }, [dataset, row]);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Só manda o que tem valor: o backend distingue "não enviado" de "vazio",
      // e um campo obrigatório enviado vazio é recusado de propósito.
      const campos: Record<string, unknown> = {};
      for (const f of dataset.schema.fields) {
        const v = values[f.name];
        if (v !== null && v !== undefined && v !== '') campos[f.name] = v;
        else if (row && row[f.name] !== null && row[f.name] !== undefined) campos[f.name] = null;
      }
      const url = row
        ? withUser(`${API}/${dataset.slug}/rows/${row.id}`)
        : withUser(`${API}/${dataset.slug}/rows`);
      await api(url, { method: row ? 'PUT' : 'POST', body: JSON.stringify({ campos }) });
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{
      background: COLORS.card, border: `1px solid ${COLORS.border}`, borderRadius: 10,
      padding: 16, marginBottom: 16,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 14 }}>
        <strong style={{ fontSize: 14 }}>{row ? 'Editar registro' : 'Novo registro'}</strong>
        <Badge color={COLORS.accentSoft}>{dataset.slug}</Badge>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 14 }}>
        {dataset.schema.fields.map((f) => (
          <div key={f.name}>
            <label htmlFor={`f-${f.name}`} style={{ display: 'block', fontSize: 11, color: COLORS.muted, marginBottom: 4 }}>
              {f.label}
              {f.required && <span style={{ color: COLORS.danger }}> *</span>}
            </label>
            <FieldInput field={f} value={values[f.name]} onChange={(v) => setValues((s) => ({ ...s, [f.name]: v }))} />
          </div>
        ))}
      </div>

      {error && (
        <div style={{ marginTop: 12, color: COLORS.danger, fontSize: 12 }}>{error}</div>
      )}

      <div style={{ display: 'flex', gap: 8, marginTop: 16 }}>
        <Button kind="primary" onClick={save} disabled={saving}>
          {saving ? 'Salvando…' : row ? 'Salvar' : 'Cadastrar'}
        </Button>
        <Button onClick={onCancel}>Cancelar</Button>
      </div>
    </div>
  );
};

// ── tela ─────────────────────────────────────────────────────────────────────

const DadosPanel = () => {
  const { slug } = useParams<{ slug?: string }>();
  const navigate = useNavigate();

  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [current, setCurrent] = useState<Dataset | null>(null);
  const [rows, setRows] = useState<RowsPayload | null>(null);
  const [busca, setBusca] = useState('');
  const [pagina, setPagina] = useState(1);
  const [ordem, setOrdem] = useState('');
  const [editing, setEditing] = useState<Row | null>(null);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadDatasets = useCallback(async () => {
    try {
      const data = await api<{ cadastros: Dataset[] }>(withUser(API));
      setDatasets(data.cadastros);
      return data.cadastros;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return [];
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    loadDatasets().finally(() => setLoading(false));
  }, [loadDatasets]);

  useEffect(() => {
    if (!slug) { setCurrent(null); return; }
    const found = datasets.find((d) => d.slug === slug);
    if (found) setCurrent(found);
  }, [slug, datasets]);

  const loadRows = useCallback(async () => {
    if (!current) return;
    try {
      setError(null);
      const data = await api<RowsPayload>(
        withUser(`${API}/${current.slug}/rows`, { busca, ordem, pagina, limite: PAGE_SIZE }),
      );
      setRows(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [current, busca, ordem, pagina]);

  useEffect(() => { void loadRows(); }, [loadRows]);

  const columns = useMemo(() => {
    if (!current) return [];
    const wanted = current.schema.list_columns.length
      ? current.schema.list_columns
      : current.schema.fields.slice(0, 5).map((f) => f.name);
    return wanted
      .map((name) => current.schema.fields.find((f) => f.name === name))
      .filter((f): f is FieldDef => Boolean(f));
  }, [current]);

  const remove = async (row: Row) => {
    if (!current) return;
    const label = String(row[current.schema.label_field] ?? row.id);
    if (!window.confirm(`Excluir "${label}"?\n\nA exclusão é reversível — o registro fica marcado como apagado e pode ser restaurado.`)) return;
    try {
      await api(withUser(`${API}/${current.slug}/rows/${row.id}`), { method: 'DELETE' });
      await loadRows();
      await loadDatasets();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const toggleOrder = (field: FieldDef) => {
    setPagina(1);
    setOrdem((prev) => (prev === `${field.name} asc` ? `${field.name} desc` : `${field.name} asc`));
  };

  // ── lista de cadastros ────────────────────────────────────────────────────
  if (!current) {
    return (
      <div style={{ padding: 24, color: COLORS.text }}>
        <h1 style={{ fontSize: 20, marginBottom: 4 }}>Dados</h1>
        <p style={{ color: COLORS.muted, fontSize: 13, marginBottom: 20 }}>
          Cadastros criados por skills ou pelo painel. A tela de cada um é gerada
          pelo schema — nada aqui é escrito à mão por cadastro.
        </p>

        {error && <div style={{ color: COLORS.danger, fontSize: 12, marginBottom: 12 }}>{error}</div>}
        {loading && <div style={{ color: COLORS.muted, fontSize: 13 }}>Carregando…</div>}

        {!loading && datasets.length === 0 && (
          <div style={{
            background: COLORS.card, border: `1px dashed ${COLORS.border}`, borderRadius: 10,
            padding: 24, fontSize: 13, color: COLORS.muted,
          }}>
            Nenhum cadastro ainda. Peça um ao Neo (“cria um cadastro de produtos
            com nome, preço e estoque”) ou crie uma skill com <code>datasets:</code> na
            aba Skills e clique em Instalar.
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))', gap: 14 }}>
          {datasets.map((d) => (
            <button
              key={d.slug}
              type="button"
              onClick={() => navigate(`/dados/${d.slug}`)}
              style={{
                textAlign: 'left', background: COLORS.card, border: `1px solid ${COLORS.border}`,
                borderRadius: 10, padding: 16, cursor: 'pointer', color: COLORS.text,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
                <strong style={{ fontSize: 15 }}>{d.title}</strong>
                {!d.enabled && <Badge color={COLORS.warn}>desligado</Badge>}
              </div>
              <div style={{ fontSize: 12, color: COLORS.muted, minHeight: 32 }}>{d.description || '—'}</div>
              <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
                <Badge color={COLORS.accentSoft}>{d.total_linhas ?? 0} registro(s)</Badge>
                <Badge>{d.schema.fields.length} campos</Badge>
                {d.skill_name && <Badge color={COLORS.ok}>skill {d.skill_name}</Badge>}
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }

  // ── CRUD de um cadastro ───────────────────────────────────────────────────
  return (
    <div style={{ padding: 24, color: COLORS.text }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 4 }}>
        <Button onClick={() => navigate('/dados')}>← Cadastros</Button>
        <h1 style={{ fontSize: 20, margin: 0 }}>{current.title}</h1>
        <Badge color={COLORS.accentSoft}>{current.slug}</Badge>
        <span style={{ fontSize: 11, color: COLORS.muted, fontFamily: 'ui-monospace, monospace' }}>
          neo_data.{current.table_name}
        </span>
      </div>
      <p style={{ color: COLORS.muted, fontSize: 13, marginTop: 0, marginBottom: 16 }}>
        {current.description}
      </p>

      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <input
          style={{ ...inputStyle, width: 260 }}
          placeholder={`Buscar em ${current.schema.search_fields.join(', ') || 'texto'}…`}
          value={busca}
          onChange={(e) => { setBusca(e.target.value); setPagina(1); }}
        />
        <Button kind="primary" onClick={() => { setCreating(true); setEditing(null); }}>+ Novo</Button>
        <Button onClick={() => void loadRows()}>Recarregar</Button>
      </div>

      {error && <div style={{ color: COLORS.danger, fontSize: 12, marginBottom: 12 }}>{error}</div>}

      {(creating || editing) && (
        <RowForm
          dataset={current}
          row={editing}
          onSaved={async () => {
            setCreating(false); setEditing(null);
            await loadRows(); await loadDatasets();
          }}
          onCancel={() => { setCreating(false); setEditing(null); }}
        />
      )}

      <div style={{ overflowX: 'auto', border: `1px solid ${COLORS.border}`, borderRadius: 10 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
          <thead>
            <tr style={{ background: COLORS.card }}>
              {columns.map((f) => (
                <th
                  key={f.name}
                  onClick={() => toggleOrder(f)}
                  style={{
                    textAlign: 'left', padding: '10px 12px', fontSize: 11, letterSpacing: 0.4,
                    textTransform: 'uppercase', color: COLORS.muted, cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {f.label}
                  {ordem.startsWith(`${f.name} `) && (ordem.endsWith('asc') ? ' ▲' : ' ▼')}
                </th>
              ))}
              <th style={{ padding: '10px 12px' }} />
            </tr>
          </thead>
          <tbody>
            {(rows?.linhas ?? []).map((row) => (
              <tr key={row.id} style={{ borderTop: `1px solid ${COLORS.border}` }}>
                {columns.map((f) => (
                  <td key={f.name} style={{ padding: '10px 12px', whiteSpace: 'nowrap' }}>
                    {formatCell(f, row[f.name])}
                  </td>
                ))}
                <td style={{ padding: '8px 12px', textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <span style={{ display: 'inline-flex', gap: 6 }}>
                    <Button onClick={() => { setEditing(row); setCreating(false); }}>Editar</Button>
                    <Button kind="danger" onClick={() => void remove(row)}>Excluir</Button>
                  </span>
                </td>
              </tr>
            ))}
            {rows && rows.linhas.length === 0 && (
              <tr>
                <td colSpan={columns.length + 1} style={{ padding: 24, textAlign: 'center', color: COLORS.muted }}>
                  {busca ? `Nada encontrado para “${busca}”.` : 'Nenhum registro ainda.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {rows && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12, fontSize: 12, color: COLORS.muted }}>
          <span>{rows.total} registro(s)</span>
          <Button disabled={pagina <= 1} onClick={() => setPagina((p) => p - 1)}>‹ Anterior</Button>
          <span>página {rows.pagina}</span>
          <Button disabled={!rows.tem_mais} onClick={() => setPagina((p) => p + 1)}>Próxima ›</Button>
        </div>
      )}
    </div>
  );
};

export default DadosPanel;
