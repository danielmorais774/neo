"""File Store API — upload/download/catálogo (docs/file-store-plan.md §8).

Padrão research.py/missions.py: APIRouter + set_store() no bootstrap.

**Auth (§14.1, revisto em 2026-09-25 — docs/api-auth-plan.md).** A regra antiga
era "espelhar as rotas do desktop", e as do desktop não tinham guarda nenhuma.
Agora têm, e estas seguem junto:

* ``POST ""`` (upload) — ``require_owner``; o dono do arquivo sai da credencial,
  não do campo ``user_id`` do multipart;
* ``GET {id}/download`` — ``require_owner`` **ou** URL assinada. A assinatura
  existe porque o painel baixa por ``<a href download>``, e navegação de âncora
  não manda header nenhum: fechar só com header quebraria o download (§7.3). Quem
  pede o link já está autenticado (``GET {id}/download-url``);
* gestão do catálogo (list/info/patch/delete/usage) — segue no panel JWT, que é
  tela de administração.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from api.auth import Principal, owner_of, require_owner
from api.auth import signed_url
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
    request: Request,
    file: UploadFile = File(...),
    retention: str = Form("temp"),
    origin: str = Form("upload-desktop"),
    session_id: str | None = Form(None),
    user_id: str | None = Form(None),
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Ingere um arquivo (multipart). Devolve o file_id para citar no chat."""
    store = _require_store()
    dono = owner_of(principal, _as_uuid(user_id), request=request)
    try:
        entry = await store.ingest_stream(
            file.file,
            name=file.filename or "arquivo",
            mime=file.content_type,
            origin=origin,
            retention=retention,
            user_id=dono,
            session_id=_as_uuid(session_id),
        )
    except FileStoreError as exc:
        status = 413 if exc.code in ("file_too_large", "quota_exceeded") else 400
        raise HTTPException(status_code=status, detail={"error": exc.code, "message": exc.message})
    return _serialize(entry)


@router.get("/{file_id}/download-url")
async def download_url(
    file_id: str,
    request: Request,
    principal: Principal = Depends(require_owner),
) -> dict[str, Any]:
    """Devolve um link de download de vida curta, para usar em ``<a download>``.

    Quem pede está autenticado; o link carrega a autorização na assinatura
    (HMAC sobre ``arquivo|dono|prazo``), porque a âncora não manda header.
    """
    store = _require_store()
    entry = await store.get(file_id)
    if entry is None or entry.status != "available":
        raise HTTPException(status_code=404, detail="arquivo não encontrado ou indisponível")
    _confere_dono_do_arquivo(entry, principal, request)

    params = signed_url.sign(f"file:{file_id}", principal.user_id or "")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return {
        "url": f"/api/v1/files/{file_id}/download?{query}",
        "expires_at": int(params["exp"]),
    }


def _confere_dono_do_arquivo(entry: Any, principal: Principal, request: Request) -> None:
    """Arquivo sem dono passa (legado e arquivo gerado pelo servidor).

    Mesma escolha das sessões antigas: a coluna é anulável e há linha anterior à
    auth. Endurecer isso sem backfill esconderia anexo do próprio dono.
    """
    dono = getattr(entry, "user_id", None)
    if dono is None or principal.user_id is None or dono == principal.user_id:
        return

    from api.auth.principal import MODE_OFF, MODE_SHADOW, auth_mode

    if auth_mode() in (MODE_OFF, MODE_SHADOW):
        logger.warning(
            "[auth:sombra] %s: arquivo %s é do usuário %s, não de %s",
            request.url.path, getattr(entry, "id", "?"), dono, principal.user_id,
        )
        return
    raise HTTPException(status_code=403, detail="este arquivo não é seu")


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    request: Request,
    exp: str | None = Query(None),
    uid: str | None = Query(None),
    sig: str | None = Query(None),
) -> FileResponse:
    """Baixa o conteúdo. ``attachment`` sempre — nunca renderizar HTML hostil
    no domínio do painel (§11.7).

    Aceita DUAS credenciais: a normal (header do aparelho / access token) ou a
    URL assinada emitida por ``/download-url``. A assinatura cobre o file_id, de
    modo que um link não serve para outro arquivo.
    """
    from api.auth.principal import resolve_principal

    principal = await resolve_principal(request)
    if not principal.tem_dono:
        try:
            dono_assinado = signed_url.verify(f"file:{file_id}", exp=exp, uid=uid, sig=sig)
        except signed_url.SignatureError as erro:
            from api.auth.principal import MODE_OFF, MODE_SHADOW, auth_mode

            if auth_mode() in (MODE_OFF, MODE_SHADOW):
                logger.warning(
                    "[auth:sombra] GET %s passaria a ser barrado: %s",
                    request.url.path, erro,
                )
                dono_assinado = None
            else:
                raise HTTPException(status_code=401, detail=f"download não autorizado: {erro}")
        if dono_assinado is not None:
            principal = Principal(kind="user", user_id=dono_assinado, label="link assinado")

    store = _require_store()
    entry = await store.get(file_id)
    if entry is None or entry.status != "available":
        raise HTTPException(status_code=404, detail="arquivo não encontrado ou indisponível")
    _confere_dono_do_arquivo(entry, principal, request)
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
