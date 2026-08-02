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


@pytest.mark.asyncio
async def test_openai_degrada_para_auto_quando_tool_choice_e_recusado():
    from core.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _Rejects400OnForcedChoice()
    provider.model = "modelo-que-pensa"

    resp = await provider.complete(
        messages=[], tools=[_tool()], model="modelo-que-pensa", tool_choice="required",
    )
    assert resp.get_content() == "ok", "o turno tem que sobreviver"
    assert provider.client.calls == ["required", "auto"], "degrada uma vez, não fica em loop"


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

    provider = OpenAIProvider.__new__(OpenAIProvider)
    provider.client = _AlwaysFails()
    provider.model = "m"

    with pytest.raises(LLMProviderError):
        await provider.complete(messages=[], tools=[_tool()], model="m", tool_choice="required")
    assert provider.client.calls == 1
