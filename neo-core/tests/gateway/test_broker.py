"""Credential Broker — resolve por referência, aplica auth, nunca revela.

Ver docs/service-gateway-dsl-plan.md §2.2.
"""

import base64

import pytest

from core.gateway.broker import CredentialBroker, CredentialResolver, SecretMaterial
from core.gateway.ir import AuthType, ConnectionConfig, GatewayError


class _FakeSettings:
    home_assistant_token = "supersecrettoken"
    api_pass = "p@ss"


def _conn(**kw) -> ConnectionConfig:
    base = dict(id="svc", name="Svc", base_url="https://svc.local")
    base.update(kw)
    return ConnectionConfig(**base)


def test_secret_material_never_reveals_in_repr_or_str():
    s = SecretMaterial("topsecret")
    assert "topsecret" not in repr(s)
    assert "topsecret" not in str(s)
    assert s.reveal() == "topsecret"  # só reveal() expõe


@pytest.mark.asyncio
async def test_env_resolution_by_attr_name():
    r = CredentialResolver(settings=_FakeSettings())
    s = await r.resolve("env://home_assistant_token")
    assert s.reveal() == "supersecrettoken"


@pytest.mark.asyncio
async def test_env_missing_raises():
    r = CredentialResolver(settings=_FakeSettings())
    with pytest.raises(GatewayError) as e:
        await r.resolve("env://nao_existe")
    assert e.value.code == "credential_unavailable"


@pytest.mark.asyncio
async def test_fernet_without_store_is_unavailable():
    r = CredentialResolver(settings=_FakeSettings())
    with pytest.raises(GatewayError) as e:
        await r.resolve("fernet://abc")
    assert e.value.code == "credential_unavailable"


@pytest.mark.asyncio
async def test_bad_scheme():
    r = CredentialResolver(settings=_FakeSettings())
    with pytest.raises(GatewayError):
        await r.resolve("weird://abc")


@pytest.mark.asyncio
async def test_bearer_auth_header():
    broker = CredentialBroker(CredentialResolver(settings=_FakeSettings()))
    auth = await broker.build_auth(_conn(auth_type=AuthType.BEARER, credential_ref="env://home_assistant_token"))
    assert auth.headers["Authorization"] == "Bearer supersecrettoken"


@pytest.mark.asyncio
async def test_basic_auth_with_username():
    broker = CredentialBroker(CredentialResolver(settings=_FakeSettings()))
    auth = await broker.build_auth(_conn(
        auth_type=AuthType.BASIC, credential_ref="env://api_pass",
        auth_config={"username": "admin"},
    ))
    expected = base64.b64encode(b"admin:p@ss").decode()
    assert auth.headers["Authorization"] == f"Basic {expected}"


@pytest.mark.asyncio
async def test_header_auth_template():
    broker = CredentialBroker(CredentialResolver(settings=_FakeSettings()))
    auth = await broker.build_auth(_conn(
        auth_type=AuthType.HEADER, credential_ref="env://api_pass",
        auth_config={"header": "X-Api-Key", "template": "{secret}"},
    ))
    assert auth.headers["X-Api-Key"] == "p@ss"


@pytest.mark.asyncio
async def test_query_param_auth():
    broker = CredentialBroker(CredentialResolver(settings=_FakeSettings()))
    auth = await broker.build_auth(_conn(
        auth_type=AuthType.QUERY_PARAM, credential_ref="env://api_pass",
        auth_config={"param": "token"},
    ))
    assert auth.params["token"] == "p@ss"


@pytest.mark.asyncio
async def test_none_auth_no_secret():
    broker = CredentialBroker(CredentialResolver(settings=_FakeSettings()))
    auth = await broker.build_auth(_conn(auth_type=AuthType.NONE))
    assert auth.headers == {} and auth.params == {}
