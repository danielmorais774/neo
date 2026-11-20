// Rótulos e regras das tarefas em background, compartilhados pela janela Tarefas
// (JobsViewer) e pela coluna "Segundo plano" do chat largo (JobsColumn).
//
// As regras de status espelham o core: `_JOB_ACTIVE_STATUSES` em
// neo-core/api/routes/desktop.py (queued/running) e a da tool `job_cancel`.

import type { JobSummary } from './tauriApi';

/** Ainda trabalhando: é o que faz o visor e a coluna consultarem a cada 4 s. */
export const ACTIVE_STATUSES = new Set(['queued', 'running']);

/** Cancelar vale também para a tarefa que PAROU esperando você (um card de
 *  liberação, um "pode enviar"): sem isto ela ficava na lista sem saída. */
export const CANCELABLE_STATUSES = new Set(['queued', 'running', 'waiting']);

/** Ainda não acabou: o filtro "Ativas" da coluna e o selo das conversas. Inclui a
 *  que espera você — ela não roda, mas também não terminou. */
export const OPEN_STATUSES = new Set(['queued', 'running', 'waiting']);

export const STATUS_LABEL: Record<string, string> = {
  queued: 'na fila',
  running: 'rodando',
  completed: 'concluída',
  failed: 'falhou',
  timeout: 'estourou',
  cancelled: 'cancelada',
  waiting: 'esperando você',
  continued: 'continuada',
};

export const KIND_LABEL: Record<string, string> = {
  sandbox_task: 'sandbox',
  browser: 'navegador',
  browser_local: 'seu navegador',
  claude_code: 'Claude Code',
  comando_local: 'seu computador',
  research: 'pesquisa',
  download_watch: 'download',
  scheduled_prompt: 'turno agendado',
  scheduled_tool: 'agenda',
  scheduled_notify: 'lembrete',
  scheduled_job: 'agenda',
};

export function kindLabel(job: JobSummary): string {
  if (job.continuation_of) return 'continuação';
  return KIND_LABEL[job.kind] ?? job.kind;
}

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return rest ? `${minutes}min ${rest}s` : `${minutes}min`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}min`;
}

/** O core grava UTC e serializa SEM sufixo de fuso; sem o 'Z', `new Date()` leria
 *  como hora local (ver `parseUtc` em useSessionManagement). */
export function parseJobTime(iso: string | null): Date | null {
  if (!iso) return null;
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  const date = new Date(hasZone ? iso : `${iso}Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** `2026-09-13T03:47:18` (UTC, sem sufixo) → data/hora local legível. */
export function formatStamp(iso: string | null): string {
  const date = parseJobTime(iso);
  if (!date) return '—';
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** Segundos desde que a tarefa começou a rodar (ou foi criada), para a que ainda
 *  não tem `duration_s` fechado. */
export function elapsedSeconds(job: JobSummary, now = Date.now()): number | null {
  const start = parseJobTime(job.started_at) ?? parseJobTime(job.created_at);
  if (!start) return null;
  return Math.max(0, Math.floor((now - start.getTime()) / 1000));
}

/** Quantas tarefas não acabadas cada conversa tem — o selo da lista de conversas. */
export interface SessionJobCount {
  running: number;
  waiting: number;
}

export function countOpenJobsBySession(jobs: JobSummary[]): Map<string, SessionJobCount> {
  const out = new Map<string, SessionJobCount>();
  for (const job of jobs) {
    if (!job.session_id || !OPEN_STATUSES.has(job.status)) continue;
    const entry = out.get(job.session_id) ?? { running: 0, waiting: 0 };
    if (job.status === 'waiting') entry.waiting += 1;
    else entry.running += 1;
    out.set(job.session_id, entry);
  }
  return out;
}
