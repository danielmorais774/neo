"""O card da Solicitação de Liberação — notificação nos aparelhos do dono.

Separado do :mod:`core.release.service` de propósito: o serviço decide REGRAS,
este arquivo conhece notificação e WebSocket. O serviço recebe este notificador
injetado, então trocar o transporte não mexe na regra.

Reusa exatamente o caminho que a missão do WhatsApp já usa
(``core/skills/whatsapp_messaging/executor.py:_request_confirmation``):
``user_notification`` + ``DeviceNotifBroadcaster``, com ``actions`` que o desktop
desenha como botões. A aprovação volta por HTTP autenticado
(``api/routes/release.py``) — **nunca** por tool, porque quem aprova não pode ser
quem pediu.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from db.models.release_request import ReleaseRequest

logger = logging.getLogger(__name__)

_ACTIONS = [
    {"action_type": "liberar", "label": "Liberar só isto", "button_style": "contained"},
    {"action_type": "recusar", "label": "Não liberar", "button_style": "outlined"},
]

_CONFIG = {"persistent": True, "position": "bottom_right", "play_sound": True}


def _titulo(row: ReleaseRequest) -> str:
    campos = ", ".join(_rotulo_curto(c) for c in (row.campos or [])) or "um dado"
    return f"Liberar {campos}?"


def _rotulo_curto(campo: str) -> str:
    """``pks:endereco.cep`` → ``endereco.cep`` (o prefixo é ruído no card)."""
    return str(campo).split(":", 1)[-1] if ":" in str(campo) else str(campo)


def _corpo(row: ReleaseRequest, preview: str = "") -> str:
    linhas: list[str] = []
    campos = ", ".join(_rotulo_curto(c) for c in (row.campos or [])) or "—"
    linhas.append(f"Dado: {campos}")
    linhas.append(f"Destino: {row.destino} ({row.destino_tipo})")
    if row.finalidade:
        linhas.append(f"Para: {row.finalidade}")
    # A linha que desarma o ataque: de quem partiu o pedido.
    if row.origem == "externo":
        quem = row.origem_ref or "um terceiro"
        linhas.append(f"⚠️ Este pedido veio de {quem}, não de você.")
    elif row.origem == "agenda":
        linhas.append("⚠️ Pedido de uma tarefa agendada, sem ninguém na frente.")
    if row.risco_motivos:
        linhas.append(f"Motivo: {row.risco_motivos[0]}")
    # O que você está aprovando, quando o que sai é uma MENSAGEM. Sem esta
    # linha o card pede aprovação de um texto invisível.
    if preview:
        linhas.append(f"\nTexto que vai sair: “{preview.strip()[:600]}”")
    if row.origem_evidencia:
        trecho = row.origem_evidencia.strip().replace("\n", " ")
        linhas.append(f"\nTexto original: “{trecho[:300]}”")
    return "\n".join(linhas)


async def _push(db: Any, user_id: UUID, mensagem: dict) -> int:
    """Empurra a mensagem para todos os aparelhos vivos do dono. Nunca levanta."""
    try:
        from core.notifications.device_notif_broadcaster import (
            get_device_notif_broadcaster,
        )

        broadcaster = get_device_notif_broadcaster()
        devices = await db.get_connected_devices(user_id)
        enviados: set[str] = set()
        for device in devices:
            for chave in (device.device_id, device.ws_connection_id):
                if chave and chave not in enviados:
                    if await broadcaster.send_notification(chave, mensagem):
                        enviados.add(chave)
        return len(enviados)
    except Exception:  # noqa: BLE001 — sem WS a linha ainda está no banco
        logger.exception("[release] falha ao empurrar notificação")
        return 0


def build_decision_announcer(db: Any) -> Any:
    """Devolve ``async (row) -> None``: a decisão aparece no chat da sessão.

    Por que uma bolha e não um turno: disparar turno de dentro de um endpoint de
    aprovação precisa de guarda contra laço (uma liberação aprovada que pede
    outra liberação) e de transporte para a resposta. Isso é passo próprio; a
    bolha já deixa a decisão VISÍVEL onde o trabalho estava (reusa o
    ``kind="chat_message"`` que o dispatcher de jobs criou).
    """

    async def announce(row: ReleaseRequest) -> None:
        campos = ", ".join(_rotulo_curto(c) for c in (row.campos or [])) or "o dado"
        if row.status == "aprovada":
            texto = (
                f"🔐 Liberei {campos} para {row.destino}"
                + (f" ({row.finalidade})" if row.finalidade else "")
                + ". Pode seguir de onde parou."
            )
        else:
            texto = f"🔐 Não liberei {campos} para {row.destino}."
        payload = {
            "release_id": str(row.id),
            "session_id": str(row.session_id) if row.session_id else "",
            "text": texto,
            "status": row.status,
        }
        try:
            notif = await db.create_notification(
                user_id=row.user_id,
                mission_id=None,
                kind="chat_message",
                title="Liberação",
                body=texto,
                payload=payload,
            )
            await _push(
                db,
                row.user_id,
                {
                    "id": str(notif.id),
                    "user_id": str(row.user_id),
                    "kind": "chat_message",
                    "title": "Liberação",
                    "body": texto,
                    "description": texto,
                    "payload": payload,
                    "created_at_unix": int(notif.created_at.timestamp()) if notif.created_at else 0,
                },
            )
        except Exception:  # noqa: BLE001 — o aviso é conveniência, não a decisão
            logger.exception("[release] falha ao anunciar decisão de %s", row.id)

    return announce


def build_text_announcer(db: Any) -> Any:
    """``async (row, texto) -> None``: publica o resultado da retomada no chat."""

    async def announce(row: ReleaseRequest, texto: str) -> None:
        corpo = (texto or "").strip()
        if not corpo:
            return
        payload = {
            "release_id": str(row.id),
            "session_id": str(row.session_id) if row.session_id else "",
            "text": corpo,
        }
        try:
            notif = await db.create_notification(
                user_id=row.user_id,
                mission_id=None,
                kind="chat_message",
                title="Continuação",
                body=corpo,
                payload=payload,
            )
            await _push(
                db,
                row.user_id,
                {
                    "id": str(notif.id),
                    "user_id": str(row.user_id),
                    "kind": "chat_message",
                    "title": "Continuação",
                    "body": corpo,
                    "description": corpo,
                    "payload": payload,
                    "created_at_unix": int(notif.created_at.timestamp()) if notif.created_at else 0,
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("[release] falha ao publicar continuação de %s", row.id)

    return announce


def build_release_notifier(db: Any) -> Any:
    """Devolve o callable ``async (user_id, row) -> notification_id | None``."""

    async def notify(user_id: UUID, row: ReleaseRequest, preview: str = "") -> UUID | None:
        titulo, corpo = _titulo(row), _corpo(row, preview)
        payload = {
            "release_id": str(row.id),
            "campos": list(row.campos or []),
            "destino": row.destino,
            "destino_tipo": row.destino_tipo,
            "finalidade": row.finalidade,
            "origem": row.origem,
            "origem_ref": row.origem_ref,
            "evidencia": (row.origem_evidencia or "")[:300],
            "risco": row.risco,
            "preview": (preview or "")[:600],
            "expira_em": row.expires_at.isoformat() if row.expires_at else None,
            "actions": _ACTIONS,
            "config": _CONFIG,
        }
        notif = await db.create_notification(
            user_id=user_id,
            mission_id=None,
            kind="release_request",
            title=titulo,
            body=corpo,
            payload=payload,
        )
        mensagem = {
            "id": str(notif.id),
            "user_id": str(user_id),
            "kind": "release_request",
            "title": titulo,
            "body": corpo,
            "description": corpo,
            "payload": payload,
            "actions": _ACTIONS,
            "config": _CONFIG,
            "created_at_unix": int(notif.created_at.timestamp()) if notif.created_at else 0,
        }
        enviados = await _push(db, user_id, mensagem)
        logger.info(
            "[release] card do pedido %s entregue a %d aparelho(s)", row.id, enviados
        )
        return notif.id

    return notify
