// O processo de uma tarefa do Claude Code: uma linha com o passo atual e, aberta,
// a caixa com o fim do diário (`logs/claude_code/<job>/trace.md`, pela mesma rota
// da janela Tarefas). "ver ›" abre a janela Tarefas já nessa tarefa.
//
// Dois lugares usam: a faixa acima do campo de mensagem (ClaudeBar), enquanto a
// tarefa está viva na conversa aberta, e o botão no balão da resposta que pediu a
// tarefa (ClaudeNoBalao), que acha o processo de novo depois que a faixa some.
// (neo-core/docs/claude-code-delegation-plan.md §13)

import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { JobSummary } from '../lib/tauriApi';
import { STATUS_VIVOS } from '../lib/claudeJob';
/** O fim do diário que cabe na caixa: ~40 linhas curtas. */
const DIARIO_TAIL_BYTES = 6_000;

const FIM: Record<string, string> = {
  completed: 'concluída',
  failed: 'falhou',
  timeout: 'passou do tempo',
  cancelled: 'cancelada',
  continued: 'seguiu numa nova rodada',
};

function duracao(segundos: number | null): string {
  if (segundos === null) return '';
  if (segundos < 60) return `${segundos}s`;
  const minutos = Math.floor(segundos / 60);
  return minutos < 60 ? `${minutos} min` : `${Math.floor(minutos / 60)}h${String(minutos % 60).padStart(2, '0')}`;
}

function passo(job: JobSummary): string {
  if (job.status === 'waiting') return 'esperando você — responda aqui no chat';
  const nota = (job.preview || '').replace(/^Claude:\s*/, '').trim();
  const fim = FIM[job.status];
  // Encerrada: o status na frente, o resumo do resultado depois.
  if (fim) return nota && job.status !== 'continued' ? `${fim} — ${nota}` : fim;
  if (nota) return nota;
  return job.status === 'queued' ? 'na fila' : 'começando — confirme no Mac se aparecer o pedido';
}

/** O tipo da linha do diário, para a cor: pensamento, recusa, rodada, fim. */
function tipoDaLinha(texto: string): string {
  if (texto.startsWith('💬')) return 'think';
  if (texto.startsWith('sem permissão') || texto.startsWith('pedindo permissão')) return 'deny';
  if (/^[▶■]/.test(texto)) return 'round';
  if (texto.startsWith('Fim:')) return 'end';
  // O que voltou de um comando: "↳ saída (n linhas):" e as linhas "│" dela.
  if (texto.startsWith('↳ erro')) return 'deny';
  if (texto.startsWith('↳') || texto.startsWith('│')) return 'out';
  return '';
}

/** `comando` entre crases vira <code>, como no chat. */
function comCodigo(texto: string): ReactNode[] {
  return texto.split(/(`[^`]+`)/).map((parte, i) =>
    parte.startsWith('`') && parte.endsWith('`') && parte.length > 1
      ? <code key={i}>{parte.slice(1, -1)}</code>
      : parte,
  );
}

function estado(status: string): string {
  if (status === 'waiting') return ' claude-bar-waiting';
  if (status === 'completed') return ' claude-bar-done';
  if (STATUS_VIVOS.has(status)) return '';
  return ' claude-bar-ended';
}

interface ClaudeProcessoProps {
  job: JobSummary;
  aberta: boolean;
  onAlternar: () => void;
  /** Muda a cada consulta de quem segura o `job`: com a caixa aberta, relê o diário junto. */
  tick: number;
  className?: string;
}

export default function ClaudeProcesso({ job, aberta, onAlternar, tick, className = '' }: ClaudeProcessoProps) {
  const [diario, setDiario] = useState('');
  // O que deu errado ao abrir o Terminal: aparece no lugar do passo por um tempo.
  const [avisoTerminal, setAvisoTerminal] = useState<string | null>(null);
  const caixa = useRef<HTMLDivElement>(null);
  // Só acompanha o fim se a pessoa não rolou para cima para ler algo.
  const seguir = useRef(true);
  const caixaId = `claude-box-${job.id}`;

  // O diário só é buscado com a caixa aberta: fechada, a linha não custa nada além
  // do que quem segura o `job` já consulta.
  useEffect(() => {
    if (!aberta) return;
    let cancelled = false;
    window.electronAPI
      ?.getJobLogs(job.id, DIARIO_TAIL_BYTES)
      .then((r) => {
        if (!cancelled) setDiario(r?.logs ?? '');
      })
      .catch(() => {
        /* a próxima volta tenta de novo; a linha continua valendo */
      });
    return () => {
      cancelled = true;
    };
  }, [aberta, job.id, tick]);

  useEffect(() => {
    const el = caixa.current;
    if (el && seguir.current) el.scrollTop = el.scrollHeight;
  }, [diario, aberta]);

  useEffect(() => {
    if (!avisoTerminal) return;
    const id = window.setTimeout(() => setAvisoTerminal(null), 8_000);
    return () => window.clearTimeout(id);
  }, [avisoTerminal]);

  const vivo = STATUS_VIVOS.has(job.status);
  const linhas = diario.split('\n').filter((l) => l.trim());
  const retomar = job.claude;

  const abrirTerminal = () => {
    if (!retomar || retomar.rodando) return;
    setAvisoTerminal(null);
    window.electronAPI
      ?.openClaudeTerminal(retomar.cwd, retomar.session_id)
      .catch((err) => setAvisoTerminal(`não abri o Terminal: ${err}`));
  };

  return (
    <div className={`claude-bar${estado(job.status)}${aberta ? ' claude-bar-open' : ''} ${className}`.trim()}>
      <div className="claude-row">
        <button
          type="button"
          className="claude-toggle"
          aria-expanded={aberta}
          aria-controls={caixaId}
          onClick={() => {
            seguir.current = true;
            onAlternar();
          }}
          title={aberta ? 'Recolher o diário' : 'Mostrar o diário aqui'}
        >
          <span className="claude-dot" />
          <span className="claude-label">Claude</span>
          <span className={`claude-step${avisoTerminal ? ' claude-step-erro' : ''}`}>
            {avisoTerminal ?? passo(job)}
          </span>
          {job.status !== 'waiting' && <span className="claude-time">{duracao(job.duration_s)}</span>}
          <span className="claude-chevron" aria-hidden="true">{aberta ? '▾' : '▸'}</span>
        </button>
        {retomar && (
          <button
            type="button"
            className="claude-open claude-terminal"
            onClick={abrirTerminal}
            disabled={retomar.rodando}
            title={
              retomar.rodando
                ? 'Espere a rodada terminar: duas cópias do Claude na mesma sessão se atropelam'
                : `Abrir o Terminal em ${retomar.cwd} com claude --resume`
            }
          >
            terminal
          </button>
        )}
        <button
          type="button"
          className="claude-open"
          onClick={() => {
            window.electronAPI?.openJobsWindow(job.id).catch((err) => {
              console.error('[Neo] Falha ao abrir a tarefa do Claude:', err);
            });
          }}
          title="Abrir na janela Tarefas"
        >
          ver ›
        </button>
      </div>
      {aberta && (
        <div
          className="claude-box"
          id={caixaId}
          ref={caixa}
          onScroll={(event) => {
            const el = event.currentTarget;
            seguir.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
        >
          {linhas.length === 0 ? (
            <div className="claude-line">{vivo ? 'Ainda sem passos.' : 'Sem diário guardado.'}</div>
          ) : (
            linhas.map((linha, i) => {
              // "15:52:10  rodando `pytest`" — a hora fica apagada, o resto colorido pelo tipo.
              const m = linha.match(/^(\d\d:\d\d:\d\d)\s{2}(.*)$/);
              const texto = m ? m[2] : linha;
              return (
                <div className="claude-line" key={i}>
                  {m && <span className="claude-ts">{m[1]}</span>}
                  <span className={tipoDaLinha(texto.trim())}>{comCodigo(texto)}</span>
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
