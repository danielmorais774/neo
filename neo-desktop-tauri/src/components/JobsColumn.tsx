// Coluna direita do chat largo: o que o Neo está rodando em segundo plano.
//
// Lê o useJobs (uma consulta só para todas as colunas). Os cartões só têm ações
// que já existem (plano, decisão 4): Abrir no visor (a janela Tarefas, já na
// tarefa), Cancelar (mesma regra do visor e da tool `job_cancel`) e Ir para a
// conversa. A tarefa que espera você NÃO tem "Enviar" aqui: a liberação fica no
// card do chat, que é onde está a conferência.

import { useEffect, useMemo, useState } from 'react';
import type { JobSummary } from '../lib/tauriApi';
import {
  ACTIVE_STATUSES,
  CANCELABLE_STATUSES,
  OPEN_STATUSES,
  elapsedSeconds,
  formatDuration,
  kindLabel,
} from '../lib/jobs';
import {
  readJobsColumnFilter,
  storeJobsColumnFilter,
  type JobsColumnFilter,
} from '../lib/chatPrefs';
import { openConversation, useActiveSession } from '../lib/activeSession';
import { refreshJobs, useJobs } from '../hooks/useJobs';
import { useSessionsList } from '../hooks/useSessionsList';
import { humanizeTime } from '../hooks/useSessionManagement';
import { ColumnIcon, type ColumnIconName } from './ColumnIcon';
import { StatusBadge } from './JobStatusBadge';
import './JobsColumn.css';

/** O cartão aberto mostra só o fim do log: poucas linhas, poucos bytes. */
const LOG_TAIL_BYTES = 2_000;
const LOG_LINES = 4;

// ESC (0x1b) montado em runtime: um literal de controle na regex quebra o lint.
const ANSI_RE = new RegExp(`${String.fromCharCode(27)}\\[[0-9;?]*[A-Za-z]`, 'g');

function lastLines(text: string, count: number): string[] {
  return text
    .replace(ANSI_RE, '')
    .split('\n')
    .map((line) => line.trimEnd())
    .filter((line) => line.trim())
    .slice(-count);
}

function iconForKind(kind: string): ColumnIconName {
  if (kind === 'sandbox_task') return 'terminal';
  if (kind === 'comando_local') return 'monitor';
  if (kind === 'browser_local') return 'globe';
  if (kind === 'browser') return 'server';
  if (kind === 'claude_code') return 'code';
  if (kind === 'research') return 'search';
  if (kind === 'download_watch') return 'download';
  if (kind.startsWith('scheduled_')) return 'clock';
  return 'layers';
}

function timeLabel(job: JobSummary, fetchedAt: number): string {
  if (job.status === 'running') {
    const seconds = job.duration_s ?? elapsedSeconds(job, fetchedAt);
    return seconds === null ? 'rodando' : `há ${formatDuration(seconds)}`;
  }
  if (job.status === 'queued') return `pedida ${humanizeTime(job.created_at)}`;
  const when = humanizeTime(job.finished_at ?? job.created_at);
  return job.duration_s !== null ? `${when} · ${formatDuration(job.duration_s)}` : when;
}

interface JobCardProps {
  job: JobSummary;
  expanded: boolean;
  isHere: boolean;
  originTitle: string;
  fetchedAt: number;
  /** Versão da consulta: a tarefa ativa relê o fim do log a cada volta. */
  version: number;
  onToggle: (jobId: string) => void;
}

function JobCard({ job, expanded, isHere, originTitle, fetchedAt, version, onToggle }: JobCardProps) {
  const [log, setLog] = useState<string[] | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [actionError, setActionError] = useState('');
  const isActive = ACTIVE_STATUSES.has(job.status);
  const canCancel = CANCELABLE_STATUSES.has(job.status);
  const isWaiting = job.status === 'waiting';
  // Parada: lê uma vez. Ativa: relê a cada volta da consulta (4 s).
  const logKey = isActive ? version : job.status;

  useEffect(() => {
    if (!expanded || !job.has_log) return;
    let cancelled = false;
    window.electronAPI
      ?.getJobLogs(job.id, LOG_TAIL_BYTES)
      .then((result) => {
        if (!cancelled) setLog(lastLines(result?.logs ?? '', LOG_LINES));
      })
      .catch((err) => {
        console.warn('[Neo] Fim do log da tarefa não veio:', err);
        if (!cancelled) setLog([]);
      });
    return () => {
      cancelled = true;
    };
  }, [expanded, job.id, job.has_log, logKey]);

  const cancel = async () => {
    setCancelling(true);
    setActionError('');
    try {
      const result = await window.electronAPI?.cancelJob(job.id);
      if (result && !result.ok && result.error) setActionError(result.error);
    } catch (err) {
      setActionError(String(err));
    } finally {
      setCancelling(false);
      void refreshJobs();
    }
  };

  const goToConversation = () => {
    if (job.session_id) openConversation(job.session_id);
  };

  return (
    <article className={`bg-card${expanded ? ' expanded' : ''}${isWaiting ? ' waiting' : ''}`}>
      <button
        type="button"
        className="bg-card-head"
        onClick={() => onToggle(job.id)}
        aria-expanded={expanded}
      >
        <span className="bg-card-icon">
          <ColumnIcon name={iconForKind(job.kind)} />
        </span>
        <span className="bg-card-text">
          <span className="bg-card-title">{job.title || kindLabel(job)}</span>
          <span className="bg-card-sub">
            {kindLabel(job)} · {timeLabel(job, fetchedAt)}
          </span>
        </span>
        <StatusBadge status={job.status} />
      </button>

      {job.preview && <div className="bg-card-preview">{job.preview}</div>}

      {isWaiting && (
        <div className="bg-card-waiting">
          <span>
            {isHere
              ? 'Parou esperando você. A liberação está no card desta conversa.'
              : 'Parou esperando você. A liberação está no card da conversa dela.'}
          </span>
          {!isHere && job.session_id && (
            <button type="button" className="bg-btn bg-btn-waiting" onClick={goToConversation}>
              Ir para a conversa
            </button>
          )}
        </div>
      )}

      {expanded && (
        <div className="bg-card-body">
          {job.has_log && (
            <pre className="bg-card-log">
              {log === null ? 'Lendo o log…' : log.length ? log.join('\n') : 'Ainda sem saída.'}
            </pre>
          )}
          {job.session_id && (
            <div className="bg-card-origin">
              <span>de:</span>
              {isHere ? (
                <span className="bg-card-origin-title">{originTitle}</span>
              ) : (
                <button type="button" className="bg-link" onClick={goToConversation} title="Ir para a conversa">
                  {originTitle}
                </button>
              )}
              {isHere && <span className="bg-here">esta conversa</span>}
            </div>
          )}
          {actionError && <div className="bg-card-error">{actionError}</div>}
          <div className="bg-card-actions">
            <button type="button" className="bg-btn" onClick={() => void window.electronAPI?.openJobsWindow(job.id)}>
              Abrir no visor
            </button>
            {canCancel && (
              <button type="button" className="bg-btn bg-btn-danger" onClick={cancel} disabled={cancelling}>
                {cancelling ? 'Cancelando…' : 'Cancelar'}
              </button>
            )}
          </div>
        </div>
      )}
    </article>
  );
}

interface JobsColumnProps {
  /** Na janela média a coluna abre por cima do chat: aí ela tem o X. */
  onClose?: () => void;
}

export function JobsColumn({ onClose }: JobsColumnProps) {
  const { jobs, counts, status, error, fetchedAt, version } = useJobs();
  const { sessions } = useSessionsList();
  const currentSessionId = useActiveSession();
  const [filter, setFilterState] = useState<JobsColumnFilter>(readJobsColumnFilter);
  const [expanded, setExpanded] = useState<string | null>(null);

  const titles = useMemo(
    () => new Map(sessions.map((session) => [session.session_id, session.title || 'Conversa sem título'])),
    [sessions],
  );
  const openJobs = useMemo(() => jobs.filter((job) => OPEN_STATUSES.has(job.status)), [jobs]);
  const hereJobs = useMemo(
    () => (currentSessionId ? jobs.filter((job) => job.session_id === currentSessionId) : []),
    [jobs, currentSessionId],
  );
  const shown = filter === 'open' ? openJobs : filter === 'session' ? hereJobs : jobs;

  const setFilter = (next: JobsColumnFilter) => {
    setFilterState(next);
    storeJobsColumnFilter(next);
  };
  const toggle = (jobId: string) => setExpanded((current) => (current === jobId ? null : jobId));

  const running = counts.running ?? 0;
  const queued = counts.queued ?? 0;
  const waiting = counts.waiting ?? 0;
  const summary = [
    queued ? `${queued} na fila` : '',
    waiting ? `${waiting} esperando você` : '',
    `${counts.completed ?? 0} concluídas`,
  ]
    .filter(Boolean)
    .join(' · ');

  const filters: { id: JobsColumnFilter; label: string; count?: number }[] = [
    { id: 'open', label: 'Ativas', count: openJobs.length },
    { id: 'session', label: 'Desta conversa', count: hereJobs.length },
    { id: 'all', label: 'Todas' },
  ];

  const emptyLabel =
    filter === 'open'
      ? 'Nada rodando agora.'
      : filter === 'session'
        ? 'Esta conversa não abriu tarefa.'
        : 'Nenhuma tarefa ainda. Trabalho demorado do Neo aparece aqui.';

  return (
    <section className="bg-column" aria-labelledby="bg-column-title">
      <div className="bg-head">
        <div className="bg-head-row">
          <h2 id="bg-column-title">Segundo plano</h2>
          {running > 0 && (
            <span className="bg-running">
              <span className="bg-pulse" />
              {running} rodando
            </span>
          )}
          {onClose && (
            <button type="button" className="bg-close" onClick={onClose} aria-label="Esconder segundo plano" title="Esconder">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <line x1="18" y1="6" x2="6" y2="18" />
                <line x1="6" y1="6" x2="18" y2="18" />
              </svg>
            </button>
          )}
        </div>
        {status === 'ready' && <span className="bg-summary">{summary}</span>}
        <div className="bg-filter" role="radiogroup" aria-label="Filtrar tarefas">
          {filters.map((option) => (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={filter === option.id}
              className={filter === option.id ? 'selected' : ''}
              onClick={() => setFilter(option.id)}
            >
              {option.label}
              {option.count !== undefined && <span className="bg-filter-count">{option.count}</span>}
            </button>
          ))}
        </div>
      </div>

      <div className="bg-list">
        {status === 'idle' || status === 'loading' ? (
          <div className="bg-state">Carregando as tarefas…</div>
        ) : status === 'error' ? (
          <div className="bg-state bg-state-error">
            Não foi possível falar com o neo-core.
            <code>{error}</code>
            <button type="button" className="bg-btn" onClick={() => void refreshJobs()}>
              Tentar de novo
            </button>
          </div>
        ) : shown.length === 0 ? (
          <div className="bg-state">{emptyLabel}</div>
        ) : (
          shown.map((job) => (
            <JobCard
              key={job.id}
              job={job}
              expanded={expanded === job.id}
              isHere={!!currentSessionId && job.session_id === currentSessionId}
              originTitle={
                (job.session_id && titles.get(job.session_id)) ||
                (job.session_id === currentSessionId ? 'Nova conversa' : `conversa ${job.session_id?.slice(0, 8)}`)
              }
              fetchedAt={fetchedAt}
              version={version}
              onToggle={toggle}
            />
          ))
        )}
      </div>

      <div className="bg-foot">
        <button type="button" className="bg-btn bg-btn-wide" onClick={() => void window.electronAPI?.openJobsWindow()}>
          Abrir visor de tarefas
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
            <polyline points="15 3 21 3 21 9" />
            <line x1="10" y1="14" x2="21" y2="3" />
          </svg>
        </button>
      </div>
    </section>
  );
}

export default JobsColumn;
