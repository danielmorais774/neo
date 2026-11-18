"""A planilha publicada pelo `file_save` passa pela conferência (session-3b19d27a)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.desktop.ai_service import AIService
from tests.files.test_contas import _planilha_da_sessao, _xlsx


class _Store:
    def __init__(self, caminho: Path | None):
        self._caminho = caminho

    async def get(self, file_id):
        return SimpleNamespace(sha256="abc") if self._caminho else None

    def blob_path(self, sha256):
        return self._caminho


_LEEM = {"file_read", "file_stage", "neo_bash"}


def _svc(caminho: Path | None, nota_de_origem: str | None = None):
    """O aviso de origem de verdade tem teste próprio (test_ledger_aneis); aqui
    basta um que fale só em tool que lê conteúdo, como ele."""
    svc = object.__new__(AIService)
    svc.file_store = _Store(caminho)

    def _origem(user_id, session_id):
        if nota_de_origem is None:
            return None

        async def anotar(nome, argumentos, resultado):
            return nota_de_origem if nome in _LEEM else None

        return anotar

    svc._origin_annotator = _origem
    return svc


def _salvo(nome="notinha-mercado.xlsx"):
    return {"file_id": "9461f139", "name": nome, "size_bytes": 7059, "retention": "perm"}


@pytest.mark.asyncio
async def test_planilha_que_nao_fecha_ganha_o_aviso(tmp_path):
    caminho = _xlsx(tmp_path / "b.xlsx", _planilha_da_sessao(33.99))
    anotar = _svc(caminho)._anotador_do_turno("u", "s")
    nota = await anotar("file_save", {"path": "/workspace/notinha-mercado.xlsx"}, _salvo())
    assert nota is not None and "33,99 → 39,99" in nota


@pytest.mark.asyncio
async def test_planilha_que_fecha_nao_ganha_nada(tmp_path):
    caminho = _xlsx(tmp_path / "b.xlsx", _planilha_da_sessao(39.99))
    anotar = _svc(caminho)._anotador_do_turno("u", "s")
    assert await anotar("file_save", {}, _salvo()) is None


@pytest.mark.asyncio
async def test_so_file_save_de_xlsx(tmp_path):
    caminho = _xlsx(tmp_path / "b.xlsx", _planilha_da_sessao(33.99))
    anotar = _svc(caminho)._anotador_do_turno("u", "s")
    assert await anotar("neo_bash", {}, _salvo()) is None
    assert await anotar("file_save", {}, _salvo("relatorio.pdf")) is None
    assert await anotar("file_save", {}, {"error": "quota"}) is None


@pytest.mark.asyncio
async def test_desligado_no_neo_toml(tmp_path, monkeypatch):
    import modules.desktop.ai_service as mod

    real = mod.ajustes()
    falso = SimpleNamespace(cerebro=real.cerebro.model_copy(update={"conferir_planilhas": False}))
    monkeypatch.setattr(mod, "ajustes", lambda: falso)
    caminho = _xlsx(tmp_path / "b.xlsx", _planilha_da_sessao(33.99))
    anotar = _svc(caminho)._anotador_do_turno("u", "s")
    assert await anotar("file_save", {}, _salvo()) is None


@pytest.mark.asyncio
async def test_aviso_de_origem_continua_e_soma_com_a_conferencia(tmp_path):
    caminho = _xlsx(tmp_path / "b.xlsx", _planilha_da_sessao(33.99))
    anotar = _svc(caminho, "(arquivo de outra conversa)")._anotador_do_turno("u", "s")
    # file_save não é tool que lê conteúdo: só a conferência.
    nota = await anotar("file_save", {}, _salvo())
    assert nota is not None and "outra conversa" not in nota
    # Tool que lê: só a origem, como antes.
    nota = await anotar("file_read", {"file_id": "x"}, {"file_id": "x"})
    assert nota == "(arquivo de outra conversa)"
