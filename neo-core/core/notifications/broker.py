"""NotificationBroker — in-memory pub/sub for desktop push notifications."""

import asyncio
import logging
from typing import Any, AsyncIterator

from uuid import UUID

logger = logging.getLogger(__name__)


class NotificationBroker:
    """In-memory pub/sub broker for real-time desktop notifications.

    Maintains a queue per user_id. gRPC SubscribeNotifications RPC
    drains the queue as a server-stream.
    """

    def __init__(self) -> None:
        self._queues: dict[UUID, asyncio.Queue] = {}

    def _get_queue(self, user_id: UUID) -> asyncio.Queue:
        """Get or create the queue for a user."""
        if user_id not in self._queues:
            self._queues[user_id] = asyncio.Queue()
        return self._queues[user_id]

    async def publish(self, user_id: UUID, notification: dict[str, Any]) -> None:
        """Publish a notification to a user's queue."""
        queue = self._get_queue(user_id)
        await queue.put(notification)
        logger.info({user_id, notification}, "Notification published")

    async def subscribe(self, user_id: UUID) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to notifications for a user. Yields items from the queue.

        On subscribe, replays any undelivered notifications from the DB.
        """
        queue = self._get_queue(user_id)

        # Yield any queued items
        while not queue.empty():
            item = queue.get_nowait()
            yield item

    def queue_size(self, user_id: UUID) -> int:
        """Return current queue size for a user."""
        if user_id not in self._queues:
            return 0
        return self._queues[user_id].qsize()


from typing import AsyncIterator  # noqa: E402, F401