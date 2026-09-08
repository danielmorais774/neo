"""Contrato de schema dos cadastros generativos (docs/datasets-crud-plan.md §5).

O MESMO JSON alimenta quatro consumidores:

1. o DDL (`core/datasets/ddl.py`) — que tipo de coluna criar;
2. a validação de write (`core/datasets/service.py`) — o que aceitar;
3. o texto entregue ao cérebro (`render_for_model`) — o que ele pode preencher;
4. o neo-panel — que widget desenhar.

Por isso o conjunto de tipos é **FECHADO** (:data:`FIELD_TYPES`). É essa
lista fechada, mais :func:`validate_identifier`, que torna seguro montar DDL por
interpolação de string: nada que venha do cliente ou do LLM chega ao SQL sem
passar por uma das duas.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Identificadores (slug do cadastro e nome de campo). Fechado de propósito:
# minúscula, dígito e underscore, começando por letra. Sem aspas, sem espaço,
# sem acento — nada que precise de quoting no Postgres.
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,32}$")

# Nomes da espinha fixa (core/datasets/ddl.py): um campo do usuário com um
# destes sombrearia a coluna de controle e quebraria o soft delete/auditoria.
SPINE_COLUMNS = frozenset({"id", "user_id", "created_at", "updated_at", "deleted_at"})

# Palavras reservadas do Postgres que apareceriam num cadastro real. Não é a
# lista completa do SQL:2016 (seriam ~900); é a interseção plausível com nomes
# que alguém daria a um campo. O quoting do DDL protege o resto.
_RESERVED = frozenset(
    {
        "all", "and", "any", "array", "as", "asc", "both", "case", "cast", "check",
        "collate", "column", "constraint", "create", "current_date", "current_time",
        "current_timestamp", "current_user", "default", "deferrable", "desc",
        "distinct", "do", "else", "end", "except", "false", "for", "foreign", "from",
        "grant", "group", "having", "in", "initially", "intersect", "into", "is",
        "join", "leading", "left", "like", "limit", "localtime", "localtimestamp",
        "not", "null", "offset", "on", "only", "or", "order", "outer", "placing",
        "primary", "references", "returning", "right", "select", "session_user",
        "similar", "some", "symmetric", "table", "then", "to", "trailing", "true",
        "union", "unique", "user", "using", "when", "where", "window", "with",
    }
)

FieldType = Literal[
    "texto",
    "texto_longo",
    "inteiro",
    "numero",
    "dinheiro",
    "booleano",
    "data",
    "data_hora",
    "opcao",
    "multi_opcao",
    "relacao",
    "arquivo",
    "json",
]


class FieldTypeSpec(BaseModel):
    """Como um tipo do cadastro se traduz para banco, painel e prompt."""

    pg_type: str
    widget: str
    #: Rótulo curto usado no bloco que o cérebro lê (`render_for_model`).
    hint: str
    #: Aceita a chave ``options`` (enum)?
    takes_options: bool = False


FIELD_TYPES: dict[str, FieldTypeSpec] = {
    "texto": FieldTypeSpec(pg_type="VARCHAR", widget="input", hint="texto"),
    "texto_longo": FieldTypeSpec(pg_type="TEXT", widget="textarea", hint="texto longo"),
    "inteiro": FieldTypeSpec(pg_type="BIGINT", widget="number", hint="inteiro"),
    "numero": FieldTypeSpec(pg_type="NUMERIC(18,4)", widget="number", hint="número"),
    # Dinheiro é SEMPRE centavos inteiros, nunca float — regra herdada de fin_*
    # (db/models/finance.py). O painel formata; o banco guarda int.
    "dinheiro": FieldTypeSpec(pg_type="BIGINT", widget="money", hint="dinheiro (centavos)"),
    "booleano": FieldTypeSpec(pg_type="BOOLEAN", widget="switch", hint="sim/não"),
    "data": FieldTypeSpec(pg_type="DATE", widget="date", hint="data (AAAA-MM-DD)"),
    "data_hora": FieldTypeSpec(pg_type="TIMESTAMPTZ", widget="datetime", hint="data e hora"),
    "opcao": FieldTypeSpec(
        pg_type="VARCHAR(60)", widget="select", hint="opção", takes_options=True
    ),
    "multi_opcao": FieldTypeSpec(
        pg_type="TEXT[]", widget="multiselect", hint="várias opções", takes_options=True
    ),
    "relacao": FieldTypeSpec(pg_type="UUID", widget="reference", hint="id de outro cadastro"),
    "arquivo": FieldTypeSpec(pg_type="UUID", widget="file", hint="id de arquivo"),
    "json": FieldTypeSpec(pg_type="JSONB", widget="json", hint="JSON"),
}

_MAX_FIELDS = 60
_MAX_TEXT_LEN = 500
_DEFAULT_TEXT_LEN = 200
_MAX_LONGTEXT_BYTES = 32 * 1024
_MAX_OPTIONS = 100


class SchemaError(ValueError):
    """Erro de definição/validação de cadastro — vira mensagem para o humano/LLM."""


def validate_identifier(value: str, *, what: str = "identificador") -> str:
    """Valida slug/nome de campo. É a ÚNICA porta para o que entra em DDL.

    Levanta :class:`SchemaError` com motivo legível — a mensagem vai tanto para o
    painel quanto para o resultado da tool (onde ensina o modelo a corrigir).
    """
    raw = (value or "").strip().lower()
    if not raw:
        raise SchemaError(f"{what} vazio.")
    if not _IDENT_RE.match(raw):
        raise SchemaError(
            f"{what} inválido: {value!r}. Use minúsculas, números e underscore, "
            "começando por letra (até 33 chars). Ex.: 'preco_custo'."
        )
    if raw in _RESERVED:
        raise SchemaError(f"{what} {raw!r} é palavra reservada do Postgres — escolha outro.")
    return raw


def slugify(value: str) -> str:
    """'Ordens de Serviço' → 'ordens_de_servico'. Best-effort para o instalador."""
    decomposed = unicodedata.normalize("NFD", (value or "").lower())
    ascii_only = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_only).strip("_")
    slug = re.sub(r"_+", "_", slug)[:33]
    if slug and not slug[0].isalpha():
        slug = f"c_{slug}"[:33]
    return slug


class FieldDef(BaseModel):
    """Um campo do cadastro."""

    name: str
    type: FieldType
    label: str = ""
    required: bool = False
    #: só `texto`: tamanho máximo da coluna.
    max: int | None = None
    #: só `opcao`/`multi_opcao`.
    options: list[str] = Field(default_factory=list)
    #: só `relacao`: slug do dataset apontado.
    dataset: str | None = None
    default: Any = None
    #: aparece na lista do painel mesmo fora de ``list_columns``? (reservado)
    help: str = ""

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        name = validate_identifier(v, what="nome de campo")
        if name in SPINE_COLUMNS:
            raise SchemaError(
                f"'{name}' é uma coluna de controle do Neo (id/user_id/created_at/"
                "updated_at/deleted_at) — escolha outro nome."
            )
        return name

    @model_validator(mode="after")
    def _check_consistency(self) -> "FieldDef":
        spec = FIELD_TYPES[self.type]
        if self.options and not spec.takes_options:
            raise SchemaError(
                f"campo '{self.name}': 'options' só vale para opcao/multi_opcao "
                f"(este é {self.type})."
            )
        if spec.takes_options:
            if not self.options:
                raise SchemaError(f"campo '{self.name}': {self.type} exige 'options'.")
            if len(self.options) > _MAX_OPTIONS:
                raise SchemaError(f"campo '{self.name}': no máximo {_MAX_OPTIONS} opções.")
            cleaned = [str(o).strip() for o in self.options if str(o).strip()]
            if len(set(cleaned)) != len(cleaned):
                raise SchemaError(f"campo '{self.name}': opções repetidas.")
            self.options = cleaned
        if self.type == "relacao":
            if not self.dataset:
                raise SchemaError(
                    f"campo '{self.name}': relacao exige 'dataset' (o slug do cadastro apontado)."
                )
            self.dataset = validate_identifier(self.dataset, what="dataset da relação")
        elif self.dataset:
            raise SchemaError(f"campo '{self.name}': 'dataset' só vale para type=relacao.")
        if self.type == "texto":
            size = int(self.max or _DEFAULT_TEXT_LEN)
            if not 1 <= size <= _MAX_TEXT_LEN:
                raise SchemaError(
                    f"campo '{self.name}': 'max' deve ficar entre 1 e {_MAX_TEXT_LEN} "
                    "(acima disso use texto_longo)."
                )
            self.max = size
        elif self.max is not None and self.type != "texto":
            self.max = None
        if not self.label:
            self.label = self.name.replace("_", " ").capitalize()
        return self

    # ------------------------------------------------------------------

    @property
    def pg_type(self) -> str:
        """Tipo Postgres desta coluna (já com o tamanho do `texto`)."""
        if self.type == "texto":
            return f"VARCHAR({int(self.max or _DEFAULT_TEXT_LEN)})"
        return FIELD_TYPES[self.type].pg_type

    def describe(self) -> str:
        """Uma linha para o bloco que o cérebro lê."""
        spec = FIELD_TYPES[self.type]
        bits = [spec.hint]
        if self.type in ("opcao", "multi_opcao"):
            bits = [f"{spec.hint}: {'|'.join(self.options)}"]
        if self.type == "relacao":
            bits = [f"id de `{self.dataset}`"]
        if self.required:
            bits.append("obrigatório")
        return f"{self.name} ({', '.join(bits)})"


class DatasetSchema(BaseModel):
    """Definição completa de um cadastro. Serializada em `neo_data_dataset.schema_json`."""

    version: int = 1
    fields: list[FieldDef]
    list_columns: list[str] = Field(default_factory=list)
    search_fields: list[str] = Field(default_factory=list)
    order_by: str = ""
    label_field: str = ""

    @model_validator(mode="after")
    def _check(self) -> "DatasetSchema":
        if not self.fields:
            raise SchemaError("O cadastro precisa de pelo menos um campo.")
        if len(self.fields) > _MAX_FIELDS:
            raise SchemaError(f"Máximo de {_MAX_FIELDS} campos por cadastro.")
        names = [f.name for f in self.fields]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise SchemaError(f"Campos repetidos: {', '.join(sorted(dupes))}.")
        known = set(names)

        for key in ("list_columns", "search_fields"):
            bad = [c for c in getattr(self, key) if c not in known]
            if bad:
                raise SchemaError(
                    f"{key} cita campo inexistente: {', '.join(bad)}. "
                    f"Campos: {', '.join(names)}."
                )
        # Busca textual só faz sentido em coluna de texto.
        bad_search = [
            c for c in self.search_fields
            if self.field(c) is not None and self.field(c).type not in ("texto", "texto_longo", "opcao")
        ]
        if bad_search:
            raise SchemaError(
                f"search_fields só aceita texto/texto_longo/opcao — inválidos: {', '.join(bad_search)}."
            )

        if self.label_field and self.label_field not in known:
            raise SchemaError(f"label_field '{self.label_field}' não é um campo do cadastro.")
        if not self.label_field:
            first_text = next((f.name for f in self.fields if f.type == "texto"), names[0])
            self.label_field = first_text
        if not self.list_columns:
            self.list_columns = names[:5]
        if not self.search_fields:
            self.search_fields = [
                f.name for f in self.fields if f.type in ("texto", "texto_longo")
            ][:3]
        self.order_by = self._normalize_order(self.order_by, known)
        return self

    def _normalize_order(self, raw: str, known: set[str]) -> str:
        """'nome asc' → validado. Vazio → mais recente primeiro (created_at desc)."""
        value = (raw or "").strip()
        if not value:
            return "created_at desc"
        parts = value.split()
        column = parts[0].lower()
        direction = (parts[1].lower() if len(parts) > 1 else "asc")
        if direction not in ("asc", "desc"):
            raise SchemaError(f"order_by inválido: {raw!r}. Use '<campo> asc|desc'.")
        if column not in known and column not in SPINE_COLUMNS:
            raise SchemaError(
                f"order_by cita campo inexistente: {column}. Campos: {', '.join(sorted(known))}."
            )
        return f"{column} {direction}"

    # ------------------------------------------------------------------

    def field(self, name: str) -> FieldDef | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def render_for_model(self, *, slug: str, table_name: str = "") -> str:
        """Bloco compacto anexado ao corpo da skill lida (~40-60 tokens + campos)."""
        where = f" (tabela neo_data.{table_name})" if table_name else ""
        lines = " · ".join(f.describe() for f in self.fields)
        return f"### Cadastro `{slug}`{where}\nCampos: {lines}"


# ---------------------------------------------------------------------------
# Coerção de valores (entrada do painel e do LLM → tipo do banco)
# ---------------------------------------------------------------------------

_MONEY_RE = re.compile(r"^-?[\d.,\s]+$")


def parse_money_to_cents(raw: Any) -> int:
    """'1.234,56' | '1234.56' | 1234.56 | 123456 → centavos inteiros.

    Formato BR primeiro (é o do usuário), com fallback para o americano. Um
    ``int`` puro é interpretado como REAIS, não centavos — é o que alguém quer
    dizer com ``preco=50``; quem já tem centavos manda string ("50,00").
    """
    if isinstance(raw, bool):
        raise SchemaError("valor de dinheiro inválido: booleano.")
    if isinstance(raw, int):
        return raw * 100
    if isinstance(raw, float):
        return int(round(raw * 100))
    text = str(raw or "").strip()
    if not text:
        raise SchemaError("valor de dinheiro vazio.")
    text = text.replace("R$", "").replace(" ", "").strip()
    if not _MONEY_RE.match(text):
        raise SchemaError(f"valor de dinheiro inválido: {raw!r}. Use 1234,56 ou 1234.56.")
    if "," in text:
        # BR: ponto é milhar, vírgula é decimal.
        text = text.replace(".", "").replace(",", ".")
    try:
        return int((Decimal(text) * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    except (InvalidOperation, ValueError) as exc:
        raise SchemaError(f"valor de dinheiro inválido: {raw!r}.") from exc


_TRUE = {"true", "1", "sim", "yes", "y", "s", "on", "verdadeiro"}
_FALSE = {"false", "0", "nao", "não", "no", "n", "off", "falso"}


def coerce_value(field: FieldDef, raw: Any) -> Any:
    """Converte o valor recebido para o que o asyncpg/SQLAlchemy espera.

    ``None`` passa direto (o NULL é decidido em service.py, que conhece o
    ``required``). Erro vira :class:`SchemaError` com o nome do campo — o
    resultado da tool devolve essa frase e o modelo corrige sozinho.
    """
    if raw is None:
        return None
    t = field.type
    try:
        if t in ("texto", "texto_longo"):
            value = str(raw)
            if t == "texto":
                limit = int(field.max or _DEFAULT_TEXT_LEN)
                if len(value) > limit:
                    raise SchemaError(
                        f"campo '{field.name}': {len(value)} chars excede o limite de {limit}."
                    )
            elif len(value.encode("utf-8")) > _MAX_LONGTEXT_BYTES:
                raise SchemaError(f"campo '{field.name}': texto acima de 32 KB.")
            return value
        if t == "inteiro":
            if isinstance(raw, bool):
                raise SchemaError(f"campo '{field.name}': esperado inteiro.")
            return int(str(raw).strip())
        if t == "numero":
            return Decimal(str(raw).strip().replace(",", "."))
        if t == "dinheiro":
            return parse_money_to_cents(raw)
        if t == "booleano":
            if isinstance(raw, bool):
                return raw
            token = str(raw).strip().lower()
            if token in _TRUE:
                return True
            if token in _FALSE:
                return False
            raise SchemaError(f"campo '{field.name}': esperado sim/não, veio {raw!r}.")
        if t == "data":
            if isinstance(raw, datetime):
                return raw.date()
            if isinstance(raw, date):
                return raw
            return date.fromisoformat(str(raw).strip()[:10])
        if t == "data_hora":
            if isinstance(raw, datetime):
                return raw
            token = str(raw).strip().replace("Z", "+00:00")
            return datetime.fromisoformat(token)
        if t == "opcao":
            value = str(raw).strip()
            if value not in field.options:
                raise SchemaError(
                    f"campo '{field.name}': '{value}' não é uma opção válida. "
                    f"Opções: {', '.join(field.options)}."
                )
            return value
        if t == "multi_opcao":
            items = raw if isinstance(raw, (list, tuple)) else [
                p.strip() for p in str(raw).split(",") if p.strip()
            ]
            values = [str(i).strip() for i in items]
            invalid = [v for v in values if v not in field.options]
            if invalid:
                raise SchemaError(
                    f"campo '{field.name}': opções inválidas {', '.join(invalid)}. "
                    f"Válidas: {', '.join(field.options)}."
                )
            return values
        if t in ("relacao", "arquivo"):
            from uuid import UUID

            return UUID(str(raw).strip())
        if t == "json":
            if isinstance(raw, (dict, list)):
                return raw
            import json as _json

            return _json.loads(str(raw))
    except SchemaError:
        raise
    except Exception as exc:  # noqa: BLE001 — toda falha de conversão vira mensagem
        raise SchemaError(
            f"campo '{field.name}': valor {raw!r} não é um(a) {FIELD_TYPES[t].hint}."
        ) from exc
    raise SchemaError(f"campo '{field.name}': tipo desconhecido {t!r}.")


def serialize_value(field: FieldDef, value: Any) -> Any:
    """Valor do banco → JSON do painel/tool (o inverso de :func:`coerce_value`)."""
    if value is None:
        return None
    if field.type == "numero" and isinstance(value, Decimal):
        return float(value)
    if field.type in ("data", "data_hora"):
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    if field.type in ("relacao", "arquivo"):
        return str(value)
    if field.type == "multi_opcao":
        return list(value or [])
    return value


# ---------------------------------------------------------------------------
# Parsing com erro legível
# ---------------------------------------------------------------------------


def _flatten_validation_error(exc: Exception) -> str:
    """Desembrulha o ValidationError do pydantic numa frase.

    Sem isto o modelo (e o painel) recebem
    ``1 validation error for DatasetSchema\\nfields.0.name\\n  Value error, …`` —
    ruído que atrapalha justamente quem deveria corrigir o argumento. Aqui sai
    só o motivo, prefixado pelo caminho do campo quando ele ajuda.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts: list[str] = []
    for err in errors():
        message = str(err.get("msg", "")).replace("Value error, ", "").strip()
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "fields")
        parts.append(f"{location}: {message}" if location and location not in message else message)
    return "; ".join(dict.fromkeys(parts)) or str(exc)


def parse_schema(raw: Any) -> DatasetSchema:
    """``DatasetSchema.model_validate`` com erro de uma linha (ver acima)."""
    if isinstance(raw, DatasetSchema):
        return raw
    try:
        return DatasetSchema.model_validate(raw or {})
    except SchemaError:
        raise
    except Exception as exc:  # noqa: BLE001 — ValidationError e afins
        raise SchemaError(_flatten_validation_error(exc)) from exc


def parse_fields(raw: Any) -> list[FieldDef]:
    """Lista de campos (o argumento ``campos`` do ``data_define``), erro legível."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SchemaError(
            "'campos' precisa ser uma lista de objetos "
            '{"name": "...", "type": "texto|dinheiro|inteiro|...", "label": "...", "required": true}.'
        )
    out: list[FieldDef] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SchemaError(f"campo inválido: {item!r} — esperado um objeto.")
        raw_type = str(item.get("type") or "").strip().lower()
        if raw_type not in FIELD_TYPES:
            raise SchemaError(
                f"tipo de campo desconhecido: {item.get('type')!r}. "
                f"Tipos válidos: {', '.join(sorted(FIELD_TYPES))}."
            )
        try:
            out.append(FieldDef.model_validate({**item, "type": raw_type}))
        except SchemaError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(_flatten_validation_error(exc)) from exc
    return out
