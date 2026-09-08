"""CRUD sobre as tabelas de cadastro (docs/datasets-crud-plan.md §6 e §9.2).

Camada única usada pelas tools do cérebro E pelas rotas do painel — o que
garante que os dois obedeçam às mesmas regras (obrigatoriedade, enum, soft
delete, auditoria). Duas invariantes que vêm de defeitos já pagos neste repo:

**Read-back.** Todo write relê a linha do banco e devolve em ``conferido``.
Sem isso volta o "salvei!" sem ter salvo — foi exatamente o que a skill HA fez
com ``entity_id`` inventado (memória ``neo-ha-false-success-invented-entity``).

**Soft delete.** ``deleted_at`` em vez de DELETE, e o delete sem confirmação
devolve *preview* em vez de apagar — o mesmo desenho do
``fin_edit_transaction(preview=true)``.

Identificadores no SQL: só o ``table_name`` vindo do registry e nomes de coluna
que existem no ``DatasetSchema`` (já validados por ``validate_identifier`` na
definição). Todo VALOR vai por bind param.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.datasets.ddl import (
    add_column_sql,
    create_schema_sql,
    create_table_sql,
    drop_column_sql,
    qualified,
    replace_check_sql,
)
from core.datasets.registry import Dataset, DatasetRegistry
from core.datasets.schema import (
    DatasetSchema,
    FieldDef,
    SchemaError,
    coerce_value,
    serialize_value,
)

logger = logging.getLogger(__name__)

MAX_LIMIT = 200
DEFAULT_LIMIT = 25

#: Operadores aceitos no `filtro`. Fechado: é o que permite montar o WHERE por
#: interpolação sem nunca aceitar SQL do chamador (o caso analítico livre é do
#: `sql_query_ro`, que tem as defesas dele).
_OPERATORS: dict[str, str] = {
    "eq": "=",
    "ne": "<>",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


class DatasetService:
    """Operações de dados de um cadastro. Sempre com ``user_id`` explícito."""

    def __init__(self, *, db: Any, registry: DatasetRegistry) -> None:
        self._db = db
        self._registry = registry

    # ------------------------------------------------------------------
    # DDL
    # ------------------------------------------------------------------

    async def ensure_table(self, dataset: Dataset) -> None:
        """Materializa a tabela do cadastro. Idempotente (tudo IF NOT EXISTS)."""
        statements = [create_schema_sql()] + create_table_sql(dataset.table_name, dataset.schema)
        async with self._db.get_session() as session:
            for stmt in statements:
                try:
                    await session.execute(text(stmt))
                except Exception as exc:  # noqa: BLE001
                    # CHECK duplicado é o caso normal de re-apply (não existe
                    # ADD CONSTRAINT IF NOT EXISTS no Postgres).
                    if "already exists" in str(exc).lower():
                        continue
                    raise
            await session.commit()
        logger.info("[datasets] tabela %s pronta", qualified(dataset.table_name))

    async def apply_schema_diff(
        self, dataset: Dataset, new_schema: DatasetSchema, *, allow_destructive: bool = False
    ) -> dict[str, list[str]]:
        """Aplica a diferença entre o schema atual e o novo.

        Aditivo (campo novo, enum ampliado) roda sozinho; destrutivo (remover
        campo) só com ``allow_destructive`` — que o instalador nunca liga
        automaticamente e as tools nunca expõem (§3.4 do plano).
        """
        old = {f.name: f for f in dataset.schema.fields}
        new = {f.name: f for f in new_schema.fields}
        added = [n for n in new if n not in old]
        removed = [n for n in old if n not in new]
        changed_type = [n for n in new if n in old and new[n].pg_type != old[n].pg_type]
        changed_options = [
            n for n in new
            if n in old and new[n].type == "opcao" and new[n].options != old[n].options
        ]

        if changed_type:
            raise SchemaError(
                "Trocar o TIPO de um campo é destrutivo e não é feito automaticamente: "
                f"{', '.join(changed_type)}. Crie um campo novo e migre os dados."
            )
        if removed and not allow_destructive:
            raise SchemaError(
                f"Remover campo apaga os dados da coluna: {', '.join(removed)}. "
                "Confirme a remoção explicitamente no painel."
            )

        statements: list[str] = []
        for name in added:
            statements += add_column_sql(dataset.table_name, new[name])
        for name in changed_options:
            statements += replace_check_sql(dataset.table_name, new[name])
        if allow_destructive:
            for name in removed:
                statements += drop_column_sql(dataset.table_name, name)

        if statements:
            async with self._db.get_session() as session:
                for stmt in statements:
                    try:
                        await session.execute(text(stmt))
                    except Exception as exc:  # noqa: BLE001
                        if "already exists" in str(exc).lower():
                            continue
                        raise
                await session.commit()
        return {"adicionados": added, "removidos": removed if allow_destructive else [], "opcoes": changed_options}

    async def table_columns(self, dataset: Dataset) -> list[str]:
        """Colunas REAIS da tabela — usado pelo `verify` do instalador."""
        async with self._db.get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'neo_data' AND table_name = :t"
                    ),
                    {"t": dataset.table_name},
                )
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def _select_columns(self, schema: DatasetSchema) -> str:
        cols = ["id", "created_at", "updated_at"] + schema.field_names
        return ", ".join(cols)

    def _build_where(
        self,
        dataset: Dataset,
        *,
        user_id: uuid.UUID,
        busca: str | None,
        filtro: dict[str, Any] | None,
        include_deleted: bool,
    ) -> tuple[str, dict[str, Any]]:
        clauses = ["user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id}
        if not include_deleted:
            clauses.append("deleted_at IS NULL")

        schema = dataset.schema
        if busca and schema.search_fields:
            ors = []
            for idx, name in enumerate(schema.search_fields):
                key = f"busca_{idx}"
                ors.append(f"{name}::text ILIKE :{key}")
                params[key] = f"%{busca.strip()}%"
            clauses.append("(" + " OR ".join(ors) + ")")

        for idx, (raw_field, raw_cond) in enumerate((filtro or {}).items()):
            field = schema.field(raw_field)
            if field is None:
                raise SchemaError(
                    f"filtro cita campo inexistente '{raw_field}'. "
                    f"Campos: {', '.join(schema.field_names)}."
                )
            key = f"f_{idx}"
            op, value = "eq", raw_cond
            if isinstance(raw_cond, dict):
                op = str(raw_cond.get("op") or raw_cond.get("operador") or "eq").lower()
                value = raw_cond.get("valor", raw_cond.get("value"))

            if op in ("nulo", "is_null"):
                clauses.append(f"{field.name} IS {'NULL' if value in (None, True, 'true') else 'NOT NULL'}")
                continue
            if op in ("contem", "contains", "like"):
                clauses.append(f"{field.name}::text ILIKE :{key}")
                params[key] = f"%{value}%"
                continue
            if op in ("em", "in"):
                items = value if isinstance(value, (list, tuple)) else [value]
                coerced = [coerce_value(field, v) for v in items]
                if not coerced:
                    clauses.append("false")
                    continue
                keys = []
                for j, v in enumerate(coerced):
                    k = f"{key}_{j}"
                    keys.append(f":{k}")
                    params[k] = v
                clauses.append(f"{field.name} IN ({', '.join(keys)})")
                continue
            sql_op = _OPERATORS.get(op)
            if sql_op is None:
                raise SchemaError(
                    f"operador de filtro desconhecido: '{op}'. "
                    f"Use: {', '.join(sorted(_OPERATORS))}, contem, em, nulo."
                )
            if value is None:
                clauses.append(f"{field.name} IS {'NULL' if sql_op == '=' else 'NOT NULL'}")
                continue
            clauses.append(f"{field.name} {sql_op} :{key}")
            params[key] = coerce_value(field, value)

        return " AND ".join(clauses), params

    def _order_clause(self, schema: DatasetSchema, ordem: str | None) -> str:
        raw = (ordem or schema.order_by or "created_at desc").strip()
        parts = raw.split()
        column = parts[0].lower()
        direction = parts[1].lower() if len(parts) > 1 else "asc"
        if direction not in ("asc", "desc"):
            direction = "asc"
        known = set(schema.field_names) | {"created_at", "updated_at", "id"}
        if column not in known:
            raise SchemaError(
                f"ordem cita campo inexistente '{column}'. Campos: {', '.join(sorted(known))}."
            )
        return f"{column} {direction.upper()} NULLS LAST"

    async def list_rows(
        self,
        dataset: Dataset,
        *,
        user_id: uuid.UUID,
        busca: str | None = None,
        filtro: dict[str, Any] | None = None,
        ordem: str | None = None,
        limite: int = DEFAULT_LIMIT,
        pagina: int = 1,
        include_deleted: bool = False,
    ) -> dict[str, Any]:
        schema = dataset.schema
        where, params = self._build_where(
            dataset, user_id=user_id, busca=busca, filtro=filtro, include_deleted=include_deleted
        )
        order = self._order_clause(schema, ordem)
        limit = max(1, min(int(limite or DEFAULT_LIMIT), MAX_LIMIT))
        page = max(1, int(pagina or 1))
        offset = (page - 1) * limit
        table = qualified(dataset.table_name)

        async with self._db.get_session() as session:
            total = (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE {where}"), params
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    text(
                        f"SELECT {self._select_columns(schema)} FROM {table} "
                        f"WHERE {where} ORDER BY {order} LIMIT :__limit OFFSET :__offset"
                    ),
                    {**params, "__limit": limit, "__offset": offset},
                )
            ).mappings().fetchall()

        return {
            "cadastro": dataset.slug,
            "total": int(total),
            "pagina": page,
            "por_pagina": limit,
            "tem_mais": offset + len(rows) < int(total),
            "linhas": [self._serialize_row(schema, r) for r in rows],
        }

    async def get_row(
        self, dataset: Dataset, *, user_id: uuid.UUID, row_id: uuid.UUID,
        include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        table = qualified(dataset.table_name)
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        async with self._db.get_session() as session:
            row = (
                await session.execute(
                    text(
                        f"SELECT {self._select_columns(dataset.schema)} FROM {table} "
                        f"WHERE id = :id AND user_id = :user_id{clause}"
                    ),
                    {"id": row_id, "user_id": user_id},
                )
            ).mappings().first()
        return self._serialize_row(dataset.schema, row) if row else None

    def _serialize_row(self, schema: DatasetSchema, row: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"id": str(row["id"])}
        for field in schema.fields:
            out[field.name] = serialize_value(field, row[field.name])
        out["_criado_em"] = row["created_at"].isoformat() if row["created_at"] else None
        out["_atualizado_em"] = row["updated_at"].isoformat() if row["updated_at"] else None
        return out

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def _prepare(
        self, schema: DatasetSchema, campos: dict[str, Any], *, partial: bool
    ) -> dict[str, Any]:
        """Valida e converte. Campo desconhecido LISTA os válidos — erro que ensina."""
        if not isinstance(campos, dict):
            raise SchemaError("'campos' precisa ser um objeto {campo: valor}.")
        unknown = [k for k in campos if schema.field(k) is None]
        if unknown:
            raise SchemaError(
                f"Campos desconhecidos: {', '.join(unknown)}. "
                f"Campos deste cadastro: {', '.join(schema.field_names)}."
            )
        prepared: dict[str, Any] = {}
        for field in schema.fields:
            if field.name in campos:
                prepared[field.name] = coerce_value(field, campos[field.name])
            elif not partial and field.default is not None:
                prepared[field.name] = coerce_value(field, field.default)

        if not partial:
            missing = [
                f.name for f in schema.fields
                if f.required and prepared.get(f.name) in (None, "")
            ]
            if missing:
                raise SchemaError(
                    f"Faltam campos obrigatórios: {', '.join(missing)}."
                )
        else:
            emptied = [
                f.name for f in schema.fields
                if f.required and f.name in prepared and prepared[f.name] in (None, "")
            ]
            if emptied:
                raise SchemaError(
                    f"Campos obrigatórios não podem ficar vazios: {', '.join(emptied)}."
                )
        return prepared

    async def create_row(
        self, dataset: Dataset, *, user_id: uuid.UUID, campos: dict[str, Any], actor: str = "painel",
    ) -> dict[str, Any]:
        prepared = self._prepare(dataset.schema, campos, partial=False)
        row_id = uuid.uuid4()
        now = datetime.utcnow()
        columns = ["id", "user_id", "created_at", "updated_at"] + list(prepared)
        params: dict[str, Any] = {
            "id": row_id, "user_id": user_id, "created_at": now, "updated_at": now, **prepared,
        }
        placeholders = ", ".join(f":{c}" for c in columns)
        table = qualified(dataset.table_name)
        async with self._db.get_session() as session:
            await session.execute(
                text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"),
                params,
            )
            await session.commit()
        # Read-back: a resposta afirma o que o BANCO tem, não o que mandamos.
        conferido = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        return {"ok": conferido is not None, "id": str(row_id), "conferido": conferido}

    async def update_row(
        self, dataset: Dataset, *, user_id: uuid.UUID, row_id: uuid.UUID,
        campos: dict[str, Any], actor: str = "painel",
    ) -> dict[str, Any]:
        atual = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        if atual is None:
            raise SchemaError(f"Linha {row_id} não existe em '{dataset.slug}' (ou já foi excluída).")
        prepared = self._prepare(dataset.schema, campos, partial=True)
        if not prepared:
            return {"ok": True, "id": str(row_id), "conferido": atual, "nota": "nada a alterar"}
        sets = ", ".join(f"{c} = :{c}" for c in prepared)
        table = qualified(dataset.table_name)
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    f"UPDATE {table} SET {sets}, updated_at = :__now "
                    "WHERE id = :__id AND user_id = :__user_id AND deleted_at IS NULL"
                ),
                {**prepared, "__now": datetime.utcnow(), "__id": row_id, "__user_id": user_id},
            )
            await session.commit()
        conferido = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        return {"ok": conferido is not None, "id": str(row_id), "conferido": conferido}

    async def delete_row(
        self, dataset: Dataset, *, user_id: uuid.UUID, row_id: uuid.UUID,
        confirmar: bool = False, actor: str = "painel",
    ) -> dict[str, Any]:
        """Soft delete. Sem ``confirmar`` NÃO apaga: devolve o que sairia."""
        atual = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        if atual is None:
            raise SchemaError(f"Linha {row_id} não existe em '{dataset.slug}' (ou já foi excluída).")
        if not confirmar:
            return {
                "ok": False,
                "preview": True,
                "id": str(row_id),
                "sairia": atual,
                "aviso": (
                    "Nada foi excluído. Confirme com o usuário e chame de novo com "
                    "confirmar=true para apagar."
                ),
            }
        table = qualified(dataset.table_name)
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    f"UPDATE {table} SET deleted_at = :__now, updated_at = :__now "
                    "WHERE id = :__id AND user_id = :__user_id AND deleted_at IS NULL"
                ),
                {"__now": datetime.utcnow(), "__id": row_id, "__user_id": user_id},
            )
            await session.commit()
        await self._registry.audit(
            dataset_slug=dataset.slug, action="delete", actor=actor,
            user_id=user_id, row_id=row_id, payload=atual,
        )
        ainda = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        return {
            "ok": ainda is None,
            "id": str(row_id),
            "excluido": atual,
            "conferido": {"existe_ainda": ainda is not None},
            "nota": "Exclusão reversível — os dados continuam no banco marcados como apagados.",
        }

    async def restore_row(
        self, dataset: Dataset, *, user_id: uuid.UUID, row_id: uuid.UUID, actor: str = "painel",
    ) -> dict[str, Any]:
        table = qualified(dataset.table_name)
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    f"UPDATE {table} SET deleted_at = NULL, updated_at = :__now "
                    "WHERE id = :__id AND user_id = :__user_id"
                ),
                {"__now": datetime.utcnow(), "__id": row_id, "__user_id": user_id},
            )
            await session.commit()
        await self._registry.audit(
            dataset_slug=dataset.slug, action="restore", actor=actor, user_id=user_id, row_id=row_id,
        )
        conferido = await self.get_row(dataset, user_id=user_id, row_id=row_id)
        return {"ok": conferido is not None, "id": str(row_id), "conferido": conferido}

    # ------------------------------------------------------------------

    async def count_rows(self, dataset: Dataset, *, user_id: uuid.UUID | None = None) -> int:
        table = qualified(dataset.table_name)
        clause = "deleted_at IS NULL"
        params: dict[str, Any] = {}
        if user_id is not None:
            clause += " AND user_id = :user_id"
            params["user_id"] = user_id
        async with self._db.get_session() as session:
            try:
                return int(
                    (await session.execute(text(f"SELECT count(*) FROM {table} WHERE {clause}"), params)).scalar_one()
                )
            except Exception:  # noqa: BLE001 — tabela ainda não materializada
                return 0
