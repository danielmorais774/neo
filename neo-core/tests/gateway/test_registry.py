"""ConnectionRegistry — seed de env do Home Assistant.

Sem db (db=None ⇒ só seeds). Ver docs/service-gateway-dsl-plan.md §2.2.
"""

import pytest

from core.gateway.ir import AuthType
from core.gateway.registry import ConnectionRegistry, is_valid_slug


class _SettingsHTTP:
    home_assistant_token = "tok"
    home_assistant_url = "http://172.18.0.1:8123"
    home_assistant_timeout = 10


class _SettingsHTTPS:
    home_assistant_token = "tok"
    home_assistant_url = "https://ha.example.com"
    home_assistant_timeout = 10


class _SettingsNoHA:
    home_assistant_token = None
    home_assistant_url = "http://x"


@pytest.mark.asyncio
async def test_ha_seed_allows_http_on_lan():
    reg = ConnectionRegistry(db=None, settings=_SettingsHTTP())
    ha = await reg.get("homeassistant")
    assert ha is not None
    assert ha.auth_type is AuthType.BEARER
    assert ha.credential_ref == "env://home_assistant_token"
    # HA em http:// interno → allow_http derivado do scheme (senão o executor recusa)
    assert ha.allow_http is True
    assert ha.default_read.value == "allow" and ha.default_write.value == "confirm"


@pytest.mark.asyncio
async def test_ha_seed_https_does_not_allow_http():
    reg = ConnectionRegistry(db=None, settings=_SettingsHTTPS())
    ha = await reg.get("homeassistant")
    assert ha.allow_http is False


@pytest.mark.asyncio
async def test_no_seed_without_token():
    reg = ConnectionRegistry(db=None, settings=_SettingsNoHA())
    assert await reg.list_enabled() == []


def test_slug_validation():
    assert is_valid_slug("github-personal")
    assert is_valid_slug("homeassistant")
    assert not is_valid_slug("A")
    assert not is_valid_slug("-bad")
    assert not is_valid_slug("has space")
