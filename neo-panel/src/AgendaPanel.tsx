import { useCallback, useEffect, useMemo, useState } from 'react';
import Screen from './ui/Screen';
import Icon from './ui/Icon';
import { currentUserId } from './lib/auth';

/*
 * Tela Agenda (/agenda) — o cron do Neo.
 * Backend: neo-core/api/routes/schedules.py. Ver docs/scheduler-plan.md §10.
 *
 * Duas coisas aqui não são enfeite:
 *
 * 1. O PREVIEW do horário. O campo "quando" aceita português ("todo dia útil às
 *    8h") e a tela mostra, ao vivo, o que o servidor entendeu e as próximas 5
 *    execuções. Ninguém deve precisar saber cron para usar isto — e ver a data
 *    resolvida é o que evita o cron fantasma às 3h da manhã.
 * 2. O botão SIMULAR. Roda de verdade, sem entregar. É o que evita descobrir
 *    que o prompt estava ruim depois de três dias de mensagem errada.
 */

const API = '/api/v1/agenda';

type ActionKind = 'prompt' | 'tool' | 'notify' | 'job';

interface Task {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  schedule_kind: string;
  schedule_expr: string;
  timezone: string;
  window_json: Record<string, unknown>;
  action_kind: ActionKind;
  action_json: Record<string, any>;
  condition_json: Record<string, any>;
  only_if_changed: boolean;
  notify_policy: string;
  session_mode: string;
  misfire_policy: string;
  next_run_at: string | null;
  next_run_local: string | null;
  last_run_at: string | null;
  last_status: string;
  run_count: number;
  skipped_count: number;
  consecutive_errors: number;
  paused_reason: string;
  descricao_horario: string;
  proximos: string[];
}

interface Fire {
  job_id: string;
  status: string;
  resultado: string;
  texto_entregue: string | null;
  motivo: string | null;
  criado_em: string | null;
  terminou_em: string | null;
  entregue: boolean;
  duracao_s: number | null;
}

interface ToolDef {
  name: string;
  description: string;
  module: string;
  parameters: Record<string, { type: string; description: string; required: boolean }>;
}

const STATUS_COLOR: Record<string, string> = {
  completed: 'var(--neo-green)',
  failed: 'var(--neo-rose)',
  timeout: 'var(--neo-rose)',
  cancelled: 'var(--neo-text-muted)',
  running: 'var(--neo-violet-light)',
  queued: 'var(--neo-text-muted)',
};

const ACTION_LABEL: Record<ActionKind, string> = {
  prompt: 'Prompt (turno do Neo)',
  tool: 'Ferramenta',
  notify: 'Alerta',
  job: 'Job pesado',
};

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

// O dono é o usuário logado (era um UUID fixo no fonte). Sem sessão, a rota vai
// sem `user_id` e o core resolve pela credencial.
const withUser = (path: string) => {
  const dono = currentUserId();
  if (!dono) return path;
  return `${path}${path.includes('?') ? '&' : '?'}user_id=${dono}`;
};

// ── Formulário ───────────────────────────────────────────────────────────────

interface FormState {
  id?: string;
  name: string;
  quando: string;
  action_kind: ActionKind;
  texto: string;
  tool: string;
  argumentos: string;
  titulo: string;
  corpo: string;
  job_kind: string;
  job_spec: string;
  notify_policy: string;
  session_mode: string;
  misfire_policy: string;
  only_if_changed: boolean;
  cond_tipo: string;
  cond_tool: string;
  cond_url: string;
  cond_campo: string;
  cond_operador: string;
  cond_valor: string;
}

const EMPTY_FORM: FormState = {
  name: '',
  quando: '',
  action_kind: 'notify',
  texto: '',
  tool: '',
  argumentos: '{}',
  titulo: '',
  corpo: '',
  job_kind: 'research',
  job_spec: '{"topic": ""}',
  notify_policy: 'chat',
  session_mode: 'nova',
  misfire_policy: 'skip',
  only_if_changed: false,
  cond_tipo: '',
  cond_tool: '',
  cond_url: '',
  cond_campo: '',
  cond_operador: 'nao_vazio',
  cond_valor: '',
};

function formFromTask(task: Task): FormState {
  const acao = task.action_json || {};
  const cond = task.condition_json || {};
  const espera = cond.espera || {};
  return {
    ...EMPTY_FORM,
    id: task.id,
    name: task.name,
    quando: task.schedule_expr,
    action_kind: task.action_kind,
    texto: acao.texto ?? '',
    tool: acao.tool ?? '',
    argumentos: JSON.stringify(acao.arguments ?? {}, null, 2),
    titulo: acao.titulo ?? '',
    corpo: acao.corpo ?? '',
    job_kind: acao.kind ?? 'research',
    job_spec: JSON.stringify(acao.spec ?? {}, null, 2),
    notify_policy: task.notify_policy,
    session_mode: task.session_mode,
    misfire_policy: task.misfire_policy,
    only_if_changed: task.only_if_changed,
    cond_tipo: cond.tipo ?? '',
    cond_tool: cond.tool ?? '',
    cond_url: cond.url ?? '',
    cond_campo: espera.campo ?? '',
    cond_operador: espera.operador ?? 'nao_vazio',
    cond_valor: espera.valor !== undefined ? String(espera.valor) : '',
  };
}

function payloadFromForm(form: FormState): Record<string, unknown> {
  const acao: Record<string, unknown> =
    form.action_kind === 'prompt' ? { texto: form.texto }
    : form.action_kind === 'tool' ? { tool: form.tool, arguments: safeJson(form.argumentos) }
    : form.action_kind === 'notify' ? { titulo: form.titulo, corpo: form.corpo }
    : { kind: form.job_kind, spec: safeJson(form.job_spec) };

  const condicao: Record<string, unknown> = {};
  if (form.cond_tipo) {
    condicao.tipo = form.cond_tipo;
    if (form.cond_tipo === 'tool') condicao.tool = form.cond_tool;
    if (form.cond_tipo === 'http') condicao.url = form.cond_url;
    if (form.cond_campo || form.cond_operador !== 'nao_vazio' || form.cond_valor) {
      condicao.espera = {
        campo: form.cond_campo || undefined,
        operador: form.cond_operador,
        valor: numeroOuTexto(form.cond_valor),
      };
    }
  }

  return {
    name: form.name,
    quando: form.quando,
    action_kind: form.action_kind,
    action_json: acao,
    notify_policy: form.notify_policy,
    session_mode: form.session_mode,
    misfire_policy: form.misfire_policy,
    only_if_changed: form.only_if_changed,
    condition_json: condicao,
  };
}

function safeJson(raw: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(raw || '{}');
    return typeof parsed === 'object' && parsed !== null ? parsed : {};
  } catch {
    return {};
  }
}

function numeroOuTexto(raw: string): unknown {
  if (raw === '') return undefined;
  const n = Number(raw);
  return Number.isNaN(n) ? raw : n;
}

// ── Componente ───────────────────────────────────────────────────────────────

const AgendaPanel = () => {
  const [tarefas, setTarefas] = useState<Task[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);
  const [aviso, setAviso] = useState<string | null>(null);
  const [pausadoGlobal, setPausadoGlobal] = useState(false);
  const [form, setForm] = useState<FormState | null>(null);
  const [preview, setPreview] = useState<{ descricao: string; proximos: string[] } | null>(null);
  const [previewErro, setPreviewErro] = useState<string | null>(null);
  const [ferramentas, setFerramentas] = useState<ToolDef[]>([]);
  const [historico, setHistorico] = useState<Record<string, Fire[]>>({});
  const [aberta, setAberta] = useState<string | null>(null);
  const [salvando, setSalvando] = useState(false);

  const carregar = useCallback(async () => {
    setCarregando(true);
    try {
      const data = await api<{ tarefas: Task[]; pausado_global: boolean }>(withUser(`${API}/tarefas`));
      setTarefas(data.tarefas ?? []);
      setPausadoGlobal(Boolean(data.pausado_global));
      setErro(null);
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    } finally {
      setCarregando(false);
    }
  }, []);

  useEffect(() => { void carregar(); }, [carregar]);

  useEffect(() => {
    if (form?.action_kind !== 'tool' || ferramentas.length) return;
    void api<{ ferramentas: ToolDef[] }>(`${API}/ferramentas`)
      .then((d) => setFerramentas(d.ferramentas ?? []))
      .catch(() => undefined);
  }, [form?.action_kind, ferramentas.length]);

  // Preview do horário: 400ms de silêncio antes de perguntar ao servidor.
  useEffect(() => {
    const quando = form?.quando?.trim();
    if (!quando) { setPreview(null); setPreviewErro(null); return; }
    const timer = window.setTimeout(() => {
      api<{ descricao: string; proximos: string[] }>(`${API}/preview?quando=${encodeURIComponent(quando)}`)
        .then((d) => { setPreview(d); setPreviewErro(null); })
        .catch((e: Error) => { setPreview(null); setPreviewErro(e.message); });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [form?.quando]);

  const salvar = async () => {
    if (!form) return;
    setSalvando(true);
    try {
      const payload = payloadFromForm(form);
      if (form.id) {
        await api(`${API}/tarefas/${form.id}`, { method: 'PATCH', body: JSON.stringify(payload) });
      } else {
        await api(withUser(`${API}/tarefas`), { method: 'POST', body: JSON.stringify(payload) });
      }
      setForm(null);
      setAviso(form.id ? 'Tarefa atualizada.' : 'Tarefa criada.');
      await carregar();
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    } finally {
      setSalvando(false);
    }
  };

  const alternar = async (task: Task) => {
    try {
      await api(`${API}/tarefas/${task.id}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: !task.enabled }),
      });
      await carregar();
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    }
  };

  const disparar = async (task: Task, simular: boolean) => {
    try {
      await api(`${API}/tarefas/${task.id}/${simular ? 'simular' : 'rodar'}`, { method: 'POST' });
      setAviso(
        simular
          ? `Simulação de "${task.name}" enfileirada — sem entrega. O resultado aparece no histórico.`
          : `"${task.name}" disparada agora.`,
      );
      window.setTimeout(() => void abrirHistorico(task.id, true), 1200);
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    }
  };

  const apagar = async (task: Task) => {
    if (!window.confirm(`Apagar a tarefa "${task.name}"? Isso não pode ser desfeito.`)) return;
    try {
      await api(`${API}/tarefas/${task.id}`, { method: 'DELETE' });
      await carregar();
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    }
  };

  const abrirHistorico = async (id: string, forcar = false) => {
    if (aberta === id && !forcar) { setAberta(null); return; }
    setAberta(id);
    try {
      const data = await api<{ disparos: Fire[] }>(`${API}/tarefas/${id}/disparos`);
      setHistorico((prev) => ({ ...prev, [id]: data.disparos ?? [] }));
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    }
  };

  const alternarKillSwitch = async () => {
    try {
      const d = await api<{ pausado: boolean }>(`${API}/pausar-tudo?pausar=${!pausadoGlobal}`, { method: 'POST' });
      setPausadoGlobal(d.pausado);
    } catch (e) {
      setErro(e instanceof Error ? e.message : String(e));
    }
  };

  const acoes = (
    <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
      <button className="neo-btn-ghost" onClick={() => void carregar()}>
        <Icon name="refresh" size={15} /> Atualizar
      </button>
      <button
        className={pausadoGlobal ? 'neo-btn-primary' : 'neo-btn-ghost'}
        onClick={() => void alternarKillSwitch()}
        title="Freio de emergência: para todos os disparos até religar (ou até o próximo restart)"
      >
        {pausadoGlobal ? 'Retomar tudo' : 'Pausar tudo'}
      </button>
      <button className="neo-btn-primary" onClick={() => setForm({ ...EMPTY_FORM })}>
        Nova tarefa
      </button>
    </div>
  );

  return (
    <Screen icon="clock" title="Agenda" actions={acoes}>
      <div className="neo-content">
        {pausadoGlobal && (
          <div className="neo-warn is-amber" style={{ marginBottom: 20 }}>
            <Icon name="warn" size={16} />
            <span>Agenda pausada por completo — nenhuma tarefa dispara até religar.</span>
          </div>
        )}
        {erro && (
          <div className="neo-warn" style={{ marginBottom: 20 }}>
            <Icon name="warn" size={16} />
            <span>{erro}</span>
            <button className="neo-btn-ghost" onClick={() => setErro(null)}>ok</button>
          </div>
        )}
        {aviso && (
          <div className="neo-note" style={{ marginBottom: 20 }}>
            {aviso} <button className="neo-btn-ghost" onClick={() => setAviso(null)}>fechar</button>
          </div>
        )}

        {form && (
          <FormCard
            form={form}
            setForm={setForm}
            preview={preview}
            previewErro={previewErro}
            ferramentas={ferramentas}
            salvando={salvando}
            onSalvar={() => void salvar()}
            onCancelar={() => setForm(null)}
          />
        )}

        {carregando && <p className="neo-muted">Carregando…</p>}

        {!carregando && !tarefas.length && !form && (
          <div className="neo-card">
            <h3 className="neo-h3">Nenhuma tarefa agendada</h3>
            <p className="neo-desc">
              A agenda é o eixo do tempo do Neo: um alerta fixo, uma ferramenta disparada
              sozinha, um job pesado ou um prompt que volta para ele processar como se você
              tivesse falado. Comece por um alerta — não gasta LLM nenhum.
            </p>
          </div>
        )}

        {tarefas.map((task) => (
          <div className="neo-card" key={task.id} style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap' }}>
              <div style={{ minWidth: 260, flex: 1 }}>
                <h3 className="neo-h3" style={{ marginBottom: 4 }}>
                  {task.name}{' '}
                  <span style={{ fontSize: 11, color: 'var(--neo-violet-pale)', fontWeight: 600 }}>
                    {ACTION_LABEL[task.action_kind] ?? task.action_kind}
                  </span>
                </h3>
                <p className="neo-desc" style={{ marginBottom: 6 }}>{task.descricao_horario}</p>
                <div className="neo-meta">
                  <span className={task.enabled ? 'neo-state-on' : 'neo-muted'}>
                    {task.enabled ? '● ligada' : '○ desligada'}
                  </span>
                  {' · '}próxima: <span className="neo-meta-value">{task.next_run_local ?? '—'}</span>
                  {' · '}execuções: <span className="neo-meta-value">{task.run_count}</span>
                  {task.skipped_count > 0 && <> · barradas pela guarda: <span className="neo-meta-value">{task.skipped_count}</span></>}
                  {task.last_status && (
                    <> · último: <span style={{ color: STATUS_COLOR[task.last_status] ?? 'var(--neo-text)' }}>{task.last_status}</span></>
                  )}
                  {task.only_if_changed && <> · <span className="neo-meta-faint">só se mudar</span></>}
                </div>
                {task.paused_reason && (
                  <div className="neo-warn is-amber" style={{ marginTop: 10 }}>
                    <Icon name="warn" size={14} /><span>{task.paused_reason}</span>
                  </div>
                )}
              </div>
              <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                <button className={task.enabled ? 'neo-toggle is-on' : 'neo-toggle is-off'} onClick={() => void alternar(task)}>
                  {task.enabled ? 'Desligar' : 'Ligar'}
                </button>
                <button className="neo-btn-ghost" onClick={() => setForm(formFromTask(task))}>Editar</button>
                <button className="neo-btn-ghost" onClick={() => void disparar(task, false)}>Rodar agora</button>
                <button className="neo-btn-ghost" onClick={() => void disparar(task, true)} title="Roda de verdade, mas sem entregar nada">
                  Simular
                </button>
                <button className="neo-btn-ghost" onClick={() => void abrirHistorico(task.id)}>
                  {aberta === task.id ? 'Fechar' : 'Histórico'}
                </button>
                <button className="neo-btn-danger" onClick={() => void apagar(task)}>Apagar</button>
              </div>
            </div>

            {aberta === task.id && (
              <div style={{ marginTop: 18, borderTop: '1px solid var(--neo-hairline)', paddingTop: 14 }}>
                {!(historico[task.id]?.length) && <p className="neo-muted">Nenhum disparo ainda.</p>}
                {(historico[task.id] ?? []).map((f) => (
                  <div key={f.job_id} style={{ marginBottom: 12, fontSize: 13 }}>
                    <div className="neo-meta" style={{ marginTop: 0 }}>
                      <span style={{ color: STATUS_COLOR[f.status] ?? 'var(--neo-text)' }}>{f.status}</span>
                      {' · '}{f.criado_em ? new Date(f.criado_em + 'Z').toLocaleString('pt-BR') : '—'}
                      {f.duracao_s !== null && <> · {f.duracao_s}s</>}
                      {' · '}{f.entregue ? 'entregue' : 'não entregue'}
                      {f.motivo && f.motivo !== 'agenda' && <> · <span className="neo-meta-faint">{f.motivo}</span></>}
                    </div>
                    <pre style={{
                      margin: '6px 0 0', padding: '10px 12px', borderRadius: 8,
                      background: 'rgba(20,20,31,0.8)', border: '1px solid var(--neo-hairline)',
                      whiteSpace: 'pre-wrap', wordBreak: 'break-word', color: 'var(--neo-text-dim)',
                      fontSize: 12.5, maxHeight: 220, overflow: 'auto',
                    }}>
                      {f.texto_entregue || f.resultado || '(sem saída)'}
                    </pre>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </Screen>
  );
};

// ── Card do formulário ───────────────────────────────────────────────────────

interface FormCardProps {
  form: FormState;
  setForm: (f: FormState) => void;
  preview: { descricao: string; proximos: string[] } | null;
  previewErro: string | null;
  ferramentas: ToolDef[];
  salvando: boolean;
  onSalvar: () => void;
  onCancelar: () => void;
}

const FormCard = ({ form, setForm, preview, previewErro, ferramentas, salvando, onSalvar, onCancelar }: FormCardProps) => {
  const set = (patch: Partial<FormState>) => setForm({ ...form, ...patch });
  const toolSelecionada = useMemo(
    () => ferramentas.find((t) => t.name === form.tool),
    [ferramentas, form.tool],
  );

  return (
    <div className="neo-card" style={{ marginBottom: 24 }}>
      <h3 className="neo-h3">{form.id ? 'Editar tarefa' : 'Nova tarefa'}</h3>

      <div className="neo-field">
        <label className="neo-field-label">Nome</label>
        <input className="neo-input" value={form.name} placeholder="Resumo da manhã"
               onChange={(e) => set({ name: e.target.value })} />
      </div>

      <div className="neo-field">
        <label className="neo-field-label">Quando</label>
        <input className="neo-input" value={form.quando}
               placeholder="todo dia às 7h · toda segunda às 9h · a cada 30 minutos · 0 7 * * 1-5"
               onChange={(e) => set({ quando: e.target.value })} />
        {preview && (
          <div className="neo-note" style={{ marginTop: 10 }}>
            <strong>{preview.descricao}</strong>
            {preview.proximos.length > 0 && (
              <div className="neo-meta" style={{ marginTop: 4 }}>
                próximos: {preview.proximos.join(' · ')}
              </div>
            )}
          </div>
        )}
        {previewErro && (
          <div className="neo-warn" style={{ marginTop: 10 }}>
            <Icon name="warn" size={14} /><span>{previewErro}</span>
          </div>
        )}
      </div>

      <div className="neo-field">
        <label className="neo-field-label">O que fazer</label>
        <div className="neo-tabbar" style={{ margin: '4px 0 14px' }}>
          {(['notify', 'tool', 'prompt', 'job'] as ActionKind[]).map((kind) => (
            <button key={kind}
                    className={`neo-tab${form.action_kind === kind ? ' is-active' : ''}`}
                    onClick={() => set({ action_kind: kind })}>
              {ACTION_LABEL[kind]}
            </button>
          ))}
        </div>

        {form.action_kind === 'notify' && (
          <>
            <p className="neo-desc">Texto fixo, entregue como está. Não gasta LLM nenhum.</p>
            <input className="neo-input" placeholder="Título" value={form.titulo}
                   onChange={(e) => set({ titulo: e.target.value })} />
            <textarea className="neo-input" rows={3} placeholder="Corpo do aviso" value={form.corpo}
                      onChange={(e) => set({ corpo: e.target.value })} />
          </>
        )}

        {form.action_kind === 'tool' && (
          <>
            <p className="neo-desc">Uma ferramenta do servidor, direto — sem turno cognitivo.</p>
            <select className="neo-select" value={form.tool} onChange={(e) => set({ tool: e.target.value })}>
              <option value="">— escolha a ferramenta —</option>
              {ferramentas.map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}
            </select>
            {toolSelecionada && <p className="neo-desc" style={{ marginTop: 8 }}>{toolSelecionada.description}</p>}
            <label className="neo-field-label" style={{ marginTop: 12 }}>Argumentos (JSON)</label>
            <textarea className="neo-input" rows={4} value={form.argumentos}
                      onChange={(e) => set({ argumentos: e.target.value })} />
          </>
        )}

        {form.action_kind === 'prompt' && (
          <>
            <p className="neo-desc">
              O texto é reenviado ao Neo na hora marcada, como se você tivesse falado. Escreva
              auto-contido: na hora, ele não terá o contexto desta tela.
            </p>
            <textarea className="neo-input" rows={4} value={form.texto}
                      placeholder="Resume o que ficou pendente ontem e o que tenho hoje."
                      onChange={(e) => set({ texto: e.target.value })} />
            <label className="neo-field-label" style={{ marginTop: 12 }}>Conversa</label>
            <select className="neo-select" value={form.session_mode}
                    onChange={(e) => set({ session_mode: e.target.value })}>
              <option value="nova">nova a cada disparo (padrão)</option>
              <option value="propria">própria da tarefa (compara com o resultado anterior)</option>
              <option value="ativa">a conversa ativa (cuidado: mistura com o seu papo)</option>
            </select>
          </>
        )}

        {form.action_kind === 'job' && (
          <>
            <p className="neo-desc">Enfileira um trabalho pesado. Quem avisa o resultado é o próprio job.</p>
            <select className="neo-select" value={form.job_kind} onChange={(e) => set({ job_kind: e.target.value })}>
              <option value="research">deep research</option>
              <option value="sandbox_task">comando no sandbox</option>
              <option value="browser_local">navegador do usuário</option>
              <option value="browser">navegador headless</option>
            </select>
            <label className="neo-field-label" style={{ marginTop: 12 }}>Parâmetros (JSON)</label>
            <textarea className="neo-input" rows={4} value={form.job_spec}
                      onChange={(e) => set({ job_spec: e.target.value })} />
          </>
        )}
      </div>

      <div className="neo-field">
        <label className="neo-field-label">Guarda (opcional)</label>
        <p className="neo-desc">
          Sem guarda, todo alerta periódico vira ruído em uma semana. A condição roda antes e,
          se não passar, o disparo nem acontece — não conta como execução nem como erro.
        </p>
        <select className="neo-select" value={form.cond_tipo} onChange={(e) => set({ cond_tipo: e.target.value })}>
          <option value="">sem condição</option>
          <option value="tool">resultado de uma ferramenta</option>
          <option value="device">algum aparelho online</option>
          <option value="http">resposta de uma URL</option>
        </select>
        {form.cond_tipo === 'tool' && (
          <input className="neo-input" placeholder="nome da ferramenta" value={form.cond_tool}
                 onChange={(e) => set({ cond_tool: e.target.value })} />
        )}
        {form.cond_tipo === 'http' && (
          <input className="neo-input" placeholder="https://…" value={form.cond_url}
                 onChange={(e) => set({ cond_url: e.target.value })} />
        )}
        {form.cond_tipo && (
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <input className="neo-input" style={{ flex: 1, minWidth: 130 }} placeholder="campo (ex: total)"
                   value={form.cond_campo} onChange={(e) => set({ cond_campo: e.target.value })} />
            <select className="neo-select" style={{ width: 150 }} value={form.cond_operador}
                    onChange={(e) => set({ cond_operador: e.target.value })}>
              {['nao_vazio', 'vazio', '>', '>=', '<', '<=', '==', '!=', 'contem'].map((op) => (
                <option key={op} value={op}>{op}</option>
              ))}
            </select>
            <input className="neo-input" style={{ width: 130 }} placeholder="valor"
                   value={form.cond_valor} onChange={(e) => set({ cond_valor: e.target.value })} />
          </div>
        )}
      </div>

      <div className="neo-field">
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <div style={{ flex: 1, minWidth: 180 }}>
            <label className="neo-field-label">Entrega</label>
            <select className="neo-select" value={form.notify_policy}
                    onChange={(e) => set({ notify_policy: e.target.value })}>
              <option value="chat">no chat</option>
              <option value="notch">no notch</option>
              <option value="both">nos dois</option>
              <option value="silent">em silêncio (só registra)</option>
            </select>
          </div>
          <div style={{ flex: 1, minWidth: 180 }}>
            <label className="neo-field-label">Se perder o horário</label>
            <select className="neo-select" value={form.misfire_policy}
                    onChange={(e) => set({ misfire_policy: e.target.value })}>
              <option value="skip">pular (padrão)</option>
              <option value="run_once">rodar uma vez atrasado</option>
              <option value="run_all">recuperar os perdidos</option>
            </select>
          </div>
        </div>
        <label className="neo-toggle-row" style={{ marginTop: 12 }}>
          <input type="checkbox" checked={form.only_if_changed}
                 onChange={(e) => set({ only_if_changed: e.target.checked })} />
          <span className="neo-toggle-label">
            Só avisar quando o resultado mudar (antídoto da fadiga de alerta)
          </span>
        </label>
      </div>

      <div className="neo-actions">
        <button className="neo-btn-primary" disabled={salvando || !form.name || !form.quando} onClick={onSalvar}>
          {salvando ? 'Salvando…' : form.id ? 'Salvar' : 'Criar tarefa'}
        </button>
        <button className="neo-btn-ghost" onClick={onCancelar}>Cancelar</button>
      </div>
    </div>
  );
};

export default AgendaPanel;
