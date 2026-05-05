"""WhatsApp gateway client — async HTTP client to neo-whatsapp service."""

import logging
from typing import Any

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)


class WhatsAppGatewayClient:
    """Async HTTP client for the neo-whatsapp service.

    Handles send, lookup with exponential backoff retry.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.whatsapp_gateway_url
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(10.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send_message(
        self,
        jid: str,
        text: str,
        idempotency_key: str | None = None,
    ) -> str:
        """Send a WhatsApp message.

        Returns the message_id from the gateway.

        Raises on 4xx: ValueError
        Raises on 5xx: retry then eventually ValueError
        """
        client = await self._get_client()
        payload: dict[str, Any] = {"jid": jid, "text": text}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key

        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await client.post("/send", json=payload)
                if response.status_code < 400:
                    data = response.json()
                    return data["message_id"]
                if response.status_code < 500:
                    logger.warning({response.status_code, payload}, "Gateway reject, no retry")
                    raise ValueError(f"Gateway rejected ({response.status_code}): {response.text}")
                # 5xx — retry
                last_error = Exception(f"Gateway 5xx: {response.status_code}")
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                last_error = e

            if attempt < 3:
                delay = 2 ** attempt
                logger.warning({attempt, delay, last_error}, "Retrying gateway call")
                await _sleep(delay)

        raise ValueError(f"Gateway send failed after 4 attempts: {last_error}")

    async def lookup_contact(self, phone_e164: str) -> tuple[str, bool]:
        """Lookup a phone number to get JID.

        Returns (jid, exists).
        """
        client = await self._get_client()
        try:
            response = await client.post("/lookup", json={"phone_e164": phone_e164})
            if response.status_code == 200:
                data = response.json()
                return data["jid"], data.get("exists", True)
            return "", False
        except Exception:
            return "", False

    async def list_chats(self) -> list[dict]:
        """Return active chats from the connected WhatsApp phone."""
        client = await self._get_client()
        response = await client.get("/chats")
        response.raise_for_status()
        return response.json().get("chats", [])

    async def list_contacts(
        self,
        query: str | None = None,
        only_saved: bool = False,
        limit: int = 200,
    ) -> list[dict]:
        """Return the user's WhatsApp contacts (saved on the phone).

        Each contact: {jid, name, phone_e164, is_my_contact, is_wa_contact, is_group}
        When `query` is provided, the gateway returns fuzzy-ranked matches.
        """
        client = await self._get_client()
        params: dict[str, str] = {"limit": str(limit)}
        if query:
            params["q"] = query
        if only_saved:
            params["only_saved"] = "1"
        try:
            response = await client.get("/contacts", params=params)
            response.raise_for_status()
            return response.json().get("contacts", [])
        except Exception as e:
            logger.warning({"err": str(e)}, "Gateway /contacts failed")
            return []

    async def get_chat_messages(self, jid: str) -> list[dict]:
        """Return the last 50 messages for a chat JID."""
        client = await self._get_client()
        response = await client.get(f"/chats/{jid}/messages")
        response.raise_for_status()
        return response.json().get("messages", [])

    async def get_chat_media(self, jid: str, msg_id: str) -> dict:
        """Download media for a message. Returns {url: base64_data_url}."""
        client = await self._get_client()
        response = await client.get(f"/chats/{jid}/media/{msg_id}")
        response.raise_for_status()
        return response.json()


def _sleep(seconds: int) -> Any:
    import asyncio
    return asyncio.sleep(seconds)