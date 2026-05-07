"""QR Status broadcaster — push QR/connection state to WebSocket clients."""

import asyncio
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class QRStatusBroadcaster:
    """Singleton broadcaster that pushes QR status updates to all WebSocket subscribers.

    Any REST call to /qr-status (or /qr-refresh) triggers a broadcast to all
    connected WS clients, so they get real-time updates without polling.
    """

    _instance: "QRStatusBroadcaster | None" = None

    def __new__(cls) -> "QRStatusBroadcaster":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._queue: asyncio.Queue | None = None
            cls._instance._connections: int = 0
        return cls._instance

    def _get_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def broadcast(self, status: dict) -> None:
        """Called by the REST endpoint after fetching new QR status."""
        queue = self._get_queue()
        try:
            # Non-blocking put — skip if queue is backed up
            queue.put_nowait(status)
        except asyncio.QueueFull:
            logger.warning("QR broadcast queue full, dropping update")
        # Wake up all subscribers
        for _ in range(self._connections):
            queue.task_done()

    async def subscribe(self) -> AsyncIterator[dict]:
        """WebSocket endpoint calls this to receive QR status updates."""
        self._connections += 1
        queue = self._get_queue()
        logger.info(f"QR WS subscriber connected (total={self._connections})")
        try:
            while True:
                item = await queue.get()
                yield item
        except asyncio.CancelledError:
            pass
        finally:
            self._connections -= 1
            logger.info(f"QR WS subscriber disconnected (total={self._connections})")

    @property
    def connection_count(self) -> int:
        return self._connections


def get_qr_broadcaster() -> QRStatusBroadcaster:
    return QRStatusBroadcaster()