"""Manifesto de anexos no prompt (docs/file-store-plan.md §5).

A garantia central da feature: com anexo, o prompt recebe a FICHA do arquivo
(nome/tipo/tamanho/id) + como ler — NUNCA o conteúdo. É também o que ATIVA as
tools file_* (lição da skill financas: tool disponível ≠ tool usada).

Testa ``AIService._build_attachments_manifest`` isoladamente (sem LLM, sem db):
o método só depende de ``self.file_store``.
"""

from __future__ import annotations

import pytest

from db.models.stored_file import StoredFile
from modules.desktop.ai_service import AIService


class _FakeStore:
    """Só o que o manifesto usa: get() + manifest_line()."""

    def __init__(self, entries: list[StoredFile]) -> None:
        self._by_id = {str(e.id): e for e in entries}

    async def get(self, file_id):
        return self._by_id.get(str(file_id))

    @staticmethod
    def manifest_line(entry):
        from core.files.store import FileStore

        return FileStore.manifest_line(entry)


def _svc(store) -> AIService:
    """Instância crua só para exercitar o método (sem passar pelo __init__)."""
    svc = AIService.__new__(AIService)
    svc.file_store = store
    svc._session_attachments = {}
    return svc


def _entry(**kw) -> StoredFile:
    base = dict(
        name="dados.csv", mime="text/csv", size_bytes=2048, sha256="a" * 64,
        origin="upload-desktop", retention="temp", status="available",
        meta_json={"line_count": 120},
    )
    base.update(kw)
    return StoredFile(**base)


@pytest.mark.asyncio
async def test_no_manifest_without_attachments():
    svc = _svc(_FakeStore([]))
    assert await svc._build_attachments_manifest(None) is None
    assert await svc._build_attachments_manifest({}) is None
    assert await svc._build_attachments_manifest({"attachments": []}) is None


@pytest.mark.asyncio
async def test_no_manifest_when_store_disabled():
    svc = _svc(None)  # flag off → file_store None
    out = await svc._build_attachments_manifest({"attachments": ["qualquer-id"]})
    assert out is None


@pytest.mark.asyncio
async def test_manifest_has_ficha_and_instructions_not_content():
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    out = await svc._build_attachments_manifest({"attachments": [str(entry.id)]})
    assert out is not None
    # A ficha
    assert "dados.csv" in out and str(entry.id) in out and "120 linhas" in out
    # O COMO ler (é isto que ativa as tools)
    assert "file_info" in out and "file_read" in out and "file_stage" in out
    # E o princípio explícito
    assert "conteúdo NÃO está aqui" in out.replace(" ", " ")


@pytest.mark.asyncio
async def test_manifest_accepts_dict_form_from_clients():
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    out = await svc._build_attachments_manifest({"attachments": [{"file_id": str(entry.id)}]})
    assert out is not None and "dados.csv" in out


@pytest.mark.asyncio
async def test_manifest_accepts_json_string_from_grpc_clients():
    """gRPC manda metadata como map<string,string> → a lista vem serializada.

    Sem isto o anexo do desktop falha SILENCIOSAMENTE (string não é list →
    nenhum manifesto, nenhum erro). Ver docs/file-store-f4-desktop-attachments-plan.md.
    """
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    import json

    out = await svc._build_attachments_manifest(
        {"attachments": json.dumps([str(entry.id)])}
    )
    assert out is not None and "dados.csv" in out


@pytest.mark.asyncio
async def test_malformed_json_string_is_ignored_without_raising():
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    assert await svc._build_attachments_manifest({"attachments": "não é json"}) is None
    # JSON válido mas do tipo errado também não pode derrubar o turno.
    assert await svc._build_attachments_manifest({"attachments": '"só-uma-string"'}) is None


@pytest.mark.asyncio
async def test_unavailable_and_unknown_attachments_are_skipped():
    gone = _entry(name="expirado.csv", status="expired")
    ok = _entry(name="bom.csv")
    svc = _svc(_FakeStore([gone, ok]))
    out = await svc._build_attachments_manifest(
        {"attachments": [str(gone.id), str(ok.id), "id-que-nao-existe"]}
    )
    assert "bom.csv" in out and "expirado.csv" not in out


@pytest.mark.asyncio
async def test_all_attachments_unresolvable_yields_no_manifest():
    svc = _svc(_FakeStore([]))
    out = await svc._build_attachments_manifest({"attachments": ["sumiu"]})
    assert out is None


@pytest.mark.asyncio
async def test_manifest_tells_model_to_read_not_to_guess_from_preview():
    """Bug de prod (session a65830cf): o Neo chamou file_info e descreveu um HTML
    de 179 linhas pelo preview ("parece ser..."), sem NUNCA ler (access_count=0).
    O manifesto tem que empurrar para a leitura, não só economizar token.
    """
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    out = await svc._build_attachments_manifest({"attachments": [str(entry.id)]})
    assert "preview do file_info é só uma amostra" in out
    assert "parece ser" in out  # a proibição explícita do hedge
    # E a fronteira catálogo × disco do usuário (o outro bug do mesmo log).
    assert "NUNCA" in out and "read_file" in out


# ── Anexo vale para a CONVERSA, não só para a mensagem ───────────────────────


@pytest.mark.asyncio
async def test_attachment_persists_on_following_turns_of_the_session():
    """Turno 2 sem `attachments` no metadata ainda enxerga o arquivo do turno 1.

    É a raiz do misroute de prod: sem manifesto, o classificador inventou
    `/Users/daniel/Downloads/form.html` e mandou para o read_file client-side.
    """
    entry = _entry(name="form.html")
    svc = _svc(_FakeStore([entry]))
    sid = "sessao-1"

    first = await svc._build_attachments_manifest(
        {"attachments": [str(entry.id)]}, session_id=sid
    )
    assert "ANEXADOS NESTA MENSAGEM" in first and "form.html" in first

    follow_up = await svc._build_attachments_manifest({}, session_id=sid)
    assert follow_up is not None, "follow-up perdeu o anexo da conversa"
    assert "form.html" in follow_up
    assert "anexados antes" in follow_up
    assert "ANEXADOS NESTA MENSAGEM" not in follow_up


@pytest.mark.asyncio
async def test_new_attachment_is_separated_from_the_older_ones():
    velho = _entry(name="velho.csv")
    novo = _entry(name="novo.csv")
    svc = _svc(_FakeStore([velho, novo]))
    sid = "sessao-2"

    await svc._build_attachments_manifest({"attachments": [str(velho.id)]}, session_id=sid)
    out = await svc._build_attachments_manifest({"attachments": [str(novo.id)]}, session_id=sid)
    novo_block, velho_block = out.split("## OUTROS ARQUIVOS DESTA CONVERSA")
    assert "novo.csv" in novo_block and "velho.csv" not in novo_block
    assert "velho.csv" in velho_block


@pytest.mark.asyncio
async def test_sessions_do_not_leak_attachments_into_each_other():
    entry = _entry()
    svc = _svc(_FakeStore([entry]))
    await svc._build_attachments_manifest({"attachments": [str(entry.id)]}, session_id="sessao-a")
    assert await svc._build_attachments_manifest({}, session_id="sessao-b") is None
    # E sem session_id (chamadores antigos) não há sticky nenhum.
    assert await svc._build_attachments_manifest({}) is None


@pytest.mark.asyncio
async def test_session_attachments_are_capped_to_the_most_recent():
    entries = [_entry(name=f"f{i}.csv") for i in range(12)]
    svc = _svc(_FakeStore(entries))
    sid = "sessao-3"
    for e in entries:
        await svc._build_attachments_manifest({"attachments": [str(e.id)]}, session_id=sid)
    assert len(svc._session_attachments[sid]) == 10
    out = await svc._build_attachments_manifest({}, session_id=sid)
    # Os 2 mais antigos caíram; o mais recente ficou.
    assert "f0.csv" not in out and "f1.csv" not in out and "f11.csv" in out


@pytest.mark.asyncio
async def test_manifest_is_compact_for_many_files():
    entries = [_entry(name=f"f{i}.csv") for i in range(12)]
    svc = _svc(_FakeStore(entries))
    out = await svc._build_attachments_manifest(
        {"attachments": [str(e.id) for e in entries]}
    )
    # Cap de 10 anexos; e o custo é uma linha por arquivo, não o arquivo.
    assert out.count("- f") == 10
    assert len(out) < 2000
