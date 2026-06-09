"""Phases 4–6 — declared cascade parsing, COGNITIVE alias, handoff safety policy."""

from core.pipeline.policy import (
    HANDOFF_EXECUTION_POLICY,
    handoff_requires_confirmation,
)
from core.router.intent_router import IntentRouter
from shared.types import IntentType, PipelineStepSpec, RoutingDecision

_IMAP = {
    "HOME_ASSISTANT": IntentType.HOME_ASSISTANT,
    "WHATSAPP_MESSAGING": IntentType.WHATSAPP_MESSAGING,
    "DIRECT_RESPONSE": IntentType.DIRECT_RESPONSE,
}


# ── Phase 5: COGNITIVE alias ────────────────────────────────────────────────
def test_cognitive_is_canonical_with_direct_response_alias():
    # Phase 5 migration: COGNITIVE is canonical (value "cognitive");
    # DIRECT_RESPONSE is now a backward-compat alias pointing to it.
    assert IntentType.COGNITIVE is IntentType.DIRECT_RESPONSE
    assert IntentType.COGNITIVE.value == "cognitive"
    assert IntentType.DIRECT_RESPONSE.value == "cognitive"


# ── Phase 4: pipeline_plan parsing ──────────────────────────────────────────
def test_valid_cascade_parsed():
    plan = [
        {"intent": "HOME_ASSISTANT", "input": "apagar as luzes", "skill_name": "home_assistant"},
        {"intent": "WHATSAPP_MESSAGING", "input": "avisar o porteiro", "skill_name": "whatsapp_messaging"},
    ]
    specs = IntentRouter._parse_pipeline_plan(plan, _IMAP)
    assert specs is not None and len(specs) == 2
    assert specs[0].intent == IntentType.HOME_ASSISTANT
    assert specs[1].input_text == "avisar o porteiro"


def test_single_action_plan_ignored():
    assert IntentRouter._parse_pipeline_plan([{"intent": "HOME_ASSISTANT", "input": "x"}], _IMAP) is None


def test_malformed_entries_drop_below_two_to_none():
    plan = [{"intent": "NOPE", "input": "x"}, {"intent": "HOME_ASSISTANT", "input": ""}]
    assert IntentRouter._parse_pipeline_plan(plan, _IMAP) is None


def test_none_plan():
    assert IntentRouter._parse_pipeline_plan(None, _IMAP) is None


def test_routing_decision_carries_pipeline_plan():
    d = RoutingDecision(
        intent=IntentType.HOME_ASSISTANT,
        pipeline_plan=[
            PipelineStepSpec(intent=IntentType.HOME_ASSISTANT, input_text="a"),
            PipelineStepSpec(intent=IntentType.WHATSAPP_MESSAGING, input_text="b"),
        ],
    )
    assert len(d.pipeline_plan) == 2


# ── Phase 6: handoff safety policy ──────────────────────────────────────────
def test_local_action_auto_executes():
    assert handoff_requires_confirmation(IntentType.HOME_ASSISTANT) is False


def test_outward_action_requires_confirmation():
    assert handoff_requires_confirmation(IntentType.WHATSAPP_MESSAGING) is True


def test_unknown_target_fails_safe_to_confirm():
    assert handoff_requires_confirmation(IntentType.CLIENT_TOOL) is True
    assert IntentType.HOME_ASSISTANT in HANDOFF_EXECUTION_POLICY
