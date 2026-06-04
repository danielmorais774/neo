"""Tests for the memory-directive routing guard and current-device PKS injection.

Covers:
- IntentRouter routes explicit memory directives ("registra no PKS", "lembra que…")
  to DIRECT_RESPONSE instead of CLIENT_TOOL/write_file (the cause of the hang).
- AIService helpers that detect a current-device reference and build the device
  identity context fed to the PKS memory planner.
"""

from __future__ import annotations

import pytest

from core.router.intent_router import IntentRouter
from modules.desktop.ai_service import AIService
from shared import IntentType


# ── Router: memory-directive guard ─────────────────────────────────────────────

# Bare instance: the guard helpers are pure, so object.__new__ avoids LLM/db deps.
_router = object.__new__(IntentRouter)


@pytest.mark.parametrize(
    "message",
    [
        "registra no PKS o id desse dispositivo",
        "lembra que esse dispositivo é um MacBook Air",
        "memoriza isso pra mim",
        "anota que eu prefiro café sem açúcar",
        "não esqueça que minha reunião é às 15h",
        "salva isso na memória",
        "remember that my birthday is in June",
    ],
)
def test_memory_directives_detected(message):
    assert IntentRouter._is_memory_directive(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "qual o clima em São Paulo?",
        "lista os arquivos da pasta Downloads",
        "escreve o relatório no arquivo report.txt",
        "oi tudo bem?",
    ],
)
def test_non_memory_messages_not_flagged(message):
    assert IntentRouter._is_memory_directive(message) is False


def test_memory_directive_routes_to_direct_response():
    decision = _router._memory_decision("registra no PKS o id desse dispositivo")
    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.needs_memory is True
    assert decision.requires_approval is False


def test_fallback_classify_keeps_memory_directive_direct():
    # Even via the keyword fallback, "registra no PKS" must not become a file op.
    decision = _router._fallback_classify("registra no PKS o id desse dispositivo")
    assert decision.intent == IntentType.DIRECT_RESPONSE


def test_fallback_classify_file_op_still_client_tool():
    # Guard must not swallow genuine local file/system requests.
    decision = _router._fallback_classify("lista os arquivos e mostra o tamanho do disco")
    assert decision.intent == IntentType.CLIENT_TOOL


# ── AIService: current-device detection + context builder ──────────────────────

@pytest.mark.parametrize(
    "message,expected",
    [
        ("esse dispositivo é um MacBook Air", True),
        ("registra o id desse computador", True),
        ("essa máquina está lenta", True),
        ("qual a previsão do tempo amanhã?", False),
        ("manda um oi pra minha mãe", False),
    ],
)
def test_references_current_device(message, expected):
    assert AIService._references_current_device(message) is expected


def test_build_current_device_context_includes_identity():
    note = AIService._build_current_device_context({
        "device_id": "dev-123",
        "hardware_id": "HW-ABC",
        "model": "MacBookAir10,1",
        "os_platform": "Darwin",
        "os_release": "25.4.0",
    })
    assert "dev-123" in note
    assert "HW-ABC" in note
    assert "MacBookAir10,1" in note
    assert "device" in note.lower()


def test_build_current_device_context_empty_without_identity():
    assert AIService._build_current_device_context({}) == ""
    assert AIService._build_current_device_context(None) == ""
    # model/os alone (no id/hardware_id) is not enough to anchor an entity.
    assert AIService._build_current_device_context({"model": "unknown"}) == ""


def test_build_current_device_context_skips_unknown_values():
    note = AIService._build_current_device_context({
        "device_id": "dev-9",
        "model": "unknown",
        "os_platform": "unknown",
    })
    assert "dev-9" in note
    assert "unknown" not in note
