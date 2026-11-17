"""QR Status broadcaster — push QR/connection state to WebSocket clients."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class QRStatusBroadcaster:
    """Broadcaster that pushes QR status updates to all WebSocket subscribers.

    Any REST call to /qr-status (or /qr-refresh) broadcasts the new state
    to all connected WS clients, so they get real-time updates without polling.
    """

    def __init__(self) -> None:
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._lock = asyncio.Lock()
        self._next_id: int = 0

    async def broadcast(self, status: dict) -> None:
        """Called by the REST endpoint after fetching new QR status."""
        async with self._lock:
            if not self._subscribers:
                return
            # Put status in each subscriber's queue
            disconnected = []
            for sub_id, queue in self._subscribers.items():
                try:
                    queue.put_nowait(status)
                except asyncio.QueueFull:
                    logger.warning(f"QR broadcast queue full for subscriber {sub_id}, dropping")
                    disconnected.append(sub_id)
            # Clean up full queues
            for sub_id in disconnected:
                del self._subscribers[sub_id]

    async def subscribe(self) -> tuple[int, Any]:
        """WebSocket endpoint calls this to receive QR status updates.

        Returns (subscriber_id, async_generator).
        The caller should iterate the generator to receive messages.
        The generator exits when the subscriber disconnects.
        """
        async with self._lock:
            sub_id = self._next_id
            self._next_id += 1
            queue: asyncio.Queue = asyncio.Queue()
            self._subscribers[sub_id] = queue
            logger.info(f"QR WS subscriber connected id={sub_id} total={len(self._subscribers)}")

        try:
            while True:
                item = await queue.get()
                yield item
        except asyncio.CancelledError:
            pass
        finally:
            async with self._lock:
                self._subscribers.pop(sub_id, None)
            logger.info(f"QR WS subscriber disconnected id={sub_id} total={len(self._subscribers)}")

    @property
    def connection_count(self) -> int:
        return len(self._subscribers)


_qr_broadcaster: QRStatusBroadcaster | None = None


def get_qr_broadcaster() -> QRStatusBroadcaster:
    global _qr_broadcaster
    if _qr_broadcaster is None:
        _qr_broadcaster = QRStatusBroadcaster()
    return _qr_broadcaster