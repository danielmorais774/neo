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

        def with_options(self, **_kwargs):
            """O ``complete()`` pede o client sem o retry interno do SDK.

            O dublê tem que oferecer a mesma interface do ``AsyncOpenAI``; como
            ele não repete nada sozinho, devolver a si mesmo já é ``max_retries=0``.
            """
            return self

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


# ── response_format: mesma regra, outro parâmetro ───────────────────────────
#
# O classificador passou a pedir saída JSON nativa para não ter como responder
# em prosa (o reparo custava uma chamada inteira). O parâmetro é da API da
# OpenAI; nos providers que não o têm ele precisa sumir antes do corpo.


@pytest.mark.asyncio
async def test_response_format_nao_vaza_para_a_anthropic():
    """A Anthropic fala `messages.create`, não `chat.completions` — dublê próprio."""
    from core.llm.claude_provider import ClaudeProvider

    class _Messages:
        async def create(self, **params):
            raise _CapturedCall(params)

    class _Client:
        messages = _Messages()

    provider = ClaudeProvider(api_key="k")
    provider.client = _Client()
    try:
        await provider.complete(
            messages=[Message(role=MessageRole.USER, content="oi")],
            tools=None,
            model="m",
            response_format={"type": "json_object"},
            disable_thinking=True,
        )
    except Exception as e:  # noqa: BLE001
        seen = e
        while seen is not None and not isinstance(seen, _CapturedCall):
            seen = seen.__cause__ or seen.__context__
        assert isinstance(seen, _CapturedCall), e
        assert "response_format" not in seen.params, "iria no corpo e viraria 400"
    else:
        raise AssertionError("a chamada não chegou ao cliente")


@pytest.mark.asyncio
async def test_response_format_chega_na_openai():
    from core.llm.openai_provider import OpenAIProvider

    params = await _params_for(
        OpenAIProvider(api_key="k"), response_format={"type": "json_object"}
    )
    assert params["response_format"] == {"type": "json_object"}


def test_endpoint_que_recusa_response_format_e_reconhecido():
    """A degradação só acontece se soubermos LER a recusa."""
    from core.llm.openai_provider import _rejects_response_format

    class _E(Exception):
        status_code = 400

    assert _rejects_response_format(_E("Invalid parameter: response_format"))
    # 400 de outra coisa não pode virar degradação silenciosa do parâmetro.
    assert not _rejects_response_format(_E("context length exceeded"))
