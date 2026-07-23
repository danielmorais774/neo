"""Validação estrutural do gateway — path normalization é o núcleo de segurança.

Sem rede, sem segredo. Ver docs/service-gateway-dsl-plan.md §1.4.
"""

import pytest

from core.gateway.ir import AuthType, ConnectionConfig, GatewayError, ServiceRequestIR
from core.gateway.validator import normalize_path, validate_request


def _conn(**kw) -> ConnectionConfig:
    base = dict(id="svc", name="Svc", base_url="https://svc.local", auth_type=AuthType.NONE)
    base.update(kw)
    return ConnectionConfig(**base)


def _ir(**kw) -> ServiceRequestIR:
    base = dict(service="svc", method="GET", path="/api/x")
    base.update(kw)
    return ServiceRequestIR(**base)


# ── normalize_path: aceita o legítimo ───────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("/api/states", "/api/states"),
    ("/api/services/light/turn_on", "/api/services/light/turn_on"),
    ("/a/./b", "/a/b"),          # colapsa '.'
    ("/a//b", "/a/b"),           # colapsa barra dupla interna
    ("/api/", "/api/"),          # preserva trailing slash
])
def test_normalize_ok(path, expected):
    assert normalize_path(path) == expected


# ── normalize_path: recusa o perigoso ───────────────────────────────────────

@pytest.mark.parametrize("path", [
    "",                      # vazio
    "api/x",                 # não começa com '/'
    "//evil.com/x",          # protocol-relative → troca de host
    "/a/../../etc/passwd",   # traversal
    "/a/%2e%2e/b",           # traversal por encoding
    "/a/%252e%252e/b",       # traversal por double-encoding
    "http://evil.com",       # URL absoluta
    "/x?y=1",                # querystring embutida
    "/x#frag",               # fragmento
    "/x\\y",                 # barra invertida
    "/x\ty",                 # caractere de controle
])
def test_normalize_rejects(path):
    with pytest.raises(GatewayError):
        normalize_path(path)


# ── validate_request: headers/query reservados, tamanho ─────────────────────

def test_reserved_header_authorization_rejected():
    ir = _ir(headers={"Authorization": "Bearer x"})
    with pytest.raises(GatewayError) as e:
        validate_request(ir, _conn())
    assert e.value.code == "reserved_header"


def test_reserved_header_cookie_and_proxy_prefix():
    for h in ("Cookie", "Host", "X-Forwarded-For", "Proxy-Connection", "X-HTTP-Method-Override"):
        with pytest.raises(GatewayError):
            validate_request(_ir(headers={h: "v"}), _conn())


def test_complementary_header_allowed():
    out = validate_request(_ir(headers={"Accept": "application/json"}), _conn())
    assert out.headers == {"Accept": "application/json"}


def test_reserved_query_param_rejected():
    with pytest.raises(GatewayError) as e:
        validate_request(_ir(query={"_method": "delete"}), _conn())
    assert e.value.code == "reserved_query"


def test_body_too_large():
    ir = _ir(method="POST", path="/x", body={"k": "a" * 100})
    with pytest.raises(GatewayError) as e:
        validate_request(ir, _conn(max_body_bytes=10))
    assert e.value.code == "body_too_large"


def test_validate_returns_normalized_copy():
    out = validate_request(_ir(path="/a/./b"), _conn())
    assert out.path == "/a/b"
