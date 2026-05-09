"""Device notifications broadcaster — push notifications to specific device via WebSocket."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class DeviceNotificationsBroadcaster:
    """Sends notifications to a specific device's WebSocket connection.

    Uses the ws_connection_id stored in the DB to identify the connection.
    Each device can have one active WS connection for notifications.
    """

    def __init__(self) -> None:
        # Maps ws_connection_id -> queue for that connection
        self._connections: dict[str, asyncio.Queue] = {}
        self._lock = asyncio.Lock()

    async def register(self, ws_connection_id: str) -> tuple[str, Any]:
        """Register a new WebSocket connection for notifications.

        Returns (connection_id, async_generator).
        The generator yields notifications until disconnect.
        """
        async with self._lock:
            queue: asyncio.Queue = asyncio.Queue()
            self._connections[ws_connection_id] = queue
            logger.info(f"[NotifBroadcaster] Connected ws_conn_id={ws_connection_id} total={len(self._connections)}")

        try:
            while True:
                item = await queue.get()
                yield item
        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                self._connections.pop(ws_connection_id, None)
            logger.info(f"[NotifBroadcaster] Disconnected ws_conn_id={ws_connection_id}")

    async def send_notification(self, ws_connection_id: str, notification: dict[str, Any]) -> bool:
        """Send a notification to a specific device by its ws_connection_id.

        Returns True if sent, False if device not connected.
        """
        async with self._lock:
            queue = self._connections.get(ws_connection_id)
            if not queue:
                return False
            try:
                queue.put_nowait(notification)
                return True
            except asyncio.QueueFull:
                logger.warning(f"[NotifBroadcaster] Queue full for {ws_connection_id}")
                return False

    async def broadcast_to_all(self, notification: dict[str, Any]) -> int:
        """Broadcast notification to all connected devices. Returns count."""
        sent = 0
        async with self._lock:
            for ws_conn_id, queue in list(self._connections.items()):
                try:
                    queue.put_nowait(notification)
                    sent += 1
                except asyncio.QueueFull:
                    pass
        return sent

    @property
    def connection_count(self) -> int:
        return len(self._connections)


_device_notif_broadcaster: DeviceNotificationsBroadcaster | None = None


def get_device_notif_broadcaster() -> DeviceNotificationsBroadcaster:
    global _device_notif_broadcaster
    if _device_notif_broadcaster is None:
        _device_notif_broadcaster = DeviceNotificationsBroadcaster()
    return _device_notif_broadcaster