"""PipelineOrchestrator — runs an ordered queue of pipeline steps.

The orchestrator is intentionally decoupled from ``AIService``: it receives a
``step_runner`` async-generator callback that knows how to dispatch a single
``PipelineStep`` to the right handler and yield ``(AIServiceResponse, StepOutcome)``
pairs. The orchestrator only owns the *control flow*: the queue, budgets and (from
Phase 2) handoff enqueuing.

Phase 1: the queue holds a single seeded step and no handoffs are produced, so the
behaviour is identical to the previous inline ``if/elif`` dispatch. The budget and
visited-set are present but inert; Phase 3 makes them load-bearing.

Self-continuation: a step may hand off to the cognitive core ITSELF
(``Handoff.is_self``), which turns the queue into a work cycle — the brain runs the
next concrete order in the same turn instead of ending it on a promise. Two things
differ from a skill handoff: the chain gets a larger budget (a retry cycle burns
steps fast), and each continuation inherits a *work note* describing what the
previous steps already did, so it never redoes finished work.

Every early exit (anti-loop, depth, budget) reports through ``on_notice``. Silence
here used to be invisible: a discarded step just logged at INFO and the turn ended
with nothing, giving no way to tell "the cycle stopped" from "the cycle never ran".
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator, Callable

from core.pipeline.types import PipelineStep, StepOutcome, order_is_near_repeat

logger = logging.getLogger(__name__)

# Async generator that runs one step and yields (response, outcome) pairs.
# ``response`` streams to the user; ``outcome`` (yielded once, last) carries the
# step result + any handoffs. Either element may be None on a given yield.
StepRunner = Callable[[PipelineStep], AsyncIterator[tuple["object | None", StepOutcome | None]]]

# (kind, human-readable message).
# Kinds: "anti_loop" | "near_repeat" | "idle_cycle" | "max_depth" | "budget" |
# "parked_client".
NoticeSink = Callable[[str, str], None]

# How many work notes to carry into a continuation. Older steps fall out: the note
# is prompt context, and an unbounded transcript would crowd out the real history.
MAX_WORK_NOTES = 6


class PipelineOrchestrator:
    """Runs a queue of pipeline steps, threading handoffs back into the queue."""

    MAX_STEPS = 4
    MAX_DEPTH = 3
    #: Quantos empurrões ("essa ordem repete o passo N — pensa de novo") antes de
    #: fechar. Um só: a primeira volta ganha a chance de achar outro caminho, a
    #: segunda fecha. O loop de shell conta parecido (`_MAX_REPEAT_NUDGES`).
    MAX_CYCLE_NUDGES = 1
    #: Passos de ciclo SEGUIDOS sem executar nada antes de fechar. Passo que só
    #: fala não é progresso — session-389114d4 interação 29 deu quatro voltas sem
    #: uma única tool, reescrevendo a mesma resposta, porque `ok=True` bastava.
    MAX_IDLE_CYCLE_STEPS = 2

    def __init__(
        self,
        step_runner: StepRunner,
        *,
        max_steps: int | None = None,
        max_depth: int | None = None,
        self_max_steps: int | None = None,
        self_max_depth: int | None = None,
        on_notice: NoticeSink | None = None,
    ) -> None:
        self._run_step = step_runner
        self._max_steps = max_steps or self.MAX_STEPS
        self._max_depth = max_depth or self.MAX_DEPTH
        # A cycle latches the larger budget for the WHOLE chain once any
        # self-continuation runs — not just for the self steps. Otherwise a cycle
        # that ends by handing off to a skill would be cut by the small budget
        # right at the payoff step.
        self._self_max_steps = max(self_max_steps or self._max_steps, self._max_steps)
        self._self_max_depth = max(self_max_depth or self._max_depth, self._max_depth)
        self._on_notice = on_notice

    def _notice(self, kind: str, message: str) -> None:
        logger.info("[PIPELINE] %s: %s", kind, message)
        if self._on_notice is None:
            return
        try:
            self._on_notice(kind, message)
        except Exception as e:  # noqa: BLE001 — telemetry must never break the turn
            logger.warning("[PIPELINE] notice sink failed: %s", e)

    async def run(self, initial_steps: list[PipelineStep]) -> AsyncIterator["object"]:
        """Execute the chain, yielding each ``AIServiceResponse`` to the caller."""
        queue: deque[PipelineStep] = deque(initial_steps)
        visited: set[tuple[str, str]] = set()
        # Texto das ordens que REALMENTE rodaram, para a guarda de quase-repetição.
        # O `visited` só pega igualdade literal e quem escreve as ordens do ciclo é
        # um LLM que parafraseia (ver `order_is_near_repeat`).
        ordens: list[str] = []
        work_notes: list[str] = []
        ran = 0
        # Latches on the first self-continuation; from then on the chain runs on
        # the cycle budget.
        cycling = False
        # Empurrões já dados, passos de ciclo seguidos sem executar nada, e se o
        # passo de fechamento já foi enfileirado (uma vez por turno).
        correcoes = 0
        sem_acao = 0
        closed_once = False

        def _note() -> str | None:
            return "\n".join(work_notes) if work_notes else None

        def _corrigir(parent: PipelineStep, kind: str, covered_by: str | None = None) -> None:
            """Enfileira o passo de autocorreção à FRENTE do que sobrou.

            Em vez de descartar o passo e deixar o turno morrer sem `is_final` —
            que é o que fazia o fechamento forçado cuspir uma string fixa do
            sistema na cara do usuário — o ciclo roda um passo cognitivo a mais:
            ou pensando de novo (`retry`), ou escrevendo a resposta final com o
            que já existe (`close`).
            """
            nonlocal closed_once
            if kind == "close":
                if closed_once:
                    return
                closed_once = True
                # O que sobrou na fila é mais do mesmo: o fechamento é o fim.
                queue.clear()
            queue.appendleft(
                PipelineStep.self_correction(
                    parent,
                    kind=kind,
                    context_note=_note(),
                    covered_by=covered_by,
                    # O `retry` reaproveita a ordem de propósito (o que muda é a
                    # instrução). O `close` ganha texto próprio: herdar a ordem
                    # repetida deixaria o log dizendo que o passo de fechamento
                    # foi mais uma tentativa da mesma coisa.
                    input_text=(
                        "fechar o turno com o que já foi levantado"
                        if kind == "close" else None
                    ),
                )
            )

        while queue:
            step = queue.popleft()
            cycling = cycling or step.is_self_continuation
            max_steps = self._self_max_steps if cycling else self._max_steps
            max_depth = self._self_max_depth if cycling else self._max_depth

            # Orçamento. O passo de FECHAMENTO é isento de propósito: ele existe
            # para o turno terminar falando, e barrá-lo aqui devolve exatamente a
            # string fixa que ele veio substituir.
            if ran >= max_steps and step.correction != "close":
                self._notice(
                    "budget",
                    f"orçamento de {max_steps} passos esgotado com {len(queue) + 1} "
                    f"ainda na fila (próximo: {step.input_text[:80]!r})",
                )
                if cycling and not closed_once:
                    _corrigir(step, "close")
                    continue
                break

            # Guardas de repetição e profundidade. Um passo de correção é isento:
            # o `retry` carrega a MESMA ordem de propósito (o que muda é a
            # instrução que vai com ele), então checá-lo descartaria justamente a
            # correção — e nada poderia corrigir uma correção.
            key = step.key()
            if not step.correction:
                exata = key in visited
                # A comparação por CONTEÚDO vale só para o ciclo. Uma cascata do
                # classificador tem passos declarados de propósito e às vezes
                # parecidos ("buscar os links", "baixar os links"); aplicar a
                # cobertura ali transformaria cascata em ciclo de correção, que é
                # uma mudança de comportamento que ninguém pediu. Fora do ciclo,
                # segue valendo só a igualdade literal, como antes.
                repetida = (
                    step.input_text if exata
                    else (order_is_near_repeat(step.input_text, ordens) if cycling else None)
                )
                if repetida and not cycling:
                    # Cascata: comportamento antigo — descarta o passo e segue a
                    # fila. Não existe ciclo aqui para pedir que se corrija.
                    self._notice(
                        "anti_loop",
                        f"passo repetido descartado — a mesma ordem já rodou neste "
                        f"turno: {step.input_text[:80]!r}",
                    )
                    continue
                if repetida:
                    correcoes += 1
                    kind = "retry" if correcoes <= self.MAX_CYCLE_NUDGES else "close"
                    self._notice(
                        "anti_loop" if exata else "near_repeat",
                        f"a ordem {step.input_text[:80]!r} "
                        + ("repete literalmente" if exata else "pede de novo")
                        + f" o que {repetida[:80]!r} já cobriu — "
                        + (
                            "devolvendo ao cérebro para ele escolher outro caminho"
                            if kind == "retry"
                            else "segunda volta no mesmo ponto, fechando o turno com o que já existe"
                        ),
                    )
                    _corrigir(step, kind, covered_by=repetida)
                    continue
                if step.depth > max_depth:
                    self._notice(
                        "max_depth",
                        f"profundidade máxima ({max_depth}) atingida em {step.input_text[:80]!r}",
                    )
                    if cycling and not closed_once:
                        _corrigir(step, "close")
                    continue
                visited.add(key)
                ordens.append(step.input_text)
            ran += 1

            logger.info(
                "[PIPELINE] step %d intent=%s origin=%s depth=%d",
                ran, step.intent.value, step.origin, step.depth,
            )

            outcome = StepOutcome(ok=True, final_text=None)
            async for resp, oc in self._run_step(step):
                if resp is not None:
                    yield resp
                if oc is not None:
                    outcome = oc

            if outcome.work_note:
                work_notes.append(outcome.work_note)
                del work_notes[:-MAX_WORK_NOTES]

            # Passo de ciclo que não executou nada não é progresso. O `close` não
            # entra na conta: ele é o fim por definição e naturalmente só fala.
            if outcome.acted:
                sem_acao = 0
            elif step.is_self_continuation and step.correction != "close":
                sem_acao += 1

            # ── O step estacionou no cliente: a fila PARA aqui ──────────────
            #
            # Um step CLIENT_TOOL não executa nada: ele emite o pedido e o
            # gerador acaba. O trabalho real acontece minutos depois, quando o
            # desktop devolve o resultado por `process_tool_result` — que é outra
            # entrada e não conhece esta fila. Seguir para o próximo step aqui é
            # despachá-lo em cima de um resultado que ainda não existe.
            #
            # Sessão d9f9417b (2026-09-20): a cascata era {criar pasta, buscar
            # links, baixar os PDFs}. O step 1 pôs um card de aprovação na tela do
            # usuário às 17:31:05 e a fila, sem esperar, disparou o step 2 (busca
            # no navegador) e o step 3 ("baixar os PDFs encontrados") — este
            # último sem nenhum link, porque a busca ainda estava rodando. Do lado
            # de fora isso é exatamente "ele pediu permissão e falhou antes de eu
            # autorizar": o card estava lá, mas o turno já tinha seguido sem ele.
            #
            # Os steps que sobram são DESCARTADOS, não perdidos em silêncio: o
            # notice os nomeia. Retomar a fila depois do round-trip é outro
            # problema (a retomada vive em `_pending_actuations`, do cérebro, e
            # não tem equivalente para cascata).
            if outcome.parked:
                restantes = [s.input_text[:80] for s in queue]
                self._notice(
                    "parked_client",
                    "passo estacionado no cliente (aguardando o desktop) — a fila "
                    "para aqui" + (
                        f"; {len(restantes)} passo(s) descartado(s): {restantes!r}"
                        if restantes else " (era o último passo)"
                    ),
                )
                break

            # Dependent chain stops on failure; classifier-seeded roots never abort
            # the whole turn here (Phase 4 cascades refine this with on_failure).
            if not outcome.ok and step.origin != "classifier":
                logger.info("[PIPELINE] step failed, stopping chain: %s", key)
                if cycling and not closed_once:
                    # Morrer aqui deixa o turno sem `is_final` e o fechamento
                    # forçado fala por ele. Melhor o Neo fechar com o que tem.
                    _corrigir(step, "close")
                    continue
                break

            # O passo de FECHAMENTO é o fim: o que ele pedir é ignorado aqui. Em
            # produção ele já roda com `enable_handoff=False`, então nem deveria
            # emitir handoff — mas a fila não pode depender disso. Sem esta trava,
            # um fecho que ainda pedisse continuação daria outra volta na guarda e
            # o turno ganharia um segundo aviso pelo mesmo motivo.
            if step.correction != "close":
                for handoff in outcome.handoffs:
                    queue.append(
                        PipelineStep.from_handoff(handoff, parent=step, context_note=_note())
                    )

            # Dois passos seguidos só falando e o ciclo quer OUTRA volta: é a
            # resposta sendo reescrita, não trabalho andando (interação 29 da
            # session-389114d4 — 4 rodadas, zero tool, 4 bolhas quase idênticas).
            # Só intervém se a cadeia ia continuar: passo sem handoff já fechou o
            # turno sozinho, com `is_final`, e aí não há nada a corrigir.
            if queue and sem_acao >= self.MAX_IDLE_CYCLE_STEPS and not closed_once:
                self._notice(
                    "idle_cycle",
                    f"{sem_acao} passos seguidos sem executar nada (só resposta de "
                    f"texto) e o ciclo pediu mais {len(queue)} — fechando o turno "
                    "com o que já existe em vez de reescrever a mesma resposta",
                )
                _corrigir(step, "close")
