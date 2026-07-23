"""File Store API — upload/download/catálogo (docs/file-store-plan.md §8).

Padrão research.py/missions.py: APIRouter + set_store() no bootstrap.

**Auth (§14.1 resolvido):** as rotas do desktop hoje NÃO têm guarda
(`app.include_router(desktop.router, prefix="/api/v1")` — sem require_panel_jwt),
e as do panel usam JWT. Replicamos isso em vez de inventar esquema novo: upload
e download ficam abertos (o desktop precisa deles e é o mesmo nível de exposição
do chat, que já é aberto); a GESTÃO do catálogo (list/info/patch/delete/usage)
exige panel JWT. Se um dia as rotas do desktop ganharem guarda, estas seguem
junto — o ponto é não abrir MENOS nem MAIS que o cliente que já existe.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from api.security import require_panel_jwt
from core.files.store import FileStore, FileStoreError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/files", tags=["files"])

_store: FileStore | None = None


def set_store(store: FileStore | None) -> None:
    global _store
    _store = store


def _require_store() -> FileStore:
    if _store is None:
        # Flag off → a feature não existe para o cliente (§12: rollback = flag off).
        raise HTTPException(status_code=503, detail="File Store desabilitado (NEO_FILES_ENABLED)")
    return _store


def _as_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _serialize(entry: Any) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "name": entry.name,
        "mime": entry.mime,
        "size_bytes": entry.size_bytes,
        "sha256": entry.sha256,
        "origin": entry.origin,
        "retention": entry.retention,
        "status": entry.status,
        "session_id": str(entry.session_id) if entry.session_id else None,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        "created_at": entry.created_at.isoformat(),
        "last_accessed_at": entry.last_accessed_at.isoformat() if entry.last_accessed_at else None,
        "access_count": entry.access_count,
        "meta": entry.meta_json or {},
    }


# ── Upload / download: mesmo nível de auth das rotas do desktop (sem guarda) ──


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    retention: str = Form("temp"),
    origin: str = Form("upload-desktop"),
    session_id: str | None = Form(None),
    user_id: str | None = Form(None),
) -> dict[str, Any]:
    """Ingere um arquivo (multipart). Devolve o file_id para citar no chat."""
    store = _require_store()
    try:
        entry = await store.ingest_stream(
            file.file,
            name=file.filename or "arquivo",
            mime=file.content_type,
            origin=origin,
            retention=retention,
            user_id=_as_uuid(user_id),
            session_id=_as_uuid(session_id),
        )
    except FileStoreError as exc:
        status = 413 if exc.code in ("file_too_large", "quota_exceeded") else 400
        raise HTTPException(status_code=status, detail={"error": exc.code, "message": exc.message})
    return _serialize(entry)


@router.get("/{file_id}/download")
async def download_file(file_id: str) -> FileResponse:
    """Baixa o conteúdo. ``attachment`` sempre — nunca renderizar HTML hostil
    no domínio do painel (§11.7)."""
    store = _require_store()
    entry = await store.get(file_id)
    if entry is None or entry.status != "available":
        raise HTTPException(status_code=404, detail="arquivo não encontrado ou indisponível")
    blob = store.blob_path(entry.sha256)
    if not blob.exists():
        raise HTTPException(status_code=410, detail="conteúdo já removido do store")
    return FileResponse(
        path=str(blob),
        media_type="application/octet-stream",
        filename=entry.name,
        headers={"Content-Disposition": f'attachment; filename="{entry.name}"'},
    )


# ── Gestão do catálogo: panel JWT ────────────────────────────────────────────


@router.get("", dependencies=[Depends(require_panel_jwt)])
async def list_files(
    retention: str | None = Query(None),
    origin: str | None = Query(None),
    status: str | None = Query("available"),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    store = _require_store()
    entries = await store.list_files(
        retention=retention, origin=origin, status=status, q=q, limit=limit
    )
    return {"files": [_serialize(e) for e in entries], "count": len(entries)}


@router.get("/usage", dependencies=[Depends(require_panel_jwt)])
async def usage() -> dict[str, Any]:
    return await _require_store().usage()


@router.get("/{file_id}", dependencies=[Depends(require_panel_jwt)])
async def get_file(file_id: str) -> dict[str, Any]:
    entry = await _require_store().get(file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    return _serialize(entry)


@router.get("/{file_id}/preview", dependencies=[Depends(require_panel_jwt)])
async def preview_file(file_id: str, max_lines: int = Query(200, ge=1, le=2000)) -> PlainTextResponse:
    store = _require_store()
    try:
        payload = await store.read_slice(file_id, start_line=1, max_lines=max_lines)
    except FileStoreError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})
    return PlainTextResponse(payload["content"])


@router.patch("/{file_id}", dependencies=[Depends(require_panel_jwt)])
async def patch_file(file_id: str, body: dict[str, Any]) -> dict[str, Any]:
    store = _require_store()
    retention = body.get("retention")
    if not retention:
        raise HTTPException(status_code=400, detail="nada para atualizar (retention)")
    try:
        entry = await store.set_retention(file_id, str(retention))
    except FileStoreError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": exc.message})
    return _serialize(entry)


@router.delete("/{file_id}", dependencies=[Depends(require_panel_jwt)])
async def delete_file(file_id: str) -> dict[str, Any]:
    ok = await _require_store().delete(file_id)
    if not ok:
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    return {"deleted": True, "id": file_id}
