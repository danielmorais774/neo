// Visor das tarefas em background do Neo — o que roda longe da conversa.
//
// Por que esta tela existe: um comando longo agora vira job sozinho (o `neo_bash`
// que passa de 2 min trabalhando), e um OCR de vídeo roda minutos em segundo
// plano. Sem um lugar para olhar, "está trabalhando" e "travou" são
// indistinguíveis — e a reação natural é pedir de novo, disparando o mesmo
// trabalho duas vezes.
//
// Atualiza sozinha: a cada 4s enquanto houver tarefa ativa (lista, detalhe e o fim
// do log da aberta), a cada 20s quando está tudo parado. O único gesto de escrita
// é cancelar — a mesma regra da tool `job_cancel` no core.

import { useCallback, useEffect, useRef, useState } from 'react';
import type { JobDetail, JobLogs, JobSummary } from '../lib/tauriApi';
import { humanizeTime } from '../hooks/useSessionManagement';
import { CopyButton } from './CodeBlock';
import './WorkingMemoryViewer.css';
import './JobsViewer.css';

interface JobsViewerProps {
  /** Fecha a JANELA (ver main.tsx). */
  onClose: () => void;
}

type LoadState = 'loading' | 'ready' | 'error';
type Filter = 'all' | 'active';

const ACTIVE_STATUSES = new Set(['queued', 'running']);
// Cancelar vale também para a tarefa que PAROU esperando você (um card de
// liberação, um "pode enviar"): sem isto ela ficava na lista sem saída.
const CANCELABLE_STATUSES = new Set(['queued', 'running', 'waiting']);
const FAST_POLL_MS = 4_000;
const SLOW_POLL_MS = 20_000;
const LOG_TAIL_BYTES = 16_000;

const STATUS_LABEL: Record<string, string> = {
  queued: 'na fila',
  running: 'rodando',
  completed: 'concluída',
  failed: 'falhou',
  timeout: 'estourou',
  cancelled: 'cancelada',
  waiting: 'esperando você',
  continued: 'continuada',
};

const KIND_LABEL: Record<string, string> = {
  sandbox_task: 'sandbox',
  browser: 'navegador',
  browser_local: 'seu navegador',
  research: 'pesquisa',
  download_watch: 'download',
  scheduled_prompt: 'turno agendado',
  scheduled_tool: 'agenda',
  scheduled_notify: 'lembrete',
  scheduled_job: 'agenda',
};

function kindLabel(job: JobSummary): string {
  if (job.continuation_of) return 'continuação';
  return KIND_LABEL[job.kind] ?? job.kind;
}

function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}min ${rest}s` : `${minutes}min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}min`;
}

/** `2026-09-13T03:47:18` (UTC, sem sufixo) → data/hora local legível. */
function formatStamp(iso: string | null): string {
  if (!iso) return '—';
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function StatusBadge({ status }: { status: string }) {
  return <span className={`jobs-status jobs-status-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export default function JobsViewer({ onClose }: JobsViewerProps) {
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [activeCount, setActiveCount] = useState(0);
  const [listState, setListState] = useState<LoadState>('loading');
  const [listError, setListError] = useState('');
  const [filter, setFilter] = useState<Filter>('all');

  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [logs, setLogs] = useState<JobLogs | null>(null);
  const [detailState, setDetailState] = useState<LoadState>('loading');
  const [detailError, setDetailError] = useState('');
  const [actionError, setActionError] = useState('');
  const [cancelling, setCancelling] = useState(false);

  // Sobe a cada volta do relógio (e a cada "recarregar"/cancelar): entra nas
  // dependências da lista E do detalhe, porque uma tarefa rodando muda os dois.
  const [tick, setTick] = useState(0);
  const logRef = useRef<HTMLPreElement>(null);
  // Só acompanha o fim do log se a pessoa não rolou para cima para ler algo.
  const followLog = useRef(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const page = await window.electronAPI?.listJobs(filter);
        if (cancelled) return;
        const list = page?.jobs ?? [];
        setJobs(list);
        setCounts(page?.counts ?? {});
        setActiveCount(page?.active ?? 0);
        setListError('');
        setListState('ready');
        // Abre a mais relevante sozinha (ativas vêm primeiro na lista).
        setSelected((current) => current ?? list[0]?.id ?? null);
      } catch (err) {
        if (cancelled) return;
        console.error('[Neo] Falha ao carregar as tarefas:', err);
        setListError(String(err));
        setListState('error');
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [filter, tick]);

  useEffect(() => {
    const id = window.setInterval(
      () => setTick((value) => value + 1),
      activeCount > 0 ? FAST_POLL_MS : SLOW_POLL_MS,
    );
    return () => window.clearInterval(id);
  }, [activeCount]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    const open = async () => {
      try {
        const [job, log] = await Promise.all([
          window.electronAPI?.getJob(selected),
          window.electronAPI?.getJobLogs(selected, LOG_TAIL_BYTES),
        ]);
        if (cancelled) return;
        setDetail(job ?? null);
        setLogs(log ?? null);
        setDetailError('');
        setDetailState('ready');
      } catch (err) {
        if (cancelled) return;
        console.error('[Neo] Falha ao abrir a tarefa:', err);
        setDetailError(String(err));
        setDetailState('error');
      }
    };
    void open();
    return () => {
      cancelled = true;
    };
  }, [selected, tick]);

  useEffect(() => {
    const el = logRef.current;
    if (el && followLog.current) el.scrollTop = el.scrollHeight;
  }, [logs]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const selectJob = (id: string) => {
    if (id === selected) return;
    // "Abrindo…" só na troca de tarefa; a atualização periódica mantém o que já
    // está na tela até a resposta chegar, sem piscar.
    setDetail(null);
    setLogs(null);
    setActionError('');
    setDetailState('loading');
    followLog.current = true;
    setSelected(id);
  };

  const cancel = useCallback(async () => {
    if (!detail) return;
    setCancelling(true);
    setActionError('');
    try {
      const result = await window.electronAPI?.cancelJob(detail.id);
      if (result && !result.ok && result.error) setActionError(result.error);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setCancelling(false);
      setTick((value) => value + 1);
    }
  }, [detail]);

  const getDetailText = useCallback(() => detail?.detail ?? '', [detail]);
  const getLogText = useCallback(() => logs?.logs ?? '', [logs]);

  const running = counts.running ?? 0;
  const queued = counts.queued ?? 0;
  const completed = counts.completed ?? 0;
  const failed = (counts.failed ?? 0) + (counts.timeout ?? 0);
  const isActive = detail ? ACTIVE_STATUSES.has(detail.status) : false;
  const isCancelable = detail ? CANCELABLE_STATUSES.has(detail.status) : false;

  return (
    <section className="wm-screen wm-standalone jobs-screen" role="region" aria-labelledby="jobs-title">
      <header className="wm-header">
        <div className="wm-header-copy">
          <h2 id="jobs-title">Tarefas em background</h2>
          <p>
            O que o Neo está rodando longe da conversa — e o que já terminou.
            {listState === 'ready' && (
              <>
                {' '}
                <strong>{running}</strong> rodando · {queued} na fila · {completed} concluídas ·{' '}
                {failed} com falha
              </>
            )}
          </p>
        </div>
        <div className="wm-header-actions">
          <div className="jobs-filter" role="radiogroup" aria-label="Filtrar tarefas">
            {(['all', 'active'] as Filter[]).map((option) => (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={filter === option}
                className={filter === option ? 'selected' : ''}
                onClick={() => setFilter(option)}
              >
                {option === 'all' ? 'Todas' : 'Ativas'}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="wm-icon-btn"
            onClick={() => setTick((value) => value + 1)}
            title="Recarregar do neo-core"
            aria-label="Recarregar"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
              <polyline points="3 3 3 8 8 8" />
            </svg>
          </button>
        </div>
      </header>

      <div className="wm-columns">
        <aside className="wm-list-pane" aria-label="Tarefas">
          {listState === 'loading' ? (
            <div className="wm-state">Carregando as tarefas…</div>
          ) : listState === 'error' ? (
            <div className="wm-state wm-state-error">
              Não foi possível falar com o neo-core.
              <code>{listError}</code>
              <button type="button" className="wm-retry" onClick={() => setTick((value) => value + 1)}>
                Tentar de novo
              </button>
            </div>
          ) : jobs.length === 0 ? (
            <div className="wm-state">
              {filter === 'active'
                ? 'Nada rodando agora.'
                : 'Nenhuma tarefa ainda. Trabalho demorado do Neo aparece aqui.'}
            </div>
          ) : (
            <ul className="wm-list">
              {jobs.map((job) => {
                const isCurrent = job.id === selected;
                return (
                  <li key={job.id}>
                    <button
                      type="button"
                      className={`wm-item${isCurrent ? ' current' : ''}`}
                      onClick={() => selectJob(job.id)}
                      aria-current={isCurrent}
                    >
                      <span className="wm-item-top">
                        <span className="wm-item-title">{job.title || job.kind}</span>
                        <StatusBadge status={job.status} />
                      </span>
                      {job.detail && <span className="jobs-item-detail">{job.detail}</span>}
                      <span className="wm-item-meta">
                        <span className="wm-badge wm-badge-auto">{kindLabel(job)}</span>
                        {job.adopted && (
                          <span
                            className="wm-badge wm-badge-model"
                            title="Começou em primeiro plano e virou tarefa por demorar — não foi interrompido"
                          >
                            virou tarefa
                          </span>
                        )}
                        <span className="wm-item-dot">·</span>
                        {humanizeTime(job.created_at)}
                        {job.duration_s !== null && (
                          <>
                            <span className="wm-item-dot">·</span>
                            {formatDuration(job.duration_s)}
                          </>
                        )}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <div className="wm-preview-pane">
          {!selected ? (
            <div className="wm-state">Escolha uma tarefa à esquerda.</div>
          ) : detailState === 'loading' ? (
            <div className="wm-state">Abrindo a tarefa…</div>
          ) : detailState === 'error' ? (
            <div className="wm-state wm-state-error">
              {detailError}
              <button type="button" className="wm-retry" onClick={() => setTick((value) => value + 1)}>
                Recarregar
              </button>
            </div>
          ) : detail ? (
            <>
              <div className="wm-preview-bar">
                <div className="wm-preview-facts">
                  <code title={detail.id}>{detail.id.slice(0, 8)}</code>
                  <span>criada {formatStamp(detail.created_at)}</span>
                  {detail.started_at && <span>início {formatStamp(detail.started_at)}</span>}
                  {detail.finished_at ? (
                    <span>fim {formatStamp(detail.finished_at)}</span>
                  ) : isActive && detail.heartbeat_at ? (
                    <span>sinal de vida {humanizeTime(detail.heartbeat_at)}</span>
                  ) : null}
                  <span>duração {formatDuration(detail.duration_s)}</span>
                  {detail.exit_code !== null && <span>exit {detail.exit_code}</span>}
                </div>
                {isCancelable && (
                  <button type="button" className="jobs-cancel" onClick={cancel} disabled={cancelling}>
                    {cancelling ? 'Cancelando…' : 'Cancelar'}
                  </button>
                )}
              </div>

              <div className="jobs-body">
                <h3 className="jobs-title">
                  <span>{detail.title}</span>
                  <StatusBadge status={detail.status} />
                </h3>
                {actionError && <div className="jobs-error">{actionError}</div>}

                {detail.detail && (
                  <section className="jobs-block">
                    <div className="jobs-block-head">
                      <span>Pedido</span>
                      <CopyButton getText={getDetailText} className="wm-copy" />
                    </div>
                    <pre className="jobs-pre">{detail.detail}</pre>
                  </section>
                )}

                {detail.then && (
                  <section className="jobs-block">
                    <div className="jobs-block-head">
                      <span>Quando terminar, o Neo vai</span>
                    </div>
                    <p className="jobs-then">{detail.then}</p>
                  </section>
                )}

                {!isActive && detail.result_summary && (
                  <section className="jobs-block">
                    <div className="jobs-block-head">
                      <span>Resultado</span>
                    </div>
                    <pre className="jobs-pre">{detail.result_summary}</pre>
                  </section>
                )}

                <section className="jobs-block jobs-log-block">
                  <div className="jobs-block-head">
                    <span>Log{logs?.truncated ? ' (fim)' : ''}</span>
                    {isActive && <span className="jobs-live">atualizando</span>}
                    <CopyButton getText={getLogText} className="wm-copy" />
                  </div>
                  <pre
                    ref={logRef}
                    className="jobs-pre jobs-log"
                    onScroll={(event) => {
                      const el = event.currentTarget;
                      followLog.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
                    }}
                  >
                    {logs?.logs || (isActive ? 'Ainda sem saída.' : 'Sem log.')}
                  </pre>
                </section>
              </div>
            </>
          ) : null}
        </div>
      </div>
    </section>
  );
}
