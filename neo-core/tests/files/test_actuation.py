"""Arquivos sob iniciativa do cérebro (plano "arquivos sob iniciativa do cérebro").

Cobre as duas mãos client-actuating do núcleo cognitivo, isoladas do LLM/gRPC:

- ``request_user_file`` (PULL): guarda de caminho não fundamentado (cai pro
  picker, nunca sobe um chute), emissão do tool_request, e a retomada que
  cataloga o arquivo puxado (sticky) + auto-stage.
- ``save_generated_file`` (PUSH): guarda de file_id inexistente, confirmação de
  entrega e o tratamento da COLISÃO (status="conflict" → re-entra pro cérebro
  decidir/perguntar, nunca sobrescreve às cegas).

Exercita os métodos de ``AIService`` diretamente (sem passar pelo __init__), no
mesmo molde de ``tests/files/test_manifest.py``.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from db.models.stored_file import StoredFile
from modules.desktop.ai_service import AIService, ClientActuationRequested, IntentType


class _FakeStore:
    def __init__(self, entries: list[StoredFile] | None = None, staged_path: str | None = None) -> None:
        self._by_id = {str(e.id): e for e in (entries or [])}
        self._staged_path = staged_path
        self.stage_calls: list[str] = []

    async def get(self, file_id):
        return self._by_id.get(str(file_id))

    async def stage(self, file_id, target="workbench", name=None):
        self.stage_calls.append(str(file_id))
        return {"path": self._staged_path or f"/workspace/files/{file_id}"}


class _FakeSessions:
    def __init__(self, history=None) -> None:
        self.added: list[dict] = []
        self._history = history or []

    async def add_message(self, **kw):
        self.added.append(kw)

    async def get_history(self, *a, **kw):
        return self._history


def _entry(**kw) -> StoredFile:
    base = dict(
        name="dados.csv", mime="text/csv", size_bytes=2048, sha256="a" * 64,
        origin="upload-desktop", retention="temp", status="available",
        meta_json={"line_count": 120},
    )
    base.update(kw)
    return StoredFile(**base)


def _artefatos(path_map: dict[str, str]):
    """Dublê do registro de artefatos (`_recent_artifacts` devolve bloco + mapa)."""

    async def _recent(user_id):
        return None, dict(path_map)

    return _recent


def _svc(*, store=None, history=None) -> AIService:
    svc = AIService.__new__(AIService)
    svc.file_store = store
    svc.sessions = _FakeSessions(history=history)
    svc._session_attachments = {}
    svc._pending_actuations = {}
    svc._pending_traces = {}
    return svc


def _spy_reenter(svc, recorder):
    """Substitui _reenter_cognitive (pesado: LLM+DB) por um gerador que só grava a chamada."""

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace, steps=None):
        recorder.append({
            "extra_note": extra_note, "user_message": user_message, "pend": pend, "steps": steps,
        })
        from modules.desktop.ai_service import AIServiceResponse

        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="[reentrada]", is_final=True,
        )

    svc._reenter_cognitive = _reenter


async def _collect(agen) -> list:
    return [x async for x in agen]


# ── _file_path_is_grounded ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_grounded_when_cited_in_message():
    svc = _svc()
    sid = uuid4()
    assert await svc._file_path_is_grounded("~/Desktop/vendas.csv", sid, "lê ~/Desktop/vendas.csv") is True


@pytest.mark.asyncio
async def test_path_grounded_by_basename_in_history():
    svc = _svc(history=[{"role": "user", "content": "tenho um arquivo vendas.csv aqui"}])
    sid = uuid4()
    # O usuário citou o NOME antes; o caminho completo casa pelo nome-base.
    assert await svc._file_path_is_grounded("/Users/x/Downloads/vendas.csv", sid, "analisa pra mim") is True


@pytest.mark.asyncio
async def test_invented_path_is_not_grounded():
    svc = _svc(history=[{"role": "user", "content": "me ajuda com uma planilha"}])
    sid = uuid4()
    assert await svc._file_path_is_grounded("/Users/daniel/Downloads/relatorio.pdf", sid, "manda o relatório") is False
    assert await svc._file_path_is_grounded("", sid, "qualquer") is False


@pytest.mark.asyncio
async def test_path_grounded_pelo_registro_de_artefatos():
    """Sessão 97774390: o Neo baixou a NFSe, confirmou por `ls` aprovado e
    anunciou o caminho. O usuário NUNCA escreveu o nome — e a guarda tratava o
    próprio achado do Neo como invenção, abria o seletor e, com o seletor
    fechado, o turno acabava em "não recebi nenhum arquivo".

    Arquivo que o Neo mesmo pôs no disco é evidência de disco, não chute — é o
    mesmo critério que o roteador já usa (`_resolve_artifact_paths`)."""
    svc = _svc(history=[{"role": "user", "content": "ler o conteudo dele e faz um resumo"}])
    svc._recent_artifacts = _artefatos(
        {"nfse_abril.pdf": "/Users/daniel/Downloads/nfse_abril.pdf"}
    )
    sid = uuid4()
    assert await svc._file_path_is_grounded(
        "/Users/daniel/Downloads/nfse_abril.pdf", sid, "ler o conteudo dele e faz um resumo"
    ) is True


@pytest.mark.asyncio
async def test_registro_nao_fundamenta_caminho_de_outra_pasta():
    """O registro fundamenta o caminho REAL, não o nome. Se o cérebro montou uma
    pasta diferente da que está registrada, continua sendo chute."""
    svc = _svc(history=[{"role": "user", "content": "resume pra mim"}])
    svc._recent_artifacts = _artefatos(
        {"nfse_abril.pdf": "/Users/daniel/Downloads/nfse_abril.pdf"}
    )
    sid = uuid4()
    assert await svc._file_path_is_grounded(
        "/Users/daniel/Desktop/nfse_abril.pdf", sid, "resume pra mim"
    ) is False


# ── _emit_client_actuation: PULL ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_pick_emits_tool_request_and_stores_resume_state():
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested("request_user_file", {"mode": "pick", "reason": "preciso da planilha"}, "tc1")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="analisa uma planilha que tenho", user_id=None,
        metadata={"client": "neo-desktop"}, trace=None, slog=None,
    ))
    reqs = [r for r in out if r.tool_request]
    assert len(reqs) == 1
    assert reqs[0].tool_request["tool_name"] == "request_user_file"
    assert '"mode": "pick"' in reqs[0].tool_request["arguments"]
    # Estado de retomada guardado (o gRPC não encaminha isso sozinho).
    assert str(sid) in svc._pending_actuations
    assert svc._pending_actuations[str(sid)]["user_message"] == "analisa uma planilha que tenho"


@pytest.mark.asyncio
async def test_pull_path_ungrounded_falls_back_to_picker():
    """O cérebro chutou um caminho que o usuário não citou → NÃO sobe o chute; abre o picker."""
    svc = _svc(history=[{"role": "user", "content": "me ajuda com uns dados"}])
    sid = uuid4()
    car = ClientActuationRequested(
        "request_user_file", {"mode": "path", "path": "/Users/daniel/Downloads/inventado.csv"}, "tc2"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="me ajuda com uns dados", user_id=None,
        metadata=None, trace=None, slog=None,
    ))
    req = next(r for r in out if r.tool_request)
    args = req.tool_request["arguments"]
    assert '"mode": "pick"' in args  # caiu pro picker
    assert "inventado.csv" not in args  # o caminho chutado foi descartado


@pytest.mark.asyncio
async def test_pull_path_grounded_is_kept():
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested(
        "request_user_file", {"mode": "path", "path": "~/Desktop/vendas.csv"}, "tc3"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="sobe o ~/Desktop/vendas.csv pra você", user_id=None,
        metadata=None, trace=None, slog=None,
    ))
    req = next(r for r in out if r.tool_request)
    assert '"mode": "path"' in req.tool_request["arguments"]
    assert "vendas.csv" in req.tool_request["arguments"]


# ── _emit_client_actuation: PUSH guard + depth cap ──────────────────────────


@pytest.mark.asyncio
async def test_push_with_unknown_file_id_is_refused_without_dispatch():
    svc = _svc(store=_FakeStore([]))  # catálogo vazio
    rec: list = []
    _spy_reenter(svc, rec)
    sid = uuid4()
    car = ClientActuationRequested("save_generated_file", {"file_id": "nao-existe", "file_name": "x.pdf"}, "tc4")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="salva no meu desktop", user_id=None,
        metadata=None, trace=None, slog=None,
    ))
    assert not any(r.tool_request for r in out)  # nada foi despachado
    # Sem beco sem saída (sessão 9b0b10cc): o cérebro retoma sabendo por quê.
    assert rec and "FILE_ID INEXISTENTE" in rec[-1]["extra_note"]


@pytest.mark.asyncio
async def test_actuation_depth_cap_stops_the_loop():
    """O teto é rede de segurança (a guarda real é a repetição) e, ao parar, o
    Neo conta o que já rodou em vez de "me diga o que você quer" (sessão 45bd5ad0)."""
    from modules.desktop.ai_service import _MAX_ACTUATION_DEPTH

    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested("request_user_file", {"mode": "pick"}, "tc5")
    passos = [{"command": "ffmpeg -i video.mov frames/%04d.jpg", "ok": True, "output": "127 frames"}]
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="de novo", user_id=None,
        metadata=None, trace=None, slog=None, depth=_MAX_ACTUATION_DEPTH, steps=passos,
    ))
    assert not any(r.tool_request for r in out)
    assert out[-1].is_final
    assert "ffmpeg -i video.mov" in out[-1].message


@pytest.mark.asyncio
async def test_three_legit_actuations_no_longer_stop_the_turn():
    svc = _svc()
    car = ClientActuationRequested("request_user_file", {"mode": "pick"}, "tc6")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="faz no sandbox", user_id=None,
        metadata=None, trace=None, slog=None, depth=3,
    ))
    assert any(r.tool_request for r in out)


@pytest.mark.asyncio
async def test_repeated_shell_command_stops_and_lists_what_ran():
    svc = _svc()
    passos = [{"command": "ls -lh /Users/daniel/Desktop/conversa-l", "ok": True, "output": "..."}]
    car = ClientActuationRequested(
        "run_command", {"command": "ls  -lh /Users/daniel/Desktop/conversa-l"}, "tc7"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=uuid4(), user_message="lista a pasta", user_id=None,
        metadata=None, trace=None, slog=None, depth=1, steps=passos,
    ))
    assert not any(r.command_request for r in out)
    assert out[-1].is_final and "já rodou neste pedido" in out[-1].message


# ── _resume_after_user_file: retomada do PULL ───────────────────────────────


@pytest.mark.asyncio
async def test_resume_cancel_is_friendly_and_final():
    svc = _svc()
    sid = uuid4()
    out = await _collect(svc._resume_after_user_file(
        sid, success=False, result=None, error="usuário não escolheu arquivo",
        user_message="analisa a planilha", pend={}, slog=None, trace=None,
    ))
    assert out[-1].is_final and "não recebi" in out[-1].message.lower()


@pytest.mark.asyncio
async def test_pull_path_do_registro_nao_abre_o_seletor():
    """O outro lado da queixa do usuário: "ele abriu uma caixa de seleção de
    arquivo e pediu para eu selecionar, sendo que ele já sabe o arquivo"."""
    svc = _svc(history=[{"role": "user", "content": "sobe ele para sandbox"}])
    svc._recent_artifacts = _artefatos(
        {"nfse_abril.pdf": "/Users/daniel/Downloads/nfse_abril.pdf"}
    )
    sid = uuid4()
    car = ClientActuationRequested(
        "request_user_file",
        {"mode": "path", "path": "/Users/daniel/Downloads/nfse_abril.pdf", "stage": True},
        "tc9",
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="sobe ele para sandbox", user_id=None,
        metadata=None, trace=None, slog=None,
    ))
    args = next(r for r in out if r.tool_request).tool_request["arguments"]
    assert '"mode": "path"' in args, "caminho conhecido não pode virar seletor"
    assert "nfse_abril.pdf" in args


@pytest.mark.asyncio
async def test_resume_cancel_diz_qual_arquivo_queria():
    """Quando o seletor só abriu porque a guarda derrubou um caminho conhecido,
    fechar o seletor não pode virar "não recebi nenhum arquivo": some com o
    arquivo que o Neo tinha acabado de localizar e contradiz o turno anterior."""
    svc = _svc()
    sid = uuid4()
    out = await _collect(svc._resume_after_user_file(
        sid, success=False, result=None, error="usuário não escolheu arquivo",
        user_message="ler o conteudo dele",
        pend={"downgraded_path": "/Users/daniel/Downloads/nfse_abril.pdf"},
        slog=None, trace=None,
    ))
    msg = out[-1].message
    assert out[-1].is_final
    assert "nfse_abril.pdf" in msg
    assert "não recebi nenhum arquivo" not in msg.lower()


@pytest.mark.asyncio
async def test_resume_success_catalogs_and_reenters():
    entry = _entry(name="vendas.csv")
    svc = _svc(store=_FakeStore([entry]))
    rec: list = []
    _spy_reenter(svc, rec)
    sid = uuid4()
    result = f'{{"file_id": "{entry.id}", "name": "vendas.csv", "size_bytes": 2048}}'
    out = await _collect(svc._resume_after_user_file(
        sid, success=True, result=result, error=None,
        user_message="quantas unidades a região Norte vendeu?", pend={"stage": False},
        slog=None, trace=None,
    ))
    # Sticky: o arquivo puxado vale pro resto da conversa.
    assert str(entry.id) in svc._session_attachments[str(sid)]
    # Re-entrou no cognitivo com a nota do arquivo recém-recebido.
    assert rec and "RECÉM-RECEBIDO" in rec[0]["extra_note"]
    assert out[-1].message == "[reentrada]"


@pytest.mark.asyncio
async def test_resume_success_with_stage_materializes_in_workspace():
    entry = _entry(name="script.py")
    store = _FakeStore([entry], staged_path="/workspace/files/script.py")
    svc = _svc(store=store)
    rec: list = []
    _spy_reenter(svc, rec)
    sid = uuid4()
    result = f'{{"id": "{entry.id}", "name": "script.py"}}'  # cliente pode mandar `id`
    await _collect(svc._resume_after_user_file(
        sid, success=True, result=result, error=None,
        user_message="roda esse script", pend={"stage": True}, slog=None, trace=None,
    ))
    assert store.stage_calls == [str(entry.id)]
    assert "/workspace/files/script.py" in rec[0]["extra_note"]


# ── _confirm_file_delivery: retomada do PUSH ────────────────────────────────


@pytest.mark.asyncio
async def test_delivery_saved_reenters_para_continuar():
    """Entregar UM arquivo não encerra o turno.

    `save_generated_file` entrega um por chamada. Fechar aqui fazia "põe os 6
    PDFs na pasta" virar um por turno, com o usuário cutucando a cada um — na
    sessão 8c449cb8 ele teve que dizer "e os outros? era pra ser os 6" e "tem
    mais ainda", e ainda assim só 3 dos 6 chegaram. O orçamento nunca foi o
    limite (`_MAX_ACTUATION_DEPTH` = 16): faltava perguntar ao cérebro.
    """
    svc = _svc()
    sid = uuid4()
    notas: list[str] = []

    from modules.desktop.ai_service import AIServiceResponse

    async def _fake_reenter(session_id, *, user_message, pend, extra_note, **kw):
        notas.append(extra_note)
        yield AIServiceResponse(
            intent=IntentType.DIRECT_RESPONSE, session_id=session_id,
            message="Pronto, salvei.", is_final=True, iterations=1,
        )

    svc._reenter_cognitive = _fake_reenter
    out = await _collect(svc._confirm_file_delivery(
        sid, success=True, result='{"status": "saved", "path": "/Users/d/Desktop/rel.pdf"}',
        error=None, user_message="salva no desktop", pend={}, slog=None, trace=None,
    ))
    assert out[-1].is_final
    assert notas, "sucesso tem que reentrar no cognitivo"
    assert "/Users/d/Desktop/rel.pdf" in notas[0]
    assert "PRÓXIMO" in notas[0], "a nota tem que cobrar a continuação"


@pytest.mark.asyncio
async def test_delivery_failure_reports_error():
    svc = _svc()
    sid = uuid4()
    out = await _collect(svc._confirm_file_delivery(
        sid, success=False, result=None, error="pasta protegida",
        user_message="salva aí", pend={}, slog=None, trace=None,
    ))
    assert out[-1].is_final and "pasta protegida" in out[-1].message


@pytest.mark.asyncio
async def test_delivery_conflict_reenters_with_existing_metadata():
    """Colisão: NÃO sobrescreve — re-entra pro cérebro decidir/perguntar, com os dados do existente."""
    svc = _svc()
    rec: list = []
    _spy_reenter(svc, rec)
    sid = uuid4()
    result = (
        '{"status": "conflict", "path": "/Users/d/Desktop/rel.pdf", '
        '"existing": {"name": "rel.pdf", "size_bytes": 2148000, "modified_at": "2026-07-24 09:10"}}'
    )
    out = await _collect(svc._confirm_file_delivery(
        sid, success=True, result=result, error=None,
        user_message="salva no desktop", pend={}, slog=None, trace=None,
    ))
    assert rec, "conflito deveria re-entrar no cognitivo"
    note = rec[0]["extra_note"]
    assert "CONFLITO" in note and "rel.pdf" in note and "overwrite=true" in note
    assert out[-1].message == "[reentrada]"


@pytest.mark.asyncio
async def test_pull_path_com_til_sai_expandido_pelo_home_do_desktop():
    """Sessão 916fe494: o desktop não expande "~" no upload e devolvia ENOENT."""
    pedido = "Transcreve o vídeo ~/Downloads/superxandao-pichau.mp4 e me mostra o texto."
    svc = _svc(history=[{"role": "user", "content": pedido}])
    sid = uuid4()
    car = ClientActuationRequested(
        "request_user_file", {"mode": "path", "path": "~/Downloads/superxandao-pichau.mp4", "stage": True}, "tc9"
    )
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message=pedido, user_id=None,
        metadata={"client": "neo-desktop-tauri", "home_dir": "/Users/daniel"}, trace=None, slog=None,
    ))
    req = next(r for r in out if r.tool_request)
    args = req.tool_request["arguments"]
    assert '"mode": "path"' in args
    assert '"/Users/daniel/Downloads/superxandao-pichau.mp4"' in args
    assert "~/" not in args


def test_home_expandido_so_mexe_no_til():
    from modules.desktop.ai_service import _home_expandido

    md = {"home_dir": "/Users/daniel/"}
    assert _home_expandido("~/Downloads/a b.mp4", md) == "/Users/daniel/Downloads/a b.mp4"
    assert _home_expandido("~", md) == "/Users/daniel"
    assert _home_expandido("/tmp/x", md) == "/tmp/x"
    assert _home_expandido("~outro/x", md) == "~outro/x"
    assert _home_expandido("~/x", None) == "~/x"


@pytest.mark.asyncio
async def test_rotulo_de_midia_atravessa_a_retomada():
    """Sem o rótulo, o aviso de fala × tela sumia no turno que já tem o vídeo no sandbox."""
    pedido = "faz a transcrição do vídeo ~/Downloads/aula.mp4"
    svc = _svc(history=[{"role": "user", "content": pedido}])
    sid = uuid4()
    car = ClientActuationRequested(
        "request_user_file", {"mode": "path", "path": "~/Downloads/aula.mp4", "stage": True}, "tc10"
    )
    await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message=pedido, user_id=None,
        metadata={"home_dir": "/Users/daniel"}, trace=None, slog=None, midia="fala",
    ))
    pend = svc._pending_actuations[str(sid)]
    assert pend["midia"] == "fala"

    vistas = []

    async def _hdr(sid_, user_message, history, decision, trace, sys_context, **kw):
        vistas.append(decision)
        if False:
            yield None

    async def _vazio(*a, **k):
        return ""

    svc._handle_direct_response = _hdr
    svc._build_system_context = lambda md: ""
    svc._build_attachments_manifest = _vazio
    svc._file_links_block = _vazio
    await _collect(svc._reenter_cognitive(
        sid, user_message=pedido, pend=pend, extra_note=None, slog=None, trace=None,
    ))
    assert vistas and vistas[0].midia == "fala"
