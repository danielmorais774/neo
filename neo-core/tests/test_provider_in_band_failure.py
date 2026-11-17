"""Provedor que cai no meio não pode virar "deu erro do meu lado" depois de 2 min.

Sessões 5656b9f7 e 615a74b6 (2026-09-16): `deepseek/deepseek-v4-flash@baidu` com
o pin TRANCADO, Baidu fora do ar por ~5 min. O mesmo payload, reenviado depois,
passou em 4 s — não era o pedido. Três defeitos transformaram a queda num turno
morto de 125 s:

  1. Bufferizado: o OpenRouter devolve 200 e escreve o erro no CORPO, sem
     `choices`. O SDK aceita, e `response.choices[0]` estourava `TypeError:
     'NoneType' object is not subscriptable` — sem código, sem retry, sem pin solto.
  2. Stream: o erro vem num evento SSE ("JSON error injected into SSE stream"),
     sem nenhuma palavra que o detector de lotação reconhecesse.
  3. Stream: a soltura do pin NUNCA rodava, nem num 429 de livro — o chunk ERROR
     que o provider base emite antes de levantar marcava a saída como iniciada.
"""

import httpx
import pytest

_REQ = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
_ERRO_BAIDU = {"code": 502, "message": "JSON error injected into SSE stream"}


def _resposta_sem_choices():
    """Exatamente o que o SDK monta a partir do corpo 200 com erro in-band."""
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    return construct_type(
        type_=ChatCompletion,
        value={"id": "gen-x", "object": "chat.completion", "created": 0,
               "model": "deepseek/deepseek-v4-flash", "error": _ERRO_BAIDU},
    )


class _FakeClient:
    """`AsyncOpenAI` mínimo: conta as chamadas e devolve o roteiro."""

    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.chamadas: list[dict] = []
        outer = self

        class _Completions:
            async def create(self, **params):
                outer.chamadas.append(params)
                item = outer.roteiro.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()

    def with_options(self, **_):
        return self


def _openai_cru(client):
    from core.llm.openai_provider import OpenAIProvider

    prov = OpenAIProvider.__new__(OpenAIProvider)
    prov.client = client
    return prov


# ── 1. bufferizado: o erro real sobe, não um TypeError ──────────────────────
@pytest.mark.asyncio
async def test_resposta_200_sem_choices_sobe_o_erro_do_provedor():
    from core.llm.openai_provider import InBandProviderError
    from shared.exceptions import LLMProviderError

    prov = _openai_cru(_FakeClient([_resposta_sem_choices()]))

    with pytest.raises(LLMProviderError) as exc:
        await prov.complete(messages=[], model="deepseek/deepseek-v4-flash",
                            bail_on_rate_limit=True)

    causa = exc.value.__cause__
    assert isinstance(causa, InBandProviderError)
    assert "NoneType" not in str(exc.value)
    assert "JSON error injected into SSE stream" in str(exc.value)
    assert causa.status_code == 502


@pytest.mark.asyncio
async def test_pin_trancado_nao_repete_o_provedor_que_caiu(monkeypatch):
    """Repetir o Baidu fora do ar custa ~60 s por tentativa: sai na primeira."""
    import core.llm.openai_provider as oa
    from shared.exceptions import LLMProviderError

    dormiu: list[float] = []

    async def _sleep(s):
        dormiu.append(s)

    monkeypatch.setattr(oa.asyncio, "sleep", _sleep)
    client = _FakeClient([_resposta_sem_choices()] * 4)
    prov = _openai_cru(client)

    with pytest.raises(LLMProviderError):
        await prov.complete(messages=[], model="m", bail_on_rate_limit=True)

    assert len(client.chamadas) == 1
    assert dormiu == []


@pytest.mark.asyncio
async def test_sem_pin_a_queda_502_ainda_entra_na_escada(monkeypatch):
    """Sem rota pra consertar, o retry com backoff continua sendo a rede."""
    import core.llm.openai_provider as oa

    async def _sleep(_):
        return None

    monkeypatch.setattr(oa.asyncio, "sleep", _sleep)
    from openai._models import construct_type
    from openai.types.chat import ChatCompletion

    ok = construct_type(type_=ChatCompletion, value={
        "id": "gen-ok", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "oi"}}],
    })
    client = _FakeClient([_resposta_sem_choices(), ok])
    prov = _openai_cru(client)

    resp = await prov.complete(messages=[], model="m")

    assert resp.get_content() == "oi"
    assert len(client.chamadas) == 2


# ── 2. o detector de lotação reconhece a queda nos dois formatos ────────────
def _pin_trancado():
    from core.llm.openrouter_provider import OpenRouterProvider

    kwargs: dict = {}
    OpenRouterProvider._split_provider_pin("deepseek/deepseek-v4-flash@baidu", kwargs)
    return kwargs


def _envolvido(causa, texto):
    from shared.exceptions import LLMProviderError

    try:
        raise LLMProviderError(texto) from causa
    except LLMProviderError as e:
        return e


def test_queda_in_band_bufferizada_solta_o_pin():
    from core.llm.openai_provider import InBandProviderError
    from core.llm.openrouter_provider import OpenRouterProvider

    kwargs = _pin_trancado()
    erro = _envolvido(InBandProviderError(_ERRO_BAIDU), "Erro na completion OpenAI: ...")

    assert OpenRouterProvider._relax_provider_pin(kwargs, erro) is True
    assert kwargs["extra_body"]["provider"] == {"order": ["baidu"], "allow_fallbacks": True}


def test_queda_no_evento_sse_solta_o_pin():
    """É o `APIError` puro que o SDK levanta ao ler `error` num evento SSE."""
    import openai
    from core.llm.openrouter_provider import OpenRouterProvider

    kwargs = _pin_trancado()
    sse = openai.APIError(_ERRO_BAIDU["message"], _REQ, body=_ERRO_BAIDU)
    erro = _envolvido(sse, f"Erro no streaming OpenAI: {sse}")

    assert OpenRouterProvider._relax_provider_pin(kwargs, erro) is True


def test_recusa_http_de_conteudo_continua_sem_soltar_o_pin():
    """`APIStatusError` também é `APIError` — e não pode passar por queda."""
    import openai
    from core.llm.openrouter_provider import OpenRouterProvider

    kwargs = _pin_trancado()
    recusa = openai.APIStatusError(
        "No endpoints found. Filter by Tool Compatibility",
        response=httpx.Response(404, request=_REQ), body=None,
    )
    erro = _envolvido(recusa, f"Erro na completion OpenAI: {recusa}")

    assert OpenRouterProvider._relax_provider_pin(kwargs, erro) is False
    assert kwargs["extra_body"]["provider"]["allow_fallbacks"] is False


# ── 3. stream: a segunda tentativa roda de verdade ──────────────────────────
def _base_stream_que_cai_uma_vez(monkeypatch, vistos, cair_sempre=False):
    """Mesmo formato do `OpenAIProvider.stream_complete` real: no except ele
    EMITE um chunk ERROR e só depois levanta."""
    import openai
    from core.llm.openai_provider import OpenAIProvider
    from shared import StreamChunk, StreamEventType
    from shared.exceptions import LLMProviderError

    async def fake_stream(self, **kwargs):
        vistos.append(dict((kwargs.get("extra_body") or {}).get("provider") or {}))
        if cair_sempre or len(vistos) == 1:
            e = openai.APIError(_ERRO_BAIDU["message"], _REQ, body=_ERRO_BAIDU)
            yield StreamChunk(event=StreamEventType.ERROR, content=str(e))
            raise LLMProviderError(f"Erro no streaming OpenAI: {e}") from e
        yield StreamChunk(event=StreamEventType.TOKEN, content="oi")

    monkeypatch.setattr(OpenAIProvider, "stream_complete", fake_stream)


@pytest.mark.asyncio
async def test_stream_solta_o_pin_e_refaz_quando_o_provedor_cai(monkeypatch):
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared import StreamEventType

    vistos: list[dict] = []
    _base_stream_que_cai_uma_vez(monkeypatch, vistos)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)

    chunks = [c async for c in sob.stream_complete(
        messages=[], model="deepseek/deepseek-v4-flash@baidu")]

    assert [c.content for c in chunks] == ["oi"], "o turno sobrevive e sem ERROR vazado"
    assert all(c.event != StreamEventType.ERROR for c in chunks)
    assert vistos == [
        {"order": ["baidu"], "allow_fallbacks": False},
        {"order": ["baidu"], "allow_fallbacks": True},
    ]


@pytest.mark.asyncio
async def test_stream_que_desiste_emite_um_error_so(monkeypatch):
    """Antes saíam dois (o repassado + o do except) — e nenhuma retentativa."""
    from core.llm.openrouter_provider import OpenRouterProvider
    from shared import StreamEventType
    from shared.exceptions import LLMProviderError

    vistos: list[dict] = []
    _base_stream_que_cai_uma_vez(monkeypatch, vistos, cair_sempre=True)
    sob = OpenRouterProvider.__new__(OpenRouterProvider)

    chunks = []
    with pytest.raises(LLMProviderError):
        async for c in sob.stream_complete(messages=[], model="deepseek/deepseek-v4-flash@baidu"):
            chunks.append(c)

    assert len(vistos) == 2, "tentou com o pin solto antes de desistir"
    assert [c.event for c in chunks] == [StreamEventType.ERROR]
