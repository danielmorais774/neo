"""NotificationBroker — in-memory pub/sub for desktop push notifications."""

import asyncio
import logging
from typing import Any, AsyncIterator
from uuid import UUID

logger = logging.getLogger(__name__)


class NotificationBroker:
    """In-memory pub/sub broker for real-time desktop notifications.

    Maintains a queue per user_id. gRPC SubscribeNotifications RPC
    keeps the stream open and yields each new notification as it arrives.
    """

    def __init__(self) -> None:
        self._queues: dict[UUID, asyncio.Queue] = {}

    def _get_queue(self, user_id: UUID) -> asyncio.Queue:
        if user_id not in self._queues:
            self._queues[user_id] = asyncio.Queue()
        return self._queues[user_id]

    async def publish(self, user_id: UUID, notification: dict[str, Any]) -> None:
        queue = self._get_queue(user_id)
        await queue.put(notification)
        logger.info(
            "Notification published user=%s kind=%s title=%s",
            user_id,
            notification.get("kind"),
            notification.get("title"),
        )

    async def subscribe(self, user_id: UUID) -> AsyncIterator[dict[str, Any]]:
        """Server-streaming subscription. Blocks on queue.get() so new
        notifications are yielded as soon as they're published. Cancels
        cleanly when the gRPC stream is closed by the client.
        """
        queue = self._get_queue(user_id)
        logger.info("Subscriber attached user=%s queue_size=%s", user_id, queue.qsize())
        try:
            while True:
                item = await queue.get()
                yield item
        except asyncio.CancelledError:
            logger.info("Subscriber detached user=%s", user_id)
            raise

    def queue_size(self, user_id: UUID) -> int:
        if user_id not in self._queues:
            return 0
        return self._queues[user_id].qsize()
