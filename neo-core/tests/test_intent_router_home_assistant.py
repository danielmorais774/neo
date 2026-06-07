"""Routing tests for the HOME_ASSISTANT intent (offline fallback + parsing)."""

from core.router.intent_router import IntentRouter
from shared import IntentType


def test_fallback_routes_light_control_to_home_assistant():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("acende a luz da sala")
    assert decision.intent == IntentType.HOME_ASSISTANT
    assert decision.skill_name == "home_assistant"


def test_fallback_routes_thermostat_query_to_home_assistant_not_weather():
    router = object.__new__(IntentRouter)
    # "temperatura" alone would be weather; "termostato" must win for HA.
    decision = router._fallback_classify("qual a temperatura do termostato?")
    assert decision.intent == IntentType.HOME_ASSISTANT


def test_fallback_routes_scene_to_home_assistant():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("ativa o modo cinema")
    assert decision.intent == IntentType.HOME_ASSISTANT


def test_fallback_weather_still_routes_to_server_tool():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("qual o clima hoje em são paulo?")
    assert decision.intent == IntentType.SERVER_TOOL


def test_fallback_whatsapp_still_routes_to_whatsapp():
    router = object.__new__(IntentRouter)
    decision = router._fallback_classify("manda um oi pra minha mãe")
    assert decision.intent == IntentType.WHATSAPP_MESSAGING


def test_connected_devices_query_not_routed_to_home_assistant():
    router = object.__new__(IntentRouter)
    # Neo's own connected devices must NOT be mistaken for smart-home control.
    decision = router._fallback_classify("quais dispositivos estão conectados agora?")
    assert decision.intent != IntentType.HOME_ASSISTANT


def test_parse_classification_maps_home_assistant_intent():
    router = object.__new__(IntentRouter)
    raw = (
        '{"intent": "HOME_ASSISTANT", "confidence": 0.95, "skill_name": "home_assistant", '
        '"explanation": "controle de luz"}'
    )
    decision = router._parse_classification(raw, user_message="acende a luz")
    assert decision.intent == IntentType.HOME_ASSISTANT
    assert decision.skill_name == "home_assistant"


def test_referential_question_not_routed_to_home_assistant():
    router = object.__new__(IntentRouter)
    # Even with a fallback, a referential follow-up about a past HA action is a
    # question, not a new command (session 9d61bfba misrouted this).
    decision = router._fallback_classify("como tu chegou nessa conta de 22?")
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.needs_memory is True


def test_referential_question_detector():
    assert IntentRouter._is_referential_question("como tu chegou nessa conta de 22?") is True
    assert IntentRouter._is_referential_question("de onde veio isso?") is True
    # A real HA command must not be caught by the referential guard.
    assert IntentRouter._is_referential_question("acende a luz da sala") is False
