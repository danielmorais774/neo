"""O round cognitivo streamado — e as duas quedas que o mantêm seguro.

Streamar existe por um motivo medido: o loop cognitivo é `await`-ado inteiro
antes do primeiro `yield`, então um turno de 6 rounds chegava ao desktop como
mais de dois minutos de silêncio (session-a82d344f, 2026-09-11). Com o stream,
o texto vai subindo na bolha de progresso enquanto é gerado.

Mas o caminho streamado NÃO tem as duas defesas do `complete()` — a escada de
429 e o rebaixamento de `tool_choice=required` no provedor que o recusa. Os
testes daqui são, na maior parte, sobre quando NÃO streamar.
"""

import asyncio

import pytest

from modules.desktop.ai_service import AIService, _progress_label
from shared import StreamChunk, StreamEventType


class _S:
    cognitive_max_tool_rounds = 3
    default_max_tokens = 1024
    cognitive_stream_enabled = True
    cognitive_reasoning_enabled = False


def _svc(**overrides):
    svc = object.__new__(AIService)
    svc.settings = _S()
    for k, v in overrides.items():
        setattr(svc.settings, k, v)
    return svc


def _tok(text):
    return StreamChunk(event=StreamEventType.TOKEN, content=text)


class _StreamProvider:
    """Provider que streama uma lista de chunks (e registra o que recebeu)."""

    def __init__(self, chunks, *, falha_em=None):
        self._chunks = chunks
        self._falha_em = falha_em  # índice do chunk em que levanta
        self.stream_kwargs = []
        self.complete_kwargs = []

    async def stream_complete(self, **kwargs):
        self.stream_kwargs.append(kwargs)
        for i, ch in enumerate(self._chunks):
            if self._falha_em is not None and i == self._falha_em:
                raise RuntimeError("provedor lotado")
            yield ch

    async def complete(self, **kwargs):
        self.complete_kwargs.append(kwargs)

        class _R:
            @staticmethod
            def get_content():
                return "resposta bufferizada"

            @staticmethod
            def get_tool_calls():
                return []

        return _R()


@pytest.mark.asyncio
async def test_round_streamado_monta_texto_e_avisa_o_progresso():
    provider = _StreamProvider([
        _tok("Olha "), _tok("só" * 40), _tok(" o resultado."),
        StreamChunk(event=StreamEventType.DONE, finish_reason="stop"),
    ])
    fila: asyncio.Queue[str] = asyncio.Queue()

    resp, ms = await _svc()._cognitive_round(
        provider=provider, messages=[], tools=None, req_model="m",
        max_tokens=100, extra={}, progress=fila,
    )

    assert resp.get_content() == "Olha " + "só" * 40 + " o resultado."
    assert resp.get_tool_calls() == []
    assert ms >= 0
    # A bolha recebeu pelo menos uma atualização parcial (throttle por tamanho).
    assert not fila.empty()


@pytest.mark.asyncio
async def test_round_streamado_remonta_tool_call_e_anuncia_antes_dos_argumentos():
    provider = _StreamProvider([
        StreamChunk(event=StreamEventType.TOOL_CALL_START, tool_call_id="c1", tool_name="neo_bash"),
        StreamChunk(event=StreamEventType.TOOL_CALL_ARGS, tool_call_id="c1", tool_arguments_delta='{"comm'),
        StreamChunk(event=StreamEventType.TOOL_CALL_ARGS, tool_call_id="c1", tool_arguments_delta='and": "ls"}'),
        StreamChunk(event=StreamEventType.DONE, finish_reason="tool_calls"),
    ])
    fila: asyncio.Queue[str] = asyncio.Queue()

    resp, _ = await _svc()._cognitive_round(
        provider=provider, messages=[], tools=[object()], req_model="m",
        max_tokens=100, extra={}, progress=fila,
    )

    calls = resp.get_tool_calls()
    assert [(c.name, c.arguments) for c in calls] == [("neo_bash", {"command": "ls"})]
    # O anúncio sai no START, antes de o modelo terminar de escrever o script —
    # que é justamente o pedaço longo que deixava a tela parada.
    assert fila.get_nowait() == "rodando no sandbox"


@pytest.mark.asyncio
async def test_tool_call_truncada_continua_levantando_no_parse():
    """O loop trata JSONDecodeError com um round pedindo escrita em pedaços.

    Se o caminho streamado engolisse o argumento cortado, essa guarda morria em
    silêncio e o turno perdia os arquivos já escritos.
    """
    provider = _StreamProvider([
        StreamChunk(event=StreamEventType.TOOL_CALL_START, tool_call_id="c1", tool_name="neo_write_file"),
        StreamChunk(event=StreamEventType.TOOL_CALL_ARGS, tool_call_id="c1", tool_arguments_delta='{"path": "/a", "cont'),
        StreamChunk(event=StreamEventType.DONE, finish_reason="length"),
    ])
    resp, _ = await _svc()._cognitive_round(
        provider=provider, messages=[], tools=[object()], req_model="m",
        max_tokens=100, extra={}, progress=None,
    )
    with pytest.raises(ValueError):  # JSONDecodeError é subclasse
        resp.get_tool_calls()


@pytest.mark.asyncio
async def test_round_que_forca_tool_nao_streama():
    """`tool_choice=required` só é rebaixado para `auto` dentro do `complete()`.

    O Baidu recusa a tool forçada com 404; quem conserta é a escada do
    `complete()`. Streamar esse round jogaria o conserto fora.
    """
    provider = _StreamProvider([_tok("nao devia streamar")])
    resp, _ = await _svc()._cognitive_round(
        provider=provider, messages=[], tools=[object()], req_model="m",
        max_tokens=100, extra={"tool_choice": "required"}, progress=None,
    )
    assert provider.stream_kwargs == []
    assert resp.get_content() == "resposta bufferizada"
    assert provider.complete_kwargs[0]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_stream_que_morre_antes_do_primeiro_chunk_cai_no_bufferizado():
    """429 costuma recusar ANTES do primeiro chunk — e aí a escada é a rede."""
    provider = _StreamProvider([_tok("x")], falha_em=0)
    resp, _ = await _svc()._cognitive_round(
        provider=provider, messages=[], tools=None, req_model="m",
        max_tokens=100, extra={}, progress=None,
    )
    assert resp.get_content() == "resposta bufferizada"
    assert len(provider.complete_kwargs) == 1


@pytest.mark.asyncio
async def test_stream_que_ja_emitiu_texto_propaga_em_vez_de_repetir():
    """Refazer depois de ter mandado texto duplicaria a resposta na tela."""
    provider = _StreamProvider([_tok("comecei a responder"), _tok("...")], falha_em=1)
    with pytest.raises(RuntimeError):
        await _svc()._cognitive_round(
            provider=provider, messages=[], tools=None, req_model="m",
            max_tokens=100, extra={}, progress=None,
        )
    assert provider.complete_kwargs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ligado,esperado", [(False, True), (True, False)])
async def test_flag_de_raciocinio_vira_disable_thinking(ligado, esperado):
    """A intenção sobe em TODA chamada — é o provider que sabe traduzi-la."""
    provider = _StreamProvider([StreamChunk(event=StreamEventType.DONE, finish_reason="stop")])
    await _svc(cognitive_reasoning_enabled=ligado)._cognitive_round(
        provider=provider, messages=[], tools=None, req_model="m",
        max_tokens=100, extra={}, progress=None,
    )
    assert provider.stream_kwargs[0]["disable_thinking"] is esperado


@pytest.mark.asyncio
async def test_stream_desligado_por_config_vai_direto_ao_bufferizado():
    provider = _StreamProvider([_tok("nao usado")])
    await _svc(cognitive_stream_enabled=False)._cognitive_round(
        provider=provider, messages=[], tools=None, req_model="m",
        max_tokens=100, extra={}, progress=None,
    )
    assert provider.stream_kwargs == []
    assert len(provider.complete_kwargs) == 1


@pytest.mark.asyncio
async def test_progresso_nunca_derruba_o_turno():
    """Fila morta (consumidor foi embora) não pode virar exceção no loop."""

    class _FilaMorta:
        def put_nowait(self, _):
            raise RuntimeError("consumidor sumiu")

    await AIService._emit_progress(_FilaMorta(), "qualquer coisa")  # não levanta


def test_rotulo_de_progresso_usa_verbo_e_alvo_curto():
    assert _progress_label("file_read", {"name": "placa.json"}) == "lendo o arquivo: placa.json"
    assert _progress_label("neo_bash", None) == "rodando no sandbox"
    # Tool desconhecida ainda diz algo verdadeiro em vez de nada.
    assert _progress_label("tool_nova", None) == "chamando tool_nova"
    # O script inteiro do neo_bash não cabe numa linha de bolha.
    longo = _progress_label("neo_bash", {"query": "x" * 200})
    assert len(longo) < 100 and longo.endswith("…")


# ── o loop inteiro: a bolha cobre também a ESPERA da tool ────────────────────


@pytest.mark.asyncio
async def test_loop_avisa_o_progresso_a_cada_tool_executada():
    """Entre pedir a tool e ter o resultado pode haver dezenas de segundos.

    O stream cobre a GERAÇÃO; esta linha cobre a EXECUÇÃO — um `neo_bash` que
    processa uma imagem é exatamente o trecho em que nada sai pelo stream.
    """
    from shared import ToolCall, ToolResult

    class _Resp:
        def __init__(self, content="", tool_calls=None):
            self._content, self._tc = content, tool_calls or []

        def get_content(self):
            return self._content

        def get_tool_calls(self):
            return self._tc

    class _P:
        def __init__(self, respostas):
            self._r = list(respostas)

        async def complete(self, **kwargs):
            return self._r.pop(0)

    class _E:
        async def execute_tool_call(self, tc):
            return ToolResult(tool_call_id=tc.id, name=tc.name, result="ok", execution_time=0.0)

    fila: asyncio.Queue[str] = asyncio.Queue()
    svc = _svc(cognitive_stream_enabled=False)
    texto = await svc._run_cognitive_tool_loop(
        provider=_P([
            _Resp(tool_calls=[ToolCall(id="1", name="file_read", arguments={"name": "placa.json"})]),
            _Resp(content="pronto"),
        ]),
        messages=[],
        req_model="m",
        model_name="m",
        provider_name="fake",
        tools=[object()],
        tool_executor=_E(),
        slog=None,
        trace=None,
        progress=fila,
    )

    assert texto == "pronto"
    assert fila.get_nowait() == "lendo o arquivo: placa.json"


@pytest.mark.asyncio
async def test_progresso_descarta_texto_em_branco():
    """A bolha não pode piscar vazia só porque a resposta abre com \\n."""
    fila: asyncio.Queue[str] = asyncio.Queue()
    await AIService._emit_progress(fila, "\n\n")
    await AIService._emit_progress(fila, "   ")
    assert fila.empty()
    await AIService._emit_progress(fila, "agora sim")
    assert fila.get_nowait() == "agora sim"
    # E a borda é aparada: o \n\n de abertura virava duas linhas vazias
    # empurrando o texto para fora da bolha.
    await AIService._emit_progress(fila, "\n\nFeito.\n")
    assert fila.get_nowait() == "Feito."


# ── o drenador: bolha sai, resposta fica, task não vira órfã ─────────────────


def _svc_com_loop(roteiro):
    """Serviço cujo loop cognitivo é `roteiro` (recebe a fila de progresso)."""
    svc = _svc()

    async def _loop(*, progress=None, **_kw):
        return await roteiro(progress)

    svc._run_cognitive_tool_loop = _loop
    return svc


@pytest.mark.asyncio
async def test_drenador_emite_bolha_e_guarda_a_resposta():
    from uuid import uuid4
    from modules.desktop.ai_service import CYCLE_PROGRESS_MARKER, _LoopRun

    async def roteiro(fila):
        await AIService._emit_progress(fila, "lendo o arquivo")
        await asyncio.sleep(0)
        await AIService._emit_progress(fila, "quase lá")
        return "resposta final"

    run = _LoopRun()
    bolhas = [
        r async for r in _svc_com_loop(roteiro)._drive_cognitive_loop(
            run, session_id=uuid4()
        )
    ]

    assert [b.message for b in bolhas] == [
        f"{CYCLE_PROGRESS_MARKER}lendo o arquivo",
        f"{CYCLE_PROGRESS_MARKER}quase lá",
    ]
    assert all(b.is_final is False for b in bolhas)
    # A resposta NÃO sai como bolha: ela é o retorno do turno.
    assert run.text == "resposta final"


@pytest.mark.asyncio
async def test_drenador_propaga_a_excecao_do_loop():
    """`ClientActuationRequested` sobe por aqui — é como o turno pede o cliente."""
    from uuid import uuid4
    from modules.desktop.ai_service import ClientActuationRequested, _LoopRun

    async def roteiro(fila):
        await AIService._emit_progress(fila, "indo")
        raise ClientActuationRequested("save_generated_file", {}, "c1")

    with pytest.raises(ClientActuationRequested):
        async for _ in _svc_com_loop(roteiro)._drive_cognitive_loop(
            _LoopRun(), session_id=uuid4()
        ):
            pass


@pytest.mark.asyncio
async def test_consumidor_que_desiste_nao_deixa_o_loop_rodando():
    """Stop do usuário: a task precisa morrer, não seguir gastando LLM e tool."""
    from uuid import uuid4
    from modules.desktop.ai_service import _LoopRun

    rodou_ate_o_fim = False

    async def roteiro(fila):
        nonlocal rodou_ate_o_fim
        await AIService._emit_progress(fila, "primeira etapa")
        await asyncio.sleep(5)  # "chamada de LLM" que o Stop deve interromper
        rodou_ate_o_fim = True
        return "nunca"

    gen = _svc_com_loop(roteiro)._drive_cognitive_loop(_LoopRun(), session_id=uuid4())
    assert (await gen.__anext__()).message.endswith("primeira etapa")
    await gen.aclose()
    await asyncio.sleep(0.05)
    assert rodou_ate_o_fim is False


# ── pin de provedor: o painel valia para todo mundo menos para o desktop ─────


class _FakeCfg:
    def __init__(self, model):
        self.model = model

    async def __call__(self, db, role, **kw):
        assert role == "cognitive"
        return {"provider": "openrouter", "model": self.model}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pedido,painel,esperado",
    [
        # O caso que motivou tudo: cliente sem pin, painel com pin, mesmo modelo.
        ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash@baidu",
         "deepseek/deepseek-v4-flash@baidu"),
        # Cliente pediu OUTRO modelo: escolha dele, não se mexe.
        ("qwen/qwen3.7-flash", "deepseek/deepseek-v4-flash@baidu", "qwen/qwen3.7-flash"),
        # Cliente já mandou pin explícito: ganha.
        ("deepseek/deepseek-v4-flash@streamlake", "deepseek/deepseek-v4-flash@baidu",
         "deepseek/deepseek-v4-flash@streamlake"),
        # Painel sem pin: nada a colar.
        ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash",
         "deepseek/deepseek-v4-flash"),
    ],
)
async def test_pin_do_painel_so_cola_quando_e_o_mesmo_modelo(
    monkeypatch, pedido, painel, esperado
):
    import modules.model_config as mc

    monkeypatch.setattr(mc, "get_model_config", _FakeCfg(painel))
    svc = _svc()
    svc.db = object()
    assert await svc._pin_from_panel(pedido) == esperado


@pytest.mark.asyncio
async def test_preferencia_ilegivel_nao_derruba_o_turno(monkeypatch):
    import modules.model_config as mc

    async def _explode(*a, **k):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(mc, "get_model_config", _explode)
    svc = _svc()
    svc.db = object()
    assert await svc._pin_from_panel("deepseek/deepseek-v4-flash") == "deepseek/deepseek-v4-flash"
