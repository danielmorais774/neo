"""Download de arquivo com nome fora do latin-1 (sessão 2dc79510).

O PDF "A Quinta Revolução Industrial — Indústria 5.0.pdf" dava 500 em todo
clique do botão e no ``save_generated_file``: a rota escrevia o
``Content-Disposition`` à mão com o nome cru, e o travessão (U+2014) não cabe
no latin-1 dos cabeçalhos HTTP. Acento (ç, ã) cabe, por isso só esse quebrou.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import principal as principal_mod
from api.routes import files as files_route

NOME = "A Quinta Revolução Industrial — Indústria 5.0.pdf"
CONTEUDO = b"%PDF-1.7 teste"


class _StoreFalso:
    def __init__(self, blob):
        self._blob = blob

    async def get(self, file_id):
        return SimpleNamespace(
            id=file_id, name=NOME, status="available", user_id=None, sha256="abc"
        )

    def blob_path(self, sha256):
        return self._blob


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    blob = tmp_path / "blob"
    blob.write_bytes(CONTEUDO)
    monkeypatch.setattr(principal_mod, "auth_mode", lambda: principal_mod.MODE_OFF)
    files_route.set_store(_StoreFalso(blob))
    app = FastAPI()
    app.include_router(files_route.router)
    yield TestClient(app)
    files_route.set_store(None)


def test_nome_com_travessao_baixa(cliente):
    resp = cliente.get("/api/v1/files/a8ea9161-2138-4b8b-9aaf-f2799893a2be/download")

    assert resp.status_code == 200
    assert resp.content == CONTEUDO


def test_cabecalho_leva_o_nome_inteiro_e_continua_attachment(cliente):
    resp = cliente.get("/api/v1/files/a8ea9161-2138-4b8b-9aaf-f2799893a2be/download")

    disposicao = resp.headers["content-disposition"]
    assert disposicao.startswith("attachment;")
    assert unquote(disposicao.split("filename*=utf-8''", 1)[1]) == NOME
