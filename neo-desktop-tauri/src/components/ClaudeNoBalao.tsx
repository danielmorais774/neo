// O processo do Claude preso ao balão da resposta que mandou o pedido (ou que
// entregou o resultado). A faixa acima do campo some quando a tarefa acaba ou a
// conversa troca; daqui o Daniel acha o processo de novo, quando quiser.
//
// Fechado é só uma pílula e não consulta nada — reabrir uma conversa com várias
// tarefas não dispara uma busca por balão. Aberto, busca a tarefa e mostra o
// mesmo ClaudeProcesso da faixa, já com o diário; tarefa viva é acompanhada.
// O `jobs` do balão vem do core: AgentResponse.jobs ao vivo, histórico reaberto
// e a notificação da entrega (core/claude_code/balao.py).

import { useEffect, useState } from 'react';
import type { JobSummary } from '../lib/tauriApi';
import { STATUS_VIVOS } from '../lib/claudeJob';
import ClaudeProcesso from './ClaudeProcesso';

/** Mesmo ritmo da faixa com tarefa viva. */
const VIVA_MS = 4_000;

export default function ClaudeNoBalao({ jobId }: { jobId: string }) {
  const [aberto, setAberto] = useState(false);
  const [job, setJob] = useState<JobSummary | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!aberto) return;
    let cancelled = false;
    window.electronAPI
      ?.getJob(jobId)
      .then((detalhe) => {
        if (cancelled) return;
        setJob(detalhe);
        setErro(null);
      })
      .catch((err) => {
        if (!cancelled) setErro(String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [aberto, jobId, tick]);

  const vivo = job !== null && STATUS_VIVOS.has(job.status);
  useEffect(() => {
    if (!aberto || !vivo) return;
    const id = window.setInterval(() => setTick((t) => t + 1), VIVA_MS);
    return () => window.clearInterval(id);
  }, [aberto, vivo]);

  if (aberto && job) {
    return (
      <ClaudeProcesso
        job={job}
        aberta
        tick={tick}
        onAlternar={() => setAberto(false)}
        className="claude-no-balao"
      />
    );
  }

  return (
    <button
      type="button"
      className="claude-anexo"
      onClick={() => {
        // Depois de um erro o `aberto` já é true: o tick é que faz buscar de novo.
        setErro(null);
        setAberto(true);
        setTick((t) => t + 1);
      }}
      title="Ver o que o Claude fez nesta tarefa"
    >
      <span className="claude-anexo-dot" aria-hidden="true" />
      <span className="claude-anexo-label">Claude</span>
      <span className="claude-anexo-acao">
        {erro ? erro : aberto ? 'abrindo…' : 'ver o processo'}
      </span>
      <span className="claude-anexo-chevron" aria-hidden="true">▸</span>
    </button>
  );
}
