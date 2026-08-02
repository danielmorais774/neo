"""Recuperação de resposta truncada do classificador.

O bug: com `max_tokens=300`, um modelo que raciocina gastava o orçamento antes
de fechar o JSON. O conteúdo voltava cortado no meio (ou vazio), `_carries_json`
dizia não, e o "reparo" mandava o modelo parar de conversar — remédio para outra
doença. Resultado: fallback por palavra-chave, que ignora o prompt e o histórico
(13 de 46 casos do golden set com deepseek-v4-flash).

Aqui os dois modos de falha são separados e cada um é verificado com o seu
remédio, sem rede: um provider falso conta as chamadas e diz o que devolveu.
"""

import pytest

from core.router.intent_router import (
    _CLASSIFIER_MAX_TOKENS,
    _CLASSIFIER_MAX_TOKENS_RETRY,
    _CLASSIFIER_TYPICAL_OUTPUT_TOKENS,
    IntentRouter,
)
from shared import IntentType, ToolParameter, ToolSchema

_GOOD_JSON = (
    '{"speech_act": "imperative", "intent": "CLIENT_TOOL", "confidence": 0.95, '
    '"tool_name": "run_command", "tool_arguments": {}, "raw_command": "df -h", '
    '"explanation": "espaço em disco", "requires_approval": false, '
    '"needs_memory": true, "knowledge_keywords": ["disco"], "needs_plan": false, '
    '"tipo_tarefa": "simples", "skill_name": null, "pipeline_plan": null, '
    '"gravity": "normal", "register_hint": null}'
)

# Como o JSON chega quando o orçamento acaba no meio da escrita.
_TRUNCATED = '{\n  "speech_act": "imperative",\n  "intent": "CLIENT_TOOL",\n  "confidence": 0.9'


class FakeResponse:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self._content = content
        self._finish_reason = finish_reason

    def get_content(self) -> str:
        return self._content

    def finish_reason(self) -> str:
        return self._finish_reason


class FakeProvider:
    """Devolve as respostas da fila e registra os kwargs de cada chamada."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0) if self._responses else FakeResponse("", "stop")


class FakeLLMRouter:
    def __init__(self, provider: FakeProvider) -> None:
        self._provider = provider

    def get_provider(self, _name: str) -> FakeProvider:
        return self._provider

    def list_available_providers(self) -> list[str]:
        return ["deepseek"]


class FakeRegistry:
    """Catálogo mínimo: `run_command` precisa EXISTIR de verdade.

    Sem ele, o guard de tool_name desconhecido rebaixa a decisão para
    DIRECT_RESPONSE e o teste mediria o guard, não a retentativa.
    """

    def get_all(self) -> list[ToolSchema]:
        return [
            ToolSchema(
                name="run_command",
                description="Executa um comando shell no terminal do usuário.",
                parameters={
                    "command": ToolParameter(type="string", description="cmd", required=True)
                },
                module="desktop",
                execution="client-side",
            )
        ]


def build_router(responses: list[FakeResponse]) -> tuple[IntentRouter, FakeProvider]:
    provider = FakeProvider(responses)
    router = IntentRouter(llm_router=FakeLLMRouter(provider), tool_registry=FakeRegistry())
    return router, provider


@pytest.mark.asyncio
async def test_truncated_json_is_retried_with_more_room():
    router, provider = build_router([
        FakeResponse(_TRUNCATED, finish_reason="length"),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    decision = await router.classify("quantos GB livres tem no meu disco?")

    assert decision.intent == IntentType.CLIENT_TOOL, "a retentativa deve recuperar o turno"
    assert decision.tool_name == "run_command"
    assert len(provider.calls) == 2
    assert provider.calls[1]["max_tokens"] == _CLASSIFIER_MAX_TOKENS_RETRY
    # A retentativa repete a MESMA pergunta: o que faltou foi espaço, não educação.
    assert len(provider.calls[1]["messages"]) == len(provider.calls[0]["messages"])


@pytest.mark.asyncio
async def test_empty_content_is_treated_as_truncation():
    """Raciocínio comendo o orçamento inteiro: volta string vazia, sem erro."""
    router, provider = build_router([
        FakeResponse("", finish_reason="stop"),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    decision = await router.classify("quantos GB livres tem no meu disco?")

    assert decision.intent == IntentType.CLIENT_TOOL
    assert provider.calls[1]["max_tokens"] == _CLASSIFIER_MAX_TOKENS_RETRY


@pytest.mark.asyncio
async def test_prose_still_gets_the_repair_nudge():
    """Modelo conversando (finish_reason=stop, texto completo) → outro remédio."""
    router, provider = build_router([
        FakeResponse("Claro! Aqui estão as chaves SSH que encontrei: id_rsa, id_ed25519."),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    decision = await router.classify("e quais?")

    assert decision.intent == IntentType.CLIENT_TOOL
    # O reparo acrescenta 2 mensagens (o eco da prosa + a ordem de parar).
    assert len(provider.calls[1]["messages"]) == len(provider.calls[0]["messages"]) + 2
    assert "CLASSIFICADOR" in provider.calls[1]["messages"][-1].content


@pytest.mark.asyncio
async def test_first_call_asks_for_room_and_no_reasoning():
    router, provider = build_router([FakeResponse(_GOOD_JSON, finish_reason="stop")])

    await router.classify("quantos GB livres tem no meu disco?")

    assert len(provider.calls) == 1, "resposta boa não deve gerar retentativa"
    assert provider.calls[0]["max_tokens"] == _CLASSIFIER_MAX_TOKENS
    assert provider.calls[0]["disable_thinking"] is True


def test_budget_keeps_a_professional_margin():
    """O teto é medido em MÚLTIPLOS da resposta típica, não em número redondo.

    Foi assim que o valor antigo apodreceu sem ninguém notar: 300 eram 5× o
    contrato de 8 campos de fev/2026 e viraram 2× quando ele dobrou. Amarrar o
    teste à razão faz um contrato maior no futuro derrubar o teste, não o turno
    do usuário.
    """
    typical = _CLASSIFIER_TYPICAL_OUTPUT_TOKENS
    assert _CLASSIFIER_MAX_TOKENS >= 10 * typical, "menos de 10× a resposta típica é pouca folga"
    assert _CLASSIFIER_MAX_TOKENS_RETRY >= 2 * _CLASSIFIER_MAX_TOKENS
    # Teto de saída mais baixo entre os providers selecionáveis no painel
    # (modelos Claude antigos param em 4096): pedir mais viraria 400.
    assert _CLASSIFIER_MAX_TOKENS_RETRY <= 4096


@pytest.mark.asyncio
async def test_gives_up_to_the_fallback_when_retry_also_fails():
    """Sem JSON nas duas tentativas, o turno degrada — mas não levanta exceção."""
    router, provider = build_router([
        FakeResponse(_TRUNCATED, finish_reason="length"),
        FakeResponse(_TRUNCATED, finish_reason="length"),
    ])

    decision = await router.classify("oi")

    assert decision is not None
    assert len(provider.calls) == 2
