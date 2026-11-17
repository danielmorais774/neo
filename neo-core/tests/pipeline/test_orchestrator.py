"""Phase 1 — orchestrator control flow.

The orchestrator is decoupled from AIService via a ``step_runner`` callback, so we
exercise it with lightweight fakes (no DB/LLM).
"""

import pytest

from core.pipeline.orchestrator import PipelineOrchestrator
from core.pipeline.types import Handoff, PipelineStep, StepOutcome
from shared.types import IntentType


def _step(intent=IntentType.DIRECT_RESPONSE, text="oi", origin="classifier", depth=0):
    return PipelineStep(intent=intent, input_text=text, origin=origin, depth=depth)


@pytest.mark.asyncio
async def test_single_step_yields_handler_responses_in_order():
    """Phase 1 core guarantee: one seeded step → handler responses pass through."""
    calls = []

    async def runner(step):
        calls.append(step)
        yield "thinking", None
        yield "final", None
        yield None, StepOutcome(ok=True, final_text="final")

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run([_step()])]

    assert out == ["thinking", "final"]   # outcome-only yield is consumed, not emitted
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_handoff_enqueues_next_step():
    """A handoff from step 1 runs a second step (Phase 2 path; queue exists in P1)."""
    seen = []

    async def runner(step):
        seen.append((step.intent, step.input_text, step.origin))
        if step.origin == "classifier":
            yield "r1", None
            yield None, StepOutcome(
                ok=True,
                final_text="r1",
                handoffs=[Handoff(
                    target_intent=IntentType.HOME_ASSISTANT,
                    input_text="ligar o ar do quarto",
                    target_skill="home_assistant",
                )],
            )
        else:
            yield "r2", None
            yield None, StepOutcome(ok=True, final_text="r2")

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run([_step()])]

    assert out == ["r1", "r2"]
    assert seen[1] == (IntentType.HOME_ASSISTANT, "ligar o ar do quarto", "handoff")


@pytest.mark.asyncio
async def test_visited_set_breaks_cycles():
    """A handoff back to the same (intent, input) is not run twice."""
    runs = 0

    async def runner(step):
        nonlocal runs
        runs += 1
        yield f"r{runs}", None
        # Always try to hand off to itself — must be de-duped by the visited set.
        yield None, StepOutcome(
            ok=True,
            handoffs=[Handoff(target_intent=step.intent, input_text=step.input_text)],
        )

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run([_step(text="loop me")])]

    assert runs == 1
    assert out == ["r1"]


@pytest.mark.asyncio
async def test_max_steps_budget_caps_runaway_chains():
    """Distinct handoffs keep flowing but are capped at MAX_STEPS."""
    counter = 0

    async def runner(step):
        nonlocal counter
        counter += 1
        n = counter
        yield f"r{n}", None
        # Each step hands off to a brand-new distinct input → unbounded if uncapped.
        yield None, StepOutcome(
            ok=True,
            handoffs=[Handoff(target_intent=IntentType.HOME_ASSISTANT, input_text=f"act-{n}")],
        )

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run([_step(text="root")])]

    assert len(out) == PipelineOrchestrator.MAX_STEPS


@pytest.mark.asyncio
async def test_failed_handoff_step_stops_chain():
    """A failing non-classifier step stops further handoffs."""
    async def runner(step):
        if step.origin == "classifier":
            yield "r1", None
            yield None, StepOutcome(
                ok=True,
                handoffs=[Handoff(target_intent=IntentType.HOME_ASSISTANT, input_text="boom")],
            )
        else:
            yield "r2", None
            yield None, StepOutcome(
                ok=False,
                handoffs=[Handoff(target_intent=IntentType.WHATSAPP_MESSAGING, input_text="never")],
            )

    orch = PipelineOrchestrator(runner)
    out = [r async for r in orch.run([_step()])]

    assert out == ["r1", "r2"]   # third (handoff after failure) never runs
