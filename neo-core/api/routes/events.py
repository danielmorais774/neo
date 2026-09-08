"""API do barramento de eventos (docs/playlists-skill-plan.md §7.5).

Duas famílias:

- ``POST /api/v1/eventos/midia`` — o aparelho conta que a faixa observada está
  acabando (ou acabou). **A resposta É o comando**: quando existe inscrição
  armada casando com o evento, ela volta com a próxima faixa e o aparelho toca.
  Um round trip, sem canal de push novo, sem mudança no `.proto`.
- ``/api/v1/eventos/inscricoes`` (painel/desktop) — o que está armado, o
  histórico de entregas e o kill switch.

**Autenticação e blast radius.** O POST vem do desktop, que hoje não carrega JWT
de painel. Em vez de inventar credencial nova, o endpoint se apoia em três
fatos: (a) ele só faz alguma coisa se existir inscrição armada para AQUELE
``device_id``, criada de dentro de uma conversa autenticada; (b) o evento
precisa citar a faixa esperada, que só quem está tocando conhece; (c) o link
devolvido sai sempre de uma linha do cadastro do usuário, NUNCA do payload. O
pior que um device_id vazado faz é adiantar a fila do dono. As rotas de gestão
(listar/encerrar) continuam atrás do JWT do painel.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.security import require_panel_jwt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/eventos", tags=["eventos"])

_registry: Any = None
_router: Any = None
_db: Any = None


def set_dependencies(registry: Any, event_router: Any, db: Any = None) -> None:
    global _registry, _router, _db
    _registry = registry
    _router = event_router
    _db = db


def _require_bus() -> Any:
    if _router is None or _registry is None:
        # Flag off → a feature não existe para o cliente (rollback = flag off).
        raise HTTPException(
            status_code=503,
            detail="Barramento de eventos desligado (NEO_EVENTS_ENABLED=false).",
        )
    return _router


async def _resolve_user(user_id: str | None) -> UUID:
    """Mesma política do devices.py/datasets.py: explícito ganha; senão, o único."""
    if user_id:
        try:
            return UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id inválido")
    if _db is None:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    user_ids = await _db.list_user_ids()
    if len(user_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"{len(user_ids)} usuários cadastrados — informe user_id explicitamente.",
        )
    return user_ids[0]


# ---------------------------------------------------------------------------
# Caminho quente (sem JWT — ver docstring do módulo)
# ---------------------------------------------------------------------------


@router.post("/midia")
async def media_event(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Evento de mídia do aparelho. Devolve a ação a executar (ou ``acao=nada``).

    Corpo:
    ``{device_id, evento, site, tab_id, video_id}`` — ``evento`` é
    ``faixa_terminando`` (o gatilho bom: dá tempo de trocar antes do autoplay) ou
    ``faixa_terminou`` (rede de segurança).
    """
    bus = _require_bus()
    try:
        return await bus.handle(payload or {})
    except Exception as exc:  # noqa: BLE001 — evento nunca derruba o endpoint
        logger.warning("[eventos] falha ao tratar evento: %s", exc)
        return {"acao": "nada", "motivo": f"erro interno: {exc}"}


# ---------------------------------------------------------------------------
# Gestão (painel/desktop)
# ---------------------------------------------------------------------------


@router.get("/inscricoes")
async def list_subscriptions(
    user_id: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
    incluir_encerradas: bool = Query(default=False),
) -> dict[str, Any]:
    """Inscrições vivas. Sem JWT: é o que o desktop chama ao reconectar para
    re-armar o que estava observando antes de reiniciar."""
    _require_bus()
    states = ("armada", "pausada", "encerrada") if incluir_encerradas else ("armada", "pausada")
    user = None
    try:
        user = await _resolve_user(user_id)
    except HTTPException:
        # Vários usuários e nenhum informado: o device_id ainda restringe.
        if not device_id:
            raise
    subs = await _registry.list(user_id=user, device_id=device_id, states=states, limit=50)
    return {
        "total": len(subs),
        "inscricoes": [
            {
                "id": str(s.id),
                "device_id": s.device_id,
                "gatilho": s.handler_ref,
                "canal": s.channel,
                "eventos": s.events,
                "alvo": s.target,
                "faixa_esperada": s.expected_ref,
                "estado": s.state,
                "motivo": s.reason,
                "disparos": s.fires,
                "expira_em": s.expires_at.isoformat() if s.expires_at else None,
                "criada_em": s.created_at.isoformat() if s.created_at else None,
            }
            for s in subs
        ],
    }


@router.get("/entregas", dependencies=[Depends(require_panel_jwt)])
async def list_deliveries(
    user_id: str | None = Query(default=None),
    inscricao: str | None = Query(default=None),
    limite: int = Query(default=30),
) -> dict[str, Any]:
    """Trilha de entrega — o que chegou, o que casou e o que saiu."""
    _require_bus()
    user = await _resolve_user(user_id)
    sub_id = None
    if inscricao:
        try:
            sub_id = UUID(inscricao)
        except ValueError:
            raise HTTPException(status_code=400, detail="inscricao inválida")
    return {"entregas": await _registry.deliveries(user_id=user, subscription_id=sub_id, limit=limite)}


@router.delete("/inscricoes", dependencies=[Depends(require_panel_jwt)])
async def end_subscriptions(
    user_id: str | None = Query(default=None),
    device_id: str | None = Query(default=None),
    gatilho: str | None = Query(default=None),
) -> dict[str, Any]:
    """Kill switch do painel: encerra tudo que casar."""
    _require_bus()
    user = await _resolve_user(user_id)
    count = await _registry.end_many(
        user_id=user, device_id=device_id, handler_ref=gatilho, reason="encerrada pelo painel"
    )
    return {"ok": True, "encerradas": count}
