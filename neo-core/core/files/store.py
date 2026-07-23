"""FileStore — entrada, catálogo e leitura de arquivos do Neo.

Ver docs/file-store-plan.md §3.3. Responsabilidades: ingerir bytes de qualquer
canal, deduplicar por sha256, catalogar em ``stored_file``, servir leitura
FRACIONADA (o conteúdo nunca entra inteiro no contexto), materializar o arquivo
onde ele vai ser usado (``stage``), colher o que o Neo produziu (``harvest``) e
recolher o lixo (``gc_pass``).

Guardas de path (§11): tudo é resolvido e preso à raiz do store; a leitura do
``/workspace`` (montado ro no core) usa realpath + checagem de prefixo +
``O_NOFOLLOW`` — o sandbox pode criar symlink, e o core não pode segui-lo para
fora do mount.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import uuid as uuid_mod
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Iterable
from uuid import UUID

from sqlalchemy import text as sa_text
from sqlmodel import select

from db.models.stored_file import FILE_ORIGINS, RETENTIONS, StoredFile

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1024 * 1024  # 1 MiB

# Mimes que tratamos como texto na F1 (extração de binário é F2).
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = frozenset({
    "application/json", "application/xml", "application/x-yaml", "application/yaml",
    "application/javascript", "application/x-ndjson", "application/csv",
    "application/sql", "application/toml", "image/svg+xml",
})


class FileStoreError(Exception):
    """Erro de negócio do store (quota, tamanho, path, estado). Mensagem é humana."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def is_text_mime(mime: str) -> bool:
    mime = (mime or "").split(";")[0].strip().lower()
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_EXACT


def safe_name(name: str) -> str:
    """Nome de exibição saneado — NUNCA usado como path de blob (o blob é o sha).

    Serve para o manifesto, o download e o nome do stage.
    """
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = base.lstrip(".") or "arquivo"
    return base[:255]


class FileStore:
    """Catálogo + blobs. Toda I/O pesada roda em thread (não trava o loop)."""

    def __init__(
        self,
        *,
        db: Any,
        root: str = "/data/files",
        workspace_root: str = "/workspace",
        max_file_bytes: int = 50 * 1024 * 1024,
        quota_bytes: int = 2 * 1024 * 1024 * 1024,
        read_cap_chars: int = 8000,
        ttl_days: int = 7,
        stagers: dict[str, Any] | None = None,
    ) -> None:
        self._db = db
        self.root = Path(root)
        self.workspace_root = Path(workspace_root)
        self.max_file_bytes = max_file_bytes
        self.quota_bytes = quota_bytes
        self.read_cap_chars = read_cap_chars
        self.ttl_days = ttl_days
        self._stagers = stagers or {}

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def blobs_root(self) -> Path:
        return self.root / "blobs"

    @property
    def derived_root(self) -> Path:
        return self.root / "derived"

    def blob_path(self, sha256: str) -> Path:
        """Path do blob. ``sha256`` é validado — hex puro não traversa."""
        if not _SHA_RE.match(sha256 or ""):
            raise FileStoreError("bad_sha", "sha256 inválido.")
        return self.blobs_root / sha256[:2] / sha256

    def ensure_dirs(self) -> None:
        self.blobs_root.mkdir(parents=True, exist_ok=True)
        self.derived_root.mkdir(parents=True, exist_ok=True)

    def resolve_workspace_path(self, path: str) -> Path:
        """Resolve ``path`` para dentro do /workspace montado no core (harvest).

        Textualmente preso à raiz E validado por realpath: o sandbox pode ter
        deixado um symlink apontando pra fora — não seguimos.
        """
        raw = (path or "").strip()
        if not raw:
            raise FileStoreError("bad_path", "path vazio.")
        candidate = Path(raw) if raw.startswith("/") else (self.workspace_root / raw)
        # Colapsa .. textualmente antes de tocar o FS.
        parts: list[str] = []
        for seg in str(candidate).split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(seg)
        textual = Path("/" + "/".join(parts))
        root = self.workspace_root
        if textual != root and root not in textual.parents:
            raise FileStoreError("bad_path", f"caminho fora do workspace ({root}): {path!r}")
        # realpath resolve QUALQUER symlink no meio do caminho; se escapou, recusa.
        real = Path(os.path.realpath(textual))
        real_root = Path(os.path.realpath(root))
        if real != real_root and real_root not in real.parents:
            raise FileStoreError(
                "symlink_escape",
                f"caminho resolve para fora do workspace (symlink?): {path!r}",
            )
        return real

    # ------------------------------------------------------------------
    # Ingestão
    # ------------------------------------------------------------------

    async def ingest_bytes(
        self,
        data: bytes,
        *,
        name: str,
        origin: str,
        mime: str | None = None,
        user_id: UUID | None = None,
        session_id: UUID | None = None,
        retention: str = "temp",
        source: dict[str, Any] | None = None,
    ) -> StoredFile:
        """Ingere bytes já em memória (upload pequeno, media do WhatsApp)."""
        return await self._ingest(
            writer=lambda dst: self._write_bytes(dst, data),
            declared_size=len(data),
            name=name, origin=origin, mime=mime, user_id=user_id,
            session_id=session_id, retention=retention, source=source,
        )

    async def ingest_stream(
        self,
        stream: BinaryIO,
        *,
        name: str,
        origin: str,
        mime: str | None = None,
        user_id: UUID | None = None,
        session_id: UUID | None = None,
        retention: str = "temp",
        source: dict[str, Any] | None = None,
    ) -> StoredFile:
        """Ingere de um stream (upload multipart) sem carregar tudo em RAM."""
        return await self._ingest(
            writer=lambda dst: self._write_stream(dst, stream),
            declared_size=None,
            name=name, origin=origin, mime=mime, user_id=user_id,
            session_id=session_id, retention=retention, source=source,
        )

    async def ingest_file(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        origin: str = "workbench",
        mime: str | None = None,
        user_id: UUID | None = None,
        session_id: UUID | None = None,
        retention: str = "temp",
        source: dict[str, Any] | None = None,
    ) -> StoredFile:
        """Ingere um arquivo já em disco (harvest do /workspace)."""
        src = Path(path)
        return await self._ingest(
            writer=lambda dst: self._copy_file(src, dst),
            declared_size=None,
            name=name or src.name, origin=origin, mime=mime, user_id=user_id,
            session_id=session_id, retention=retention, source=source,
        )

    async def _ingest(
        self,
        *,
        writer: Any,
        declared_size: int | None,
        name: str,
        origin: str,
        mime: str | None,
        user_id: UUID | None,
        session_id: UUID | None,
        retention: str,
        source: dict[str, Any] | None,
    ) -> StoredFile:
        if origin not in FILE_ORIGINS:
            raise FileStoreError("bad_origin", f"origin inválida: {origin!r}")
        if retention not in RETENTIONS:
            raise FileStoreError("bad_retention", f"retention inválida: {retention!r}")
        if declared_size is not None and declared_size > self.max_file_bytes:
            raise FileStoreError(
                "file_too_large",
                f"arquivo de {declared_size} bytes excede o limite de {self.max_file_bytes} bytes.",
            )
        await self._assert_quota()

        display = safe_name(name)
        self.ensure_dirs()
        # Escreve num temporário, hasheia no caminho, depois move pro blob final.
        tmp = self.root / f".incoming-{uuid_mod.uuid4().hex}"
        try:
            sha, size = await asyncio.to_thread(writer, tmp)
            if size > self.max_file_bytes:
                raise FileStoreError(
                    "file_too_large",
                    f"arquivo de {size} bytes excede o limite de {self.max_file_bytes} bytes.",
                )
            dest = self.blob_path(sha)
            await asyncio.to_thread(self._promote, tmp, dest)
        finally:
            if tmp.exists():
                await asyncio.to_thread(tmp.unlink, True)

        resolved_mime = mime or mimetypes.guess_type(display)[0] or "application/octet-stream"
        meta = await asyncio.to_thread(self._probe, dest, resolved_mime)
        if source:
            meta["source"] = source

        expires_at = None
        if retention == "temp":
            expires_at = datetime.utcnow() + timedelta(days=self.ttl_days)

        entry = StoredFile(
            user_id=user_id, session_id=session_id, origin=origin, name=display,
            mime=resolved_mime, size_bytes=size, sha256=sha, retention=retention,
            expires_at=expires_at, status="available", meta_json=meta,
        )
        async with self._db.get_session() as session:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        logger.info(
            "[files] ingest %s (%s, %d bytes, origin=%s, retention=%s)",
            entry.id, display, size, origin, retention,
        )
        return entry

    # -- writers (síncronos, rodam em thread) --------------------------------

    @staticmethod
    def _write_bytes(dst: Path, data: bytes) -> tuple[str, int]:
        digest = hashlib.sha256(data).hexdigest()
        dst.write_bytes(data)
        return digest, len(data)

    @staticmethod
    def _write_stream(dst: Path, stream: BinaryIO) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        with open(dst, "wb") as out:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
                out.write(chunk)
        return hasher.hexdigest(), size

    @staticmethod
    def _copy_file(src: Path, dst: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        # O_NOFOLLOW: se o último componente virou symlink, falha em vez de seguir.
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            with os.fdopen(fd, "rb") as inp, open(dst, "wb") as out:
                while True:
                    chunk = inp.read(_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
        except OSError as exc:
            raise FileStoreError("read_failed", f"não consegui ler o arquivo: {exc}") from exc
        return hasher.hexdigest(), size

    @staticmethod
    def _promote(tmp: Path, dest: Path) -> None:
        """Move o temporário para o blob final. Se já existe, é o mesmo conteúdo
        (mesmo sha) — dedup: descarta o novo e mantém o velho."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            tmp.unlink(missing_ok=True)
            return
        shutil.move(str(tmp), str(dest))

    def _probe(self, blob: Path, mime: str) -> dict[str, Any]:
        """Metadados baratos calculados uma vez na ingestão."""
        meta: dict[str, Any] = {}
        if is_text_mime(mime):
            try:
                head = blob.read_bytes()[: 256 * 1024].decode("utf-8", errors="replace")
                meta["preview_head"] = head[:500]
                # line_count exato só p/ arquivo que cabe no head; senão é estimativa.
                if blob.stat().st_size <= 256 * 1024:
                    meta["line_count"] = head.count("\n") + (0 if head.endswith("\n") else 1)
                meta["extraction"] = "ok"  # texto puro não precisa de extração
            except OSError:
                meta["extraction"] = "failed"
        else:
            # Binário: extração de texto (PDF/DOCX/XLSX) chega na F2.
            meta["extraction"] = "pending"
        return meta

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------

    async def get(self, file_id: UUID | str) -> StoredFile | None:
        fid = self._as_uuid(file_id)
        if fid is None:
            return None
        async with self._db.get_session() as session:
            return await session.get(StoredFile, fid)

    async def list_files(
        self,
        *,
        retention: str | None = None,
        origin: str | None = None,
        status: str | None = "available",
        session_id: UUID | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[StoredFile]:
        stmt = select(StoredFile)
        if status:
            stmt = stmt.where(StoredFile.status == status)
        if retention:
            stmt = stmt.where(StoredFile.retention == retention)
        if origin:
            stmt = stmt.where(StoredFile.origin == origin)
        if session_id:
            stmt = stmt.where(StoredFile.session_id == session_id)
        if q:
            stmt = stmt.where(StoredFile.name.ilike(f"%{q}%"))  # type: ignore[attr-defined]
        stmt = stmt.order_by(StoredFile.created_at.desc()).limit(max(1, min(limit, 200)))  # type: ignore[attr-defined]
        async with self._db.get_session() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def read_slice(
        self, file_id: UUID | str, *, start_line: int = 1, max_lines: int = 200
    ) -> dict[str, Any]:
        """Leitura FRACIONADA por linhas, com cap duro de chars por chamada.

        Devolve ``has_more`` + ``next_start_line`` para o modelo paginar só o que
        precisar — o arquivo inteiro nunca entra no contexto.
        """
        entry = await self.get(file_id)
        if entry is None:
            raise FileStoreError("not_found", "arquivo não encontrado.")
        if entry.status != "available":
            raise FileStoreError(
                "not_available",
                f"arquivo está '{entry.status}' (temp expirado ou deletado) — não dá para ler.",
            )
        if not is_text_mime(entry.mime):
            raise FileStoreError(
                "not_text",
                f"'{entry.name}' é {entry.mime}: extração de texto chega na F2. "
                "Use file_stage e processe no sandbox com neo_bash.",
            )
        blob = self.blob_path(entry.sha256)
        start = max(1, int(start_line or 1))
        count = max(1, int(max_lines or 200))
        payload = await asyncio.to_thread(
            self._read_lines, blob, start, count, self.read_cap_chars
        )
        await self._touch(entry.id)
        payload["file_id"] = str(entry.id)
        payload["name"] = entry.name
        return payload

    @staticmethod
    def _read_lines(blob: Path, start: int, count: int, cap_chars: int) -> dict[str, Any]:
        lines: list[str] = []
        chars = 0
        truncated = False
        last_line = start - 1
        with open(blob, "r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh, start=1):
                if idx < start:
                    continue
                if len(lines) >= count:
                    last_line = idx - 1
                    return {
                        "content": "".join(lines),
                        "start_line": start,
                        "end_line": last_line,
                        "has_more": True,
                        "next_start_line": idx,
                        "truncated_by_cap": truncated,
                    }
                if chars + len(line) > cap_chars and lines:
                    # Cap de chars atingido — para aqui e diz onde continuar.
                    return {
                        "content": "".join(lines),
                        "start_line": start,
                        "end_line": idx - 1,
                        "has_more": True,
                        "next_start_line": idx,
                        "truncated_by_cap": True,
                    }
                lines.append(line[:cap_chars])
                chars += len(line)
                last_line = idx
        return {
            "content": "".join(lines),
            "start_line": start,
            "end_line": last_line,
            "has_more": False,
            "next_start_line": None,
            "truncated_by_cap": truncated,
        }

    # ------------------------------------------------------------------
    # Stage / harvest / retenção / delete
    # ------------------------------------------------------------------

    async def stage(
        self, file_id: UUID | str, *, target: str = "workbench", name: str | None = None
    ) -> dict[str, Any]:
        """Materializa o arquivo no ambiente de uso (§4). Strategy por ``target``."""
        entry = await self.get(file_id)
        if entry is None:
            raise FileStoreError("not_found", "arquivo não encontrado.")
        if entry.status != "available":
            raise FileStoreError("not_available", f"arquivo está '{entry.status}'.")
        stager = self._stagers.get(target)
        if stager is None:
            known = ", ".join(sorted(self._stagers)) or "(nenhum)"
            raise FileStoreError("unknown_target", f"target '{target}' não suportado. Disponíveis: {known}")
        ref = await stager.stage(entry, self.blob_path(entry.sha256), name=safe_name(name or entry.name))
        await self._touch(entry.id)
        return {"file_id": str(entry.id), "target": target, **ref}

    async def harvest(
        self,
        workspace_path: str,
        *,
        name: str | None = None,
        retention: str = "perm",
        origin: str = "workbench",
        user_id: UUID | None = None,
        session_id: UUID | None = None,
    ) -> StoredFile:
        """Registra no catálogo um arquivo que o Neo produziu no /workspace."""
        real = self.resolve_workspace_path(workspace_path)
        if not real.is_file():
            raise FileStoreError("not_found", f"não existe arquivo em {workspace_path!r} no workspace.")
        return await self.ingest_file(
            real, name=name or real.name, origin=origin, retention=retention,
            user_id=user_id, session_id=session_id,
            source={"workspace_path": str(workspace_path)},
        )

    async def set_retention(self, file_id: UUID | str, retention: str) -> StoredFile:
        if retention not in RETENTIONS:
            raise FileStoreError("bad_retention", f"retention inválida: {retention!r}")
        entry = await self.get(file_id)
        if entry is None:
            raise FileStoreError("not_found", "arquivo não encontrado.")
        if entry.status != "available":
            raise FileStoreError("not_available", f"arquivo está '{entry.status}'.")
        entry.retention = retention
        entry.expires_at = (
            datetime.utcnow() + timedelta(days=self.ttl_days) if retention == "temp" else None
        )
        async with self._db.get_session() as session:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
        return entry

    async def delete(self, file_id: UUID | str) -> bool:
        """Soft-delete + unlink do blob se ninguém mais aponta pro sha."""
        entry = await self.get(file_id)
        if entry is None:
            return False
        entry.status = "deleted"
        async with self._db.get_session() as session:
            session.add(entry)
            await session.commit()
        await self._unlink_if_orphan(entry.sha256)
        return True

    # ------------------------------------------------------------------
    # GC / quota
    # ------------------------------------------------------------------

    async def gc_pass(self) -> dict[str, int]:
        """Marca temp vencido como ``expired`` e apaga blobs órfãos."""
        now = datetime.utcnow()
        async with self._db.get_session() as session:
            result = await session.execute(
                select(StoredFile).where(
                    StoredFile.status == "available",
                    StoredFile.retention == "temp",
                    StoredFile.expires_at.is_not(None),  # type: ignore[attr-defined]
                    StoredFile.expires_at < now,  # type: ignore[operator]
                )
            )
            stale = list(result.scalars().all())
            for entry in stale:
                entry.status = "expired"
                session.add(entry)
            if stale:
                await session.commit()

        removed = 0
        for sha in {e.sha256 for e in stale}:
            if await self._unlink_if_orphan(sha):
                removed += 1
        if stale:
            logger.info("[files] gc: %d expirados, %d blobs removidos", len(stale), removed)
        return {"expired": len(stale), "blobs_removed": removed}

    async def _unlink_if_orphan(self, sha256: str) -> bool:
        """Apaga o blob (e o derived) se nenhuma linha ``available`` usa o sha."""
        async with self._db.get_session() as session:
            result = await session.execute(
                select(StoredFile).where(
                    StoredFile.sha256 == sha256, StoredFile.status == "available"
                ).limit(1)
            )
            if result.scalars().first() is not None:
                return False  # ainda referenciado por outra ingestão
        try:
            blob = self.blob_path(sha256)
            await asyncio.to_thread(blob.unlink, True)
            derived = self.derived_root / sha256
            if derived.exists():
                await asyncio.to_thread(shutil.rmtree, derived, True)
            return True
        except (OSError, FileStoreError) as exc:
            logger.warning("[files] falha ao remover blob %s: %s", sha256[:12], exc)
            return False

    async def usage(self) -> dict[str, Any]:
        """Uso REAL em disco (soma por sha DISTINTO — dedup não conta 2x)."""
        async with self._db.get_session() as session:
            disk = await session.execute(sa_text(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM ("
                "  SELECT DISTINCT ON (sha256) sha256, size_bytes"
                "  FROM stored_file WHERE status = 'available'"
                ") t"
            ))
            total = int(disk.scalar() or 0)
            counts = await session.execute(sa_text(
                "SELECT retention, COUNT(*) FROM stored_file "
                "WHERE status = 'available' GROUP BY retention"
            ))
            by_retention = {row[0]: int(row[1]) for row in counts.fetchall()}
            expired = await session.execute(sa_text(
                "SELECT COUNT(*) FROM stored_file WHERE status = 'expired'"
            ))
        return {
            "bytes_used": total,
            "quota_bytes": self.quota_bytes,
            "percent": round(100.0 * total / self.quota_bytes, 1) if self.quota_bytes else 0.0,
            "temp": by_retention.get("temp", 0),
            "perm": by_retention.get("perm", 0),
            "expired": int(expired.scalar() or 0),
        }

    async def _assert_quota(self) -> None:
        used = (await self.usage())["bytes_used"]
        if used >= self.quota_bytes:
            raise FileStoreError(
                "quota_exceeded",
                f"quota do store cheia ({used} de {self.quota_bytes} bytes). "
                "Apague ou marque arquivos como temporários no painel.",
            )

    # ------------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------------

    async def _touch(self, file_id: UUID) -> None:
        try:
            async with self._db.get_session() as session:
                entry = await session.get(StoredFile, file_id)
                if entry is not None:
                    entry.last_accessed_at = datetime.utcnow()
                    entry.access_count = (entry.access_count or 0) + 1
                    session.add(entry)
                    await session.commit()
        except Exception as exc:  # noqa: BLE001 — telemetria nunca derruba leitura
            logger.debug("[files] touch falhou: %s", exc)

    @staticmethod
    def _as_uuid(value: Any) -> UUID | None:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def manifest_line(entry: StoredFile) -> str:
        """Linha compacta do manifesto (§5) — é isto que entra no prompt, não o arquivo."""
        size_mb = entry.size_bytes / (1024 * 1024)
        size_h = f"{size_mb:.1f} MB" if size_mb >= 1 else f"{max(1, entry.size_bytes // 1024)} KB"
        extra = ""
        pages = (entry.meta_json or {}).get("pages")
        lines = (entry.meta_json or {}).get("line_count")
        if pages:
            extra = f", {pages} págs"
        elif lines:
            extra = f", {lines} linhas"
        return f"- {entry.name} — {entry.mime}, {size_h}{extra}, id={entry.id}"
