"""FileStore — ingestão/dedup, leitura fracionada, retenção, GC, quota, path-jail.

Sem Postgres: um fake de db client (get_session) com store em memória que imita
o suficiente da API do SQLModel usada pelo FileStore. Sem rede, sem sandbox.
Ver docs/file-store-plan.md §13 (aceite da F1).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.files.store import FileStore, FileStoreError, is_text_mime, safe_name
from db.models.stored_file import StoredFile


# ── fake db ─────────────────────────────────────────────────────────────────
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
    """Imita o AsyncPostgresClient no que o store usa; queries são interpretadas
    pelos testes que precisam (list/gc/usage usam caminhos próprios abaixo)."""

    def __init__(self) -> None:
        self.rows: dict = {}

    def get_session(self):
        return _FakeSession(self.rows)


def _store(tmp_path: Path, db: _FakeDB, **kw) -> FileStore:
    params = dict(
        db=db,
        root=str(tmp_path / "store"),
        workspace_root=str(tmp_path / "workspace"),
        max_file_bytes=1024 * 1024,
        quota_bytes=10 * 1024 * 1024,
        read_cap_chars=200,
        ttl_days=7,
    )
    params.update(kw)
    s = FileStore(**params)
    s.ensure_dirs()
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    return s


# Quota/list/gc fazem SQL que o fake não interpreta — neutralizamos onde o teste
# não é sobre isso (cada um tem seu teste dedicado com stub próprio).
@pytest.fixture
def store(tmp_path, monkeypatch):
    db = _FakeDB()
    s = _store(tmp_path, db)

    async def _no_quota():
        return None

    monkeypatch.setattr(s, "_assert_quota", _no_quota)
    return s


# ── helpers puros ───────────────────────────────────────────────────────────

def test_safe_name_strips_path_and_control_chars():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("/tmp/a/b/relatorio final.pdf") == "relatorio final.pdf"
    assert safe_name("evil\x00name.txt") == "evilname.txt"
    assert safe_name("") == "arquivo"
    assert safe_name("...") == "arquivo"


def test_is_text_mime():
    assert is_text_mime("text/csv") and is_text_mime("application/json")
    assert is_text_mime("text/plain; charset=utf-8")
    assert not is_text_mime("application/pdf") and not is_text_mime("image/png")


# ── ingestão + dedup ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_bytes_creates_blob_and_entry(store):
    entry = await store.ingest_bytes(b"a,b\n1,2\n", name="dados.csv", origin="upload-panel")
    assert entry.size_bytes == 8
    assert entry.mime == "text/csv"
    assert entry.retention == "temp" and entry.expires_at is not None
    blob = store.blob_path(entry.sha256)
    assert blob.exists() and blob.read_bytes() == b"a,b\n1,2\n"
    # texto → preview e extraction ok já na ingestão
    assert entry.meta_json["extraction"] == "ok"
    assert entry.meta_json["line_count"] == 2


@pytest.mark.asyncio
async def test_dedup_same_content_one_blob_two_rows(store):
    a = await store.ingest_bytes(b"igual", name="a.txt", origin="upload-panel")
    b = await store.ingest_bytes(b"igual", name="b.txt", origin="upload-panel")
    assert a.sha256 == b.sha256
    assert a.id != b.id                      # duas ingestões
    assert store.blob_path(a.sha256).exists()  # um blob só
    blobs = list((store.blobs_root).rglob("*"))
    assert len([p for p in blobs if p.is_file()]) == 1


@pytest.mark.asyncio
async def test_perm_has_no_expiry(store):
    entry = await store.ingest_bytes(b"x", name="a.txt", origin="upload-panel", retention="perm")
    assert entry.retention == "perm" and entry.expires_at is None


@pytest.mark.asyncio
async def test_file_too_large_rejected(tmp_path, monkeypatch):
    db = _FakeDB()
    s = _store(tmp_path, db, max_file_bytes=10)

    async def _no_quota():
        return None

    monkeypatch.setattr(s, "_assert_quota", _no_quota)
    with pytest.raises(FileStoreError) as e:
        await s.ingest_bytes(b"x" * 50, name="big.txt", origin="upload-panel")
    assert e.value.code == "file_too_large"


@pytest.mark.asyncio
async def test_bad_origin_and_retention(store):
    with pytest.raises(FileStoreError):
        await store.ingest_bytes(b"x", name="a.txt", origin="inventada")
    with pytest.raises(FileStoreError):
        await store.ingest_bytes(b"x", name="a.txt", origin="upload-panel", retention="eterna")


@pytest.mark.asyncio
async def test_binary_marks_extraction_pending(store):
    entry = await store.ingest_bytes(b"%PDF-1.4 fake", name="doc.pdf", origin="upload-panel")
    assert entry.mime == "application/pdf"
    assert entry.meta_json["extraction"] == "pending"


# ── leitura fracionada ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_slice_paginates(store):
    content = "".join(f"linha {i}\n" for i in range(1, 51))
    entry = await store.ingest_bytes(content.encode(), name="x.txt", origin="upload-panel")
    first = await store.read_slice(entry.id, start_line=1, max_lines=10)
    assert first["content"].startswith("linha 1\n")
    assert first["end_line"] == 10 and first["has_more"] is True
    assert first["next_start_line"] == 11
    second = await store.read_slice(entry.id, start_line=first["next_start_line"], max_lines=10)
    assert second["content"].startswith("linha 11\n")


@pytest.mark.asyncio
async def test_read_slice_last_page_has_no_more(store):
    entry = await store.ingest_bytes(b"a\nb\nc\n", name="x.txt", origin="upload-panel")
    out = await store.read_slice(entry.id, start_line=1, max_lines=100)
    assert out["has_more"] is False and out["next_start_line"] is None
    assert out["content"] == "a\nb\nc\n"


@pytest.mark.asyncio
async def test_read_slice_respects_char_cap(tmp_path, monkeypatch):
    db = _FakeDB()
    s = _store(tmp_path, db, read_cap_chars=30)

    async def _no_quota():
        return None

    monkeypatch.setattr(s, "_assert_quota", _no_quota)
    content = "".join(f"linha bem comprida numero {i}\n" for i in range(1, 20))
    entry = await s.ingest_bytes(content.encode(), name="x.txt", origin="upload-panel")
    out = await s.read_slice(entry.id, start_line=1, max_lines=100)
    # Parou pelo cap, não por acabar o arquivo.
    assert out["has_more"] is True
    assert len(out["content"]) <= 60


@pytest.mark.asyncio
async def test_read_binary_refuses_with_actionable_message(store):
    entry = await store.ingest_bytes(b"\x89PNG fake", name="img.png", origin="upload-panel")
    with pytest.raises(FileStoreError) as e:
        await store.read_slice(entry.id)
    assert e.value.code == "not_text"
    assert "file_stage" in e.value.message  # diz ao modelo o caminho certo


@pytest.mark.asyncio
async def test_read_missing_file(store):
    with pytest.raises(FileStoreError) as e:
        await store.read_slice("00000000-0000-0000-0000-000000000000")
    assert e.value.code == "not_found"


# ── path-jail / symlink (§11) ───────────────────────────────────────────────

def test_resolve_workspace_path_rejects_escape(store):
    for bad in ["../etc/passwd", "/etc/passwd", "/workspace/../secret", "a/../../b"]:
        with pytest.raises(FileStoreError):
            store.resolve_workspace_path(bad)


def test_resolve_workspace_path_accepts_inside(store, tmp_path):
    ws = tmp_path / "workspace"
    (ws / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "sub" / "f.txt").write_text("oi")
    resolved = store.resolve_workspace_path("sub/f.txt")
    assert resolved == (ws / "sub" / "f.txt").resolve()


def test_resolve_workspace_path_blocks_symlink_escape(store, tmp_path):
    """O sandbox pode plantar um symlink pra /etc — o core não pode segui-lo."""
    ws = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("segredo do host")
    link = ws / "evil.txt"
    os.symlink(outside, link)
    with pytest.raises(FileStoreError) as e:
        store.resolve_workspace_path("evil.txt")
    assert e.value.code == "symlink_escape"


@pytest.mark.asyncio
async def test_harvest_from_workspace(store, tmp_path):
    ws = tmp_path / "workspace"
    (ws / "out").mkdir(parents=True, exist_ok=True)
    (ws / "out" / "resultado.csv").write_text("a,b\n1,2\n")
    entry = await store.harvest("out/resultado.csv", retention="perm")
    assert entry.name == "resultado.csv"
    assert entry.origin == "workbench" and entry.retention == "perm"
    assert entry.meta_json["source"]["workspace_path"] == "out/resultado.csv"
    assert store.blob_path(entry.sha256).exists()


@pytest.mark.asyncio
async def test_harvest_missing_file(store):
    with pytest.raises(FileStoreError) as e:
        await store.harvest("nao/existe.csv")
    assert e.value.code == "not_found"


# ── retenção ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_retention_toggles_expiry(store):
    entry = await store.ingest_bytes(b"x", name="a.txt", origin="upload-panel")
    assert entry.expires_at is not None
    perm = await store.set_retention(entry.id, "perm")
    assert perm.retention == "perm" and perm.expires_at is None
    temp = await store.set_retention(entry.id, "temp")
    assert temp.retention == "temp" and temp.expires_at is not None


# ── manifesto (o que entra no prompt) ───────────────────────────────────────

def test_manifest_line_is_compact_and_has_id():
    entry = StoredFile(
        name="relatorio.pdf", mime="application/pdf", size_bytes=1_258_291,
        sha256="a" * 64, origin="upload-desktop", retention="temp",
        meta_json={"pages": 34},
    )
    line = FileStore.manifest_line(entry)
    assert "relatorio.pdf" in line and "1.2 MB" in line and "34 págs" in line
    assert str(entry.id) in line
    assert len(line) < 200  # é uma FICHA, não o arquivo
