"""Phase 0 — pipeline types: defaults, key() stability, handoff derivation."""

from core.pipeline.types import Handoff, PipelineStep, StepOutcome, normalize_input
from shared.types import IntentType


def test_normalize_input_strips_accents_and_collapses_whitespace():
    assert normalize_input("Ligar  o AR-Condicionado") == "ligar o ar-condicionado"
    assert normalize_input("  ÁRVORE   ") == "arvore"
    assert normalize_input("") == ""


def test_pipeline_step_defaults():
    step = PipelineStep(intent=IntentType.HOME_ASSISTANT, input_text="acende a luz")
    assert step.origin == "classifier"
    assert step.depth == 0
    assert step.skill_name is None
    assert step.decision is None


def test_pipeline_step_key_is_normalized_and_stable():
    a = PipelineStep(intent=IntentType.HOME_ASSISTANT, input_text="Ligar  o AC")
    b = PipelineStep(intent=IntentType.HOME_ASSISTANT, input_text="ligar o ac")
    assert a.key() == b.key() == ("home_assistant", "ligar o ac")


def test_handoff_defaults():
    h = Handoff(target_intent=IntentType.WHATSAPP_MESSAGING, input_text="avisa o pai")
    assert h.on_failure == "stop"
    assert h.user_visible is True
    assert h.target_skill is None


def test_step_from_handoff_increments_depth_and_sets_origin():
    parent = PipelineStep(
        intent=IntentType.DIRECT_RESPONSE, input_text="...", depth=1
    )
    h = Handoff(
        target_intent=IntentType.HOME_ASSISTANT,
        input_text="ligar o ar do quarto",
        target_skill="home_assistant",
    )
    child = PipelineStep.from_handoff(h, parent=parent)
    assert child.intent == IntentType.HOME_ASSISTANT
    assert child.skill_name == "home_assistant"
    assert child.origin == "handoff"
    assert child.depth == 2


def test_step_outcome_defaults():
    oc = StepOutcome(ok=True)
    assert oc.final_text is None
    assert oc.handoffs == []
