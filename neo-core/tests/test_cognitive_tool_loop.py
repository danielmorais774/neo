"""Cognitive agentic read-tool loop — the brain calls read tools inline.

Exercises `_run_cognitive_tool_loop` with a fake provider (scripted to call a tool
then answer) and a fake executor, plus `CognitiveReadExecutor` user scoping.
"""

import pytest
from uuid import uuid4

from core.cognitive import CognitiveReadExecutor, cognitive_tools_for
from modules.desktop.ai_service import AIService
from shared import ToolCall, ToolResult
from shared.types import Message, MessageRole


# ── fakes ───────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, content="", tool_calls=None):
        self._content = content
        self._tc = tool_calls or []

    def get_content(self):
        return self._content

    def get_tool_calls(self):
        return self._tc


class _ScriptedProvider:
    """Returns a queued response per complete() call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.tool_flags = []
        self.tool_payloads = []   # a lista EXATA que iria pro provider, por round

    async def complete(self, *, messages, tools=None, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        self.tool_flags.append(tools is not None)
        self.tool_payloads.append(list(tools or []))
        return self._responses.pop(0)


class _FakeExecutor:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def execute_tool_call(self, tool_call):
        self.calls.append((tool_call.name, tool_call.arguments))
        return ToolResult(tool_call_id=tool_call.id, name=tool_call.name, result=self._result, execution_time=0.0)


def _svc(settings_overrides=None):
    svc = object.__new__(AIService)
    class _S:
        cognitive_max_tool_rounds = 3
        default_max_tokens = 1024
        # O loop lê estas duas em todo round: sem elas o dublê não é o
        # Settings real e o teste passa a medir o AttributeError.
        cognitive_stream_enabled = False
        cognitive_reasoning_enabled = False
    svc.settings = _S()
    for k, v in (settings_overrides or {}).items():
        setattr(svc.settings, k, v)
    return svc


@pytest.mark.asyncio
async def test_loop_calls_tool_then_answers_from_result():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="search_pks", arguments={"query": "treino"})]),
        _Resp(content="Não achei treino no seu PKS — quer que eu salve agora?"),
    ])
    execu = _FakeExecutor({"found": False, "count": 0, "results": []})
    messages = [Message(role=MessageRole.SYSTEM, content="sys"),
                Message(role=MessageRole.USER, content="salvou mesmo?")]
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages, req_model=None, model_name="m",
        provider_name="p", tools=cognitive_tools_for(uuid4()), tool_executor=execu,
        slog=None, trace=None,
    )
    assert "Não achei" in out
    assert execu.calls == [("search_pks", {"query": "treino"})]
    # results were fed back as a USER message before the final answer
    assert any(m.role == MessageRole.USER and "Tool: search_pks" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_loop_no_tool_call_is_single_pass():
    svc = _svc()
    provider = _ScriptedProvider([_Resp(content="oi, tudo bem?")])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="oi")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "oi, tudo bem?"
    assert len(provider.tool_flags) == 1  # only one LLM call


@pytest.mark.asyncio
async def test_loop_retries_once_on_empty_response():
    """DeepSeek devolve content VAZIO quando o raciocínio consome o max_tokens.
    Vazio + zero tool_calls não é resposta final — o loop deve retentar UMA vez
    (com teto maior) antes de aceitar, senão o turno cai no fallback 'não
    consegui gerar uma resposta' (foi o que derrubou 'como tu tá? ta calmo').
    Ver deepseek-empty-response-reasoning."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(content=""),                    # 1ª chamada: vazia (orçamento no raciocínio)
        _Resp(content="Tô de boa, Daniel."),  # retry com teto maior: agora responde
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="como tu tá?")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "Tô de boa, Daniel."
    assert len(provider.tool_flags) == 2  # chamada original + 1 retry


@pytest.mark.asyncio
async def test_loop_empty_after_retry_falls_through_without_looping():
    """Trava anti-loop: se continuar vazio mesmo após o retry, devolve string
    vazia (o chamador aplica o fallback) tendo retentado EXATAMENTE uma vez —
    nunca fica retentando indefinidamente."""
    svc = _svc()
    provider = _ScriptedProvider([_Resp(content=""), _Resp(content="")])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="como tu tá?")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == ""
    assert len(provider.tool_flags) == 2  # só um retry, mesmo seguindo vazio


@pytest.mark.asyncio
async def test_loop_dedups_repeated_tool_calls():
    svc = _svc()
    tc = ToolCall(id="1", name="search_pks", arguments={"query": "treino"})
    provider = _ScriptedProvider([
        _Resp(tool_calls=[tc]),
        _Resp(tool_calls=[ToolCall(id="2", name="search_pks", arguments={"query": "treino"})]),
        _Resp(content="resposta final"),
    ])
    execu = _FakeExecutor({"found": False})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "resposta final"
    assert len(execu.calls) == 1  # identical 2nd call not re-executed


@pytest.mark.asyncio
async def test_loop_respects_max_rounds():
    svc = _svc({"cognitive_max_tool_rounds": 1})
    # Always tries to call a tool; after max_rounds the last call has tools=None.
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="search_pks", arguments={"query": "a"})]),
        _Resp(content="forçado a responder"),
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="x")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "forçado a responder"
    assert provider.tool_flags == [True, False]  # last round: no tools


@pytest.mark.asyncio
async def test_loop_forces_continuation_on_pending_promise():
    """sessões 7b0956de e 918e515d: o modelo escreveu 1-3 arquivos, depois
    respondeu 'vou zipar e já te mando' / 'vou baixar as imagens... segue o
    próximo passo' SEM chamar mais tool nenhuma, encerrando o turno com a
    entrega pela metade. A guarda deve negar esse texto como final e forçar
    mais uma rodada com tool obrigatória."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_write_file", arguments={"path": "/workspace/index.html", "content": "<html></html>"})]),
        _Resp(content="Arquivo pronto. Agora vou zipar tudo e já te mando o arquivo."),  # promessa, zero tool call
        _Resp(tool_calls=[ToolCall(id="2", name="neo_bash", arguments={"command": "zip -j out.zip index.html"})]),
        _Resp(content="Zip criado e pronto pra baixar."),
    ])
    execu = _FakeExecutor({"ok": True})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="monta o site e zipa")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "Zip criado e pronto pra baixar."
    assert [name for name, _ in execu.calls] == ["neo_write_file", "neo_bash"]
    # round 3 (índice 3) é a rodada final forçada sem tools do próprio
    # max_rounds=3 — não tem mais o que a guarda force: os 3 rounds de tool já
    # foram usados (write, promessa negada, bash).
    assert provider.tool_flags == [True, True, True, False]


@pytest.mark.asyncio
async def test_loop_promise_guard_injects_corrective_instruction():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(content="Vou baixar as imagens e montar o site. Segue o próximo passo."),
        _Resp(content="Ok, terminei sem chamar nada — resposta final."),
    ])
    messages = [Message(role=MessageRole.USER, content="cria o site")]
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=messages,
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "Ok, terminei sem chamar nada — resposta final."
    assert provider.tool_flags == [True, True]
    assert any(
        m.role == MessageRole.USER and "PARE: você descreveu o que ainda vai fazer" in (m.content or "")
        for m in messages
    )


@pytest.mark.asyncio
async def test_loop_promise_guard_retries_only_once():
    """Trava anti-loop: se a resposta continuar prometendo sem executar mesmo
    depois da rodada forçada, aceita como final na 2ª vez — nunca força
    indefinidamente."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(content="Vou gerar os arquivos e te mando em seguida."),
        _Resp(content="Vou gerar os arquivos e te mando em seguida."),  # promessa de novo
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="cria o site")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "Vou gerar os arquivos e te mando em seguida."
    assert provider.tool_flags == [True, True]  # não forçou uma 3ª rodada


@pytest.mark.asyncio
async def test_loop_promise_guard_skips_after_legitimate_background_deferral():
    """Se ele já chamou neo_bash_background de verdade, 'vou avisar quando
    terminar' é o comportamento CERTO — a guarda não deve forçar mais nada."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash_background", arguments={"command": "long_running_job.sh"})]),
        _Resp(content="Coloquei pra rodar em background — vou te avisar quando terminar."),
    ])
    execu = _FakeExecutor({"job_id": "abc"})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="processa esse arquivo grande")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "Coloquei pra rodar em background — vou te avisar quando terminar."
    assert provider.tool_flags == [True, True]  # sem rodada extra forçada


@pytest.mark.asyncio
async def test_loop_forces_continuation_on_download_claim_without_file_save():
    """sessão 95ccd52d: só neo_write_file rodou, o modelo respondeu 'está
    pronto, você pode baixar pelo botão abaixo' sem NUNCA chamar file_save.
    Diferente da anti-promessa (futuro), aqui é afirmação de entrega já
    pronta — a guarda deve negar esse texto como final e forçar mais uma
    rodada até file_save rodar de verdade."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_write_file", arguments={"path": "/workspace/extrato.csv", "content": "a,b\n1,2"})]),
        _Resp(content="Arquivo CSV está pronto. Você pode baixar pelo botão abaixo."),  # alegação falsa, zero tool call
        _Resp(tool_calls=[ToolCall(id="2", name="file_save", arguments={"path": "/workspace/extrato.csv", "name": "extrato.csv"})]),
        _Resp(content="Arquivo salvo e pronto pra baixar."),
    ])
    execu = _FakeExecutor({"file_id": "abc-123", "name": "extrato.csv", "size_bytes": 10})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="gera o csv das minhas finanças")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "Arquivo salvo e pronto pra baixar."
    assert [name for name, _ in execu.calls] == ["neo_write_file", "file_save"]


@pytest.mark.asyncio
async def test_loop_download_claim_guard_skips_when_file_already_saved():
    """file_save já rodou neste turno — 'pode baixar pelo botão' é verdade,
    a guarda não deve forçar rodada nenhuma."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="file_save", arguments={"path": "/workspace/x.csv", "name": "x.csv"})]),
        _Resp(content="Arquivo salvo, pode baixar pelo botão abaixo."),
    ])
    execu = _FakeExecutor({"file_id": "abc", "name": "x.csv", "size_bytes": 5})
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="gera o csv")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
    )
    assert out == "Arquivo salvo, pode baixar pelo botão abaixo."
    assert provider.tool_flags == [True, True]  # sem rodada extra forçada


@pytest.mark.asyncio
async def test_loop_download_claim_guard_retries_only_once():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(content="Está pronto, pode baixar pelo botão abaixo."),
        _Resp(content="Está pronto, pode baixar pelo botão abaixo."),  # alega de novo, sem file_save
    ])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="gera o csv")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == "Está pronto, pode baixar pelo botão abaixo."
    assert provider.tool_flags == [True, True]  # não forçou uma 3ª rodada


@pytest.mark.asyncio
async def test_loop_populates_tool_trace_with_compact_summaries():
    """session-7b0956de: 3x neo_write_file sem persistir rastro nenhum foi o
    motivo do follow-up ("cade os arquivos?") ter refeito tudo do zero. Aqui
    garantimos que o resumo compacto (path+bytes, nunca o conteúdo do arquivo)
    fica disponível pro chamador persistir em extra_metadata.

    O trace ganha campos que servem só ao caderno de trabalho (aqui,
    `primeira_linha`) — o contrato original continua valendo para o que é
    persistido, e é `_strip_note_detail` que o garante (asserção no fim)."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_write_file", arguments={"path": "/workspace/index.html", "content": "<html></html>"})]),
        _Resp(content="Arquivo pronto."),
    ])
    execu = _FakeExecutor({"ok": True, "path": "/workspace/index.html", "bytes": 13})
    trace: list = []
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="cria o html")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
        tool_trace=trace,
    )
    from modules.desktop.ai_service import _strip_note_detail

    assert out == "Arquivo pronto."
    # O que vai para o extra_metadata (reinjetado no contexto de todo turno
    # seguinte) continua sendo exatamente o resumo compacto de antes.
    assert _strip_note_detail(trace) == [
        {"tool": "neo_write_file", "ok": True, "path": "/workspace/index.html", "bytes": 13}
    ]
    # O caderno aponta para o arquivo (que continua no /workspace) em vez de
    # copiá-lo — docs/deep-read-plan.md §6. Só a primeira linha, para reconhecer.
    assert "note_detail" not in trace[0]
    assert trace[0]["primeira_linha"] == "<html></html>"


@pytest.mark.asyncio
async def test_loop_tool_trace_records_errors():
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash", arguments={"command": "zip -j out.zip missing.txt"})]),
        _Resp(content="Deu erro no zip."),
    ])

    class _FailingExecutor:
        async def execute_tool_call(self, tool_call):
            return ToolResult(tool_call_id=tool_call.id, name=tool_call.name, result=None, error="exit 1", execution_time=0.0)

    trace: list = []
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="zipa")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FailingExecutor(), slog=None, trace=None,
        tool_trace=trace,
    )
    assert trace == [{"tool": "neo_bash", "ok": False, "error": "exit 1"}]


# ── guarda de verdade-do-resultado (docs/self-continuation-plan.md §4) ──────
@pytest.mark.asyncio
async def test_loop_artifact_claim_guard_forces_verification_round():
    """Reprodução da sessão bb22cc33: bash sai 0 sem produzir nada e o modelo
    afirma que as imagens existem. A guarda tem que forçar uma rodada com tool
    obrigatória em vez de aceitar a conclusão falsa."""
    svc = _svc()
    provider = _ScriptedProvider([
        _Resp(tool_calls=[ToolCall(id="1", name="neo_bash", arguments={
            "command": "python3 -c \"baixar('/workspace/ciclo-prova')\""})]),
        _Resp(content="Baixei as 3 imagens para /workspace/ciclo-prova. As imagens são reais."),
        _Resp(content="Na verdade não baixei nada: a pasta está vazia, o download falhou."),
    ])
    # exit_code 0 e stdout vazio — "rodou" sem materializar arquivo nenhum.
    execu = _FakeExecutor({"ok": True, "exit_code": 0, "stdout": "", "stderr": ""})
    trace: list = []
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="baixa as imagens")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=execu, slog=None, trace=None,
        tool_trace=trace,
    )
    assert out == "Na verdade não baixei nada: a pasta está vazia, o download falhou."
    assert provider.tool_flags == [True, True, True]   # rodada extra foi forçada


@pytest.mark.asyncio
async def test_loop_artifact_claim_guard_retries_only_once():
    """Modelo reafirma a mentira: aceita e segue, sem loop infinito."""
    svc = _svc()
    lie = "Baixei as imagens para /workspace/ciclo-prova."
    provider = _ScriptedProvider([_Resp(content=lie), _Resp(content=lie)])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="baixa")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
        # Trace com uma tool que FALHOU: o turno trabalhou e não produziu nada.
        tool_trace=[{"tool": "neo_bash", "ok": False, "error": "exit 8"}],
    )
    assert out == lie
    assert provider.tool_flags == [True, True]


@pytest.mark.asyncio
async def test_loop_artifact_claim_guard_ignores_turn_that_ran_no_tools():
    """session-b407c213: "me mostra as imagens que baixou" não rodou tool nenhuma —
    os downloads foram no turno ANTERIOR. O trace é do turno, não da conversa, então
    a guarda não tem o que julgar aqui. Acusar empurrava o modelo a "verificar" o
    sandbox com `run_command`, que roda na máquina do usuário."""
    svc = _svc()
    claim = "Aqui estão as imagens que baixei: /workspace/portifolio-naruto/images/naruto-capa.png"
    provider = _ScriptedProvider([_Resp(content=claim)])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="me mostra as imagens")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
        tool_trace=[],   # turno sem nenhuma tool
    )
    assert out == claim
    assert provider.tool_flags == [True]


@pytest.mark.asyncio
async def test_loop_artifact_claim_guard_silent_without_a_trace():
    """Sem tool_trace não há evidência — a guarda não pode acusar no escuro."""
    svc = _svc()
    claim = "Salvei o arquivo relatorio.csv na pasta."
    provider = _ScriptedProvider([_Resp(content=claim)])
    out = await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="salva")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    assert out == claim
    assert provider.tool_flags == [True]


# ── CognitiveReadExecutor scoping ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_read_executor_withholds_scoped_tools_without_user():
    ex = CognitiveReadExecutor(global_executor=None, db=None, pks_service=None, user_id=None)
    out = await ex.execute_tool_call(ToolCall(id="1", name="search_pks", arguments={"query": "x"}))
    assert out.error is not None  # cannot scope → error, never a wrong-user read


# ── payload de tools: um nome, um schema ────────────────────────────────────
@pytest.mark.asyncio
async def test_loop_nunca_manda_nome_de_tool_repetido_ao_provider():
    """Sessões c35e576f e 1e7c5943: DeepSeek recusava o turno com 400 "Tool names
    must be unique." porque `browser_task` entrava duas vezes — uma pela skill
    `compras` (que lista a tool no frontmatter) e outra pelas JOB_TOOLS do
    workbench. Cada camada da montagem só deduplicava contra a anterior, então a
    colisão entre camadas NÃO adjacentes passava batido. A OpenAI aceitava o mesmo
    payload, o que fazia o bug parecer "problema do DeepSeek".

    O loop é o último ponto antes do payload: a garantia mora aqui."""
    svc = _svc()
    provider = _ScriptedProvider([_Resp(content="ok")])
    dup = [t for t in cognitive_tools_for(uuid4()) if t.name in {"web_search", "search_pks"}]
    assert dup, "fixture precisa de pelo menos uma tool real para duplicar"
    tools = list(cognitive_tools_for(uuid4())) + dup   # a colisão que quebrava o turno

    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="oi")],
        req_model=None, model_name="m", provider_name="p",
        tools=tools, tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )

    enviado = [t.name for t in provider.tool_payloads[0]]
    assert len(enviado) == len(set(enviado)), f"nome repetido no payload: {enviado}"
    # dedup não pode AMPUTAR o toolset — só colapsar as repetições
    assert set(enviado) == {t.name for t in tools}


@pytest.mark.asyncio
async def test_dedup_mantem_a_primeira_ocorrencia_e_a_ordem():
    """A 1a ocorrência é a da camada mais específica (read set antes das action
    tools), e a ordem do payload é estável — trocar isso mudaria silenciosamente
    qual schema o modelo vê."""
    from modules.desktop.ai_service import _dedup_tools_by_name

    class _T:
        def __init__(self, name, tag):
            self.name, self.tag = name, tag

    a, b, c = _T("x", "primeira"), _T("y", "unica"), _T("x", "segunda")
    out = _dedup_tools_by_name([a, b, c])
    assert [t.name for t in out] == ["x", "y"]
    assert out[0].tag == "primeira"


@pytest.mark.asyncio
async def test_rounds_depois_da_primeira_tool_usam_a_temperatura_de_oficina():
    """[cerebro] no neo.toml: 0.9 na conversa, 0.3 depois que uma tool rodou.
    Na session-20cb02b6 as 52 rodadas (código python-docx incluso) foram a 0.9."""
    from shared.ajustes import ajustes

    svc = _svc()
    temps = []

    class _Gravador(_ScriptedProvider):
        async def complete(self, *, temperature=None, **kwargs):
            temps.append(temperature)
            return await super().complete(temperature=temperature, **kwargs)

    provider = _Gravador([
        _Resp(tool_calls=[ToolCall(id="1", name="search_pks", arguments={"query": "x"})]),
        _Resp(content="pronto"),
    ])
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="oi")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
    )
    c = ajustes().cerebro
    assert temps == [c.temperatura, c.temperatura_com_ferramentas]
    assert c.temperatura_com_ferramentas < c.temperatura


@pytest.mark.asyncio
async def test_tarefa_usa_a_temperatura_de_oficina_desde_a_primeira_rodada():
    """Session-a20f4722: a 1ª rodada é a que ESCOLHE a tool, e a 0.9 escolheu `find`
    no Mac para um pedido da internet. Com `onde` de tarefa, 0.3 desde o começo."""
    from shared.ajustes import ajustes

    svc = _svc()
    temps = []

    class _Gravador(_ScriptedProvider):
        async def complete(self, *, temperature=None, **kwargs):
            temps.append(temperature)
            return await super().complete(temperature=temperature, **kwargs)

    provider = _Gravador([_Resp(content="pronto")])
    await svc._run_cognitive_tool_loop(
        provider=provider, messages=[Message(role=MessageRole.USER, content="baixa e junta")],
        req_model=None, model_name="m", provider_name="p",
        tools=cognitive_tools_for(uuid4()), tool_executor=_FakeExecutor({}), slog=None, trace=None,
        tarefa=True,
    )
    assert temps == [ajustes().cerebro.temperatura_com_ferramentas]


def test_aviso_de_rota_so_para_sandbox_no_cerebro():
    from modules.desktop.ai_service import _AVISO_ROTA_SANDBOX, _aviso_rota
    from shared import IntentType, RoutingDecision

    sandbox = RoutingDecision(intent=IntentType.DIRECT_RESPONSE, onde="sandbox")
    assert _aviso_rota(sandbox) == _AVISO_ROTA_SANDBOX
    assert "run_command" in _AVISO_ROTA_SANDBOX
    for onde in ("maquina", "navegador", "nenhum", None):
        assert _aviso_rota(RoutingDecision(intent=IntentType.DIRECT_RESPONSE, onde=onde)) is None
    assert _aviso_rota(RoutingDecision(intent=IntentType.CLIENT_TOOL, onde="sandbox")) is None
