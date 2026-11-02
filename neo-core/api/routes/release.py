"""Rotas da Solicitação de Liberação (docs/release-request-plan.md §11).

**Quem aprova.** A decisão exige o TOKEN DO APARELHO PAREADO, que o core resolve
para ``(user_id, device_id)`` sem confiar em nada que o cliente declare — a mesma
credencial e a mesma função (``_device_from_token``) que ``/devices/mine`` já usa.
O serviço ainda confere que quem decide é o DONO do pedido.

**Por que o JWT do painel NÃO aprova.** Ele é estático, com ``sub="neo-panel"``,
e não identifica usuário (achado registrado em ``device-control-plan``). Aceitar
o painel aqui seria fingir uma identidade que não existe, e "só eu com meu
usuário" deixaria de ser verdade. O painel LISTA (tela de administração) e
aprova quando houver login real — §11 do plano.

Nenhuma destas rotas é chamável por tool: o modelo cria pedido
(``solicitar_dado``), você decide.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.security import require_panel_jwt
from core.release import ReleaseError, ReleaseView

logger = logging.getLogger(__name__)

#: Rotas autenticadas pelo TOKEN DO APARELHO (desktop, notch, celular).
router = APIRouter(prefix="/api/v1/liberacoes", tags=["liberacoes"])

#: Tela de administração — panel JWT, só leitura.
panel_router = APIRouter(
    prefix="/api/v1/liberacoes", tags=["liberacoes-painel"],
    dependencies=[Depends(require_panel_jwt)],
)

_service: Any = None
_announcer: Any = None
_resumer: Any = None


def set_service(service: Any, announcer: Any = None, resumer: Any = None) -> None:
    global _service, _announcer, _resumer
    _service = service
    _announcer = announcer
    _resumer = resumer


def _require_service() -> Any:
    if _service is None:
        raise HTTPException(status_code=503, detail="Liberações não estão ligadas neste core")
    return _service


async def _device_do_token(token: str):
    """Reusa a resolução de identidade das rotas de device — uma definição só."""
    from api.routes import devices as devices_routes

    return await devices_routes._device_from_token(token)


def _erro(exc: ReleaseError) -> HTTPException:
    status = {
        "nao_encontrado": 404,
        "nao_e_seu": 403,
        "estado_errado": 409,
        "nao_aprovada": 409,
        "conteudo_mudou": 409,
        "fila_cheia": 429,
    }.get(exc.code, 400)
    return HTTPException(status_code=status, detail={"erro": exc.code, "mensagem": exc.message})


# ── aparelho pareado (é quem decide) ─────────────────────────────────────

@router.get("/minhas")
async def minhas_liberacoes(token: str = Query(...)) -> dict:
    """Pendentes do DONO deste token — o que o card está esperando."""
    service = _require_service()
    device = await _device_do_token(token)
    pendentes = await service.list_pending(device.user_id)
    return {
        "total": len(pendentes),
        "liberacoes": [ReleaseView.of(r).to_dict() for r in pendentes],
    }


@router.post("/{release_id}/decidir")
async def decidir(
    release_id: str,
    token: str = Query(...),
    payload: dict = Body(default_factory=dict),
) -> dict:
    """Aprova ou recusa. ``{"aprovar": true|false}``.

    Uma rota só para as duas decisões: o que muda é o corpo, e ter duas rotas
    faria a recusa ser a que alguém esquece de implementar no cliente.
    """
    service = _require_service()
    device = await _device_do_token(token)
    aprovar = bool(payload.get("aprovar"))
    try:
        vista = await service.decide(
            release_id,
            approved=aprovar,
            decided_by=device.user_id,
            device_id=device.device_id,
            source=str(payload.get("origem") or "desktop")[:24],
        )
    except ReleaseError as exc:
        raise _erro(exc) from exc
    logger.warning(
        "[release] %s decidido por %s via %s", release_id, device.device_id, vista.status
    )
    # A decisão aparece no chat onde o trabalho estava parado. Falhar aqui não
    # desfaz a decisão — ela já está gravada.
    row = await service.get(release_id) if (_announcer or _resumer) else None
    if _announcer is not None and row is not None:
        try:
            await _announcer(row)
        except Exception:  # noqa: BLE001
            logger.exception("[release] falha ao anunciar %s", release_id)
    # Retomada (F3c): só a APROVAÇÃO continua alguma coisa; a recusa passa por
    # lá para encerrar a tarefa que esperava por ela. Nunca segura a resposta.
    retomado = False
    if _resumer is not None and row is not None:
        try:
            retomado = bool(await _resumer(row)) and aprovar
        except Exception:  # noqa: BLE001
            logger.exception("[release] falha ao retomar %s", release_id)
    return {"ok": True, "liberacao": vista.to_dict(), "retomado": retomado}


# ── painel (administração, só leitura) ───────────────────────────────────

@panel_router.get("")
async def listar(
    user_id: UUID = Query(...),
    status: str = Query("pendentes", pattern="^(pendentes|todas)$"),
    limit: int = Query(60, ge=1, le=300),
) -> dict:
    """Liberações de um usuário, para a tela de administração.

    Só leitura de propósito: ver não é aprovar — quem aprova é o aparelho
    pareado (ver o docstring do módulo). `status=todas` traz o histórico com as
    decididas, usadas e expiradas; o default segue sendo só as pendentes, que é
    o que uma tela de "o que está me esperando" precisa.

    Cada item vem com o TEXTO DO CARD que chegou no aparelho (`card`), lido da
    notificação — a tela mostra o mesmo que você aprovaria, não uma segunda
    redação do mesmo pedido.
    """
    service = _require_service()
    rows = (
        await service.list_recent(user_id, limit=limit)
        if status == "todas"
        else await service.list_pending(user_id)
    )
    cards = await service.cards_of(rows)
    itens = []
    for row in rows:
        item = ReleaseView.of(row).to_dict()
        item["card"] = cards.get(item["id"])
        itens.append(item)
    return {"total": len(itens), "status": status, "liberacoes": itens}


@panel_router.get("/{release_id}")
async def detalhe(release_id: str) -> dict:
    service = _require_service()
    row = await service.get(release_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Liberação não encontrada")
    cards = await service.cards_of([row])
    vista = ReleaseView.of(row).to_dict()
    vista["card"] = cards.get(vista["id"])
    return {"liberacao": vista}
