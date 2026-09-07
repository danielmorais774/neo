"""Service Gateway API — aba Serviços do neo-panel (F2).

Padrão research.py/files.py: APIRouter + setters no bootstrap; registrado em
api/main.py com ``require_panel_jwt`` (é tela de administração — sempre JWT).

Três recursos:
- ``/connections``  — CRUD das conexões + "testar" (GET de healthcheck real)
- ``/credentials``  — cadastro do cofre Fernet. **O segredo é write-only**: entra
  no POST/PUT e nunca volta no GET (só rótulo, tipo e hint dos últimos 4 chars).
- ``/audit``        — o que o Neo chamou, com que política, status e duração

Nada aqui devolve segredo. O ``credential_ref`` de uma conexão é só um ponteiro
(``env://VAR`` | ``fernet://<id>``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlmodel import select

from core.gateway.ir import GatewayError, ServiceRequestIR

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/gateway", tags=["gateway"])

_gateway: Any = None          # GatewayService
_registry: Any = None         # ConnectionRegistry
_cred_store: Any = None       # CredentialStore | None
_db: Any = None


def set_context(*, gateway: Any = None, registry: Any = None, cred_store: Any = None, db: Any = None) -> None:
    global _gateway, _registry, _cred_store, _db
    _gateway, _registry, _cred_store, _db = gateway, registry, cred_store, db


def _require_gateway() -> Any:
    if _gateway is None or _registry is None:
        raise HTTPException(status_code=503, detail="Service Gateway desabilitado (NEO_GATEWAY_ENABLED)")
    return _gateway


def _require_creds() -> Any:
    if _cred_store is None:
        raise HTTPException(
            status_code=503,
            detail="Cofre de credenciais indisponível: defina NEO_CRED_KEY na env do neo-core "
                   "e reinicie o container.",
        )
    return _cred_store


def _serialize_conn(cfg: Any, *, seed: bool) -> dict[str, Any]:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "provider": cfg.provider,
        "base_url": cfg.base_url,
        "auth_type": cfg.auth_type.value,
        "auth_config": cfg.auth_config,
        "credential_ref": cfg.credential_ref,   # ponteiro, não segredo
        "default_headers": cfg.default_headers,
        "timeout_s": cfg.timeout_s,
        "retry_max": cfg.retry_max,
        "verify_tls": cfg.verify_tls,
        "allow_http": cfg.allow_http,
        "follow_redirects": cfg.follow_redirects,
        "max_body_bytes": cfg.max_body_bytes,
        "max_response_bytes": cfg.max_response_bytes,
        "default_read": cfg.default_read.value,
        "default_write": cfg.default_write.value,
        "policies": [r.model_dump(mode="json") for r in cfg.policies],
        "docs_url": cfg.docs_url,
        # [F3] A spec volta para o painel poder editá-la. É config (não segredo):
        # o LLM só vê as operações que o service_docs seleciona, nunca isto.
        "openapi_spec": cfg.openapi_spec,
        "openapi_ref": cfg.openapi_ref,
        "capabilities": cfg.capabilities,
        "enabled": cfg.enabled,
        # Seed (vinda do .env) é read-only: o painel esconde os botões.
        "managed_by": "env" if seed else "panel",
        "editable": not seed,
    }


# ── Conexões ────────────────────────────────────────────────────────────────


@router.get("/connections")
async def list_connections() -> dict[str, Any]:
    _require_gateway()
    conns = await _registry.list_all()
    return {
        "connections": [_serialize_conn(c, seed=_registry.is_seed(c.id)) for c in conns],
        "count": len(conns),
    }


@router.get("/connections/{slug}")
async def get_connection(slug: str) -> dict[str, Any]:
    _require_gateway()
    cfg = await _registry.get(slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="conexão não encontrada")
    return _serialize_conn(cfg, seed=_registry.is_seed(slug))


@router.put("/connections/{slug}")
async def upsert_connection(slug: str, body: dict[str, Any]) -> dict[str, Any]:
    _require_gateway()
    payload = {**body, "id": slug}
    try:
        cfg = await _registry.upsert(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _serialize_conn(cfg, seed=False)


@router.delete("/connections/{slug}")
async def delete_connection(slug: str) -> dict[str, Any]:
    _require_gateway()
    try:
        ok = await _registry.delete(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="conexão não encontrada")
    return {"deleted": True, "id": slug}


@router.post("/connections/{slug}/test")
async def test_connection(slug: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Faz um GET real no path de healthcheck e devolve o status cru.

    Passa pelo MESMO pipeline do modelo (validate → policy → broker → executor),
    então testa a credencial e a política de verdade — não um caminho paralelo.
    """
    gateway = _require_gateway()
    cfg = await _registry.get(slug)
    if cfg is None:
        raise HTTPException(status_code=404, detail="conexão não encontrada")
    path = str((body or {}).get("path") or "/")
    result = await gateway.handle_request(
        {"service": slug, "method": "GET", "path": path}, source="panel",
    )
    # Traduz o resultado do gateway para algo que a tela mostra direto.
    if result.get("error"):
        return {"ok": False, "detail": result.get("message"), "error": result["error"]}
    if result.get("denied"):
        return {"ok": False, "detail": f"bloqueado pela política: {result.get('reason')}", "error": "denied"}
    if result.get("pending_confirmation"):
        return {"ok": False, "detail": "GET caiu em confirmação — revise a política de leitura", "error": "confirm"}
    status = result.get("status")
    return {
        "ok": bool(result.get("ok")),
        "status": status,
        "duration_ms": result.get("duration_ms"),
        "body_preview": (result.get("body") or "")[:300],
    }


# ── Credenciais (segredo é write-only) ──────────────────────────────────────


@router.get("/credential-kinds")
async def list_credential_kinds() -> dict[str, Any]:
    """Spec dos tipos: que campos cada um pede além do segredo.

    O painel RENDERIZA o formulário a partir daqui — schema num lugar só. Antes
    o formulário era fixo (um campo "Segredo") e o tipo não mudava nada, então
    "usuário e senha" não tinha onde ser digitado.
    """
    from core.credentials.kinds import CREDENTIAL_KIND_SPECS

    return {"kinds": [spec.to_dict() for spec in CREDENTIAL_KIND_SPECS]}


@router.get("/credentials")
async def list_credentials() -> dict[str, Any]:
    store = _require_creds()
    infos = await store.list()
    return {"credentials": [i.to_dict() for i in infos], "count": len(infos)}


@router.post("/credentials")
async def create_credential(body: dict[str, Any]) -> dict[str, Any]:
    store = _require_creds()
    try:
        info = await store.create(
            label=str(body.get("label") or ""),
            secret=str(body.get("secret") or ""),
            kind=str(body.get("kind") or "token"),
            description=body.get("description"),
            fields=body.get("fields") or {},
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        status = 409 if code == "duplicate_label" else 400  # bad_fields cai em 400
        raise HTTPException(status_code=status, detail=getattr(exc, "message", str(exc)))
    return info.to_dict()


@router.put("/credentials/{credential_id}")
async def update_credential(credential_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Atualiza rótulo/tipo/descrição e, se ``secret`` vier, SUBSTITUI o segredo.
    Não existe leitura do valor antigo — nem aqui, nem em lugar nenhum."""
    store = _require_creds()
    try:
        info = await store.update(
            credential_id,
            secret=body.get("secret") or None,
            label=body.get("label"),
            description=body.get("description"),
            kind=body.get("kind"),
            fields=body.get("fields"),
        )
    except Exception as exc:
        code = getattr(exc, "code", None)
        status = 404 if code == "not_found" else (409 if code == "duplicate_label" else 400)
        raise HTTPException(status_code=status, detail=getattr(exc, "message", str(exc)))
    return info.to_dict()


@router.delete("/credentials/{credential_id}")
async def delete_credential(credential_id: str) -> dict[str, Any]:
    store = _require_creds()
    # Recusa se alguma conexão ainda aponta pra ela — evita quebrar em silêncio.
    if _registry is not None:
        ref = f"fernet://{credential_id}"
        for cfg in await _registry.list_all():
            if cfg.credential_ref == ref:
                raise HTTPException(
                    status_code=409,
                    detail=f"a conexão '{cfg.id}' usa esta credencial — troque-a antes de apagar.",
                )
    ok = await store.delete(credential_id)
    if not ok:
        raise HTTPException(status_code=404, detail="credencial não encontrada")
    return {"deleted": True, "id": credential_id}


# ── Auditoria ───────────────────────────────────────────────────────────────


@router.get("/audit")
async def list_audit(
    connection_id: str | None = Query(None),
    decision: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    if _db is None:
        raise HTTPException(status_code=503, detail="Service Gateway desabilitado")
    from db.models.gateway import ServiceRequestLog

    stmt = select(ServiceRequestLog)
    if connection_id:
        stmt = stmt.where(ServiceRequestLog.connection_id == connection_id)
    if decision:
        stmt = stmt.where(ServiceRequestLog.policy_decision == decision)
    stmt = stmt.order_by(ServiceRequestLog.ts.desc()).limit(limit)  # type: ignore[attr-defined]
    async with _db.get_session() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()
    return {
        "entries": [
            {
                "id": str(r.id),
                "ts": r.ts.isoformat(),
                "source": r.source,
                "connection_id": r.connection_id,
                "method": r.method,
                "path": r.path,
                "policy_decision": r.policy_decision,
                "policy_reason": r.policy_reason,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "error": r.error,
            }
            for r in rows
        ],
        "count": len(rows),
    }
