"""Routing tests for the cognitive-core guard.

Conditional/habit-based actions and preference questions must reach
DIRECT_RESPONSE (the cognitive core) so it can read the PKS and decide whether to
hand off to a skill — instead of the cheap router firing the skill blindly
(regression for session 6bb1d340: "se eu costumo, pode ligar" → HOME_ASSISTANT).
"""

import pytest

from core.router.intent_router import IntentRouter
from shared import IntentType


# Messages that MUST go to the cognitive core (DIRECT_RESPONSE).
_BRAIN_CASES = [
    "Como gosto de usar o ar nas quintas? Se eu costumo, pode ligar",
    "se eu costumo usar o ar nas quintas, pode ligar",
    "como eu gosto de deixar a temperatura do quarto à noite?",
    "se eu uso isso de manhã, liga pra mim",
]

# Direct imperatives / pure queries that must STAY on their skill/tool path.
_NON_BRAIN_CASES = [
    "acende a luz da sala",
    "desliga o ar do quarto",
    "qual a temperatura do termostato?",
    "ativa o modo cinema",
    "como faço para acender a luz?",
]


@pytest.mark.parametrize("message", _BRAIN_CASES)
def test_conditional_or_preference_flagged_for_cognitive_core(message):
    assert IntentRouter._is_brain_reasoning_request(message) is True


@pytest.mark.parametrize("message", _NON_BRAIN_CASES)
def test_direct_imperatives_not_flagged(message):
    assert IntentRouter._is_brain_reasoning_request(message) is False


@pytest.mark.parametrize("message", _BRAIN_CASES)
def test_fallback_routes_conditional_to_direct_response(message):
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify(message)
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.needs_memory is True


def test_imperative_still_reaches_home_assistant_in_fallback():
    """The guard must not steal plain commands from the skill path."""
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("desliga o ar do quarto")
    assert decision.intent == IntentType.HOME_ASSISTANT
