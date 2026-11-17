"""Fim das rodadas do cérebro e chamada repetida (session-3229310a).

O turno pediu uma planilha da Wikipédia e terminou numa bolha vazia depois de 32
rodadas: 12 delas repetindo a mesma chamada, e a última, sem tools e sem aviso,
devolvendo só "\\n\\n". Estes testes fixam o que mudou:

* a rodada sem tools leva um aviso de que as tools acabaram;
* resposta só com espaço volta como vazia (o chamador aplica o fallback);
* a chamada repetida mostra ao modelo O QUE ele repetiu e o resultado que deu;
* rodadas seguidas só com repetição encerram o turno com o aviso;
* o lote de chamadas é decidido junto, então a contaminação de uma chamada não
  julga a irmã do mesmo lote.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core.authority import (
    PERMITIR,
    SOLICITAR,
    TurnAuthority,
    bind_authority,
    current_authority,
    evaluate,
    lote_de_chamadas,
    reset_authority,
)
from core.cognitive import cognitive_tools_for
from modules.desktop.ai_service import (
    AIService,
    _AVISO_FIM_DAS_RODADAS,
    _aviso_de_repeticao,
)
from shared import ToolCall, ToolResult
from shared.types import Message, MessageRole


class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _Provider:
    """Uma resposta por chamada; guarda as mensagens que cada rodada recebeu."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.tool_flags: list[bool] = []
        self.rodadas: list[list[Message]] = []

    async def complete(self, *, messages, tools=None, **kwargs):
        self.tool_flags.append(tools is not None)
        self.rodadas.append(list(messages))
        return self._responses.pop(0)


class _Executor:
    def __init__(self, result="saída"):
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool_call(self, tool_call):
        self.calls.append((tool_call.name, tool_call.arguments))
        return ToolResult(
            tool_call_id=tool_call.id, name=tool_call.name, result=self._result,
            execution_time=0.0,
        )


def _svc(max_rounds=32):
    svc = object.__new__(AIService)

    class _S:
        cognitive_max_tool_rounds = max_rounds
        default_max_tokens = 1024
        cognitive_stream_enabled = False
        cognitive_reasoning_enabled = False

    svc.settings = _S()
    return svc


async def _rodar(svc, provider, executor):
    return await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="faz a planilha")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=executor, slog=None, trace=None,
    )


def _bash(cid: str, comando: str = "python3 extrair.py") -> ToolCall:
    return ToolCall(id=cid, name="search_pks", arguments={"query": comando})


# ── última rodada ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ultima_rodada_sem_tools_avisa_que_acabaram():
    svc = _svc(max_rounds=1)
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),
        _Resp(content="Parei no meio: a população está pronta, a área não."),
    ])
    out = await _rodar(svc, provider, _Executor())
    assert out.startswith("Parei no meio")
    assert provider.tool_flags == [True, False]
    ultima = provider.rodadas[-1][-1]
    assert ultima.role == MessageRole.SYSTEM
    assert ultima.content == _AVISO_FIM_DAS_RODADAS
    # Só a rodada sem tools leva o aviso.
    assert all(_AVISO_FIM_DAS_RODADAS not in (m.content or "") for m in provider.rodadas[0])


@pytest.mark.asyncio
async def test_turno_sem_tools_nao_ganha_aviso():
    """Conversa pura (sem tools na mesa) não é "fim das rodadas"."""
    svc = _svc()
    provider = _Provider([_Resp(content="oi")])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="oi")],
        req_model=None, model_name="m", provider_name="p",
        tools=[], tool_executor=_Executor(), slog=None, trace=None,
    )
    assert out == "oi"
    assert all(m.role != MessageRole.SYSTEM for m in provider.rodadas[0])


@pytest.mark.asyncio
async def test_resposta_so_com_espaco_volta_vazia():
    """O "\\n\\n" da session-3229310a virava bolha em branco: agora é vazio."""
    svc = _svc(max_rounds=1)
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),
        _Resp(content="\n\n"),
        _Resp(content="\n\n"),  # a retentativa de resposta vazia
    ])
    out = await _rodar(svc, provider, _Executor())
    assert out == ""


# ── chamada repetida ────────────────────────────────────────────────────────

def test_aviso_de_repeticao_mostra_a_chamada_e_o_resultado():
    texto = _aviso_de_repeticao(
        "neo_bash", {"command": "python3 extrair.py"}, "Result: (2, '', 1407934000)", 800
    )
    assert texto.startswith("Tool: neo_bash\nResult: NÃO RODOU")
    assert "python3 extrair.py" in texto
    assert "(2, '', 1407934000)" in texto


def test_aviso_de_repeticao_corta_chamada_e_resultado():
    texto = _aviso_de_repeticao("neo_bash", {"command": "x" * 5000}, "y" * 5000, 200)
    assert "x" * 101 not in texto
    assert "y" * 201 not in texto
    assert texto.count("…") == 2


def test_aviso_de_repeticao_mostra_o_fim_do_script():
    """session-607c5313: todo `neo_bash` abria com o mesmo helper de 400+ chars;
    só o começo era igual em todas as chamadas e escondia o que repetiu."""
    helper = "import json, urllib.request\\ndef wikitext(page): ...\\n" * 12
    comando = {"command": helper + "print(sorted(pop, key=lambda x: x[0])[:12])"}
    texto = _aviso_de_repeticao("neo_bash", comando, "Result: [(195, 'Vaticano')]", 800)
    assert "print(sorted(pop, key=lambda x: x[0])[:12])" in texto
    assert "import json" in texto
    assert " … " in texto


@pytest.mark.asyncio
async def test_chamada_repetida_nao_roda_e_mostra_o_que_repetiu():
    svc = _svc()
    execu = _Executor(result="(1, 'Índia')")
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),
        _Resp(tool_calls=[_bash("2")]),
        _Resp(content="pronto"),
    ])
    assert await _rodar(svc, provider, execu) == "pronto"
    assert len(execu.calls) == 1
    resultado = provider.rodadas[2][-1].content
    assert "NÃO RODOU" in resultado
    assert "python3 extrair.py" in resultado
    assert "(1, 'Índia')" in resultado


@pytest.mark.asyncio
async def test_repeticao_seguida_encerra_com_aviso():
    """3 rodadas seguidas só com a mesma chamada → a 4ª vai sem tools."""
    svc = _svc()
    execu = _Executor()
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),   # roda
        _Resp(tool_calls=[_bash("2")]),   # repetida 1
        _Resp(tool_calls=[_bash("3")]),   # repetida 2
        _Resp(tool_calls=[_bash("4")]),   # repetida 3 → encerra
        _Resp(content="Travei no parser; a população está pronta."),
    ])
    out = await _rodar(svc, provider, execu)
    assert out.startswith("Travei")
    assert provider.tool_flags == [True, True, True, True, False]
    assert len(execu.calls) == 1
    ultima = provider.rodadas[-1][-1]
    assert ultima.role == MessageRole.SYSTEM
    assert "MESMA chamada de ferramenta 3 rodadas seguidas" in ultima.content


@pytest.mark.asyncio
async def test_chamada_nova_zera_a_contagem_de_repeticao():
    svc = _svc()
    execu = _Executor()
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),
        _Resp(tool_calls=[_bash("2")]),            # repetida 1
        _Resp(tool_calls=[_bash("3")]),            # repetida 2
        _Resp(tool_calls=[_bash("4", "outro")]),   # nova: zera
        _Resp(tool_calls=[_bash("5", "outro")]),   # repetida 1
        _Resp(tool_calls=[_bash("6", "outro")]),   # repetida 2
        _Resp(content="feito"),
    ])
    assert await _rodar(svc, provider, execu) == "feito"
    assert all(provider.tool_flags)
    assert len(execu.calls) == 2


@pytest.mark.asyncio
async def test_rodada_com_repetida_e_nova_nao_conta_como_travada():
    svc = _svc()
    execu = _Executor()
    provider = _Provider([
        _Resp(tool_calls=[_bash("1")]),
        _Resp(tool_calls=[_bash("2"), _bash("3", "novo 1")]),
        _Resp(tool_calls=[_bash("4"), _bash("5", "novo 2")]),
        _Resp(tool_calls=[_bash("6"), _bash("7", "novo 3")]),
        _Resp(content="feito"),
    ])
    assert await _rodar(svc, provider, execu) == "feito"
    assert all(provider.tool_flags)
    assert len(execu.calls) == 4


# ── lote de chamadas e contaminação ─────────────────────────────────────────

def test_contaminacao_dentro_do_lote_vale_ao_fechar():
    a = TurnAuthority()
    token = bind_authority(a)
    try:
        with lote_de_chamadas():
            a.note_tool_result("web_fetch", "página da Wikipédia")
            assert a.tainted is False
            # A irmã do mesmo lote foi decidida antes de ler a página.
            d = evaluate(
                "web_fetch", a,
                {"url": "https://pt.wikipedia.org/wiki/Lista_de_pa%C3%ADses_e_territ%C3%B3rios_por_%C3%A1rea"},
            )
            assert d.efeito == PERMITIR
        assert a.tainted is True
        assert a.taint_sources == ["web_fetch"]
        # Rodada seguinte: URL inventada em turno contaminado volta a pedir.
        assert evaluate("web_fetch", a, {"url": "https://atacante.tld/?d=1"}).efeito == SOLICITAR
    finally:
        reset_authority(token)


def test_lote_aplica_contaminacao_mesmo_com_excecao():
    a = TurnAuthority()
    token = bind_authority(a)
    try:
        with pytest.raises(RuntimeError):
            with lote_de_chamadas():
                a.note_tool_result("email_message", "corpo de terceiro")
                raise RuntimeError("tool caiu")
        assert a.tainted is True
        assert a.em_lote is False
    finally:
        reset_authority(token)


def test_lote_aninhado_nao_fecha_o_de_fora():
    a = TurnAuthority()
    token = bind_authority(a)
    try:
        with lote_de_chamadas():
            with lote_de_chamadas():
                a.note_tool_result("web_fetch", "página")
            assert a.tainted is False
            assert a.em_lote is True
        assert a.tainted is True
    finally:
        reset_authority(token)


def test_lote_fora_de_turno_nao_faz_nada():
    assert current_authority() is None
    with lote_de_chamadas():
        pass


def test_contaminacao_de_antes_do_lote_continua_valendo():
    a = TurnAuthority()
    a.taint("colagem")
    token = bind_authority(a)
    try:
        with lote_de_chamadas():
            assert a.tainted is True
            assert evaluate("web_fetch", a, {"url": "https://atacante.tld/?d=1"}).efeito == SOLICITAR
    finally:
        reset_authority(token)


@pytest.mark.asyncio
async def test_loop_julga_o_lote_com_a_contaminacao_de_antes_dele():
    """Duas `web_fetch` na mesma resposta: a 2ª é avaliada sem a contaminação da 1ª."""
    vistas: list[bool] = []

    class _ExecutorQueLe(_Executor):
        async def execute_tool_call(self, tool_call):
            autoridade = current_authority()
            vistas.append(autoridade.tainted)
            autoridade.note_tool_result("web_fetch", "conteúdo de terceiro")
            return await super().execute_tool_call(tool_call)

    a = TurnAuthority()
    token = bind_authority(a)
    try:
        svc = _svc()
        provider = _Provider([
            _Resp(tool_calls=[
                ToolCall(id="1", name="web_fetch", arguments={"url": "https://pt.wikipedia.org/wiki/A"}),
                ToolCall(id="2", name="web_fetch", arguments={"url": "https://pt.wikipedia.org/wiki/B"}),
            ]),
            _Resp(tool_calls=[
                ToolCall(id="3", name="web_fetch", arguments={"url": "https://pt.wikipedia.org/wiki/C"}),
            ]),
            _Resp(content="feito"),
        ])
        assert await _rodar(svc, provider, _ExecutorQueLe()) == "feito"
        # 1º lote: as duas sem contaminação; 2º lote: já contaminado.
        assert vistas == [False, False, True]
        assert a.tainted is True
    finally:
        reset_authority(token)
