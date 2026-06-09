"""PipelineOrchestrator — runs an ordered queue of pipeline steps.

The orchestrator is intentionally decoupled from ``AIService``: it receives a
``step_runner`` async-generator callback that knows how to dispatch a single
``PipelineStep`` to the right handler and yield ``(AIServiceResponse, StepOutcome)``
pairs. The orchestrator only owns the *control flow*: the queue, budgets and (from
Phase 2) handoff enqueuing.

Phase 1: the queue holds a single seeded step and no handoffs are produced, so the
behaviour is identical to the previous inline ``if/elif`` dispatch. The budget and
visited-set are present but inert; Phase 3 makes them load-bearing.
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


class PipelineOrchestrator:
    """Runs a queue of pipeline steps, threading handoffs back into the queue."""

    MAX_STEPS = 4
    MAX_DEPTH = 3

    def __init__(self, step_runner: StepRunner) -> None:
        self._run_step = step_runner

    async def run(self, initial_steps: list[PipelineStep]) -> AsyncIterator["object"]:
        """Execute the chain, yielding each ``AIServiceResponse`` to the caller."""
        queue: deque[PipelineStep] = deque(initial_steps)
        visited: set[tuple[str, str]] = set()
        ran = 0

        while queue and ran < self.MAX_STEPS:
            step = queue.popleft()

            # Anti-loop / depth guards (inert in Phase 1: single step, no handoffs).
            key = step.key()
            if key in visited:
                logger.info("[PIPELINE] skipping already-visited step %s", key)
                continue
            if step.depth > self.MAX_DEPTH:
                logger.info("[PIPELINE] skipping step over max depth: %s", key)
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

            # Dependent chain stops on failure; classifier-seeded roots never abort
            # the whole turn here (Phase 4 cascades refine this with on_failure).
            if not outcome.ok and step.origin != "classifier":
                logger.info("[PIPELINE] step failed, stopping chain: %s", key)
                break

            # Phase 2 enqueues handoffs here. Empty in Phase 1.
            for handoff in outcome.handoffs:
                queue.append(PipelineStep.from_handoff(handoff, parent=step))

        if ran >= self.MAX_STEPS and queue:
            logger.warning(
                "[PIPELINE] hit MAX_STEPS=%d with %d step(s) still queued",
                self.MAX_STEPS, len(queue),
            )
