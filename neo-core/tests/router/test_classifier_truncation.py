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
    # O reparo acrescenta 3 mensagens: eco da prosa + correção de SISTEMA +
    # a mensagem do usuário REPETIDA (ver o teste do alvo, logo abaixo).
    msgs = provider.calls[1]["messages"]
    assert len(msgs) == len(provider.calls[0]["messages"]) + 3
    assert "CLASSIFICADOR" in msgs[-2].content


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


# ── o reparo não pode virar o alvo (sessão 289b6c29, 2026-09-07) ────────────

_WHATSAPP_ECHOING_OUR_PROMPT = (
    '{"speech_act": "imperative", "intent": "WHATSAPP_MESSAGING", "confidence": 0.95, '
    '"tool_name": null, "tool_arguments": {"text": "PARE. Você é um CLASSIFICADOR, não o '
    'assistente: não responda ao usuário e não invente conteúdo. Devolva SÓ o objeto JSON do '
    'formato de saída, classificando a última mensagem do usuário. Nada antes, nada depois.", '
    '"recipient": "minha mãe"}, "raw_command": null, "explanation": "a mensagem contém PARE", '
    '"requires_approval": false, "needs_memory": true, "knowledge_keywords": [], '
    '"needs_plan": false, "tipo_tarefa": "simples", "skill_name": null, "pipeline_plan": null, '
    '"gravity": "normal", "register_hint": null}'
)


@pytest.mark.asyncio
async def test_reparo_nao_vira_a_mensagem_a_classificar():
    """A instrução de reparo é de SISTEMA e o alvo volta a ser o usuário.

    O prompt manda classificar "a última mensagem do usuário". Enquanto o reparo
    ia como USER, ele mesmo virava esse alvo — e o classificador fazia o trabalho
    certo na mensagem errada.
    """
    router, provider = build_router([
        FakeResponse('A faixa 10 da lista que mostrei é "Tired of You".'),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    await router.classify("era a faixa de 10 daquela primeira lista que vc me mostrou")

    msgs = provider.calls[1]["messages"]
    assert msgs[-1].role.value == "user"
    assert msgs[-1].content == "era a faixa de 10 daquela primeira lista que vc me mostrou", (
        "a última mensagem de USER tem que ser a do usuário, não a nossa instrução"
    )
    assert msgs[-2].role.value == "system", "a correção é instrução, não conteúdo"


@pytest.mark.asyncio
async def test_classificacao_que_ecoa_o_proprio_prompt_e_descartada():
    """A rede: mesmo que o modelo confunda de novo, nada disso vira ação.

    Este é o JSON REAL da sessão 289b6c29 — ele gerou um rascunho de WhatsApp
    para um contato de verdade com o prompt do Neo dentro. Só a confirmação da
    skill impediu o envio.
    """
    router, _ = build_router([FakeResponse(_WHATSAPP_ECHOING_OUR_PROMPT, finish_reason="stop")])

    decision = await router.classify("era a faixa de 10 daquela primeira lista que vc me mostrou")

    assert decision.intent == IntentType.DIRECT_RESPONSE, "tem que cair no cognitivo"
    assert decision.skill_name is None
    assert not decision.tool_arguments
    assert decision.raw_command is None


@pytest.mark.asyncio
async def test_guarda_nao_atrapalha_quem_fala_do_assunto_de_proposito():
    """Perguntar sobre o classificador continua funcionando — a guarda é sobre ECO."""
    router, _ = build_router([FakeResponse(_WHATSAPP_ECHOING_OUR_PROMPT, finish_reason="stop")])

    decision = await router.classify(
        'manda pra minha mãe: "você é um classificador" traduzido pro inglês'
    )

    assert decision.intent == IntentType.WHATSAPP_MESSAGING, (
        "o usuário escreveu a frase; não é o nosso prompt vazando"
    )


# ── âncora: WhatsApp exige destinatário que o usuário citou ─────────────────

def _wa_json(recipient: str, text: str = "oi") -> str:
    import json as _json
    return _json.dumps({
        "speech_act": "imperative", "intent": "WHATSAPP_MESSAGING", "confidence": 0.95,
        "tool_name": None, "tool_arguments": {"text": text, "recipient": recipient},
        "raw_command": None, "explanation": "envio", "requires_approval": False,
        "needs_memory": True, "knowledge_keywords": [], "needs_plan": False,
        "tipo_tarefa": "simples", "skill_name": None, "pipeline_plan": None,
        "gravity": "normal", "register_hint": None,
    })


@pytest.mark.asyncio
async def test_destinatario_inventado_nao_dispara_whatsapp():
    """O caso do Daniel: turno sobre playlist, destinatário saído do nada.

    `speech_act=imperative` + `confidence=0.95` satisfaziam a REGRA DURA do
    prompt — por isso a checagem que faltava é de CONTEÚDO, não de conjugação.
    """
    router, _ = build_router([FakeResponse(_wa_json("minha mãe"), finish_reason="stop")])

    decision = await router.classify(
        "era a faixa de 10 daquela primeira lista que vc me mostrou",
        conversation_history=[
            {"role": "user", "content": "toca a 10 do exies"},
            {"role": "assistant", "content": "A faixa 10 é Tired of You, do Head for the Door."},
        ],
    )

    assert decision.intent == IntentType.DIRECT_RESPONSE
    assert decision.skill_name is None
    assert not decision.tool_arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("msg, recipient", [
    ("manda uma mensagem para minha irmã avisando que vou chegar mais tarde", "minha irmã"),
    # o usuário escreve "mae", o classificador devolve "mãe" — acento não separa
    ("manda uma mensagem para minha mae de parabens", "minha mãe"),
    ('manda uma mensagem whatsapp para 97817997611221@lid, manda um "oi"', "97817997611221@lid"),
    ("pede uma pizza no Domino's", "Domino's"),
    ("procura o João na lista do WhatsApp e manda um oi", "João"),
    ("manda whatsapp pro 5511988217222: oi tudo bem?", "5511988217222"),
    ("avisa o porteiro que vou dormir", "porteiro"),
])
async def test_envio_legitimo_continua_passando(msg, recipient):
    """Os 7 formatos reais do corpus (1887 mensagens) e dos exemplos do prompt."""
    router, _ = build_router([FakeResponse(_wa_json(recipient), finish_reason="stop")])
    decision = await router.classify(msg)
    assert decision.intent == IntentType.WHATSAPP_MESSAGING, f"{recipient!r} foi citado"


@pytest.mark.asyncio
async def test_destinatario_combinado_no_historico_vale():
    """"manda: chego 19h" depois de combinar o contato não repete o nome."""
    router, _ = build_router([FakeResponse(_wa_json("Ana", "chego 19h"), finish_reason="stop")])
    decision = await router.classify(
        "manda: chego 19h",
        conversation_history=[{"role": "user", "content": "quero falar com a Ana"}],
    )
    assert decision.intent == IntentType.WHATSAPP_MESSAGING


@pytest.mark.asyncio
async def test_sem_destinatario_a_skill_pergunta_em_vez_de_barrar():
    """Sem `recipient` a skill pede a quem enviar — barrar aqui só quebraria o fluxo."""
    import json as _json
    payload = _json.loads(_wa_json("x"))
    payload["tool_arguments"] = {"text": "oi"}
    router, _ = build_router([FakeResponse(_json.dumps(payload), finish_reason="stop")])
    decision = await router.classify("manda um oi pra ele")
    assert decision.intent == IntentType.WHATSAPP_MESSAGING


# ── `{}` vazio e teto de raciocínio (session-a20f4722, 13/11) ───────────────


@pytest.mark.asyncio
async def test_objeto_vazio_e_tratado_como_vazio_e_nao_como_prosa():
    """O MiniMax m3.1-flash devolveu `{}` em 1 de 30 pedidos reais (2 tokens)."""
    router, provider = build_router([
        FakeResponse("{}", finish_reason="stop"),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    decision = await router.classify("quantos GB livres tem no meu disco?")

    assert decision.intent == IntentType.CLIENT_TOOL
    assert provider.calls[1]["max_tokens"] == _CLASSIFIER_MAX_TOKENS_RETRY
    # Retentativa simples, sem o "PARE, você é classificador" do reparo de prosa.
    assert len(provider.calls[1]["messages"]) == len(provider.calls[0]["messages"])


def test_objeto_sem_intent_nao_conta_como_classificacao():
    assert IntentRouter._carries_json("{}") is False
    assert IntentRouter._carries_json('{"speech_act": "question"}') is False
    assert IntentRouter._is_empty_object("{}") is True
    assert IntentRouter._is_empty_object(_GOOD_JSON) is False


@pytest.mark.asyncio
async def test_classificador_pede_o_teto_de_raciocinio_do_neo_toml():
    from shared.ajustes import ajustes

    router, provider = build_router([
        FakeResponse("", finish_reason="length"),
        FakeResponse(_GOOD_JSON, finish_reason="stop"),
    ])

    await router.classify("quantos GB livres tem no meu disco?")

    teto = ajustes().roteador.teto_raciocinio
    assert teto > 0
    assert provider.calls[0]["teto_raciocinio"] == teto
    assert provider.calls[1]["teto_raciocinio"] == teto


@pytest.mark.asyncio
async def test_campo_onde_chega_na_decisao():
    """Session-a20f4722: a regra mestra ficava só na explicação, que não chega ao cérebro."""
    com_onde = _GOOD_JSON.replace('"tipo_tarefa": "simples"', '"tipo_tarefa": "simples", "onde": "Máquina"')
    router, _ = build_router([FakeResponse(com_onde, finish_reason="stop")])
    decision = await router.classify("quantos GB livres tem no meu disco?")
    assert decision.onde == "maquina"


@pytest.mark.asyncio
async def test_onde_desconhecido_vira_none():
    com_onde = _GOOD_JSON.replace('"tipo_tarefa": "simples"', '"tipo_tarefa": "simples", "onde": "nuvem"')
    router, _ = build_router([FakeResponse(com_onde, finish_reason="stop")])
    decision = await router.classify("quantos GB livres tem no meu disco?")
    assert decision.onde is None


def test_prompt_do_classificador_pede_o_campo_onde():
    from core.router.intent_router import ROUTER_SYSTEM_PROMPT

    assert '"onde": "sandbox"|"maquina"|"navegador"|"nenhum"' in ROUTER_SYSTEM_PROMPT
