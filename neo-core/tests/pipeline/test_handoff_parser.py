"""Phase 2 — handoff block extraction from the cognitive core's reply."""

from core.pipeline.handoff_parser import extract_handoffs
from shared.types import IntentType


def test_extracts_block_and_strips_from_visible_text():
    text = (
        "Você costuma ligar nas quintas, então pode ligar.\n\n"
        "[[HANDOFF:home_assistant]]ligar o ar-condicionado do quarto[[/HANDOFF]]"
    )
    clean, handoffs = extract_handoffs(text)
    assert clean == "Você costuma ligar nas quintas, então pode ligar."
    assert "[[HANDOFF" not in clean
    assert len(handoffs) == 1
    h = handoffs[0]
    assert h.target_intent == IntentType.HOME_ASSISTANT
    assert h.target_skill == "home_assistant"
    assert h.input_text == "ligar o ar-condicionado do quarto"


def test_no_block_is_passthrough():
    clean, handoffs = extract_handoffs("apenas uma conversa normal")
    assert clean == "apenas uma conversa normal"
    assert handoffs == []


def test_multiple_blocks_in_order():
    text = (
        "[[HANDOFF:home_assistant]]apagar as luzes[[/HANDOFF]]"
        "[[HANDOFF:whatsapp_messaging]]avisar o pai que vou dormir[[/HANDOFF]]"
    )
    clean, handoffs = extract_handoffs(text)
    assert clean == ""
    assert [h.target_skill for h in handoffs] == ["home_assistant", "whatsapp_messaging"]
    assert handoffs[1].target_intent == IntentType.WHATSAPP_MESSAGING


def test_unknown_skill_is_dropped_but_block_removed():
    clean, handoffs = extract_handoffs("antes [[HANDOFF:foobar]]x[[/HANDOFF]] depois")
    assert handoffs == []
    assert "HANDOFF" not in clean
    assert "antes" in clean and "depois" in clean


def test_empty_input_block_is_dropped():
    clean, handoffs = extract_handoffs("oi [[HANDOFF:home_assistant]]   [[/HANDOFF]]")
    assert handoffs == []
    assert "HANDOFF" not in clean


def test_whitespace_and_case_tolerance():
    text = "ok [[ handoff : Home_Assistant ]]ligar a luz[[ / handoff ]]"
    clean, handoffs = extract_handoffs(text)
    assert len(handoffs) == 1
    assert handoffs[0].target_intent == IntentType.HOME_ASSISTANT
    assert handoffs[0].input_text == "ligar a luz"


def test_stray_opening_tag_is_cleaned_defensively():
    # Malformed (no closing tag) → no handoff, but the stray tag must not leak.
    clean, handoffs = extract_handoffs("texto [[HANDOFF:home_assistant]] sem fechamento")
    assert handoffs == []
    assert "[[HANDOFF" not in clean
