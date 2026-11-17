"""Pipeline completo do gateway ponta a ponta, sem rede (httpx.MockTransport).

Exercita validate → policy → confirm/broker → execute → redact no HTTPExecutor
REAL, só trocando o transporte. Cobre: host fixo pelo admin, auth aplicada,
segredo redactado na resposta, fluxo de confirmação e deny sem tocar a rede.
Ver docs/service-gateway-dsl-plan.md §4.
"""

import httpx
import pytest

from core.gateway.audit import AuditWriter
from core.gateway.broker import CredentialBroker, CredentialResolver
from core.gateway.confirm import ConfirmStore
from core.gateway.executor import HTTPExecutor
from core.gateway.ir import AuthType, ConnectionConfig, PolicyEffect, PolicyRule
from core.gateway.service import GatewayService


class _FakeSettings:
    home_assistant_token = "supersecrettoken"


class _FakeRegistry:
    def __init__(self, conn: ConnectionConfig) -> None:
        self._conn = conn

    async def get(self, slug):
        return self._conn if slug == self._conn.id else None

    async def list_enabled(self):
        return [self._conn]


def _connection() -> ConnectionConfig:
    return ConnectionConfig(
        id="homeassistant",
        name="Home Assistant",
        base_url="https://ha.local",
        auth_type=AuthType.BEARER,
        credential_ref="env://home_assistant_token",
        default_read=PolicyEffect.ALLOW,
        default_write=PolicyEffect.CONFIRM,
        policies=[
            PolicyRule(effect=PolicyEffect.DENY, methods=["*"], path="/api/services/shell_command/*"),
            PolicyRule(effect=PolicyEffect.ALLOW, methods=["POST"], path="/api/services/light/*"),
        ],
    )


def _build(handler):
    """Monta um GatewayService com o HTTPExecutor real + transporte mockado."""
    calls: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    conn = _connection()
    service = GatewayService(
        registry=_FakeRegistry(conn),
        broker=CredentialBroker(CredentialResolver(settings=_FakeSettings())),
        http_executor=HTTPExecutor(transport=httpx.MockTransport(_record)),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(None),  # no-op (sem db)
    )
    return service, calls


@pytest.mark.asyncio
async def test_get_allowed_applies_auth_and_fixed_host():
    def handler(req):
        return httpx.Response(200, json=[{"entity_id": "light.sala", "state": "on"}])

    service, calls = _build(handler)
    result = await service.handle_request(
        {"service": "homeassistant", "method": "GET", "path": "/api/states"}
    )
    assert result["status"] == 200
    assert result["ok"] is True
    # host veio da conexão (nunca do LLM); auth aplicada pelo broker.
    assert str(calls[0].url) == "https://ha.local/api/states"
    assert calls[0].headers["authorization"] == "Bearer supersecrettoken"


@pytest.mark.asyncio
async def test_response_secret_is_redacted():
    def handler(req):
        return httpx.Response(200, text='{"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}')

    service, _ = _build(handler)
    result = await service.handle_request(
        {"service": "homeassistant", "method": "GET", "path": "/api/config"}
    )
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in result["body"]


@pytest.mark.asyncio
async def test_deny_makes_no_http_call():
    def handler(req):
        raise AssertionError("não deveria tocar a rede num deny")

    service, calls = _build(handler)
    result = await service.handle_request(
        {"service": "homeassistant", "method": "POST", "path": "/api/services/shell_command/rm",
         "body": {"x": 1}}
    )
    assert result.get("denied") is True
    assert calls == []


@pytest.mark.asyncio
async def test_confirm_flow_defers_then_executes():
    def handler(req):
        return httpx.Response(200, json={"result": "ok"})

    service, calls = _build(handler)
    # POST fora do allow explícito → default_write=confirm
    pending = await service.handle_request(
        {"service": "homeassistant", "method": "POST", "path": "/api/services/lock/unlock",
         "body": {"entity_id": "lock.porta"}}
    )
    assert pending["pending_confirmation"] is True
    assert calls == []  # nada tocou a rede ainda
    cid = pending["confirmation_id"]

    done = await service.handle_confirm(cid)
    assert done["status"] == 200
    assert len(calls) == 1  # só agora executou

    # uso único: segunda confirmação falha
    again = await service.handle_confirm(cid)
    assert again.get("error") == "confirmation_not_found"


@pytest.mark.asyncio
async def test_allowed_write_light_executes_directly():
    def handler(req):
        return httpx.Response(200, json={"result": "ok"})

    service, calls = _build(handler)
    result = await service.handle_request(
        {"service": "homeassistant", "method": "POST", "path": "/api/services/light/turn_on",
         "body": {"entity_id": "light.sala"}}
    )
    assert result["status"] == 200
    assert len(calls) == 1
    assert calls[0].headers["authorization"] == "Bearer supersecrettoken"


@pytest.mark.asyncio
async def test_unknown_service_errors():
    service, _ = _build(lambda req: httpx.Response(200))
    result = await service.handle_request({"service": "nope", "method": "GET", "path": "/x"})
    assert result["error"] == "unknown_service"


@pytest.mark.asyncio
async def test_invalid_path_is_rejected_before_network():
    def handler(req):
        raise AssertionError("não deveria tocar a rede num path inválido")

    service, calls = _build(handler)
    result = await service.handle_request(
        {"service": "homeassistant", "method": "GET", "path": "/api/../../etc/passwd"}
    )
    assert result["error"] == "invalid_path"
    assert calls == []


@pytest.mark.asyncio
async def test_service_list_shows_connection_without_secret():
    service, _ = _build(lambda req: httpx.Response(200))
    listing = await service.list_services()
    slugs = [c["service"] for c in listing["connections"]]
    assert "homeassistant" in slugs
    # nada de credencial/host interno vaza no que o modelo vê
    assert "supersecrettoken" not in str(listing)
