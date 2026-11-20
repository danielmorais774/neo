"""Quem travou, e como evitá-lo só na nova tentativa (session-435a6701, 20/11/2026).

O DigitalOcean abortou a rodada final com "The model is repeating the same
chunk". A rodada agora é refeita uma vez, sem o provedor que falhou: o nome
vem do erro e vai para ``provider.ignore`` daquela chamada só.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.llm.openrouter_provider import OpenRouterProvider, provedor_que_falhou
from shared import LLMProviderError

TEXTO = (
    "Erro no streaming OpenRouter: Upstream error from DigitalOcean: "
    "The model is repeating the same chunk = _css_css_css_css."
)


def test_nome_vem_do_texto_do_erro():
    assert provedor_que_falhou(LLMProviderError(TEXTO)) == "DigitalOcean"


def test_nome_vem_da_causa_embrulhada():
    original = RuntimeError("Upstream error from Inferact vLLM: boom")
    try:
        try:
            raise original
        except RuntimeError as e:
            raise LLMProviderError("Erro no streaming OpenAI: boom") from e
    except LLMProviderError as fora:
        assert provedor_que_falhou(fora) == "Inferact vLLM"


def test_metadata_do_corpo_vence_o_texto():
    erro = RuntimeError("Upstream error from X: y")
    erro.body = {"message": "y", "metadata": {"provider_name": "Nebius"}}
    assert provedor_que_falhou(erro) == "Nebius"


def test_erro_sem_provedor_nao_inventa():
    assert provedor_que_falhou(LLMProviderError("timeout")) is None
    assert provedor_que_falhou(None) is None


def test_provedor_a_evitar_soma_ao_ignore_do_neo_toml():
    kwargs: dict = {}
    OpenRouterProvider._apply_provider_ignore(kwargs, ["DigitalOcean"])
    assert kwargs["extra_body"]["provider"] == {"ignore": ["open-inference", "DigitalOcean"]}


def test_pin_explicito_nao_ganha_ignore():
    kwargs: dict = {}
    OpenRouterProvider._split_provider_pin("minimax/minimax-m3.1-flash@nebius", kwargs)
    OpenRouterProvider._apply_provider_ignore(kwargs, ["DigitalOcean"])
    assert kwargs["extra_body"]["provider"] == {"order": ["nebius"], "allow_fallbacks": False}


@pytest.mark.asyncio
async def test_complete_tira_o_kwarg_e_manda_o_ignore(monkeypatch):
    provider = OpenRouterProvider.__new__(OpenRouterProvider)
    enviado: dict = {}

    async def _super_complete(self, *, messages, tools=None, model=None, **kwargs):
        enviado.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        "core.llm.openai_provider.OpenAIProvider.complete", _super_complete, raising=True
    )
    from shared.types import Message, MessageRole

    try:
        await provider.complete(
            messages=[Message(role=MessageRole.USER, content="oi")],
            model="minimax/minimax-m3.1-flash",
            evitar_provedores=["DigitalOcean"],
        )
    except Exception:  # noqa: BLE001 — só interessa o que foi montado para a API
        pass
    assert "evitar_provedores" not in enviado
    assert "DigitalOcean" in enviado["extra_body"]["provider"]["ignore"]
