"""Construtor de DDL dos cadastros (docs/datasets-crud-plan.md §4.3 e §9.3).

Este é o único módulo do Neo que monta SQL por interpolação de string. Ele só é
seguro porque **nada aqui aceita texto livre**:

- todo identificador passou por :func:`core.datasets.schema.validate_identifier`;
- todo tipo veio do mapa fechado :data:`~core.datasets.schema.FIELD_TYPES`;
- o schema é a constante :data:`DATA_SCHEMA`, nunca um parâmetro.

Literais de `CHECK IN (…)` (as opções de um campo `opcao`) são o único texto
livre que chega ao SQL — e vão por :func:`_quote_literal`, que dobra aspas.
"""

from __future__ import annotations

import secrets

from core.datasets.schema import DatasetSchema, FieldDef, SchemaError, validate_identifier

#: Schema Postgres dedicado. Constante — nunca vem de fora. Mantém as tabelas de
#: cadastro longe do `public` (protege o Neo de um bug aqui) e deixa o
#: `alembic autogenerate` honesto (o `include_object` do env.py filtra por ele).
DATA_SCHEMA = "neo_data"

#: Colunas de controle criadas em TODA tabela de cadastro.
SPINE_DDL = (
    "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
    "user_id UUID NOT NULL",
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "deleted_at TIMESTAMPTZ NULL",
)


def new_code() -> str:
    """Sufixo único da tabela — 4 chars hex (`produtos_k3f9`)."""
    return secrets.token_hex(2)


def build_table_name(slug: str, code: str) -> str:
    """`<slug>_<code>`, validado dos dois lados."""
    safe_slug = validate_identifier(slug, what="slug do cadastro")
    if not code or not code.isalnum() or len(code) > 8:
        raise SchemaError(f"código de tabela inválido: {code!r}.")
    name = f"{safe_slug}_{code.lower()}"
    if len(name) > 63:  # limite de identificador do Postgres
        raise SchemaError(f"nome de tabela longo demais: {name!r}.")
    return name


def qualified(table_name: str) -> str:
    """`neo_data.produtos_k3f9` — o table_name JÁ veio do registry, nunca do cliente."""
    if not table_name or not table_name.replace("_", "").isalnum():
        raise SchemaError(f"nome de tabela inválido: {table_name!r}.")
    return f"{DATA_SCHEMA}.{table_name}"


def _quote_literal(value: str) -> str:
    """Literal SQL seguro (aspas simples dobradas) — só para as opções do CHECK."""
    return "'" + str(value).replace("'", "''") + "'"


def _column_ddl(field: FieldDef) -> str:
    """`preco BIGINT` — sem NOT NULL: obrigatoriedade é validada na aplicação.

    De propósito: um campo vira obrigatório depois de a tabela já ter linhas, e
    um `ALTER … SET NOT NULL` falharia no meio do `apply`. A regra vive no
    schema (que o painel e as tools respeitam), não na coluna.
    """
    return f"{field.name} {field.pg_type}"


def _check_constraint(table_name: str, field: FieldDef) -> str | None:
    """CHECK do enum de um campo `opcao`. `multi_opcao` fica só na aplicação."""
    if field.type != "opcao" or not field.options:
        return None
    values = ", ".join(_quote_literal(o) for o in field.options)
    return (
        f"ALTER TABLE {qualified(table_name)} "
        f"ADD CONSTRAINT ck_{table_name}_{field.name} "
        f"CHECK ({field.name} IS NULL OR {field.name} IN ({values}))"
    )


def create_schema_sql() -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {DATA_SCHEMA}"


def create_table_sql(table_name: str, schema: DatasetSchema) -> list[str]:
    """Todos os statements para materializar um cadastro novo, em ordem."""
    table = qualified(table_name)
    columns = list(SPINE_DDL) + [_column_ddl(f) for f in schema.fields]
    statements = [
        f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(columns) + "\n)",
        # Todo acesso filtra por dono e por não-apagado.
        f"CREATE INDEX IF NOT EXISTS ix_{table_name}_user ON {table} (user_id)",
        f"CREATE INDEX IF NOT EXISTS ix_{table_name}_alive ON {table} (user_id, deleted_at)",
        f"CREATE INDEX IF NOT EXISTS ix_{table_name}_created ON {table} (created_at DESC)",
    ]
    for field in schema.fields:
        constraint = _check_constraint(table_name, field)
        if constraint:
            statements.append(constraint)
    # Ordenação padrão da lista costuma ser por label — índice barato que evita
    # sort em memória na tela do painel.
    if schema.label_field and schema.field(schema.label_field):
        statements.append(
            f"CREATE INDEX IF NOT EXISTS ix_{table_name}_label "
            f"ON {table} (user_id, {schema.label_field})"
        )
    return statements


def add_column_sql(table_name: str, field: FieldDef) -> list[str]:
    """ALTER aditivo — o único que o instalador aplica sozinho (§3.4 do plano)."""
    table = qualified(table_name)
    statements = [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_column_ddl(field)}"]
    constraint = _check_constraint(table_name, field)
    if constraint:
        statements.append(constraint)
    return statements


def drop_column_sql(table_name: str, field_name: str) -> list[str]:
    """ALTER destrutivo — só com confirmação humana explícita."""
    safe = validate_identifier(field_name, what="nome de campo")
    table = qualified(table_name)
    return [
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table_name}_{safe}",
        f"ALTER TABLE {table} DROP COLUMN IF EXISTS {safe}",
    ]


def replace_check_sql(table_name: str, field: FieldDef) -> list[str]:
    """Troca o CHECK quando as opções de um campo `opcao` mudam."""
    table = qualified(table_name)
    statements = [
        f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table_name}_{field.name}"
    ]
    constraint = _check_constraint(table_name, field)
    if constraint:
        statements.append(constraint)
    return statements


def drop_table_sql(table_name: str) -> str:
    """Só chamado por remoção explícita de cadastro — NUNCA por desinstalar skill."""
    return f"DROP TABLE IF EXISTS {qualified(table_name)}"
