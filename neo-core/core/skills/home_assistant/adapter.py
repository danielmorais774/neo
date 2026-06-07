"""Home Assistant adapter — async HTTP client to the Home Assistant REST API.

Talks directly to a Home Assistant instance (no intermediate gateway). Uses a
long-lived access token (Bearer) and the documented REST endpoints:

  - GET  /api/states                       → all entity states
  - GET  /api/states/{entity_id}           → one entity state
  - POST /api/services/{domain}/{service}  → call a service (control / scene / script)
  - GET  /api/services                     → available services (validation)
  - GET  /api/                             → healthcheck (validates token + URL)

Mirrors the retry/backoff style of the WhatsApp gateway client
(`core/skills/whatsapp_messaging/gateway_client.py`): 4xx → permanent
``ValueError``; 5xx / network errors → exponential backoff retry.
"""

import asyncio
import logging
from typing import Any

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)


class HomeAssistantNotConfiguredError(RuntimeError):
    """Raised when no Home Assistant token is configured."""


class HomeAssistantAdapter:
    """Async HTTP client for the Home Assistant REST API."""

    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.home_assistant_url.rstrip("/")
        self._token = settings.home_assistant_token
        self._timeout = settings.home_assistant_timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._token)

    async def _get_client(self) -> httpx.AsyncClient:
        if not self._token:
            raise HomeAssistantNotConfiguredError(
                "Home Assistant não configurado (defina HOME_ASSISTANT_TOKEN)."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(float(self._timeout)),
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Perform a request with backoff retry on 5xx / network errors.

        Raises ValueError on 4xx (permanent) and after exhausting retries.
        """
        client = await self._get_client()
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await client.request(method, path, **kwargs)
                if response.status_code < 400:
                    return response
                if response.status_code < 500:
                    raise ValueError(
                        f"Home Assistant rejeitou ({response.status_code}): {response.text[:200]}"
                    )
                last_error = Exception(f"Home Assistant 5xx: {response.status_code}")
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as e:
                last_error = e

            if attempt < 3:
                delay = 2 ** attempt
                logger.warning("Retrying HA call %s %s (attempt=%s, err=%s)", method, path, attempt, last_error)
                await asyncio.sleep(delay)

        raise ValueError(f"Chamada ao Home Assistant falhou após 4 tentativas: {last_error}")

    async def get_states(self) -> list[dict[str, Any]]:
        """Return all entity states."""
        response = await self._request("GET", "/api/states")
        return response.json()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """Return the state of a single entity."""
        response = await self._request("GET", f"/api/states/{entity_id}")
        return response.json()

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Call a Home Assistant service. Returns the list of changed entities."""
        response = await self._request(
            "POST",
            f"/api/services/{domain}/{service}",
            json=data or {},
        )
        result = response.json()
        # HA returns a list of changed states; be defensive about shape.
        return result if isinstance(result, list) else []

    async def get_services(self) -> list[dict[str, Any]]:
        """Return the available services grouped by domain."""
        response = await self._request("GET", "/api/services")
        return response.json()

    async def healthcheck(self) -> bool:
        """Return True when the HA API responds with a valid token."""
        try:
            response = await self._request("GET", "/api/")
            return response.status_code == 200
        except (ValueError, HomeAssistantNotConfiguredError, httpx.HTTPError):
            return False

    def _ws_url(self) -> str:
        """WebSocket URL derived from the REST base URL (http→ws, https→wss)."""
        base = self.base_url
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/api/websocket"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/api/websocket"
        return "ws://" + base + "/api/websocket"

    async def rename_entity(self, entity_id: str, name: str | None) -> dict[str, Any]:
        """Rename an entity's friendly name in the entity registry (persistent).

        Uses the Home Assistant **WebSocket API** (``config/entity_registry/update``),
        because the REST API can only override ``friendly_name`` transiently. Pass
        an empty/None ``name`` to reset to the integration default.

        Raises ValueError on auth failure or if HA rejects the update.
        """
        import asyncio
        import json

        if not self._token:
            raise HomeAssistantNotConfiguredError(
                "Home Assistant não configurado (defina HOME_ASSISTANT_TOKEN)."
            )

        # Lazy import keeps websockets optional at module load.
        import websockets

        timeout = float(self._timeout)
        new_name = name if name else None  # "" → reset to default
        ws_url = self._ws_url()

        async def _recv(ws: Any) -> dict[str, Any]:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(raw)

        try:
            async with websockets.connect(ws_url, open_timeout=timeout, close_timeout=timeout) as ws:
                # Auth handshake: server → auth_required, client → auth, server → auth_ok.
                await _recv(ws)
                await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                auth = await _recv(ws)
                if auth.get("type") != "auth_ok":
                    raise ValueError("Falha de autenticação no WebSocket do Home Assistant.")

                await ws.send(json.dumps({
                    "id": 1,
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                    "name": new_name,
                }))
                # Skip any pushed events until our command result (id == 1) arrives.
                resp: dict[str, Any] = {}
                for _ in range(20):
                    resp = await _recv(ws)
                    if resp.get("id") == 1 and resp.get("type") == "result":
                        break
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            raise ValueError(f"Falha ao conectar no WebSocket do Home Assistant: {e}") from e

        if not resp.get("success"):
            err = resp.get("error") or {}
            raise ValueError(f"Home Assistant recusou o rename: {err.get('message') or err or 'desconhecido'}")

        result = resp.get("result") or {}
        # HA wraps the updated entity under "entity_entry" in recent versions.
        return result.get("entity_entry", result)
