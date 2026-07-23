"""Ingestão e busca de specs OpenAPI/Swagger (F3 — §9 do plano).

Resolve o pedido original: *"analise a OpenAPI do meu serviço e use um endpoint
que ainda não tem tool"*. O modelo NÃO lê a spec inteira (uma OpenAPI real tem
centenas de KB — estouraria o contexto e custaria caro): ela é indexada uma vez
em operações compactas, e o ``service_docs`` devolve só as N mais relevantes
para a intenção do turno. É o mesmo princípio do File Store (manifesto + fatia)
e do progressive disclosure das skills.

Suporta OpenAPI 3.x e Swagger 2.0 (o `basePath` do 2.0 vira prefixo do path).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_METHODS = ("get", "head", "post", "put", "patch", "delete")
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


@dataclass
class Operation:
    """Uma operação da spec, já compacta o suficiente para caber no contexto."""

    method: str
    path: str
    summary: str = ""
    operation_id: str = ""
    tags: list[str] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    body_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"method": self.method, "path": self.path}
        if self.summary:
            out["summary"] = self.summary
        if self.params:
            out["params"] = self.params
        if self.body_hint:
            out["body"] = self.body_hint
        return out

    @property
    def haystack(self) -> str:
        return " ".join([self.path, self.summary, self.operation_id, " ".join(self.tags)])


class OpenAPIIndex:
    """Índice de operações de uma spec. Busca por keyword com score simples.

    Sem embeddings de propósito: o vocabulário aqui é técnico e literal
    ("turn_on", "issues", "containers"), onde casar token bate semântica — e não
    paga latência nem depende do modelo de embedding estar quente.
    """

    def __init__(self, operations: list[Operation], *, title: str = "", version: str = "") -> None:
        self.operations = operations
        self.title = title
        self.version = version

    def __len__(self) -> int:
        return len(self.operations)

    def search(self, query: str, limit: int = 8) -> list[Operation]:
        """Operações mais relevantes para ``query``. Sem query → as primeiras."""
        if not query or not query.strip():
            return self.operations[:limit]
        wanted = _tokens(query)
        if not wanted:
            return self.operations[:limit]
        scored: list[tuple[int, int, Operation]] = []
        for idx, op in enumerate(self.operations):
            have = _tokens(op.haystack)
            score = len(wanted & have)
            # Casar no path vale mais: é o que o modelo precisa acertar.
            score += len(wanted & _tokens(op.path))
            # Método citado na intenção ("listar" ~ GET não; mas "post" sim).
            if op.method.lower() in wanted:
                score += 1
            if score:
                scored.append((-score, idx, op))  # idx desempata mantendo a ordem da spec
        scored.sort()
        return [op for _, _, op in scored[:limit]]


def parse_spec(raw: str | dict[str, Any]) -> OpenAPIIndex:
    """Converte uma spec (JSON/YAML/dict) num índice de operações.

    Levanta ``ValueError`` com mensagem humana — quem chama é o painel/tool.
    """
    spec = _load(raw)
    if not isinstance(spec, dict):
        raise ValueError("spec inválida: esperava um objeto JSON/YAML no topo.")

    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("spec sem 'paths' — não parece uma OpenAPI/Swagger.")

    # Swagger 2.0 põe o prefixo em basePath; OpenAPI 3 põe em servers[].url.
    base_path = str(spec.get("basePath") or "").rstrip("/")
    info = spec.get("info") or {}

    operations: list[Operation] = []
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters") or []
        for method in _METHODS:
            op_spec = item.get(method)
            if not isinstance(op_spec, dict):
                continue
            operations.append(
                _build_operation(
                    method=method.upper(),
                    path=f"{base_path}{path}" if base_path else str(path),
                    op_spec=op_spec,
                    shared_params=shared_params,
                )
            )

    if not operations:
        raise ValueError("spec sem operações reconhecíveis (get/post/…) nos paths.")
    logger.info("[gateway] openapi indexada: %d operações", len(operations))
    return OpenAPIIndex(
        operations,
        title=str(info.get("title") or ""),
        version=str(info.get("version") or ""),
    )


def _load(raw: str | dict[str, Any]) -> Any:
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        raise ValueError("spec vazia.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml  # já é dependência do projeto (pyyaml)

        return yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"spec não é JSON nem YAML válido: {exc}") from exc


def _build_operation(
    *, method: str, path: str, op_spec: dict[str, Any], shared_params: list[Any]
) -> Operation:
    params: list[dict[str, Any]] = []
    for p in list(shared_params) + list(op_spec.get("parameters") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        if not name:
            continue
        entry: dict[str, Any] = {"name": name, "in": p.get("in", "query")}
        if p.get("required"):
            entry["required"] = True
        schema = p.get("schema") or {}
        ptype = schema.get("type") or p.get("type")
        if ptype:
            entry["type"] = ptype
        params.append(entry)

    return Operation(
        method=method,
        path=path,
        summary=str(op_spec.get("summary") or op_spec.get("description") or "")[:200],
        operation_id=str(op_spec.get("operationId") or ""),
        tags=[str(t) for t in (op_spec.get("tags") or [])],
        params=params[:12],
        body_hint=_body_hint(op_spec),
    )


def _body_hint(op_spec: dict[str, Any]) -> str:
    """Descrição CURTA do corpo: os nomes dos campos de topo bastam para o modelo
    montar a chamada; o schema inteiro (com $ref) não cabe no contexto."""
    body = op_spec.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content") or {}
        for mime in ("application/json", *content.keys()):
            media = content.get(mime)
            if isinstance(media, dict):
                schema = media.get("schema") or {}
                props = schema.get("properties")
                if isinstance(props, dict) and props:
                    required = set(schema.get("required") or [])
                    fields = [
                        f"{k}{'*' if k in required else ''}" for k in list(props)[:12]
                    ]
                    return f"{mime}: {{{', '.join(fields)}}}"
                if schema.get("$ref"):
                    return f"{mime} (schema: {str(schema['$ref']).split('/')[-1]})"
                return str(mime)
    # Swagger 2.0: body é um parameter com in=body.
    for p in op_spec.get("parameters") or []:
        if isinstance(p, dict) and p.get("in") == "body":
            schema = p.get("schema") or {}
            props = schema.get("properties")
            if isinstance(props, dict) and props:
                return f"json: {{{', '.join(list(props)[:12])}}}"
            return "json"
    return ""
