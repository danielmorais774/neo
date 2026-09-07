"""Canal de controle por device — request/response sobre WebSocket.

Por que um canal novo em vez de reusar o de notificações
(docs/device-control-plan.md §3):

- o desktop mostra uma notificação nativa para TODA mensagem que chega no WS de
  notificações (``websocket.rs``, filtrado só por foco de janela) — reusar
  aquele cano significaria um popup a cada ajuste de volume;
- notificação é fire-and-forget; controle precisa de resposta correlacionada,
  senão o Neo diz "abaixei o volume" sem nenhuma evidência de que abaixou.

E por que não o stream gRPC: ``ExecuteTask`` é aberto POR TURNO pelo desktop
(``ai_agent.rs``: abre o stream, manda a mensagem, encerra na resposta final).
Um device ocioso — exatamente o alvo de "abaixa o volume do PC da sala" — não
tem stream vivo. Este WS é o único cano permanente core→device com volta.

Envelope:

    core → device   {"id": "c-7f3a", "action": "volume_set", "args": {"level": 30}}
    device → core   {"id": "c-7f3a", "ok": true,  "result": {"level": 30}}
                    {"id": "c-7f3a", "ok": false, "error": "não suportado"}
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# Timeout padrão de um comando. Ações locais (volume, media key) respondem em
# milissegundos; 5s é folga para uma máquina sob carga, e curto o bastante para
# o turno não parecer travado.
DEFAULT_TIMEOUT_S = 5.0

# Quanto esperamos o device SUMIR depois de um suspend/shutdown. Nessas ações a
# confirmação é a desconexão, não uma resposta — a máquina cai antes de
# conseguir responder.
DISCONNECT_CONFIRM_TIMEOUT_S = 20.0


class DeviceOffline(Exception):
    """O device alvo não tem canal de controle aberto."""


class DeviceTimeout(Exception):
    """O device recebeu o comando mas não respondeu a tempo."""


class DeviceControlBroadcaster:
    """Roteia comandos para o WS de controle de cada device e casa as respostas."""

    def __init__(self) -> None:
        # device_id -> fila de saída (o laço do WS consome e manda pro device)
        self._outbound: dict[str, asyncio.Queue] = {}
        # correlation_id -> Future aguardando a resposta do device
        self._pending: dict[str, asyncio.Future] = {}
        # device_id -> correlation_ids em voo (para falhar todos no disconnect)
        self._inflight: dict[str, set[str]] = {}
        # device_id -> eventos acordados quando aquele device desconecta
        self._disconnect_waiters: dict[str, list[asyncio.Event]] = {}
        self._lock = asyncio.Lock()

    # ── lado do device (o endpoint WS) ──────────────────────────────────

    async def register(self, device_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Registra o canal de um device e itera os comandos destinados a ele.

        Uma conexão por device: reconectar derruba a anterior (senão um device
        que reconecta sem fechar direito fica com dois canais e o comando vai
        para o socket morto).
        """
        async with self._lock:
            previous = self._outbound.get(device_id)
            if previous is not None:
                logger.warning("[control] %s reconectou — derrubando canal anterior", device_id)
                previous.put_nowait(None)  # sentinela: encerra o laço antigo
            queue: asyncio.Queue = asyncio.Queue()
            self._outbound[device_id] = queue
            self._inflight.setdefault(device_id, set())
            logger.info("[control] device conectado: %s (total=%d)", device_id, len(self._outbound))

        try:
            while True:
                item = await queue.get()
                if item is None:  # substituído por uma conexão nova
                    return
                yield item
        except asyncio.CancelledError:
            pass
        finally:
            await self._drop(device_id, queue)

    async def _drop(self, device_id: str, queue: asyncio.Queue) -> None:
        """Remove o canal e falha tudo que estava em voo para aquele device."""
        async with self._lock:
            # Só remove se ainda for ESTE canal — uma reconexão já pode ter
            # instalado outro, e apagá-lo aqui deixaria o device mudo.
            if self._outbound.get(device_id) is queue:
                self._outbound.pop(device_id, None)
            inflight = self._inflight.pop(device_id, set())
            waiters = self._disconnect_waiters.pop(device_id, [])

        for cid in inflight:
            future = self._pending.pop(cid, None)
            if future is not None and not future.done():
                future.set_exception(DeviceOffline(f"device {device_id} desconectou"))
        for event in waiters:
            event.set()
        logger.info("[control] device desconectado: %s", device_id)

    async def kick(self, device_id: str) -> bool:
        """Derruba o canal de um device — usado quando ele é REMOVIDO.

        Sem isto, um device apagado do banco continuaria com o socket aberto e
        aceitando comandos até se desconectar sozinho: o token já não existe,
        mas a conexão que ele abriu enquanto existia, sim.
        """
        async with self._lock:
            queue = self._outbound.get(device_id)
            if queue is None:
                return False
            queue.put_nowait(None)  # sentinela: encerra o laço do WS
        return True

    async def resolve(self, device_id: str, payload: dict[str, Any]) -> None:
        """Casa uma resposta vinda do device com quem a espera."""
        cid = str(payload.get("id") or "")
        future = self._pending.pop(cid, None)
        async with self._lock:
            self._inflight.get(device_id, set()).discard(cid)
        if future is None:
            # Resposta atrasada de um comando que já deu timeout — descartar é o
            # certo, mas registrar ajuda a distinguir "device lento" de "device mudo".
            logger.warning("[control] resposta sem dono de %s: id=%s", device_id, cid)
            return
        if not future.done():
            future.set_result(payload)

    # ── lado do core (as tools) ─────────────────────────────────────────

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._outbound

    def connected_devices(self) -> list[str]:
        return list(self._outbound)

    async def request(
        self,
        device_id: str,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> dict[str, Any]:
        """Manda um comando e ESPERA a resposta do device.

        Levanta ``DeviceOffline`` se não há canal, ``DeviceTimeout`` se o device
        não respondeu. Nunca devolve sucesso sem confirmação do device — é o que
        impede o Neo de dizer "abaixei" sem ter abaixado.
        """
        async with self._lock:
            queue = self._outbound.get(device_id)
            if queue is None:
                raise DeviceOffline(f"device {device_id} não está com o canal de controle aberto")
            cid = f"c-{secrets.token_hex(4)}"
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[cid] = future
            self._inflight.setdefault(device_id, set()).add(cid)
            queue.put_nowait({"id": cid, "action": action, "args": args or {}})

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(cid, None)
            async with self._lock:
                self._inflight.get(device_id, set()).discard(cid)
            raise DeviceTimeout(
                f"device {device_id} não respondeu a '{action}' em {timeout:g}s"
            ) from exc

    async def send_and_expect_disconnect(
        self,
        device_id: str,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float = DISCONNECT_CONFIRM_TIMEOUT_S,
    ) -> bool:
        """Comando que derruba a máquina: a confirmação é a DESCONEXÃO.

        ``suspend``/``shutdown``/``restart`` matam o WS antes de qualquer
        resposta — esperar ``result`` nelas é garantir timeout falso em todo
        sucesso. Devolve True se o device sumiu dentro da janela.
        """
        event = asyncio.Event()
        async with self._lock:
            queue = self._outbound.get(device_id)
            if queue is None:
                raise DeviceOffline(f"device {device_id} não está com o canal de controle aberto")
            self._disconnect_waiters.setdefault(device_id, []).append(event)
            queue.put_nowait({"id": f"c-{secrets.token_hex(4)}", "action": action, "args": args or {}})

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            async with self._lock:
                waiters = self._disconnect_waiters.get(device_id)
                if waiters and event in waiters:
                    waiters.remove(event)


_device_control_broadcaster: DeviceControlBroadcaster | None = None


def get_device_control_broadcaster() -> DeviceControlBroadcaster:
    global _device_control_broadcaster
    if _device_control_broadcaster is None:
        _device_control_broadcaster = DeviceControlBroadcaster()
    return _device_control_broadcaster
