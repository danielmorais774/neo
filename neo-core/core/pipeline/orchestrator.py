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

from core.pipeline.types import PipelineStep, StepOutcome

logger = logging.getLogger(__name__)

# Async generator that runs one step and yields (response, outcome) pairs.
# ``response`` streams to the user; ``outcome`` (yielded once, last) carries the
# step result + any handoffs. Either element may be None on a given yield.
StepRunner = Callable[[PipelineStep], AsyncIterator[tuple["object | None", StepOutcome | None]]]

# (kind, human-readable message). Kinds: "anti_loop" | "max_depth" | "budget".
NoticeSink = Callable[[str, str], None]

# How many work notes to carry into a continuation. Older steps fall out: the note
# is prompt context, and an unbounded transcript would crowd out the real history.
MAX_WORK_NOTES = 6


class PipelineOrchestrator:
    """Runs a queue of pipeline steps, threading handoffs back into the queue."""

    MAX_STEPS = 4
    MAX_DEPTH = 3

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
        work_notes: list[str] = []
        ran = 0
        # Latches on the first self-continuation; from then on the chain runs on
        # the cycle budget.
        cycling = False

        while queue:
            step = queue.popleft()
            cycling = cycling or step.is_self_continuation
            max_steps = self._self_max_steps if cycling else self._max_steps
            max_depth = self._self_max_depth if cycling else self._max_depth

            if ran >= max_steps:
                self._notice(
                    "budget",
                    f"orçamento de {max_steps} passos esgotado com {len(queue) + 1} "
                    f"ainda na fila (próximo: {step.input_text[:80]!r})",
                )
                break

            # Anti-loop / depth guards.
            key = step.key()
            if key in visited:
                self._notice(
                    "anti_loop",
                    f"passo repetido descartado — a mesma ordem já rodou neste turno: "
                    f"{step.input_text[:80]!r}",
                )
                continue
            if step.depth > max_depth:
                self._notice(
                    "max_depth",
                    f"profundidade máxima ({max_depth}) atingida em {step.input_text[:80]!r}",
                )
                continue
            visited.add(key)
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

            # Dependent chain stops on failure; classifier-seeded roots never abort
            # the whole turn here (Phase 4 cascades refine this with on_failure).
            if not outcome.ok and step.origin != "classifier":
                logger.info("[PIPELINE] step failed, stopping chain: %s", key)
                break

            note = "\n".join(work_notes) if work_notes else None
            for handoff in outcome.handoffs:
                queue.append(
                    PipelineStep.from_handoff(handoff, parent=step, context_note=note)
                )
