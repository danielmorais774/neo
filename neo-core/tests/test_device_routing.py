"""Routing for device questions: known/registered (PKS) vs connected (live tool).

Regression for session e245da98 — "quais dispositivos eu tenho" was routed to
SERVER_TOOL/list_connected_devices (websocket-only) instead of the PKS knowledge
path, and the correction turn repeated the same tool. The disambiguation is
deterministic (no LLM), so it is exercised through the IntentRouter helpers.
"""

import pytest

from core.router.intent_router import IntentRouter
from shared import IntentType


# ── _is_known_devices_query ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "quais dispositivos eu tenho",
        "quais dispositivos eu tenho?",
        "meus aparelhos",
        "quais devices você conhece sobre mim",
        "lista os equipamentos que estão cadastrados",
        "quais dispositivos registrados eu tenho",
    ],
)
def test_known_devices_query_detected(message):
    assert IntentRouter._is_known_devices_query(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "quais dispositivos estão conectados agora",
        "quais devices estão online",
        "lista os dispositivos conectados ao neo em tempo real",
        "quais aparelhos estão ligados agora",
    ],
)
def test_connected_devices_query_not_treated_as_known(message):
    assert IntentRouter._is_known_devices_query(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "qual a previsão do tempo amanhã",
        "o que você sabe sobre o clima de hoje",
        "manda um oi pra minha mãe",
    ],
)
def test_unrelated_queries_not_treated_as_known_devices(message):
    assert IntentRouter._is_known_devices_query(message) is False


def test_correction_turn_uses_history_to_reach_pks():
    # Turn 2 has no device noun and even mentions "conectado", but references
    # the prior device exchange ("os que ...") + a knowledge qualifier.
    history = [
        {"role": "user", "content": "quais dispositivos eu tenho"},
        {"role": "assistant", "content": "Você tem 1 dispositivo conectado: Desktop."},
    ]
    message = "esse é o que você está conectado, o que eu quero é os que você tem conhecimento"
    assert IntentRouter._is_known_devices_query(message, history) is True


def test_correction_turn_without_device_history_does_not_fire():
    # Same shape, but the conversation was never about devices → stay off PKS path.
    history = [
        {"role": "user", "content": "qual a capital da França"},
        {"role": "assistant", "content": "Paris."},
    ]
    message = "quero os que você tem conhecimento"
    assert IntentRouter._is_known_devices_query(message, history) is False


# ── _known_devices_decision ──────────────────────────────────────────────────

def test_known_devices_decision_is_direct_response_with_memory():
    router = object.__new__(IntentRouter)
    decision = router._known_devices_decision("quais dispositivos eu tenho")
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.needs_memory is True
    assert "dispositivo" in (decision.knowledge_keywords or [])
    assert decision.tool_name is None
    assert decision.requires_approval is False


# ── _apply_memory_heuristics backstop ────────────────────────────────────────

def test_memory_heuristics_flag_devices_even_if_classifier_misses():
    router = object.__new__(IntentRouter)
    from shared import RoutingDecision

    decision = RoutingDecision(
        intent=IntentType.DIRECT_RESPONSE,
        confidence=0.8,
        needs_memory=False,  # simulate the classifier's mistake (session e245da98)
    )
    router._apply_memory_heuristics(decision, "quais dispositivos eu tenho")
    assert decision.needs_memory is True
    assert "dispositivo" in (decision.knowledge_keywords or [])


# ── _fallback_classify (LLM unavailable) ─────────────────────────────────────

def test_fallback_routes_known_devices_to_direct_response():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("quais dispositivos eu tenho")
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.needs_memory is True


# ── SERVER_TOOL safety net: history seeding for the executor ──────────────────

def test_server_tool_memory_seeds_recent_history():
    from uuid import uuid4
    from modules.desktop.ai_service import AIService

    history = [
        {"role": "user", "content": "quais dispositivos eu tenho"},
        {"role": "assistant", "content": "Você tem 1 dispositivo conectado."},
    ]
    memory = AIService._build_server_tool_memory(uuid4(), history, "e os outros?")
    assert memory is not None
    contents = [m.content for m in memory.get_messages()]
    assert "quais dispositivos eu tenho" in contents
    assert "Você tem 1 dispositivo conectado." in contents


def test_server_tool_memory_drops_duplicate_current_message():
    from uuid import uuid4
    from modules.desktop.ai_service import AIService

    # If get_history raced and already includes the current turn, don't duplicate it.
    history = [
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "quais dispositivos eu tenho"},
    ]
    memory = AIService._build_server_tool_memory(uuid4(), history, "quais dispositivos eu tenho")
    contents = [m.content for m in memory.get_messages()] if memory else []
    assert contents.count("quais dispositivos eu tenho") == 0


def test_server_tool_memory_none_without_history():
    from uuid import uuid4
    from modules.desktop.ai_service import AIService

    assert AIService._build_server_tool_memory(uuid4(), [], "oi") is None
    assert AIService._build_server_tool_memory(uuid4(), None, "oi") is None
