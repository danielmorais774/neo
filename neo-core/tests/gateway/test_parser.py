"""Dialeto .http → IR (F3 — §6 do plano).

O parser é formato de AUTORIA (exemplos em skills, requests salvos). Cada recusa
aqui é deliberada: o que o REST Client permite e nós não, é o que deixaria o
host/segredo vir do arquivo em vez da conexão.
"""

import pytest

from core.gateway.ir import GatewayError
from core.gateway.parser import parse_http, split_requests

_EXEMPLO = """### liga a luz da sala
@service homeassistant

POST /api/services/light/turn_on
Content-Type: application/json

{
  "entity_id": "light.sala"
}
"""


def test_parses_the_plan_example():
    ir = parse_http(_EXEMPLO)
    assert ir.service == "homeassistant"
    assert ir.method == "POST"
    assert ir.path == "/api/services/light/turn_on"
    assert ir.headers == {"Content-Type": "application/json"}
    assert ir.body == {"entity_id": "light.sala"}


def test_parses_simple_get_without_body():
    ir = parse_http("@service github-personal\n\nGET /repos/user/repo/issues\n")
    assert ir.method == "GET" and ir.path == "/repos/user/repo/issues"
    assert ir.body is None and ir.query == {}


def test_query_string_is_split_into_query_dict():
    ir = parse_http("@service gh\n\nGET /issues?state=open&page=2\n")
    assert ir.path == "/issues"
    assert ir.query == {"state": "open", "page": "2"}


def test_repeated_query_param_becomes_list():
    ir = parse_http("@service gh\n\nGET /x?tag=a&tag=b\n")
    assert ir.query == {"tag": ["a", "b"]}


def test_comments_and_blank_lines_are_ignored():
    ir = parse_http("# comentário\n// outro\n\n@service gh\n\nGET /x\n")
    assert ir.service == "gh" and ir.path == "/x"


def test_text_body_kept_as_string():
    ir = parse_http("@service gh\n\nPOST /x\nContent-Type: text/plain\n\nolá mundo\n")
    assert ir.body == "olá mundo"


# ── recusas deliberadas ─────────────────────────────────────────────────────

def test_absolute_url_is_rejected():
    """A herança do REST Client que NÃO aceitamos: o host é da conexão."""
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n\nGET https://evil.com/x\n")
    assert e.value.code == "absolute_url"


def test_protocol_relative_is_rejected():
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n\nGET //evil.com/x\n")
    assert e.value.code == "absolute_url"


def test_missing_service_directive_is_rejected():
    with pytest.raises(GatewayError) as e:
        parse_http("GET /x\n")
    assert e.value.code == "missing_service"


def test_relative_path_required():
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n\nGET api/x\n")
    assert e.value.code == "relative_path_required"


def test_reserved_header_rejected_at_parse_time():
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n\nGET /x\nAuthorization: Bearer abc\n")
    assert e.value.code == "reserved_header"


def test_variables_and_scripts_are_unsupported():
    """Sem {{vars}}/env: é por onde ferramentas do gênero vazam segredo."""
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n@token = {{$processEnv TOKEN}}\n\nGET /x\n")
    assert e.value.code == "unsupported_directive"


def test_bad_service_line():
    with pytest.raises(GatewayError) as e:
        parse_http("@service NOME_INVALIDO!\n\nGET /x\n")
    assert e.value.code == "bad_service_line"


def test_bad_request_line():
    with pytest.raises(GatewayError) as e:
        parse_http("@service gh\n\nFETCH /x\n")
    assert e.value.code == "bad_request_line"


def test_invalid_json_body_with_json_content_type():
    with pytest.raises(GatewayError) as e:
        parse_http('@service gh\n\nPOST /x\nContent-Type: application/json\n\n{quebrado\n')
    assert e.value.code == "bad_json_body"


# ── múltiplos blocos ────────────────────────────────────────────────────────

def test_split_requests_by_separator():
    text = "### um\n@service ha\n\nGET /1\n\n### dois\n@service gh\n\nGET /2\n"
    blocks = split_requests(text)
    assert len(blocks) == 2
    assert parse_http(blocks[0]).path == "/1"
    assert parse_http(blocks[1]).path == "/2"


def test_split_ignores_empty_blocks():
    assert len(split_requests("###\n\n###\n@service ha\n\nGET /x\n")) == 1


def test_one_char_slug_rejected():
    """Mesmo mínimo do registry (2-40 chars) — um formato, uma regra."""
    with pytest.raises(GatewayError) as e:
        parse_http("@service a\n\nGET /x\n")
    assert e.value.code == "bad_service_line"


def test_parsed_ir_flows_through_the_same_validator():
    """Um formato, uma verdade: o .http produz a MESMA IR da tool."""
    from core.gateway.ir import AuthType, ConnectionConfig
    from core.gateway.validator import validate_request

    ir = parse_http(_EXEMPLO)
    conn = ConnectionConfig(id="homeassistant", name="HA", base_url="http://ha", auth_type=AuthType.NONE)
    out = validate_request(ir, conn)
    assert out.path == "/api/services/light/turn_on"
