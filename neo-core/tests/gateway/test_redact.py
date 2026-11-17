"""Redaction — o segredo nunca chega ao contexto/log, venha de onde vier.

Ver docs/service-gateway-dsl-plan.md §1.2, §2.2.
"""

from core.gateway.redact import cap_body, redact_error, redact_headers, redact_text


def test_redacts_bearer():
    out = redact_text("Authorization: Bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWX12345")
    assert "ghp_" not in out                # o token some (o que importa)
    assert "Authorization" in out           # o rótulo do header sobrevive (legibilidade)


def test_redacts_provider_tokens():
    for token in [
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "xoxb-123456789012-abcdefghijklmnop",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "AKIAIOSFODNN7EXAMPLE",
    ]:
        assert token not in redact_text(f"segredo={token} fim")


def test_redacts_jwt_and_pem():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk"
    assert jwt not in redact_text(f"token {jwt}")
    pem = "-----BEGIN PRIVATE KEY-----\nMIIabc123\n-----END PRIVATE KEY-----"
    assert "MIIabc123" not in redact_text(pem)


def test_redacts_echoed_json_api_key():
    body = '{"api_key": "supersecretvalue123", "x": 1}'
    out = redact_text(body)
    assert "supersecretvalue123" not in out


def test_redact_headers_zeroes_auth():
    out = redact_headers({"Authorization": "Bearer x", "Content-Type": "application/json"})
    assert out["Authorization"] == "«redacted»"
    assert out["Content-Type"] == "application/json"


def test_cap_body_truncates_on_byte_limit():
    body, truncated = cap_body("x" * 1000, 100)
    assert truncated is True
    assert len(body.encode("utf-8")) < 1000
    body2, truncated2 = cap_body("short", 100)
    assert truncated2 is False and body2 == "short"


def test_cap_body_handles_multibyte_boundary():
    # corte no meio de um caractere multibyte não deve quebrar
    body, truncated = cap_body("á" * 100, 51)  # 'á' = 2 bytes
    assert truncated is True
    assert isinstance(body, str)


def test_redact_error_no_raw_headers():
    try:
        raise ValueError("falha com Authorization: Bearer sk-ABCDEFGHIJKLMNOP012345")
    except ValueError as e:
        out = redact_error(e)
    assert "sk-ABCDEFGHIJKLMNOP012345" not in out
