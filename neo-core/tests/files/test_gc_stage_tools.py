"""GC (expiração + refcount do blob), Stager do workbench e tools file_*.

O fake de db aqui é "scriptado": cada `execute` consome o próximo resultado da
fila, porque o que se testa é o COMPORTAMENTO do gc/refcount (marca expirado,
apaga blob órfão, poupa blob ainda referenciado), não o SQL em si.
Ver docs/file-store-plan.md §7 e §4.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core.files.executor import FilesExecutor
from core.files.stagers import WorkbenchStager
from core.files.store import FileStore
from db.models.stored_file import StoredFile


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _ScriptedSession:
    def __init__(self, db) -> None:
        self._db = db

    def add(self, entry) -> None:
        self._db.rows[entry.id] = entry
        self._db.added.append(entry)

    async def commit(self) -> None:
        self._db.commits += 1

    async def refresh(self, entry) -> None:
        return None

    async def get(self, model, pk):
        return self._db.rows.get(pk)

    async def execute(self, stmt):
        if self._db.results:
            return _Result(self._db.results.popleft())
        return _Result([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ScriptedDB:
    def __init__(self, results=None) -> None:
        self.rows: dict = {}
        self.added: list = []
        self.commits = 0
        self.results = deque(results or [])

    def get_session(self):
        return _ScriptedSession(self)


def _entry(sha: str = "a" * 64, **kw) -> StoredFile:
    base = dict(
        name="x.txt", mime="text/plain", size_bytes=3, sha256=sha,
        origin="upload-panel", retention="temp", status="available",
        expires_at=datetime.utcnow() - timedelta(days=1),
    )
    base.update(kw)
    return StoredFile(**base)


def _store(tmp_path: Path, db) -> FileStore:
    s = FileStore(
        db=db, root=str(tmp_path / "store"), workspace_root=str(tmp_path / "ws"),
        max_file_bytes=1024, quota_bytes=1024 * 1024, read_cap_chars=500, ttl_days=7,
    )
    s.ensure_dirs()
    return s


def _make_blob(store: FileStore, sha: str) -> Path:
    blob = store.blob_path(sha)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(b"abc")
    return blob


# ── GC ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gc_expires_stale_and_unlinks_orphan_blob(tmp_path):
    sha = "b" * 64
    stale = _entry(sha=sha)
    # 1ª query: os vencidos. 2ª (refcount): ninguém mais usa o sha → órfão.
    db = _ScriptedDB([[stale], []])
    store = _store(tmp_path, db)
    blob = _make_blob(store, sha)

    result = await store.gc_pass()

    assert result == {"expired": 1, "blobs_removed": 1}
    assert stale.status == "expired"   # linha sobrevive, marcada
    assert not blob.exists()           # blob some


@pytest.mark.asyncio
async def test_gc_keeps_blob_still_referenced(tmp_path):
    """Mesmo conteúdo ingerido 2x: expirar UMA linha não pode apagar o blob."""
    sha = "c" * 64
    stale = _entry(sha=sha)
    survivor = _entry(sha=sha, retention="perm", expires_at=None)
    db = _ScriptedDB([[stale], [survivor]])  # refcount encontra o sobrevivente
    store = _store(tmp_path, db)
    blob = _make_blob(store, sha)

    result = await store.gc_pass()

    assert result == {"expired": 1, "blobs_removed": 0}
    assert blob.exists()  # dedup respeitado


@pytest.mark.asyncio
async def test_gc_noop_when_nothing_expired(tmp_path):
    db = _ScriptedDB([[]])
    store = _store(tmp_path, db)
    assert await store.gc_pass() == {"expired": 0, "blobs_removed": 0}


@pytest.mark.asyncio
async def test_delete_soft_deletes_and_unlinks(tmp_path):
    sha = "d" * 64
    entry = _entry(sha=sha)
    db = _ScriptedDB([[]])          # refcount: nenhum available
    db.rows[entry.id] = entry
    store = _store(tmp_path, db)
    blob = _make_blob(store, sha)

    assert await store.delete(entry.id) is True
    assert entry.status == "deleted"  # soft — a linha fica p/ auditoria
    assert not blob.exists()


# ── Stager ──────────────────────────────────────────────────────────────────

class _FakeSandboxResult:
    def __init__(self, exit_code=0, stdout="", stderr="") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandbox:
    workspace_root = "/workspace"

    def __init__(self, exit_code=0, stderr="") -> None:
        self.commands: list[str] = []
        self._exit = exit_code
        self._stderr = stderr

    async def run(self, command, **kw):
        self.commands.append(command)
        return _FakeSandboxResult(self._exit, stderr=self._stderr)


@pytest.mark.asyncio
async def test_workbench_stager_copies_from_readonly_mount(tmp_path):
    sha = "e" * 64
    entry = _entry(sha=sha, name="dados.csv")
    sandbox = _FakeSandbox()
    stager = WorkbenchStager(sandbox, store_mount="/files")

    ref = await stager.stage(entry, Path("/irrelevante"), name="dados.csv")

    assert ref["path"] == "/workspace/files/dados.csv"
    cmd = sandbox.commands[0]
    # Copia do mount RO do store (ótica do sandbox), não de um path do core.
    assert f"/files/blobs/{sha[:2]}/{sha}" in cmd
    assert "mkdir -p" in cmd and "cp " in cmd


@pytest.mark.asyncio
async def test_workbench_stager_raises_on_failure(tmp_path):
    entry = _entry(sha="f" * 64)
    stager = WorkbenchStager(_FakeSandbox(exit_code=1, stderr="No such file"))
    with pytest.raises(RuntimeError, match="No such file"):
        await stager.stage(entry, Path("/x"), name="a.txt")


@pytest.mark.asyncio
async def test_stage_unknown_target_lists_available(tmp_path):
    entry = _entry(sha="a" * 64)
    db = _ScriptedDB()
    db.rows[entry.id] = entry
    store = _store(tmp_path, db)
    store._stagers = {"workbench": WorkbenchStager(_FakeSandbox())}
    from core.files.store import FileStoreError

    with pytest.raises(FileStoreError) as e:
        await store.stage(entry.id, target="browser-agent")
    assert e.value.code == "unknown_target"
    assert "workbench" in e.value.message


# ── Tools (FilesExecutor) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_info_hints_stage_for_binary(tmp_path):
    entry = _entry(sha="a" * 64, name="doc.pdf", mime="application/pdf",
                   meta_json={"extraction": "pending"})
    db = _ScriptedDB()
    db.rows[entry.id] = entry
    executor = FilesExecutor(_store(tmp_path, db))

    out = await executor.execute("file_info", {"file_id": str(entry.id)})

    assert out["mime"] == "application/pdf"
    assert "file_stage" in out["note"]  # empurra o modelo pro caminho certo


@pytest.mark.asyncio
async def test_executor_turns_store_error_into_message(tmp_path):
    db = _ScriptedDB()
    executor = FilesExecutor(_store(tmp_path, db))
    out = await executor.execute("file_read", {"file_id": "00000000-0000-0000-0000-000000000000"})
    assert out["error"] == "not_found"
    assert "message" in out  # acionável, sem stack


@pytest.mark.asyncio
async def test_executor_info_not_found(tmp_path):
    db = _ScriptedDB()
    executor = FilesExecutor(_store(tmp_path, db))
    out = await executor.execute("file_info", {"file_id": "00000000-0000-0000-0000-000000000000"})
    assert out["error"] == "not_found"


@pytest.mark.asyncio
async def test_executor_unknown_tool_raises(tmp_path):
    executor = FilesExecutor(_store(tmp_path, _ScriptedDB()))
    with pytest.raises(ValueError, match="Unknown files tool"):
        await executor.execute("file_teleport", {})
