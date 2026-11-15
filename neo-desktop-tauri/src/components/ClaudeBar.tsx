// Faixa do Claude Code no chat — ver o que ele está fazendo sem poluir a conversa.
//
// Uma linha acima do campo de mensagem, só enquanto existe tarefa do Claude na
// fila, rodando ou parada esperando o Daniel. Mostra o último passo (o
// `progress_note` que o orquestrador grava: "rodando `pytest`", "💬 Vou criar…") e
// some quando a tarefa termina — o resultado chega como a bolha de sempre.
//
// A faixa é da CONVERSA onde a tarefa nasceu: "Novo chat" a esconde e voltar
// àquela conversa a mostra de novo. É lá que o Claude espera a resposta — o
// aviso que leva ao `claude_reply` só existe naquela sessão (ai_service).
//
// A linha e a caixa do diário são o ClaudeProcesso, o mesmo do botão no balão
// que pediu a tarefa: depois que a faixa some, é por lá que se acha o processo.
// Aqui fica só achar a tarefa viva e o ritmo das consultas. Aberta ou fechada, a
// caixa fica como a pessoa deixou.
//
// Nada disso vira mensagem: a conversa continua só com o pedido e o resultado.

import { useEffect, useState } from 'react';
import type { JobSummary } from '../lib/tauriApi';
import { readClaudeBoxOpen, storeClaudeBoxOpen } from '../lib/chatPrefs';
import { STATUS_VIVOS } from '../lib/claudeJob';
import ClaudeProcesso from './ClaudeProcesso';

/** Com tarefa viva, acompanha o passo; sem ela, só confere de vez em quando. */
const VIVA_MS = 4_000;
const PARADA_MS = 30_000;
/** Depois de um turno, a tarefa pode ter acabado de nascer: rápido por 2 min.
 *  Medido no job decc8224 (07/11): a rodada inteira levou 13 s, e a consulta de
 *  30 s só achou a tarefa quando ela já esperava o Daniel — a faixa parecia
 *  aparecer DEPOIS do trabalho. No job 0a33cb31 (mesmo dia) ainda apareceu aos
 *  18 s de 25: o `ocupado` vinha do `isLoading`, que nunca liga — o log do core
 *  mostrou consultas a cada 30 s cravados durante o turno inteiro. */
const ALERTA_MS = 120_000;

interface ClaudeBarProps {
  /** O turno do Neo está rodando — é nele que o `claude_task` cria a tarefa. */
  ocupado?: boolean;
  /** A conversa aberta; vazia enquanto o Rust ainda não respondeu. */
  sessaoId: string;
}

export default function ClaudeBar({ ocupado = false, sessaoId }: ClaudeBarProps) {
  // Todas as tarefas vivas, de qualquer conversa: trocar de conversa só muda
  // qual delas aparece, na hora, sem esperar a próxima consulta.
  const [vivas, setVivas] = useState<JobSummary[]>([]);
  const [tick, setTick] = useState(0);
  const [alerta, setAlerta] = useState(true);
  const [aberta, setAberta] = useState(readClaudeBoxOpen);

  useEffect(() => {
    let cancelled = false;
    window.electronAPI
      ?.listJobs('all', 15)
      .then((page) => {
        if (cancelled) return;
        setVivas(
          (page?.jobs ?? []).filter((j) => j.kind === 'claude_code' && STATUS_VIVOS.has(j.status)),
        );
      })
      // Core fora do ar: a faixa só some; o chat tem os próprios avisos.
      .catch(() => {
        if (!cancelled) setVivas([]);
      });
    return () => {
      cancelled = true;
    };
  }, [tick]);

  // Voltar a uma conversa: o passo mostrado pode estar velho — confere na hora.
  useEffect(() => {
    if (!sessaoId) return;
    const agora = window.setTimeout(() => setTick((t) => t + 1), 0);
    return () => window.clearTimeout(agora);
  }, [sessaoId]);

  // Começo e fim de cada turno: consulta NA HORA e fica no ritmo rápido.
  useEffect(() => {
    const agora = window.setTimeout(() => {
      setTick((t) => t + 1);
      setAlerta(true);
    }, 0);
    const fim = window.setTimeout(() => setAlerta(false), ALERTA_MS);
    return () => {
      window.clearTimeout(agora);
      window.clearTimeout(fim);
    };
  }, [ocupado]);

  // Uma tarefa viva em OUTRA conversa também mantém o ritmo: ao voltar para
  // ela, o passo já está fresco.
  const rapido = vivas.length > 0 || alerta || ocupado;
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), rapido ? VIVA_MS : PARADA_MS);
    return () => window.clearInterval(id);
  }, [rapido]);

  const job = sessaoId ? (vivas.find((j) => j.session_id === sessaoId) ?? null) : null;
  if (!job) return null;

  return (
    <ClaudeProcesso
      job={job}
      aberta={aberta}
      tick={tick}
      onAlternar={() => {
        const proxima = !aberta;
        setAberta(proxima);
        storeClaudeBoxOpen(proxima);
      }}
    />
  );
}
