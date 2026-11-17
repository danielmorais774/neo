"""Workstream B — speech-act + bias-to-brain routing.

Side-effecting skills (HA/WhatsApp) only fire on a confident imperative; everything
conditional/definitional/uncertain routes to the cognitive core. Read-only paths
(SERVER_TOOL/CLIENT_TOOL) are untouched. Regression for sessions 6bb1d340 / 3e51616c.
"""

import json

import pytest

from core.router.intent_router import IntentRouter
from shared.types import IntentType, RoutingDecision


def _router() -> IntentRouter:
    return object.__new__(IntentRouter)


def _parse(payload: dict) -> RoutingDecision:
    return _router()._parse_classification(json.dumps(payload), payload.get("_msg", ""))


# ── bias-to-brain on parsed classifier output ───────────────────────────────
@pytest.mark.parametrize(
    "speech_act,confidence,expected",
    [
        ("imperative", 0.9, IntentType.HOME_ASSISTANT),   # clear command stays
        ("imperative", 0.6, IntentType.DIRECT_RESPONSE),  # low confidence → brain
        ("conditional", 0.9, IntentType.DIRECT_RESPONSE),
        ("definition", 0.95, IntentType.DIRECT_RESPONSE),
        ("question", 0.9, IntentType.DIRECT_RESPONSE),
    ],
)
def test_home_assistant_only_fires_on_confident_imperative(speech_act, confidence, expected):
    d = _parse({
        "speech_act": speech_act, "intent": "HOME_ASSISTANT",
        "confidence": confidence, "skill_name": "home_assistant",
    })
    assert d.intent == expected


def test_whatsapp_conditional_routes_to_brain():
    d = _parse({
        "speech_act": "conditional", "intent": "WHATSAPP_MESSAGING",
        "confidence": 0.9, "skill_name": "whatsapp_messaging",
    })
    assert d.intent == IntentType.DIRECT_RESPONSE


def test_whatsapp_imperative_stays():
    d = _parse({
        "speech_act": "imperative", "intent": "WHATSAPP_MESSAGING",
        "confidence": 0.9, "skill_name": "whatsapp_messaging",
        "tool_arguments": {"text": "oi", "recipient": "mãe"},
    })
    assert d.intent == IntentType.WHATSAPP_MESSAGING
    assert d.tool_arguments == {"text": "oi", "recipient": "mãe"}  # whatsapp keeps args


def test_server_tool_question_is_untouched():
    # Read-only path: a question is the normal trigger, must NOT go to brain.
    d = _parse({"speech_act": "question", "intent": "SERVER_TOOL", "confidence": 0.6})
    assert d.intent == IntentType.SERVER_TOOL


# ── Workstream A: HA carries no execution details ───────────────────────────
def test_home_assistant_execution_fields_discarded():
    d = _parse({
        "speech_act": "imperative", "intent": "HOME_ASSISTANT", "confidence": 0.9,
        "skill_name": "home_assistant", "tool_name": "ha_call_service",
        "tool_arguments": {"entity_id": "light.quarto", "service": "turn_on"},
    })
    assert d.intent == IntentType.HOME_ASSISTANT
    assert d.tool_name is None
    assert d.tool_arguments is None


# ── the two real failures ───────────────────────────────────────────────────
def test_brincadeira_definition_not_home_assistant():
    d = _parse({
        "speech_act": "definition", "intent": "HOME_ASSISTANT", "confidence": 0.8,
        "skill_name": "home_assistant", "tool_arguments": {"entity_id": "light.quarto"},
    })
    assert d.intent == IntentType.DIRECT_RESPONSE
    assert d.tool_arguments is None
    assert d.needs_memory is True
