"""Rotas HTTP do File Store (docs/file-store-plan.md §8) com o app de verdade.

Exercita o que os testes de unidade não cobrem: parsing multipart, FileResponse
do download, códigos de status e a GUARDA (upload/download abertos como as rotas
do desktop; gestão do catálogo exige panel JWT — §14.1).

Sem Postgres (o model usa JSONB): db fake + FileStore real em tmp_path.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import files as files_route
from core.files.store import FileStore


class _FakeSession:
    def __init__(self, rows: dict) -> None:
        self._rows = rows

    def add(self, entry) -> None:
        self._rows[entry.id] = entry

    async def commit(self) -> None:
        return None

    async def refresh(self, entry) -> None:
        return None

    async def get(self, model, pk):
        return self._rows.get(pk)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict = {}

    def get_session(self):
        return _FakeSession(self.rows)


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = FileStore(
        db=_FakeDB(), root=str(tmp_path / "store"), workspace_root=str(tmp_path / "ws"),
        max_file_bytes=1024, quota_bytes=1024 * 1024, read_cap_chars=500, ttl_days=7,
    )
    store.ensure_dirs()

    async def _no_quota():
        return None

    monkeypatch.setattr(store, "_assert_quota", _no_quota)
    files_route.set_store(store)
    app = FastAPI()
    app.include_router(files_route.router)
    with TestClient(app) as c:
        yield c
    files_route.set_store(None)


def test_upload_then_download_roundtrip(client):
    resp = client.post(
        "/api/v1/files",
        files={"file": ("dados.csv", b"a,b\n1,2\n", "text/csv")},
        data={"retention": "temp", "origin": "upload-desktop"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "dados.csv"
    assert body["mime"] == "text/csv"
    assert body["size_bytes"] == 8
    assert body["retention"] == "temp" and body["expires_at"]
    file_id = body["id"]

    got = client.get(f"/api/v1/files/{file_id}/download")
    assert got.status_code == 200
    assert got.content == b"a,b\n1,2\n"
    # Nunca renderizar no domínio do painel (§11.7)
    assert "attachment" in got.headers["content-disposition"]


def test_upload_perm_has_no_expiry(client):
    resp = client.post(
        "/api/v1/files",
        files={"file": ("nota.txt", b"guardar", "text/plain")},
        data={"retention": "perm"},
    )
    assert resp.json()["expires_at"] is None


def test_upload_sanitizes_path_traversal_name(client):
    resp = client.post(
        "/api/v1/files",
        files={"file": ("../../etc/passwd", b"x", "text/plain")},
        data={"retention": "temp"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "passwd"  # nome é rótulo, não path


def test_upload_too_large_returns_413(client):
    resp = client.post(
        "/api/v1/files",
        files={"file": ("big.bin", b"x" * 5000, "application/octet-stream")},
        data={"retention": "temp"},
    )
    assert resp.status_code == 413
    assert resp.json()["detail"]["error"] == "file_too_large"


def test_download_unknown_returns_404(client):
    resp = client.get("/api/v1/files/00000000-0000-0000-0000-000000000000/download")
    assert resp.status_code == 404


def test_catalog_management_requires_panel_jwt(client):
    """Guarda ligada: gestão do catálogo não é pública (upload/download são)."""
    for method, path in [
        ("get", "/api/v1/files"),
        ("get", "/api/v1/files/usage"),
        ("delete", "/api/v1/files/00000000-0000-0000-0000-000000000000"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code in (401, 403), f"{path} ficou aberto: {resp.status_code}"


def test_routes_return_503_when_store_disabled(client):
    files_route.set_store(None)  # simula flag off
    resp = client.post(
        "/api/v1/files",
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 503
