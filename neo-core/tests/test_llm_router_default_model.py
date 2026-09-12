"""O provider DEFAULT honra o DEFAULT_MODEL do env.

Sem isto o `DEFAULT_MODEL` do `.env` não valia para ninguém: cada provider nasce
com o modelo de fábrica DELE, e quem chama `get_provider()` sem nome — PlanManager,
extrator de preferências, compactador de contexto — não passa modelo. O operador
configurava uma coisa e o sistema rodava outra (neo-llm-slots-and-wallets).
"""

from __future__ import annotations

import pytest

from core.llm.router import LLMRouter


def _router(monkeypatch, *, provider: str, model: str) -> LLMRouter:
    r = LLMRouter.__new__(LLMRouter)

    class _S:
        default_provider = provider
        default_model = model

    r.settings = _S()

    class _P:
        def __init__(self, m):
            self.model = m

    r._providers = {
        "openrouter": _P("google/gemini-3.1-flash-lite"),
        "deepseek": _P("deepseek-v4-flash"),
    }
    r._apply_default_model()
    return r


def test_default_model_chega_no_provider_default(monkeypatch):
    r = _router(monkeypatch, provider="openrouter", model="deepseek/deepseek-v4-flash@baidu")
    assert r._providers["openrouter"].model == "deepseek/deepseek-v4-flash@baidu"
    assert r._providers["deepseek"].model == "deepseek-v4-flash", "só o default muda"


def test_pin_de_endpoint_nao_e_outro_modelo(monkeypatch):
    """`@baidu` escolhe o ENDPOINT no OpenRouter; o catálogo não conhece o id pinado."""
    r = _router(monkeypatch, provider="openrouter", model="deepseek/deepseek-v4-flash@baidu")
    assert "@baidu" in r._providers["openrouter"].model


def test_par_invertido_e_ignorado(monkeypatch):
    """Modelo declaradamente de OUTRO provider é engano: trocaria 402 por 404."""
    r = _router(monkeypatch, provider="openrouter", model="gpt-4o")
    assert r._providers["openrouter"].model == "google/gemini-3.1-flash-lite"


def test_sem_default_model_nao_mexe(monkeypatch):
    r = _router(monkeypatch, provider="openrouter", model="")
    assert r._providers["openrouter"].model == "google/gemini-3.1-flash-lite"


def test_provider_default_ausente_nao_levanta(monkeypatch):
    """Chave não configurada ⇒ provider não registrado; isto não pode derrubar o boot."""
    r = _router(monkeypatch, provider="grok", model="grok-3-mini")
    assert "grok" not in r._providers
