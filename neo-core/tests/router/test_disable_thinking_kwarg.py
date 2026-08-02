"""`disable_thinking` é intenção do chamador, não parâmetro de API.

Quem pede saída curta e estruturada (o classificador) diz o QUE quer; cada
provider decide como traduzir. O DeepSeek sabe (`extra_body`); os outros
precisam ENGOLIR a chave — indo junto no corpo da requisição, a API responde
400 e o turno morre, que é pior do que o problema original.

Engolir NÃO é o comportamento certo para todo mundo: o OpenRouter **herda** o
`complete` da OpenAI, então por herança ele descartava a flag — e os modelos
servidos lá raciocinam por padrão. O classificador pedia saída estruturada em
todo turno e pagava o raciocínio inteiro em silêncio (medido 2026-08-01 no
`qwen3.5-flash`: 11-20s e até 3.995 tokens de reasoning; com o campo, ~0,8s).
"""

import pytest

from core.llm.deepseek_provider import _THINKING_DISABLED, DeepSeekProvider
from core.llm.openai_provider import OpenAIProvider
from core.llm.openrouter_provider import _REASONING_DISABLED, OpenRouterProvider
from shared import Message, MessageRole


class _CapturedCall(Exception):
    """Interrompe a chamada carregando os parâmetros montados."""

    def __init__(self, params: dict) -> None:
        self.params = params
        super().__init__("captured")


def _client_that_captures():
    class _Completions:
        async def create(self, **params):
            raise _CapturedCall(params)

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    return _Client()


async def _params_for(provider, **kwargs) -> dict:
    provider.client = _client_that_captures()
    messages = [Message(role=MessageRole.USER, content="oi")]
    try:
        await provider.complete(messages=messages, tools=None, model="m", **kwargs)
    except Exception as e:  # noqa: BLE001 — cada provider embrulha o erro do de baixo
        seen = e
        while seen is not None:
            if isinstance(seen, _CapturedCall):
                return seen.params
            seen = seen.__cause__ or seen.__context__
        raise
    raise AssertionError("a chamada não chegou ao cliente")


@pytest.mark.asyncio
async def test_deepseek_translates_into_extra_body():
    params = await _params_for(DeepSeekProvider(api_key="k"), disable_thinking=True)

    assert params["extra_body"] == _THINKING_DISABLED
    assert "disable_thinking" not in params


@pytest.mark.asyncio
async def test_openai_swallows_the_flag():
    params = await _params_for(OpenAIProvider(api_key="k"), disable_thinking=True)

    assert "disable_thinking" not in params, "iria no corpo e viraria 400"
    assert "extra_body" not in params


@pytest.mark.asyncio
async def test_openrouter_translates_into_reasoning_field():
    """Não pode herdar o "engolir" da OpenAI: aqui a flag tem tradução."""
    params = await _params_for(OpenRouterProvider(api_key="k"), disable_thinking=True)

    assert params["extra_body"] == _REASONING_DISABLED
    assert "disable_thinking" not in params


@pytest.mark.asyncio
async def test_openrouter_keeps_caller_extra_body():
    params = await _params_for(
        OpenRouterProvider(api_key="k"),
        disable_thinking=True,
        extra_body={"reasoning": {"enabled": True}, "transforms": []},
    )

    # Quem foi explícito manda: o extra_body do chamador vence a tradução.
    assert params["extra_body"]["reasoning"] == {"enabled": True}
    assert params["extra_body"]["transforms"] == []


@pytest.mark.asyncio
async def test_absent_flag_changes_nothing():
    params = await _params_for(DeepSeekProvider(api_key="k"))

    assert "extra_body" not in params


@pytest.mark.asyncio
async def test_openrouter_absent_flag_changes_nothing():
    params = await _params_for(OpenRouterProvider(api_key="k"))

    assert "extra_body" not in params
