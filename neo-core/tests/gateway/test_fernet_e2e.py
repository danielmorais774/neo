"""fernet:// ponta a ponta: cofre → broker → request autenticada (F2).

Fecha o elo que a F1 deixou aberto (o broker recusava fernet://). Usa o
HTTPExecutor real com transporte mockado, como test_service_e2e.
"""

from __future__ import annotations

import httpx
import pytest

from core.credentials.store import CredentialStore, generate_key
from core.gateway.audit import AuditWriter
from core.gateway.broker import CredentialBroker, CredentialResolver
from core.gateway.confirm import ConfirmStore
from core.gateway.executor import HTTPExecutor
from core.gateway.ir import AuthType, ConnectionConfig, PolicyEffect
from core.gateway.service import GatewayService


class _FakeSession:
    def __init__(self, db) -> None:
        self._db = db

    def add(self, entry) -> None:
        self._db.rows[entry.id] = entry

    async def commit(self) -> None:
        return None

    async def refresh(self, entry) -> None:
        return None

    async def get(self, model, pk):
        return self._db.rows.get(pk)

    async def execute(self, stmt):
        class _R:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return []

            def first(self_inner):
                return None

        return _R()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeDB:
    def __init__(self) -> None:
        self.rows: dict = {}

    def get_session(self):
        return _FakeSession(self)


class _FakeRegistry:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def get(self, slug):
        return self._conn if slug == self._conn.id else None

    async def list_enabled(self):
        return [self._conn]


@pytest.mark.asyncio
async def test_connection_with_fernet_ref_authenticates_real_request():
    db = _FakeDB()
    store = CredentialStore(db=db, key=generate_key())
    info = await store.create(label="github-personal", secret="ghp_tokendoutilizador123456")

    conn = ConnectionConfig(
        id="github-personal",
        name="GitHub",
        base_url="https://api.github.com",
        auth_type=AuthType.BEARER,
        credential_ref=info.ref,      # fernet://<id> — cadastrado pelo painel
        default_read=PolicyEffect.ALLOW,
    )

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[{"number": 1, "title": "bug"}])

    service = GatewayService(
        registry=_FakeRegistry(conn),
        broker=CredentialBroker(CredentialResolver(settings=None, fernet_store=store)),
        http_executor=HTTPExecutor(transport=httpx.MockTransport(handler)),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(None),
    )

    result = await service.handle_request(
        {"service": "github-personal", "method": "GET", "path": "/repos/u/r/issues"}
    )

    assert result["status"] == 200 and result["ok"] is True
    # O segredo saiu do cofre e virou header — sem passar pelo modelo.
    assert calls[0].headers["authorization"] == "Bearer ghp_tokendoutilizador123456"
    assert str(calls[0].url) == "https://api.github.com/repos/u/r/issues"
    # E não vazou na resposta que volta pro cognitivo.
    assert "ghp_tokendoutilizador123456" not in str(result)


@pytest.mark.asyncio
async def test_missing_credential_in_vault_is_clear_error():
    db = _FakeDB()
    store = CredentialStore(db=db, key=generate_key())
    conn = ConnectionConfig(
        id="svc", name="Svc", base_url="https://x.local",
        auth_type=AuthType.BEARER,
        credential_ref="fernet://00000000-0000-0000-0000-000000000000",
    )
    service = GatewayService(
        registry=_FakeRegistry(conn),
        broker=CredentialBroker(CredentialResolver(settings=None, fernet_store=store)),
        http_executor=HTTPExecutor(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(None),
    )
    result = await service.handle_request({"service": "svc", "method": "GET", "path": "/x"})
    assert result["error"] == "credential_unavailable"
    assert "não existe no cofre" in result["message"]


@pytest.mark.asyncio
async def test_no_vault_configured_tells_operator_what_to_do():
    conn = ConnectionConfig(
        id="svc", name="Svc", base_url="https://x.local",
        auth_type=AuthType.BEARER, credential_ref="fernet://abc",
    )
    service = GatewayService(
        registry=_FakeRegistry(conn),
        broker=CredentialBroker(CredentialResolver(settings=None, fernet_store=None)),
        http_executor=HTTPExecutor(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(None),
    )
    result = await service.handle_request({"service": "svc", "method": "GET", "path": "/x"})
    assert result["error"] == "credential_unavailable"
    assert "NEO_CRED_KEY" in result["message"]
