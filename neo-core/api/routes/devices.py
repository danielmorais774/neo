"""Connected devices routes."""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from pydantic import BaseModel, Field

from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["devices"])          # HTTP — requer panel JWT
ws_router = APIRouter(tags=["devices-ws"])   # WebSocket — desktop, sem auth

_db: AsyncPostgresClient | None = None


def set_db(db: AsyncPostgresClient) -> None:
    global _db
    _db = db


@router.get("/devices")
async def list_connected_devices(user_id: str | None = None) -> dict:
    """Return list of devices from DB, with real-time online status from WS registry.

    Online/offline is determined by merging the persisted device records with the
    WebSocket registry (authoritative when a device has an active connection,
    otherwise the DB value is trusted). Shared with the `list_connected_devices`
    agent tool via `modules.devices.service.list_devices_with_status`.
    """
    from modules.devices.service import list_devices_with_status

    uid_uuid = None
    if user_id:
        try:
            uid_uuid = UUID(user_id)
        except ValueError:
            pass

    devices = await list_devices_with_status(_db, uid_uuid)
    return {"devices": devices}


@router.get("/devices/resolve")
async def resolve_device_by_hardware_id(
    user_id: str,
    hardware_id: str,
) -> dict:
    """Resolve the canonical device_id for a user/hardware_id pair."""
    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")

    try:
        uid_uuid = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id")

    normalized_hardware_id = hardware_id.strip().upper()
    if not normalized_hardware_id:
        raise HTTPException(status_code=400, detail="hardware_id is required")

    device = await _db.get_device_by_hardware_id(uid_uuid, normalized_hardware_id)
    return {
        "device_id": device.device_id if device else None,
        "found": device is not None,
    }


@router.post("/devices/{device_id}/send-notification")
async def send_test_notification(device_id: str) -> dict:
    """Send a test notification to a device via its active WebSocket connection.

    The device must have an active /ws/devices/notifications/{device_id} connection.
    """
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()

    notification = {
        "kind": "test_notification",
        "title": "Test Notification",
        "body": f"This is a test notification sent to device {device_id}",
    }

    sent = await broadcaster.send_notification(device_id, notification)
    if not sent:
        raise HTTPException(status_code=503, detail="Device not connected to notifications WS")

    return {"ok": True, "sent_to": device_id}


@router.post("/debug/test-notification")
async def debug_send_notification(notification: dict) -> dict:
    """Send a full notification payload to a device via WebSocket.

    Accepts the complete notification shape with actions, fields, config, etc.
    Device is identified by 'device_id' in the notification body.
    """
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()

    device_id = notification.get("device_id", "test-device")
    sent = await broadcaster.send_notification(device_id, notification)
    if not sent:
        raise HTTPException(status_code=503, detail="Device not connected to notifications WS")

    return {"ok": True, "sent_to": device_id}


class PairingCodeResponse(BaseModel):
    code: str
    expires_in_s: int


class PairRequest(BaseModel):
    """Pedido de pareamento vindo do device (sem JWT — o código É a autorização)."""

    code: str
    device_id: str
    hardware_id: str = ""
    device_type: str = "neo-desktop"
    model: str = "unknown"
    os_platform: str = "unknown"
    os_version: str = "unknown"
    client_version: str = ""
    display_name: str | None = Field(default=None, max_length=64)
    capabilities: dict | None = None


class PairResponse(BaseModel):
    device_id: str
    device_token: str
    user_id: str
    display_name: str | None


class RenameRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)


@router.post("/devices/pairing-code", response_model=PairingCodeResponse)
async def create_pairing_code(user_id: str | None = None) -> PairingCodeResponse:
    """Gera um código de pareamento de 6 dígitos (chamado pelo painel).

    Esta rota está atrás do JWT do painel — é ela que carrega a autoridade de
    dizer "este device passa a ser do usuário X". O device em si não autentica
    nada; ele só apresenta o código.

    ``user_id`` é opcional: com um único usuário cadastrado (o caso real hoje)
    ele é inferido, porque UUID digitado à mão é convite a parear o device na
    conta errada. Com mais de um, passa a ser obrigatório.
    """
    from modules.devices.pairing import generate_pairing_code

    if user_id:
        try:
            uid_uuid = UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid user_id")
    else:
        if not _db:
            raise HTTPException(status_code=503, detail="DB unavailable")
        user_ids = await _db.list_user_ids()
        if len(user_ids) != 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{len(user_ids)} usuários cadastrados — informe user_id "
                    "explicitamente."
                ),
            )
        uid_uuid = user_ids[0]

    code, ttl = generate_pairing_code(uid_uuid)
    return PairingCodeResponse(code=code, expires_in_s=ttl)


@ws_router.post("/devices/pair", response_model=PairResponse)
async def pair_device(payload: PairRequest) -> PairResponse:
    """Troca um código de pareamento por um token permanente do device.

    Vive no ``ws_router`` (sem JWT do painel) de propósito: quem chama é o
    desktop, que não tem — e nunca teve — credencial. A autorização é o código
    de uso único, emitido por quem TEM credencial.
    """
    from modules.devices.pairing import consume_pairing_code, issue_device_token

    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")

    device_id = payload.device_id.strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    user_id = consume_pairing_code(payload.code)
    if user_id is None:
        raise HTTPException(status_code=403, detail="Código inválido ou expirado")

    token, token_hash = issue_device_token()
    device = await _db.pair_device(
        user_id=user_id,
        device_id=device_id,
        pairing_token_hash=token_hash,
        device_type=payload.device_type,
        model=payload.model,
        hardware_id=payload.hardware_id.strip().upper(),
        os_platform=payload.os_platform,
        os_version=payload.os_version,
        client_version=payload.client_version,
        display_name=payload.display_name,
        capabilities=payload.capabilities,
    )

    logger.info("[pair] device %s pareado com user %s", device_id, user_id)
    # O token cru sai daqui e nunca mais: o banco só tem o hash.
    return PairResponse(
        device_id=device.device_id,
        device_token=token,
        user_id=str(device.user_id),
        display_name=device.display_name,
    )


@router.patch("/devices/{device_id}")
async def rename_device(device_id: str, payload: RenameRequest) -> dict:
    """Dá (ou tira) o nome humano de um device — "PC da sala"."""
    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")

    name = (payload.display_name or "").strip() or None
    device = await _db.set_device_display_name(device_id, name)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return {"device_id": device.device_id, "display_name": device.display_name}


async def _device_from_token(token: str):
    """Resolve o device dono do token, ou 401. Sem confiar em nada do cliente."""
    from modules.devices.pairing import hash_device_token

    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")
    device = await _db.get_device_by_token_hash(hash_device_token(token))
    if device is None:
        raise HTTPException(status_code=401, detail="Token de device inválido")
    return device


@ws_router.get("/devices/mine")
async def list_my_devices(token: str) -> dict:
    """Lista os devices do DONO deste token — a visão que o desktop tem.

    Existe separada de ``GET /devices`` porque aquela está atrás do JWT do
    painel, que o desktop não tem. Aqui a credencial é o token do próprio
    device, e o escopo sai dele: um device só enxerga os irmãos da mesma conta.
    """
    from modules.devices.service import list_devices_with_status

    device = await _device_from_token(token)
    devices = await list_devices_with_status(_db, device.user_id)
    for entry in devices:
        entry["is_current_device"] = entry["device_id"] == device.device_id
    return {"devices": devices}


@ws_router.patch("/devices/mine/{target_device_id}")
async def rename_my_device(target_device_id: str, token: str, payload: RenameRequest) -> dict:
    """Renomeia um device da própria conta, autenticado pelo token do device.

    Deixa o usuário batizar "PC da sala" direto do desktop, sem abrir o painel.
    Só alcança devices do MESMO dono — o token não vira chave da conta alheia.
    """
    device = await _device_from_token(token)
    target = await _db.get_device_by_id(target_device_id)
    if target is None or target.user_id != device.user_id:
        raise HTTPException(status_code=404, detail="Device not found")

    name = (payload.display_name or "").strip() or None
    updated = await _db.set_device_display_name(target_device_id, name)
    return {"device_id": updated.device_id, "display_name": updated.display_name}


async def _remove_device(device_id: str) -> None:
    """Apaga o device e derruba o canal de controle dele.

    A ordem importa: derrubar primeiro deixaria uma janela em que o device
    reconecta (o token ainda vale) antes do DELETE. Apagar primeiro faz a
    reconexão ser recusada com 4401.
    """
    from core.notifications.device_control import get_device_control_broadcaster
    from grpc_server import _devices_ws_registry

    await _db.delete_device(device_id)
    await get_device_control_broadcaster().kick(device_id)
    await _devices_ws_registry.unregister(device_id)
    logger.info("[devices] device removido: %s", device_id)


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str) -> dict:
    """Remove um device (painel). Invalida o token e derruba o canal aberto."""
    if not _db:
        raise HTTPException(status_code=503, detail="DB unavailable")

    device = await _db.get_device_by_id(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    await _remove_device(device_id)
    return {"ok": True, "removed": device_id}


@ws_router.delete("/devices/mine/{target_device_id}")
async def delete_my_device(target_device_id: str, token: str) -> dict:
    """Remove um device da PRÓPRIA conta, autenticado pelo token do device.

    Deixa o usuário tirar um aparelho velho da lista sem abrir o painel. Só
    alcança devices do mesmo dono — o token não vira chave da conta alheia.

    Remover o próprio device é permitido e equivale a DESPAREAR: o token vai
    junto, e o aparelho volta a pedir código.
    """
    device = await _device_from_token(token)
    target = await _db.get_device_by_id(target_device_id)
    if target is None or target.user_id != device.user_id:
        raise HTTPException(status_code=404, detail="Device not found")

    await _remove_device(target_device_id)
    return {
        "ok": True,
        "removed": target_device_id,
        "was_self": target_device_id == device.device_id,
    }


async def _device_ws_authorized(
    websocket: WebSocket, device_id: str, token: str | None
) -> bool:
    """Autoriza um canal ANTIGO (`/online`, `/notifications`) ANTES do handshake.

    Chamar isto antes de ``accept()`` importa: aceitar e só então fechar faz o
    cliente ver um 101 bem-sucedido seguido de um close — indistinguível de uma
    queda de rede, e portanto candidato a retry infinito. Recusando antes, ele
    recebe um erro HTTP e sabe que precisa parear.

    Duas regras diferentes, e a distinção importa (F5, docs/device-control-plan.md §2.4):

    - **token errado → SEMPRE recusa**, com ou sem flag. Credencial inválida
      nunca é "tudo bem"; só a AUSÊNCIA dela é que é negociável.
    - **token ausente** → depende de ``neo_device_auth_required``. Off (padrão):
      aceita e loga WARN, porque esses dois canais nasceram sem auth e virar a
      chave de uma vez derruba o desktop em execução no meio de um save (o core
      roda --reload). On: recusa.

    O canal de CONTROLE não passa por aqui — ele exige token sempre, sem fase
    permissiva, porque é novo e é o único que suspende ou desliga a máquina.
    """
    from shared.config import get_settings

    required = getattr(get_settings(), "neo_device_auth_required", False)

    if not token:
        if required:
            logger.warning("[auth] canal recusado: %s sem token (auth obrigatória)", device_id)
            await websocket.close(code=4401, reason="unpaired")
            return False
        logger.warning(
            "[auth] %s conectou SEM token — aceito pela fase permissiva. "
            "Pareie o dispositivo antes de ligar neo_device_auth_required.",
            device_id,
        )
        return True

    from modules.devices.pairing import hash_device_token

    if not _db:
        await websocket.close(code=1011, reason="DB unavailable")
        return False

    device = await _db.get_device_by_token_hash(hash_device_token(token))
    if device is None or device.device_id != device_id:
        logger.warning("[auth] canal recusado: token inválido para %s", device_id)
        await websocket.close(code=4401, reason="unpaired")
        return False
    return True


@ws_router.websocket("/ws/devices/online")
async def ws_device_online(
    websocket: WebSocket,
    device_id: str = Query(...),
    token: str | None = Query(default=None),
):
    """WebSocket endpoint for device online/offline signaling.

    Connection open = online, disconnection = offline (immediate).
    No heartbeats needed — TCP state is the source of truth.

    ``token`` é o do pareamento — obrigatório só com
    ``neo_device_auth_required`` ligada (ver ``_device_ws_authorized``).
    """
    if not await _device_ws_authorized(websocket, device_id, token):
        return
    await websocket.accept()
    # Late import to avoid circular import issues
    from grpc_server import _devices_ws_registry

    try:
        # Pass the WS connection ID so we can track it in the DB
        await _devices_ws_registry.register(device_id, str(id(websocket)))
        try:
            while True:
                # Wait for any message (or just connection close)
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
    finally:
        await _devices_ws_registry.unregister(device_id)


@ws_router.websocket("/ws/devices/notifications/{device_id}")
async def ws_device_notifications(
    websocket: WebSocket,
    device_id: str,
    token: str | None = Query(default=None),
):
    """WebSocket endpoint for receiving notifications on a device.

    Desktop connects here to receive push notifications from neo-core.
    Uses device_id as the connection key (same as /ws/devices/online).

    Mesma regra de token do ``/online``: recusa token errado sempre, e token
    ausente só quando ``neo_device_auth_required`` está ligada.
    """
    if not await _device_ws_authorized(websocket, device_id, token):
        return
    await websocket.accept()
    from core.notifications.device_notif_broadcaster import get_device_notif_broadcaster

    broadcaster = get_device_notif_broadcaster()
    try:
        async for notification in broadcaster.register(device_id):
            await websocket.send_json(notification)
    except WebSocketDisconnect:
        pass


@ws_router.websocket("/ws/devices/control/{device_id}")
async def ws_device_control(
    websocket: WebSocket,
    device_id: str,
    token: str = Query(...),
):
    """Canal de controle full-duplex de um device (volume, mídia, energia).

    **O token é obrigatório e não tem fase permissiva.** Os canais antigos
    (`/online`, `/notifications`) nasceram sem auth e não podem ser endurecidos
    de um dia para o outro sem derrubar o desktop em execução — mas este é novo,
    não tem cliente antigo para quebrar, e é o único que consegue suspender ou
    desligar a máquina do usuário. Ver docs/device-control-plan.md §2.3.

    O ``device_id`` da URL só vale se o token pertencer a ELE: sem essa
    checagem, um device pareado poderia abrir o canal de outro.
    """
    from core.notifications.device_control import get_device_control_broadcaster
    from modules.devices.pairing import hash_device_token

    if not _db:
        await websocket.close(code=1011, reason="DB unavailable")
        return

    device = await _db.get_device_by_token_hash(hash_device_token(token))
    if device is None or device.device_id != device_id:
        logger.warning("[control] recusado: token inválido para device=%s", device_id)
        # 4401 (fora da faixa reservada) = "pareie de novo", distinto de um
        # erro de rede — o desktop usa isso para não ficar em retry infinito.
        await websocket.close(code=4401, reason="unpaired")
        return

    await websocket.accept()
    broadcaster = get_device_control_broadcaster()

    async def _pump_outbound() -> None:
        """Comandos do core → device."""
        async for command in broadcaster.register(device_id):
            await websocket.send_json(command)

    outbound = asyncio.create_task(_pump_outbound())
    try:
        while True:
            payload = await websocket.receive_json()
            await broadcaster.resolve(device_id, payload)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("[control] erro no canal de %s: %s", device_id, e)
    finally:
        outbound.cancel()
        try:
            await outbound
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
