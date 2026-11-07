"""Modelo que EXIGE raciocínio não pode morrer no `disable_thinking` do sistema.

Medido em 2026-11-06: o `liquid/lfm-3-1.7b`, cadastrado pelo painel, respondia
    400 "Reasoning is mandatory for this endpoint and cannot be disabled."
a TODO turno — o cérebro (e o classificador, o afeto, o navegador…) pede
`reasoning.enabled=false`, e o chat mostrava "deu erro do meu lado". O provider
aprende na primeira recusa, refaz sem o campo e não manda mais para esse modelo.
"""

from __future__ import annotations

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_chunk import ChoiceDelta
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from core.llm import openrouter_provider as orp
from core.llm.openrouter_provider import _REASONING_DISABLED, OpenRouterProvider
from shared import Message, MessageRole

_MANDATORY = (
    "Error code: 400 - {'error': {'message': 'Reasoning is mandatory for this endpoint "
    "and cannot be disabled.', 'code': 400, 'metadata': {'provider_name': None}}}"
)


def _completion(text: str) -> ChatCompletion:
    return ChatCompletion(
        id="x",
        object="chat.completion",
        created=0,
        model="m",
        choices=[
            Choice(
                index=0,
                finish_reason="stop",
                message=ChatCompletionMessage(role="assistant", content=text),
            )
        ],
    )


async def _chunks(text: str):
    yield ChatCompletionChunk(
        id="x",
        object="chat.completion.chunk",
        created=0,
        model="m",
        choices=[ChunkChoice(index=0, delta=ChoiceDelta(content=text), finish_reason=None)],
    )
    yield ChatCompletionChunk(
        id="x",
        object="chat.completion.chunk",
        created=0,
        model="m",
        choices=[ChunkChoice(index=0, delta=ChoiceDelta(), finish_reason="stop")],
    )


class _Gateway:
    """Recusa como o OpenRouter recusou: só quando o raciocínio vem desligado."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **params):
        self.calls.append(params)
        if (params.get("extra_body") or {}).get("reasoning") == _REASONING_DISABLED["reasoning"]:
            raise RuntimeError(_MANDATORY)
        if params.get("stream"):
            return _chunks("oi do stream")
        return _completion("oi")


def _provider() -> tuple[OpenRouterProvider, _Gateway]:
    gateway = _Gateway()

    class _Chat:
        completions = gateway

    class _Client:
        chat = _Chat()

        def with_options(self, **_kwargs):
            return self

    provider = OpenRouterProvider(api_key="k")
    provider.client = _Client()
    return provider, gateway


@pytest.fixture(autouse=True)
def _esquece_o_aprendido():
    orp._REASONING_MANDATORY.clear()
    yield
    orp._REASONING_MANDATORY.clear()


_MSGS = [Message(role=MessageRole.USER, content="oi")]


@pytest.mark.asyncio
async def test_complete_refaz_com_raciocinio_e_lembra_do_modelo():
    provider, gateway = _provider()
    response = await provider.complete(_MSGS, model="liquid/lfm-3-1.7b", disable_thinking=True)
    assert response.get_content() == "oi"
    assert len(gateway.calls) == 2, "uma recusa, uma tentativa sem o campo"
    assert "reasoning" not in (gateway.calls[1].get("extra_body") or {})

    # Daqui em diante o campo nem vai: nenhuma recusa nova.
    await provider.complete(_MSGS, model="liquid/lfm-3-1.7b", disable_thinking=True)
    assert len(gateway.calls) == 3
    assert "reasoning" not in (gateway.calls[2].get("extra_body") or {})


@pytest.mark.asyncio
async def test_stream_do_chat_refaz_antes_do_primeiro_chunk():
    provider, gateway = _provider()
    texto = ""
    async for chunk in provider.stream_complete(
        _MSGS, model="liquid/lfm-3-1.7b", disable_thinking=True
    ):
        texto += chunk.content or ""
    assert "oi do stream" in texto
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_pin_de_provedor_sobrevive_ao_refazer():
    provider, gateway = _provider()
    await provider.complete(_MSGS, model="liquid/lfm-3-1.7b@liquid", disable_thinking=True)
    assert gateway.calls[1]["model"] == "liquid/lfm-3-1.7b"
    assert gateway.calls[1]["extra_body"]["provider"] == {"order": ["liquid"], "allow_fallbacks": False}
    assert "liquid/lfm-3-1.7b" in orp._REASONING_MANDATORY


@pytest.mark.asyncio
async def test_outros_modelos_continuam_com_o_raciocinio_desligado():
    provider, gateway = _provider()
    await provider.complete(_MSGS, model="liquid/lfm-3-1.7b", disable_thinking=True)

    class _Aceita(_Gateway):
        async def create(self, **params):
            self.calls.append(params)
            return _completion("ok")

    outro = _Aceita()
    provider.client.chat.completions = outro
    await provider.complete(_MSGS, model="google/gemini-3.1-flash-lite", disable_thinking=True)
    assert outro.calls[0]["extra_body"]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_erro_que_nao_e_de_raciocinio_continua_subindo():
    provider, gateway = _provider()

    class _Quebrado(_Gateway):
        async def create(self, **params):
            self.calls.append(params)
            raise RuntimeError("Error code: 400 - model not found")

    quebrado = _Quebrado()
    provider.client.chat.completions = quebrado
    with pytest.raises(Exception, match="model not found"):
        await provider.complete(_MSGS, model="x/y", disable_thinking=True)
    assert len(quebrado.calls) == 1 and not orp._REASONING_MANDATORY
