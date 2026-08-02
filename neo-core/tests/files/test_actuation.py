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

    async def _reenter(session_id, *, user_message, pend, extra_note, slog, trace):
        recorder.append({"extra_note": extra_note, "user_message": user_message, "pend": pend})
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
    sid = uuid4()
    car = ClientActuationRequested("save_generated_file", {"file_id": "nao-existe", "file_name": "x.pdf"}, "tc4")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="salva no meu desktop", user_id=None,
        metadata=None, trace=None, slog=None,
    ))
    assert not any(r.tool_request for r in out)  # nada foi despachado
    assert out[-1].is_final and "não achei" in out[-1].message.lower()


@pytest.mark.asyncio
async def test_actuation_depth_cap_stops_the_loop():
    svc = _svc()
    sid = uuid4()
    car = ClientActuationRequested("request_user_file", {"mode": "pick"}, "tc5")
    out = await _collect(svc._emit_client_actuation(
        car, session_id=sid, user_message="de novo", user_id=None,
        metadata=None, trace=None, slog=None, depth=3,
    ))
    assert not any(r.tool_request for r in out)
    assert out[-1].is_final


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
async def test_delivery_saved_confirms():
    svc = _svc()
    sid = uuid4()
    out = await _collect(svc._confirm_file_delivery(
        sid, success=True, result='{"status": "saved", "path": "/Users/d/Desktop/rel.pdf"}',
        error=None, user_message="salva no desktop", pend={}, slog=None, trace=None,
    ))
    assert out[-1].is_final and "salvei" in out[-1].message.lower()
    assert "/Users/d/Desktop/rel.pdf" in out[-1].message


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
