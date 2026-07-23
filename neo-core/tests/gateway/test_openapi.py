"""Índice OpenAPI + service_docs (F3 — §9 do plano).

A garantia central: o modelo recebe as operações RELEVANTES, nunca a spec
inteira (que estouraria o contexto).
"""

from __future__ import annotations

import json

import pytest

from core.gateway.audit import AuditWriter
from core.gateway.broker import CredentialBroker, CredentialResolver
from core.gateway.confirm import ConfirmStore
from core.gateway.executor import HTTPExecutor
from core.gateway.ir import ConnectionConfig
from core.gateway.openapi import parse_spec
from core.gateway.service import GatewayService

_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Minha API", "version": "2.1"},
    "paths": {
        "/repos/{owner}/{repo}/issues": {
            "get": {
                "summary": "List repository issues",
                "operationId": "issues/list",
                "tags": ["issues"],
                "parameters": [
                    {"name": "state", "in": "query", "schema": {"type": "string"}},
                    {"name": "owner", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
            },
            "post": {
                "summary": "Create an issue",
                "tags": ["issues"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["title"],
                                "properties": {"title": {}, "body": {}, "labels": {}},
                            }
                        }
                    }
                },
            },
        },
        "/user/starred": {"get": {"summary": "List repos starred by the user", "tags": ["activity"]}},
        "/gists": {"delete": {"summary": "Delete a gist", "tags": ["gists"]}},
    },
}


def test_parse_indexes_all_operations():
    index = parse_spec(_SPEC)
    assert len(index) == 4
    assert index.title == "Minha API" and index.version == "2.1"


def test_parse_accepts_json_string_and_yaml():
    assert len(parse_spec(json.dumps(_SPEC))) == 4
    yaml_spec = """
openapi: 3.0.0
info:
  title: Y
paths:
  /ping:
    get:
      summary: ping
"""
    index = parse_spec(yaml_spec)
    assert len(index) == 1 and index.operations[0].path == "/ping"


def test_parse_swagger2_basepath_prefixes_path():
    swagger = {
        "swagger": "2.0",
        "basePath": "/api/v3",
        "paths": {"/pets": {"get": {"summary": "list pets"}}},
    }
    index = parse_spec(swagger)
    assert index.operations[0].path == "/api/v3/pets"


def test_parse_rejects_garbage_with_human_message():
    for bad, msg in [("", "vazia"), ("{}", "paths"), ("nao sou spec", "")]:
        with pytest.raises(ValueError):
            parse_spec(bad)


def test_search_ranks_by_intent():
    index = parse_spec(_SPEC)
    hits = index.search("criar issue", limit=3)
    assert hits, "deveria achar algo"
    # 'issues' casa no path e no tag; o POST tem 'Create' no summary.
    assert any(h.method == "POST" and "issues" in h.path for h in hits)


def test_search_without_query_returns_head():
    index = parse_spec(_SPEC)
    assert len(index.search("", limit=2)) == 2


def test_operation_dict_is_compact_and_useful():
    index = parse_spec(_SPEC)
    post = next(o for o in index.operations if o.method == "POST")
    d = post.to_dict()
    assert d["method"] == "POST" and "{owner}" in d["path"]
    # O hint do corpo diz os campos e marca o obrigatório — sem despejar o schema.
    assert "title*" in d["body"]
    assert len(json.dumps(d)) < 400


def test_body_hint_for_swagger2_body_param():
    swagger = {
        "swagger": "2.0",
        "paths": {
            "/x": {
                "post": {
                    "parameters": [
                        {"in": "body", "name": "b", "schema": {"properties": {"a": {}, "b": {}}}}
                    ]
                }
            }
        },
    }
    index = parse_spec(swagger)
    assert "json: {a, b}" in index.operations[0].to_dict()["body"]


# ── service_docs ────────────────────────────────────────────────────────────

class _Registry:
    def __init__(self, conn) -> None:
        self._conn = conn

    async def get(self, slug):
        return self._conn if slug == self._conn.id else None

    async def list_enabled(self):
        return [self._conn]


def _service(conn) -> GatewayService:
    return GatewayService(
        registry=_Registry(conn),
        broker=CredentialBroker(CredentialResolver(settings=None)),
        http_executor=HTTPExecutor(),
        confirm_store=ConfirmStore(),
        audit=AuditWriter(None),
    )


def _conn(**kw) -> ConnectionConfig:
    base = dict(id="gh", name="GitHub", base_url="https://api.github.com")
    base.update(kw)
    return ConnectionConfig(**base)


@pytest.mark.asyncio
async def test_service_docs_returns_relevant_operations_not_whole_spec():
    svc = _service(_conn(openapi_spec=json.dumps(_SPEC)))
    out = await svc.describe_service("gh", "criar issue")
    assert out["spec"]["total_operations"] == 4
    assert out["spec"]["showing"] <= 8
    assert any(op["method"] == "POST" for op in out["operations"])
    # O payload é uma seleção, não a spec: muito menor que o original.
    assert len(json.dumps(out)) < len(json.dumps(_SPEC))


@pytest.mark.asyncio
async def test_service_docs_without_spec_points_to_docs_url():
    svc = _service(_conn(docs_url="https://docs.exemplo"))
    out = await svc.describe_service("gh")
    assert "operations" not in out
    assert out["docs_url"] == "https://docs.exemplo"
    assert "Sem OpenAPI" in out["note"]


@pytest.mark.asyncio
async def test_service_docs_with_broken_spec_degrades_gracefully():
    svc = _service(_conn(openapi_spec="{isto nao é json nem yaml: ["))
    out = await svc.describe_service("gh")
    assert "operations" not in out  # não quebra; cai no modo sem spec
    assert "note" in out


@pytest.mark.asyncio
async def test_service_docs_unknown_service():
    svc = _service(_conn())
    out = await svc.describe_service("nao-existe")
    assert out["error"] == "unknown_service"
