"""tool_choice forçado não pode mais derrubar um turno.

Contexto: os modelos DeepSeek v4 rodam em thinking mode e a API recusa
`tool_choice` forçado nesse estado com 400 "Thinking mode does not support this
tool_choice". Como TODAS as guardas do núcleo cognitivo (anti-mentira,
anti-promessa, anti-alegação-de-download) e as skills tool_backed autocarregadas
forçam a tool via `force_next`/`force_tool_first`, qualquer disparo delas matava
o turno inteiro — e o texto do 400 ainda virava a "resposta" salva no banco
(sessões 8d1c8155 e 1aec71b8).

Três defesas, testadas aqui:
  1. DeepSeekProvider desliga o thinking SÓ na chamada que força a tool
     (sondado contra a API real: é o único jeito de manter a GARANTIA).
  2. OpenAIProvider degrada pra "auto" se um provider qualquer recusar o
     tool_choice — perder a garantia é ruim, perder o turno é pior.
  3. Falha de provider fecha o turno com frase curta, não com o stack trace.
"""

import pytest

from core.llm.openai_provider import forces_tool_call


# ── 1. semântica de "isto exige tool" ───────────────────────────────────────
def test_forces_tool_call_reconhece_as_duas_formas_de_obrigar():
    assert forces_tool_call("required") is True
    # dict nomeando a função obriga do mesmo jeito e bate na mesma restrição
    assert forces_tool_call({"type": "function", "function": {"name": "file_save"}}) is True
    # estes deixam o modelo escolher
    assert forces_tool_call("auto") is False
    assert forces_tool_call("none") is False
    assert forces_tool_call(None) is False


# ── 2. DeepSeek: thinking desligado só quando força ─────────────────────────
class _CapturingDeepSeek:
    """DeepSeekProvider real, mas com o super().complete() trocado por captura."""

    def __init__(self):
        from core.llm.deepseek_provider import DeepSeekProvider
        self.provider = DeepSeekProvider.__new__(DeepSeekProvider)
        self.provider.model = DeepSeekProvider.DEFAULT_MODEL
        self.seen: dict = {}

    async def complete(self, **kwargs):
        from core.llm.openai_provider import OpenAIProvider

        async def _capture(**inner):
            self.seen = inner
            return object()

        original = OpenAIProvider.complete
        OpenAIProvider.complete = lambda _self, **kw: _capture(**kw)  # type: ignore[assignment]
        try:
            await type(self.provider).complete(self.provider, **kwargs)
        finally:
            OpenAIProvider.complete = original  # type: ignore[assignment]
        return self.seen


async def _dorme_nada(_segundos):
    """Substitui ``asyncio.sleep`` nos testes de backoff.

    O que está sob teste é QUANTAS vezes a escada repete e sob quais condições —
    não os 2+5+10s reais, que só fariam a suíte durar 17s por caso.
    """
    return None


def _tool():
    from shared import ToolSchema
    from shared.types import ToolParameter

    return ToolSchema(
        name="file_save", description="publica arquivo no catálogo",
        parameters={"path": ToolParameter(type="string", description="p", required=True)},
        module="files",
    )


@pytest.mark.asyncio
async def test_deepseek_desliga_thinking_ao_forcar_tool():
    seen = await _CapturingDeepSeek().complete(
        messages=[], tools=[_tool()], tool_choice="required",
    )
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_deepseek_nao_mexe_no_thinking_no_caminho_normal():
    """`auto` é o caminho de 99% dos turnos — o raciocínio tem que continuar lá."""
    seen = await _CapturingDeepSeek().complete(
        messages=[], tools=[_tool()], tool_choice="auto",
    )
    assert "extra_body" not in seen

    seen = await _CapturingDeepSeek().complete(messages=[], tools=[_tool()])
    assert "extra_body" not in seen

    # sem tools o tool_choice nem é válido na API — não inventar extra_body
    seen = await _CapturingDeepSeek().complete(messages=[], tool_choice="required")
    assert "extra_body" not in seen


@pytest.mark.asyncio
async def test_deepseek_preserva_extra_body_do_chamador():
    seen = await _CapturingDeepSeek().complete(
        messages=[], tools=[_tool()], tool_choice="required",
        extra_body={"algo_do_chamador": 1},
    )
    assert seen["extra_body"]["algo_do_chamador"] == 1
    assert seen["extra_body"]["thinking"] == {"type": "disabled"}


# ── 3. rede genérica: 400 por tool_choice degrada pra auto ──────────────────
class _Rejects400OnForcedChoice:
    """Fake do client: 400 enquanto o tool_choice obrigar, sucesso com "auto"."""

    def __init__(self):
        self.calls: list = []

        class _Completions:
            @staticmethod
            async def create(**params):
                self.calls.append(params.get("tool_choice"))
                if forces_tool_call(params.get("tool_choice")):
                    raise RuntimeError(
                        "Error code: 400 - {'error': {'message': 'Thinking mode does not "
                        "support this tool_choice', 'type': 'invalid_request_error'}}"
                    )
                class _Msg:
                    content = "ok"
                    tool_calls = None
                class _Choice:
                    message = _Msg()
                    finish_reason = "stop"
                class _Resp:
                    choices = [_Choice()]
                return _Resp()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()

    def with_options(self, **_kwargs):
        """O ``complete()`` pede o client sem o retry interno do SDK.

        O dublê precisa oferecer a mesma interface do ``AsyncOpenAI`` real; como
        ele não repete nada por conta própria, devolver a si mesmo já reproduz o
        efeito de ``max_retries=0``.
        """
        return self


@pytest.mark.asyncio
async def test_openai_degrada_para_auto_quando_tool_choice_e_recusado():
    from core.llm.openai_provider import (
        _FORCED_TOOL_CHOICE_UNSUPPORTED,
        OpenAIProvider,
    )

    _FORCED_TOOL_CHOICE_UNSUPPORTED.clear()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Rejects400OnForcedChoice()
    provider.model = "modelo-que-pensa"

    resp = await provider.complete(
        messages=[], tools=[_tool()], model="modelo-que-pensa", tool_choice="required",
    )
    assert resp.get_content() == "ok", "o turno tem que sobreviver"
    assert provider.client.calls == ["required", "auto"], "degrada uma vez, não fica em loop"


@pytest.mark.asyncio
async def test_recusa_de_tool_forcada_e_memorizada_entre_chamadas():
    """A 2ª chamada já sai em "auto" — a recusa é de infra, não é transitória.

    Sem isto, todo turno com skill autocarregada paga uma ida e volta só para
    ouvir o mesmo 404/400 (medido na session-d94dc42f: ~2s por turno).
    """
    from core.llm.openai_provider import (
        _FORCED_TOOL_CHOICE_UNSUPPORTED,
        OpenAIProvider,
    )

    _FORCED_TOOL_CHOICE_UNSUPPORTED.clear()
    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Rejects400OnForcedChoice()
    provider.model = "modelo-que-pensa"

    for _ in range(2):
        await provider.complete(
            messages=[], tools=[_tool()], model="modelo-que-pensa", tool_choice="required",
        )

    assert provider.client.calls == ["required", "auto", "auto"], (
        "a segunda chamada não pode repetir a tentativa que já se sabe condenada"
    )
    _FORCED_TOOL_CHOICE_UNSUPPORTED.clear()


@pytest.mark.asyncio
async def test_openai_nao_degrada_erro_que_nao_e_de_tool_choice():
    """400 de outra causa continua estourando — degradar mascararia o bug real."""
    from core.llm.openai_provider import OpenAIProvider
    from shared import LLMProviderError

    class _AlwaysFails:
        def __init__(self):
            self.calls = 0
            outer = self

            class _Completions:
                @staticmethod
                async def create(**params):
                    outer.calls += 1
                    raise RuntimeError("Error code: 400 - {'message': 'context length exceeded'}")

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

        def with_options(self, **_kwargs):
            return self

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _AlwaysFails()
    provider.model = "m"

    with pytest.raises(LLMProviderError):
        await provider.complete(messages=[], tools=[_tool()], model="m", tool_choice="required")
    assert provider.client.calls == 1


# ── 4. falha por crédito/limite não vira "tenta de novo" ────────────────────
def test_falha_de_credito_e_distinguida_de_bug_nosso():
    """Sessão 1e7c5943: a OpenRouter respondeu 403 "Key limit exceeded (total
    limit)" numa chave com teto de US$ 3 já estourado e SEM reset. O turno fechava
    com "deu erro do meu lado. Tenta de novo?" — conselho errado: toda tentativa
    ia bater na mesma 403, e a causa real só existia no log .md da sessão."""
    from modules.desktop.ai_service import _is_no_credit_failure

    openrouter = (
        "Erro na completion OpenRouter: Error code: 403 - {'error': {'message': "
        "'Key limit exceeded (total limit).', 'code': 403}}"
    )
    assert _is_no_credit_failure(openrouter) is True
    assert _is_no_credit_failure("Error code: 429 - exceeded your current quota") is True
    assert _is_no_credit_failure("your credit balance is too low") is True


def test_bug_nosso_nao_e_confundido_com_falta_de_credito():
    """A frase de crédito manda o usuário mexer na chave — dizer isso quando o
    defeito é nosso manda ele caçar o problema no lugar errado. O 400 de tool
    duplicada (a outra falha da mesma sessão) é o caso a não confundir."""
    from modules.desktop.ai_service import _is_no_credit_failure

    assert _is_no_credit_failure(
        "Erro na completion DeepSeek: Error code: 400 - {'error': {'message': "
        "'Tool names must be unique.', 'code': 'invalid_request_error'}}"
    ) is False
    assert _is_no_credit_failure("Connection timeout") is False
    assert _is_no_credit_failure("Thinking mode does not support this tool_choice") is False
    # "402" nu aparece dentro de id de request e de contagem de token — não pode
    # sozinho classificar a falha como falta de crédito.
    assert _is_no_credit_failure("Error code: 400 (request_id req_8402abf)") is False
    assert _is_no_credit_failure("max_tokens 402 excede o limite do modelo") is False


def test_frase_de_credito_nao_alimenta_o_afeto():
    """As duas frases de fecho precisam casar o mesmo prefixo reconhecido pelo
    affect_updater — senão o fecho de erro volta a ser lido como fala do Neo e
    vira insumo de emoção."""
    from core.agent.affect_updater import _ERROR_RESPONSE_PREFIXES
    from modules.desktop.ai_service import TURN_FAILED_NO_CREDIT, TURN_FAILED_REPLY

    for frase in (TURN_FAILED_REPLY, TURN_FAILED_NO_CREDIT):
        assert frase.startswith(_ERROR_RESPONSE_PREFIXES), frase


# ── pin de provedor do OpenRouter: lotado não pode matar a tarefa ────────


@pytest.mark.asyncio
async def test_pin_lotado_afrouxa_e_a_chamada_sobrevive():
    """429 no provedor fixado não pode derrubar o turno.

    Um laço de passos (navegador) é a carga que estoura RPM: medido em
    2026-09-03, o pool compartilhado do Baidu recusou e, com o fallback
    trancado, a tarefa morreu no passo 1. A preferência continua valendo (o
    `order` fica), só a tranca sai.
    """
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    kwargs: dict = {}
    modelo = OpenRouterProvider._split_provider_pin(
        "deepseek/deepseek-v4-flash@baidu", kwargs
    )
    assert modelo == "deepseek/deepseek-v4-flash"
    assert kwargs["extra_body"]["provider"] == {"order": ["baidu"], "allow_fallbacks": False}

    lotado = LLMProviderError(
        "Error code: 429 - deepseek/deepseek-v4-flash is temporarily rate-limited "
        "upstream (rpm_rate_limit_exceeded)"
    )
    assert OpenRouterProvider._relax_provider_pin(kwargs, lotado) is True
    # A preferência SOBREVIVE: o gateway ainda tenta o Baidu primeiro.
    assert kwargs["extra_body"]["provider"] == {"order": ["baidu"], "allow_fallbacks": True}
    # E só afrouxa uma vez: sem tranca, não há o que afrouxar de novo.
    assert OpenRouterProvider._relax_provider_pin(kwargs, lotado) is False


@pytest.mark.asyncio
async def test_pin_nao_afrouxa_por_erro_que_nao_e_lotacao():
    """Afrouxar em erro de CONTEÚDO esconderia a causa.

    O 404 de compatibilidade de tool tem tratamento próprio
    (`_rejects_forced_tool_choice`); trocar de provedor ali mascararia o motivo
    real e ainda cobraria outro preço por isso.
    """
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    kwargs: dict = {}
    OpenRouterProvider._split_provider_pin("deepseek/deepseek-v4-flash@baidu", kwargs)
    conteudo = LLMProviderError(
        "Error code: 404 - No endpoints found. Filter by Tool Compatibility"
    )
    assert OpenRouterProvider._relax_provider_pin(kwargs, conteudo) is False
    assert kwargs["extra_body"]["provider"]["allow_fallbacks"] is False


@pytest.mark.asyncio
async def test_sem_pin_nao_ha_o_que_afrouxar():
    """Modelo sem `@slug` nunca entra nesse caminho — nem numa 2ª tentativa."""
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    kwargs: dict = {}
    assert (
        OpenRouterProvider._split_provider_pin("google/gemini-3.1-flash-lite", kwargs)
        == "google/gemini-3.1-flash-lite"
    )
    assert not kwargs
    assert OpenRouterProvider._relax_provider_pin(kwargs, LLMProviderError("429")) is False


@pytest.mark.asyncio
async def test_complete_refaz_a_chamada_quando_o_pin_esta_lotado(monkeypatch):
    """O caminho REAL, não só o helper: 429 → afrouxa → segunda chamada passa.

    Prova as três coisas que importam: a chamada sobrevive, a segunda vai com o
    fallback liberado, e o `order` continua lá (o provedor preferido ainda é
    tentado primeiro).
    """
    from core.llm.openai_provider import OpenAIProvider
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    vistos: list[dict] = []

    async def fake_complete(self, **kwargs):
        vistos.append(dict(kwargs.get("extra_body") or {}))
        if len(vistos) == 1:
            raise LLMProviderError(
                "Erro na completion OpenAI: Error code: 429 - deepseek/deepseek-v4-flash "
                "is temporarily rate-limited upstream (rpm_rate_limit_exceeded)"
            )
        return "resposta"

    monkeypatch.setattr(OpenAIProvider, "complete", fake_complete)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)  # sem rede

    out = await sob.complete(messages=[], model="deepseek/deepseek-v4-flash@baidu")

    assert out == "resposta", "o turno tem que sobreviver ao provedor lotado"
    assert len(vistos) == 2, "tentou de novo exatamente uma vez"
    assert vistos[0]["provider"] == {"order": ["baidu"], "allow_fallbacks": False}
    assert vistos[1]["provider"] == {"order": ["baidu"], "allow_fallbacks": True}


@pytest.mark.asyncio
async def test_complete_nao_refaz_duas_vezes(monkeypatch):
    """Lotado de novo depois de afrouxar é falha de verdade — sobe o erro.

    Sem esta trava, um gateway em pane viraria laço de tentativas pagas.
    """
    from core.llm.openai_provider import OpenAIProvider
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    chamadas = {"n": 0}

    async def sempre_lotado(self, **kwargs):
        chamadas["n"] += 1
        raise LLMProviderError("Error code: 429 - rate-limited upstream")

    monkeypatch.setattr(OpenAIProvider, "complete", sempre_lotado)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)

    with pytest.raises(LLMProviderError):
        await sob.complete(messages=[], model="deepseek/deepseek-v4-flash@baidu")
    assert chamadas["n"] == 2


@pytest.mark.asyncio
async def test_pin_trancado_pede_para_nao_gastar_o_backoff(monkeypatch):
    """Com pin trancado o 429 tem que voltar NA HORA; solto, o backoff volta a valer.

    É a inversão que custava os ~30s da session-d94dc42f: o conserto barato
    (trocar de rota) estava esperando o caro (dormir 2+5+10s) fracassar primeiro.
    """
    from core.llm.openai_provider import OpenAIProvider
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared.exceptions import LLMProviderError

    flags: list = []

    async def fake_complete(self, **kwargs):
        flags.append(kwargs.get("bail_on_rate_limit"))
        if len(flags) == 1:
            raise LLMProviderError("Error code: 429 - rate-limited upstream")
        return "resposta"

    monkeypatch.setattr(OpenAIProvider, "complete", fake_complete)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)

    assert await sob.complete(messages=[], model="deepseek/deepseek-v4-flash@baidu") == "resposta"
    assert flags == [True, False], (
        "1ª chamada (pin trancado) sai na hora; 2ª (fallback liberado) pode esperar"
    )


@pytest.mark.asyncio
async def test_sem_pin_o_backoff_continua_sendo_a_unica_saida(monkeypatch):
    """Sem pin não há rota a consertar — a flag não vai, a escada é a rede."""
    from core.llm.openai_provider import OpenAIProvider
    from core.llm.openrouter_provider import OpenRouterProvider

    flags: list = []

    async def fake_complete(self, **kwargs):
        flags.append(kwargs.get("bail_on_rate_limit"))
        return "resposta"

    monkeypatch.setattr(OpenAIProvider, "complete", fake_complete)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)

    await sob.complete(messages=[], model="deepseek/deepseek-v4-flash")
    assert flags == [False]


@pytest.mark.asyncio
async def test_bail_on_rate_limit_nao_vaza_para_a_api(monkeypatch):
    """A flag é intenção nossa: se chegar no corpo da request, a API responde 400."""
    from core.llm.openai_provider import OpenAIProvider

    seen: dict = {}

    class _Client:
        def __init__(self):
            class _Completions:
                @staticmethod
                async def create(**params):
                    seen.update(params)
                    class _Msg:
                        content = "ok"
                        tool_calls = None
                    class _Choice:
                        message = _Msg()
                        finish_reason = "stop"
                    class _Resp:
                        choices = [_Choice()]
                    return _Resp()

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

        def with_options(self, **_kwargs):
            return self

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Client()
    provider.model = "m"

    await provider.complete(messages=[], model="m", bail_on_rate_limit=True)
    assert "bail_on_rate_limit" not in seen


# ── 6. desligar o retry do SDK não pode custar a rede contra 5xx ────────────
@pytest.mark.asyncio
async def test_falha_transitoria_e_repetida_pela_escada_nossa(monkeypatch):
    """5xx/conexão caída continuam sendo repetidos depois do `max_retries=0`.

    Antes, quem repetia 5xx era a escada INTERNA do SDK. Ao desligá-la (para não
    triplicar cada tentativa nossa e alimentar o rate limiter), essa metade da
    resiliência tinha que vir para cá — senão a economia de latência viraria
    fragilidade: um 502 do gateway derrubaria o turno inteiro de primeira.
    """
    import asyncio as _asyncio

    from core.llm.openai_provider import OpenAIProvider

    monkeypatch.setattr(_asyncio, "sleep", _dorme_nada)
    tentativas = {"n": 0}

    class _Flaky:
        def __init__(self):
            class _Completions:
                @staticmethod
                async def create(**_params):
                    tentativas["n"] += 1
                    if tentativas["n"] < 3:
                        erro = RuntimeError("Error code: 503 - upstream unavailable")
                        erro.status_code = 503
                        raise erro
                    class _Msg:
                        content = "ok"
                        tool_calls = None
                    class _Choice:
                        message = _Msg()
                        finish_reason = "stop"
                    class _Resp:
                        choices = [_Choice()]
                    return _Resp()

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

        def with_options(self, **_kwargs):
            return self

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Flaky()
    provider.model = "m"

    resp = await provider.complete(messages=[], model="m")
    assert resp.get_content() == "ok"
    assert tentativas["n"] == 3, "repetiu o 503 em vez de derrubar o turno"


@pytest.mark.asyncio
async def test_erro_de_conteudo_nao_e_repetido(monkeypatch):
    """400 de conteúdo sobe na primeira: repetir só queima tempo e dinheiro."""
    import asyncio as _asyncio

    from core.llm.openai_provider import OpenAIProvider
    from shared import LLMProviderError

    monkeypatch.setattr(_asyncio, "sleep", _dorme_nada)
    tentativas = {"n": 0}

    class _Recusa:
        def __init__(self):
            class _Completions:
                @staticmethod
                async def create(**_params):
                    tentativas["n"] += 1
                    raise RuntimeError("Error code: 400 - context length exceeded")

            class _Chat:
                completions = _Completions()

            self.chat = _Chat()

        def with_options(self, **_kwargs):
            return self

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Recusa()
    provider.model = "m"

    with pytest.raises(LLMProviderError):
        await provider.complete(messages=[], model="m")
    assert tentativas["n"] == 1
