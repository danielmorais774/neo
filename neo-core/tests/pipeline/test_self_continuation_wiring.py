"""Self-continuation wiring inside ``AIService._run_pipeline``.

test_self_continuation.py pins the orchestrator in isolation. This file pins the
adapter between it and the handlers — the part that only exists in ai_service and
that a production run exercises only when the model happens to promise something:

- the continuation step is dispatched back into the cognitive handler;
- the work note reaches it through ``metadata["pipeline_work_note"]``;
- a progress bubble is emitted per continuation, carrying CYCLE_PROGRESS_MARKER
  (the desktop keys "Executando" off it);
- the turn ALWAYS ends on an ``is_final`` — without it the desktop never emits
  `complete` and the chat spins forever.

``AIService.__new__`` avoids the real constructor (DB/LLM/PKS): ``_run_pipeline``
only reads ``self.settings`` and the ``_handle_*`` generators.
"""

import pytest

from core.pipeline.types import Handoff
from modules.desktop.ai_service import (
    CYCLE_PROGRESS_MARKER,
    AIService,
    AIServiceResponse,
)
from shared.config import Settings
from shared.types import IntentType, RoutingDecision


def _service(**overrides) -> AIService:
    svc = AIService.__new__(AIService)
    svc.settings = Settings(**overrides)
    return svc


def _decision() -> RoutingDecision:
    return RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE,
        confidence=1.0,
        needs_memory=False,
        needs_plan=False,
    )


async def _run(svc: AIService) -> list[AIServiceResponse]:
    return [
        r
        async for r in svc._run_pipeline(
            decision=_decision(),
            user_message="monta o site e me manda o zip",
            history=[],
            sid=None,
            trace=None,
            sys_context=None,
            slog=None,
            user_id=None,
            metadata={"client": "test"},
            saved_msg=None,
            allow_clarification=False,
            clarification_round=1,
        )
    ]


def _install_handler(svc: AIService, scripted):
    """Replace the cognitive handler with a scripted generator.

    ``scripted`` maps the step input → (reply_text, handoffs). Records every call's
    (input, metadata) so the test can assert what the continuation actually saw.
    """
    seen: list[tuple[str, dict]] = []

    async def fake_handle_direct_response(sid, msg, history, dec, trace, sys_context, **kw):
        seen.append((msg, dict(kw.get("metadata") or {})))
        reply, handoffs = scripted(msg)
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE,
            session_id=sid,
            message=reply,
            is_final=not handoffs,
            metadata={"persist_reply": True} if handoffs else None,
        )
        if handoffs:
            yield AIServiceResponse(
                intent=IntentType.DIRECT_RESPONSE,
                session_id=sid,
                message=None,
                is_final=False,
                metadata={
                    "__pipeline_handoffs__": handoffs,
                    "__pipeline_work__": {
                        "reply": reply,
                        "tool_trace": [
                            {"tool": "neo_bash", "ok": False, "error": "ERROR 403: Forbidden"}
                        ],
                        "files": None,
                    },
                },
            )

    svc._handle_direct_response = fake_handle_direct_response
    return seen


@pytest.mark.asyncio
async def test_cycle_dispatches_continuations_and_ends_on_a_final():
    svc = _service()

    def scripted(msg):
        if "zip" in msg:
            return "Vou baixar as imagens.", [
                Handoff(
                    target_intent=IntentType.DIRECT_RESPONSE,
                    input_text="baixar as imagens com curl -A Mozilla",
                    is_self=True,
                )
            ]
        return "Pronto, zip entregue.", []

    seen = _install_handler(svc, scripted)
    out = await _run(svc)

    # Both steps ran: the root and the continuation with ITS order (not the root's).
    assert [m for m, _ in seen] == [
        "monta o site e me manda o zip",
        "baixar as imagens com curl -A Mozilla",
    ]
    # The turn terminates — exactly one final, and it is the last thing yielded.
    assert [r.is_final for r in out].count(True) == 1
    assert out[-1].is_final is True
    assert out[-1].message == "Pronto, zip entregue."


@pytest.mark.asyncio
async def test_continuation_receives_the_work_note_and_a_progress_bubble():
    svc = _service()

    def scripted(msg):
        if "zip" in msg:
            return "Vou tentar outra fonte.", [
                Handoff(
                    target_intent=IntentType.DIRECT_RESPONSE,
                    input_text="baixar do Wikimedia",
                    is_self=True,
                )
            ]
        return "Fechado.", []

    seen = _install_handler(svc, scripted)
    out = await _run(svc)

    # The continuation was told what really happened — the tool FAILURE, verbatim
    # enough to change approach, and not the model's "vou tentar" promise.
    note = seen[1][1]["pipeline_work_note"]
    assert "neo_bash" in note and "403" in note
    assert "Vou tentar outra fonte" not in note

    # Exactly one progress bubble, for the continuation only (not the root), marked
    # so the desktop renders it as executing rather than thinking.
    bubbles = [r for r in out if r.message and r.message.startswith(CYCLE_PROGRESS_MARKER)]
    assert len(bubbles) == 1
    assert "baixar do Wikimedia" in bubbles[0].message
    assert bubbles[0].is_final is False


@pytest.mark.asyncio
async def test_intermediate_replies_are_persistent_bubbles_not_thinking():
    """They must survive in the chat; only the last one closes the turn."""
    svc = _service()

    def scripted(msg):
        if "zip" in msg:
            return "Passo 1 feito.", [
                Handoff(
                    target_intent=IntentType.DIRECT_RESPONSE,
                    input_text="fazer o passo 2",
                    is_self=True,
                )
            ]
        return "Passo 2 feito.", []

    _install_handler(svc, scripted)
    out = await _run(svc)

    intermediate = next(r for r in out if r.message == "Passo 1 feito.")
    assert intermediate.is_final is False
    assert (intermediate.metadata or {}).get("persist_reply") is True


@pytest.mark.asyncio
async def test_budget_exhaustion_still_terminates_the_turn():
    """The spinner-forever bug: a dropped handoff left the stream with no final."""
    svc = _service(pipeline_self_handoff_max_steps=2)
    n = 0

    def scripted(msg):
        nonlocal n
        n += 1
        return f"tentativa {n}", [
            Handoff(
                target_intent=IntentType.DIRECT_RESPONSE,
                input_text=f"tentar de outro jeito {n}",
                is_self=True,
            )
        ]

    _install_handler(svc, scripted)
    out = await _run(svc)

    finals = [r for r in out if r.is_final]
    assert len(finals) == 1
    # The closer explains the real reason and is its own message — never a repeat
    # of the last reply, which already reached the chat as its own bubble.
    assert "limite de passos" in finals[0].message
    assert finals[0].message not in {"tentativa 1", "tentativa 2"}


@pytest.mark.asyncio
async def test_anti_loop_stop_also_terminates_and_names_the_reason():
    svc = _service()

    def scripted(msg):
        return "de novo", [
            Handoff(
                target_intent=IntentType.DIRECT_RESPONSE,
                input_text="monta o site e me manda o zip",   # same as the root
                is_self=True,
            )
        ]

    _install_handler(svc, scripted)
    out = await _run(svc)

    finals = [r for r in out if r.is_final]
    assert len(finals) == 1
    assert "mesma tentativa" in finals[0].message


@pytest.mark.asyncio
async def test_no_handoff_is_the_ordinary_single_step_turn():
    """Regression guard: the common path must be untouched by all of the above."""
    svc = _service()
    seen = _install_handler(svc, lambda msg: ("resposta direta", []))
    out = await _run(svc)

    assert len(seen) == 1
    assert len(out) == 1
    assert out[0].is_final is True
    assert out[0].message == "resposta direta"
