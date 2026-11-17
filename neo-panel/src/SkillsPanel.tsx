import { useCallback, useEffect, useMemo, useState } from 'react';

// Aba Skills — CRUD das skills declarativas (neo-core/skills/<name>/SKILL.md).
// Backend: neo-core/api/routes/skills.py. Ver docs/skills-library-plan.md.
//
// Os 3 níveis que nascem de um SKILL.md são criáveis aqui; o nível 4 (pipeline)
// é pacote Python e aparece só para leitura. O neo-core recarrega o catálogo
// sozinho (hot-reload por mtime) — salvar aqui já vale na próxima mensagem.
const SKILLS_API = '/api/v1/skills';

type Kind = 'instructional' | 'scripted' | 'tool_backed' | 'pipeline';

interface SkillEntry {
  name: string;
  description: string;
  keywords: string[];
  kind: Kind;
  tools: string[];
  env?: string[];
  requires?: string[];
  enabled: boolean;
  version: number | string;
  available: boolean;
  unavailable_reason?: string | null;
  editable: boolean;
  intent_type?: string;
  handoff?: boolean;
}

interface SkillFile {
  path: string;
  size: number;
  editable: boolean;
  sandbox_path: string | null;
}

interface SkillSource {
  name: string;
  meta: Record<string, unknown>;
  body: string;
  loaded: boolean;
  parse_error: string | null;
  files: SkillFile[];
  entry: SkillEntry | null;
}

interface ListPayload {
  root: string;
  skills: SkillEntry[];
  pipelines: SkillEntry[];
  errors: { skill: string; error: string }[];
}

interface Draft {
  name: string;
  description: string;
  kind: Exclude<Kind, 'pipeline'>;
  keywords: string;
  tools: string[];
  enabled: boolean;
  version: number;
  body: string;
  isNew: boolean;
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

const KIND_LABEL: Record<Kind, string> = {
  instructional: 'instrucional',
  scripted: 'com scripts',
  tool_backed: 'com tools',
  pipeline: 'pipeline (código)',
};

const KIND_HELP: Record<Exclude<Kind, 'pipeline'>, string> = {
  instructional:
    'Só instruções. O Neo lê e resolve com as ferramentas que já tem. É o tipo mais simples e o que você deve preferir.',
  scripted:
    'Instruções + scripts prontos, que rodam no sandbox do Neo (sem acesso à sua rede local). Exige o workbench ligado.',
  tool_backed:
    'Instruções + ferramentas do servidor. Ler a skill destrava as ferramentas escolhidas naquele turno — antes disso elas nem aparecem para o Neo.',
};

const NEW_BODY = `# Título da skill

## Quando usar
Descreva a situação em que o Neo deve seguir estas instruções.

## Como usar
1. Primeiro passo.
2. Segundo passo.

## Regras
- Seja específico; não repita o que o Neo já sabe (persona, regras gerais).
`;

const EMPTY_DRAFT: Draft = {
  name: '',
  description: '',
  kind: 'instructional',
  keywords: '',
  tools: [],
  enabled: true,
  version: 1,
  body: NEW_BODY,
  isNew: true,
};

async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new Error(data?.detail ?? `HTTP ${res.status}`);
  return data as T;
}

// ── átomos visuais ───────────────────────────────────────────────────────────

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

const Field = ({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: React.ReactNode;
  children: React.ReactNode;
}) => (
  <div style={{ marginBottom: 16 }}>
    <label style={{ display: 'block', fontSize: 12, color: COLORS.muted, marginBottom: 6, fontWeight: 500 }}>
      {label}
    </label>
    {children}
    {hint && <div style={{ fontSize: 11, color: COLORS.muted, marginTop: 5, lineHeight: 1.5 }}>{hint}</div>}
  </div>
);

// ── painel ───────────────────────────────────────────────────────────────────

export default function SkillsPanel() {
  const [list, setList] = useState<ListPayload | null>(null);
  const [tools, setTools] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [files, setFiles] = useState<SkillFile[]>([]);
  const [parseError, setParseError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [catalog, setCatalog] = useState<{ catalog_note: string | null; estimated_tokens: number } | null>(null);
  const [showCatalog, setShowCatalog] = useState(false);

  // arquivo aberto no editor secundário (references/ ou scripts/)
  const [openFile, setOpenFile] = useState<{ path: string; content: string; isNew: boolean } | null>(null);

  const refresh = useCallback(async () => {
    try {
      setList(await api<ListPayload>(SKILLS_API));
      setError(null);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  }, []);

  useEffect(() => {
    refresh();
    api<{ tools: string[] }>(`${SKILLS_API}/tools`)
      .then((d) => setTools(d.tools))
      .catch(() => setTools([]));
  }, [refresh]);

  const flash = (msg: string) => {
    setNotice(msg);
    setTimeout(() => setNotice(null), 3500);
  };

  const openSkill = async (name: string) => {
    setError(null);
    setOpenFile(null);
    try {
      const src = await api<SkillSource>(`${SKILLS_API}/${encodeURIComponent(name)}`);
      const meta = src.meta;
      setSelected(name);
      setParseError(src.parse_error);
      setFiles(src.files);
      setDraft({
        name,
        description: String(meta.description ?? ''),
        kind: (meta.kind as Draft['kind']) ?? 'instructional',
        keywords: ((meta.keywords as string[]) ?? []).join(', '),
        tools: (meta.tools as string[]) ?? [],
        enabled: meta.enabled === undefined ? true : Boolean(meta.enabled),
        version: Number(meta.version ?? 1),
        body: src.body,
        isNew: false,
      });
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const startNew = () => {
    setSelected(null);
    setParseError(null);
    setFiles([]);
    setOpenFile(null);
    setError(null);
    setDraft({ ...EMPTY_DRAFT });
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setError(null);
    const payload = {
      name: draft.name,
      description: draft.description,
      body: draft.body,
      kind: draft.kind,
      keywords: draft.keywords.split(',').map((k) => k.trim()).filter(Boolean),
      tools: draft.kind === 'tool_backed' ? draft.tools : [],
      enabled: draft.enabled,
      version: draft.version,
    };
    try {
      if (draft.isNew) {
        await api(SKILLS_API, { method: 'POST', body: JSON.stringify(payload) });
      } else {
        await api(`${SKILLS_API}/${encodeURIComponent(draft.name)}`, {
          method: 'PUT',
          body: JSON.stringify(payload),
        });
      }
      await refresh();
      await openSkill(draft.name);
      flash(`Skill "${draft.name}" salva — o Neo já a enxerga na próxima mensagem.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (name: string, enabled: boolean) => {
    try {
      await api(`${SKILLS_API}/${encodeURIComponent(name)}/enabled`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      });
      await refresh();
      if (selected === name) await openSkill(name);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const remove = async (name: string) => {
    if (!window.confirm(`Apagar a skill "${name}"? A pasta inteira (SKILL.md, references/, scripts/) some.`)) return;
    try {
      await api(`${SKILLS_API}/${encodeURIComponent(name)}`, { method: 'DELETE' });
      setSelected(null);
      setDraft(null);
      await refresh();
      flash(`Skill "${name}" apagada.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const loadCatalog = async () => {
    try {
      setCatalog(await api(`${SKILLS_API}/catalog`));
      setShowCatalog(true);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  // ── arquivos da skill ──────────────────────────────────────────────────────

  const openFileEditor = async (path: string) => {
    if (!selected) return;
    try {
      const d = await api<{ content: string }>(`${SKILLS_API}/${encodeURIComponent(selected)}/files/${path}`);
      setOpenFile({ path, content: d.content, isNew: false });
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const newFile = () => {
    if (!draft) return;
    const dir = draft.kind === 'scripted' ? 'scripts' : 'references';
    const ext = dir === 'scripts' ? 'py' : 'md';
    setOpenFile({ path: `${dir}/novo.${ext}`, content: '', isNew: true });
  };

  const saveFile = async () => {
    if (!selected || !openFile) return;
    try {
      await api(`${SKILLS_API}/${encodeURIComponent(selected)}/files/${openFile.path}`, {
        method: 'PUT',
        body: JSON.stringify({ content: openFile.content }),
      });
      const src = await api<SkillSource>(`${SKILLS_API}/${encodeURIComponent(selected)}`);
      setFiles(src.files);
      setOpenFile({ ...openFile, isNew: false });
      flash(`Arquivo ${openFile.path} salvo.`);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const removeFile = async (path: string) => {
    if (!selected || !window.confirm(`Apagar ${path}?`)) return;
    try {
      await api(`${SKILLS_API}/${encodeURIComponent(selected)}/files/${path}`, { method: 'DELETE' });
      const src = await api<SkillSource>(`${SKILLS_API}/${encodeURIComponent(selected)}`);
      setFiles(src.files);
      if (openFile?.path === path) setOpenFile(null);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const descLeft = 200 - (draft?.description.trim().length ?? 0);
  const canSave = useMemo(
    () =>
      !!draft &&
      draft.name.trim().length > 0 &&
      draft.description.trim().length > 0 &&
      descLeft >= 0 &&
      draft.body.trim().length > 0 &&
      (draft.kind !== 'tool_backed' || draft.tools.length > 0) &&
      !saving,
    [draft, descLeft, saving],
  );

  return (
    <div style={{ background: COLORS.bg, minHeight: '100%', color: COLORS.text, padding: 24 }}>
      {/* Cabeçalho */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6 }}>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 600 }}>🎓 Skills</h1>
        <div style={{ flex: 1 }} />
        <Button onClick={loadCatalog} title="O que o Neo lê a cada turno">
          Ver catálogo do cérebro
        </Button>
        <Button onClick={refresh}>Recarregar</Button>
        <Button variant="primary" onClick={startNew}>
          + Nova skill
        </Button>
      </div>
      <p style={{ color: COLORS.muted, fontSize: 13, margin: '0 0 20px', lineHeight: 1.6 }}>
        Procedimentos prontos que o Neo consulta sob demanda. Ele sempre vê o nome e a descrição; só lê o conteúdo
        quando o pedido combina. <span style={{ color: COLORS.border }}>·</span>{' '}
        <code style={{ color: COLORS.muted }}>{list?.root ?? '…'}</code>
      </p>

      {notice && (
        <div style={{ ...banner(COLORS.ok), marginBottom: 16 }}>{notice}</div>
      )}
      {error && (
        <div style={{ ...banner(COLORS.danger), marginBottom: 16 }}>
          {error}
          <span onClick={() => setError(null)} style={{ float: 'right', cursor: 'pointer', opacity: 0.6 }}>
            ✕
          </span>
        </div>
      )}
      {!!list?.errors.length && (
        <div style={{ ...banner(COLORS.warn), marginBottom: 16 }}>
          <strong>{list.errors.length} skill(s) fora do catálogo por erro de arquivo:</strong>
          <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
            {list.errors.map((e) => (
              <li key={e.skill} style={{ fontSize: 12 }}>
                <code>{e.skill}</code> — {e.error}{' '}
                <span onClick={() => openSkill(e.skill)} style={{ color: COLORS.accentSoft, cursor: 'pointer' }}>
                  abrir
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {showCatalog && catalog && (
        <div style={{ ...card(), marginBottom: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
            <strong style={{ fontSize: 13 }}>Catálogo injetado no prompt cognitivo</strong>
            <Badge color={COLORS.accentSoft}>~{catalog.estimated_tokens} tokens/turno</Badge>
            <div style={{ flex: 1 }} />
            <span onClick={() => setShowCatalog(false)} style={{ cursor: 'pointer', color: COLORS.muted }}>
              ✕
            </span>
          </div>
          <pre style={preStyle}>{catalog.catalog_note ?? '(nenhuma skill ativa)'}</pre>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '320px 1fr', gap: 20, alignItems: 'start' }}>
        {/* Lista */}
        <div style={card()}>
          <SectionTitle>Editáveis pelo painel</SectionTitle>
          {list?.skills.length === 0 && (
            <p style={{ color: COLORS.muted, fontSize: 12 }}>Nenhuma skill ainda. Crie a primeira.</p>
          )}
          {list?.skills.map((s) => (
            <div
              key={s.name}
              onClick={() => openSkill(s.name)}
              style={{
                padding: '10px 12px',
                borderRadius: 6,
                cursor: 'pointer',
                marginBottom: 6,
                background: selected === s.name ? 'rgba(124, 58, 237, 0.12)' : 'transparent',
                border: `1px solid ${selected === s.name ? COLORS.accent + '55' : 'transparent'}`,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span
                  title={s.available ? 'Ativa no catálogo' : (s.unavailable_reason ?? 'Indisponível')}
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: '50%',
                    background: s.available ? COLORS.ok : COLORS.muted,
                    flexShrink: 0,
                  }}
                />
                <span style={{ fontSize: 13, fontWeight: 500, flex: 1 }}>{s.name}</span>
                <Badge>{KIND_LABEL[s.kind]}</Badge>
              </div>
              <div style={{ fontSize: 11, color: COLORS.muted, marginTop: 4, lineHeight: 1.4 }}>{s.description}</div>
              {!s.available && s.unavailable_reason && (
                <div style={{ fontSize: 10, color: COLORS.warn, marginTop: 4 }}>{s.unavailable_reason}</div>
              )}
            </div>
          ))}

          {!!list?.pipelines.length && (
            <>
              <SectionTitle style={{ marginTop: 20 }}>Pipelines (código Python)</SectionTitle>
              {list.pipelines.map((p) => (
                <div key={p.name} style={{ padding: '10px 12px', opacity: 0.65 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <span style={{ fontSize: 13, flex: 1 }}>{p.name}</span>
                    <Badge>read-only</Badge>
                  </div>
                  <div style={{ fontSize: 11, color: COLORS.muted, marginTop: 4 }}>{p.description}</div>
                </div>
              ))}
            </>
          )}
        </div>

        {/* Editor */}
        <div style={card()}>
          {!draft ? (
            <div style={{ color: COLORS.muted, fontSize: 13, padding: '40px 0', textAlign: 'center' }}>
              Escolha uma skill à esquerda ou crie uma nova.
            </div>
          ) : (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18 }}>
                <strong style={{ fontSize: 15 }}>{draft.isNew ? 'Nova skill' : draft.name}</strong>
                <div style={{ flex: 1 }} />
                {!draft.isNew && (
                  <>
                    <label style={{ fontSize: 12, color: COLORS.muted, display: 'flex', alignItems: 'center', gap: 6 }}>
                      <input
                        type="checkbox"
                        checked={draft.enabled}
                        onChange={(e) => toggleEnabled(draft.name, e.target.checked)}
                      />
                      ativa
                    </label>
                    <Button variant="danger" onClick={() => remove(draft.name)}>
                      Apagar
                    </Button>
                  </>
                )}
                <Button variant="primary" onClick={save} disabled={!canSave}>
                  {saving ? 'Salvando…' : 'Salvar'}
                </Button>
              </div>

              {parseError && (
                <div style={{ ...banner(COLORS.danger), marginBottom: 16 }}>
                  Frontmatter inválido no arquivo: {parseError}. Preencha os campos abaixo e salve para consertar — o
                  corpo foi preservado.
                </div>
              )}

              {draft.isNew && (
                <Field
                  label="Nome"
                  hint="Minúsculas, números e hífen. Vira o nome da pasta e não pode mudar depois. Ex.: relatorio-treino"
                >
                  <input
                    style={inputStyle}
                    value={draft.name}
                    placeholder="minha-skill"
                    onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  />
                </Field>
              )}

              <Field
                label="Descrição"
                hint={
                  <span style={{ color: descLeft < 0 ? COLORS.danger : COLORS.muted }}>
                    É a ÚNICA linha que o Neo vê sempre — precisa bastar para ele decidir “é essa”. {descLeft} chars
                    restantes.
                  </span>
                }
              >
                <textarea
                  style={{ ...inputStyle, minHeight: 56, resize: 'vertical' }}
                  value={draft.description}
                  placeholder="Consulta o banco Postgres do Neo com SQL somente-leitura. Use para perguntas sobre dados internos."
                  onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                />
              </Field>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                <Field label="Tipo" hint={KIND_HELP[draft.kind]}>
                  <select
                    style={inputStyle}
                    value={draft.kind}
                    onChange={(e) => setDraft({ ...draft, kind: e.target.value as Draft['kind'], tools: [] })}
                  >
                    <option value="instructional">Instrucional — só instruções</option>
                    <option value="scripted">Com scripts — roda no sandbox</option>
                    <option value="tool_backed">Com tools — destrava ferramentas</option>
                  </select>
                </Field>

                <Field label="Palavras-chave" hint="Separadas por vírgula. Ajudam o Neo a achar a skill quando o catálogo cresce.">
                  <input
                    style={inputStyle}
                    value={draft.keywords}
                    placeholder="banco, sql, consulta, dados"
                    onChange={(e) => setDraft({ ...draft, keywords: e.target.value })}
                  />
                </Field>
              </div>

              {draft.kind === 'tool_backed' && (
                <Field
                  label="Ferramentas destravadas"
                  hint="Só ferramentas que já existem no neo-core (criar uma nova exige código). Ao ler esta skill, o Neo passa a poder chamá-las naquele turno."
                >
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                    {tools.map((t) => {
                      const on = draft.tools.includes(t);
                      return (
                        <span
                          key={t}
                          onClick={() =>
                            setDraft({
                              ...draft,
                              tools: on ? draft.tools.filter((x) => x !== t) : [...draft.tools, t],
                            })
                          }
                          style={{
                            fontSize: 11,
                            fontFamily: 'ui-monospace, monospace',
                            padding: '4px 8px',
                            borderRadius: 5,
                            cursor: 'pointer',
                            border: `1px solid ${on ? COLORS.accent : COLORS.border}`,
                            background: on ? 'rgba(124, 58, 237, 0.18)' : 'transparent',
                            color: on ? COLORS.accentSoft : COLORS.muted,
                          }}
                        >
                          {t}
                        </span>
                      );
                    })}
                  </div>
                </Field>
              )}

              <Field
                label="Instruções (corpo do SKILL.md)"
                hint="Curto e imperativo. Detalhe grande (schemas, tabelas) vai para um arquivo de references/ e é citado pelo caminho."
              >
                <textarea
                  style={{ ...inputStyle, minHeight: 320, fontFamily: 'ui-monospace, monospace', fontSize: 12, lineHeight: 1.6, resize: 'vertical' }}
                  value={draft.body}
                  onChange={(e) => setDraft({ ...draft, body: e.target.value })}
                />
              </Field>

              {/* Arquivos — só depois que a skill existe no disco */}
              {!draft.isNew && (
                <div style={{ borderTop: `1px solid ${COLORS.border}`, paddingTop: 16, marginTop: 4 }}>
                  <div style={{ display: 'flex', alignItems: 'center', marginBottom: 10 }}>
                    <SectionTitle style={{ margin: 0 }}>Arquivos da skill</SectionTitle>
                    <div style={{ flex: 1 }} />
                    <Button onClick={newFile}>+ Novo arquivo</Button>
                  </div>
                  {files.length === 0 && !openFile && (
                    <p style={{ color: COLORS.muted, fontSize: 12, margin: 0 }}>
                      Nenhum. Use <code>references/</code> para detalhe longo e <code>scripts/</code> para código que
                      roda no sandbox.
                    </p>
                  )}
                  {files.map((f) => (
                    <div
                      key={f.path}
                      style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0', fontSize: 12 }}
                    >
                      <code
                        onClick={() => f.editable && openFileEditor(f.path)}
                        style={{ color: f.editable ? COLORS.accentSoft : COLORS.muted, cursor: f.editable ? 'pointer' : 'default' }}
                      >
                        {f.path}
                      </code>
                      <span style={{ color: COLORS.muted }}>{f.size} B</span>
                      {f.sandbox_path && (
                        <span style={{ color: COLORS.muted, fontFamily: 'ui-monospace, monospace', fontSize: 10 }}>
                          sandbox: {f.sandbox_path}
                        </span>
                      )}
                      <div style={{ flex: 1 }} />
                      <span onClick={() => removeFile(f.path)} style={{ cursor: 'pointer', color: COLORS.danger, opacity: 0.7 }}>
                        apagar
                      </span>
                    </div>
                  ))}

                  {openFile && (
                    <div style={{ marginTop: 14 }}>
                      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
                        <input
                          style={{ ...inputStyle, fontFamily: 'ui-monospace, monospace', fontSize: 12 }}
                          value={openFile.path}
                          disabled={!openFile.isNew}
                          onChange={(e) => setOpenFile({ ...openFile, path: e.target.value })}
                        />
                        <Button variant="primary" onClick={saveFile}>
                          Salvar arquivo
                        </Button>
                        <Button onClick={() => setOpenFile(null)}>Fechar</Button>
                      </div>
                      <textarea
                        style={{ ...inputStyle, minHeight: 220, fontFamily: 'ui-monospace, monospace', fontSize: 12, lineHeight: 1.6, resize: 'vertical' }}
                        value={openFile.content}
                        onChange={(e) => setOpenFile({ ...openFile, content: e.target.value })}
                      />
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ── helpers de estilo ────────────────────────────────────────────────────────

const card = (): React.CSSProperties => ({
  background: COLORS.card,
  border: `1px solid ${COLORS.border}`,
  borderRadius: 10,
  padding: 18,
});

const banner = (color: string): React.CSSProperties => ({
  background: `${color}14`,
  border: `1px solid ${color}44`,
  color,
  borderRadius: 8,
  padding: '10px 14px',
  fontSize: 13,
});

const preStyle: React.CSSProperties = {
  background: COLORS.bg,
  border: `1px solid ${COLORS.border}`,
  borderRadius: 6,
  padding: 12,
  fontSize: 11,
  lineHeight: 1.6,
  color: COLORS.muted,
  whiteSpace: 'pre-wrap',
  margin: 0,
  maxHeight: 260,
  overflow: 'auto',
};

const SectionTitle = ({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) => (
  <h3
    style={{
      fontSize: 11,
      textTransform: 'uppercase',
      letterSpacing: 0.6,
      color: COLORS.muted,
      margin: '0 0 12px',
      fontWeight: 600,
      ...style,
    }}
  >
    {children}
  </h3>
);
