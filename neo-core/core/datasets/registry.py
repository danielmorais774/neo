"""Registry dos cadastros — cache em memória sobre ``neo_data_dataset``.

Mesmo desenho do :class:`~core.gateway.registry.ConnectionRegistry`: carrega uma
vez, serve de memória e invalida quando alguém escreve. O que ele acrescenta é a
**resolução de nome** (:meth:`DatasetRegistry.resolve`): o modelo escreve
``produto`` quando o slug é ``produtos``, e falhar nisso é o defeito que já
custou caro em ``file_id`` (memória ``neo-file-id-name-resolution``). Aqui o
erro ensina: quem não resolve recebe a lista dos cadastros que existem.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.datasets.ddl import build_table_name, new_code
from core.datasets.schema import DatasetSchema, SchemaError, validate_identifier

logger = logging.getLogger(__name__)


class DatasetNotFound(LookupError):
    """Cadastro inexistente — a mensagem já lista os disponíveis."""


class Dataset:
    """Uma definição carregada (schema já parseado)."""

    __slots__ = (
        "id", "user_id", "slug", "code", "table_name", "title",
        "description", "schema", "skill_name", "enabled", "schema_version",
    )

    def __init__(self, row: Any) -> None:
        self.id: uuid.UUID = row.id
        self.user_id: uuid.UUID | None = row.user_id
        self.slug: str = row.slug
        self.code: str = row.code
        self.table_name: str = row.table_name
        self.title: str = row.title
        self.description: str = row.description or ""
        self.skill_name: str | None = row.skill_name
        self.enabled: bool = bool(row.enabled)
        self.schema_version: int = int(row.schema_version or 1)
        self.schema: DatasetSchema = DatasetSchema.model_validate(row.schema_json or {})

    def to_dict(self, *, with_schema: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "table_name": self.table_name,
            "skill_name": self.skill_name,
            "enabled": self.enabled,
            "user_id": str(self.user_id) if self.user_id else None,
        }
        if with_schema:
            data["schema"] = self.schema.model_dump()
        return data

    def describe_for_model(self) -> str:
        return self.schema.render_for_model(slug=self.slug, table_name=self.table_name)


def _singularish(word: str) -> set[str]:
    """Variações plausíveis de plural/singular em PT — só para desempate."""
    w = (word or "").lower()
    out = {w}
    if w.endswith("es"):
        out.add(w[:-2])
    if w.endswith("s"):
        out.add(w[:-1])
    else:
        out.add(w + "s")
        out.add(w + "es")
    if w.endswith("ao"):
        out.add(w[:-2] + "oes")
    if w.endswith("oes"):
        out.add(w[:-3] + "ao")
    return out


class DatasetRegistry:
    """Carrega, cacheia e escreve definições de cadastro."""

    def __init__(self, *, db: Any) -> None:
        self._db = db
        self._cache: list[Dataset] | None = None

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def invalidate(self) -> None:
        self._cache = None

    async def refresh(self) -> None:
        self._cache = await self._load()

    async def _ensure(self) -> list[Dataset]:
        if self._cache is None:
            self._cache = await self._load()
        return self._cache

    async def _load(self) -> list[Dataset]:
        async with self._db.get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, user_id, slug, code, table_name, title, description, "
                        "schema_json, skill_name, enabled, schema_version "
                        "FROM neo_data_dataset ORDER BY slug"
                    )
                )
            ).fetchall()
        out: list[Dataset] = []
        for row in rows:
            try:
                out.append(Dataset(row))
            except Exception as exc:  # noqa: BLE001 — definição podre não derruba o resto
                logger.warning(
                    "[DatasetRegistry] cadastro '%s' ignorado (schema inválido): %s",
                    getattr(row, "slug", "?"), exc,
                )
        return out

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    async def all(self, *, user_id: uuid.UUID | None = None, enabled_only: bool = False) -> list[Dataset]:
        items = await self._ensure()
        if enabled_only:
            items = [d for d in items if d.enabled]
        if user_id is not None:
            items = [d for d in items if d.user_id is None or d.user_id == user_id]
        return items

    async def get(self, slug: str, *, user_id: uuid.UUID | None = None) -> Dataset | None:
        """Match EXATO por slug (respeitando o dono). Sem fuzzy — ver `resolve`."""
        for item in await self.all(user_id=user_id):
            if item.slug == (slug or "").strip().lower():
                return item
        return None

    async def resolve(self, slug: str, *, user_id: uuid.UUID | None = None) -> Dataset:
        """Exato → singular/plural → erro que LISTA o que existe.

        A terceira etapa é o ponto: um erro que só diz "não existe" vira retry
        infinito no tool loop; um que mostra o catálogo é corrigido no round
        seguinte.
        """
        wanted = (slug or "").strip().lower()
        available = await self.all(user_id=user_id, enabled_only=True)
        exact = next((d for d in available if d.slug == wanted), None)
        if exact is not None:
            return exact
        variants = _singularish(wanted)
        near = [d for d in available if d.slug in variants or wanted in _singularish(d.slug)]
        if len(near) == 1:
            logger.info("[DatasetRegistry] '%s' resolvido para '%s'", wanted, near[0].slug)
            return near[0]
        names = ", ".join(d.slug for d in available) or "(nenhum cadastro criado ainda)"
        if len(near) > 1:
            raise DatasetNotFound(
                f"'{slug}' é ambíguo — pode ser {', '.join(d.slug for d in near)}. "
                "Use o nome exato."
            )
        raise DatasetNotFound(f"Cadastro '{slug}' não existe. Cadastros disponíveis: {names}.")

    # ------------------------------------------------------------------
    # Escrita da DEFINIÇÃO (o DDL fica no installer/service)
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        schema: DatasetSchema,
        user_id: uuid.UUID | None,
        skill_name: str | None = None,
    ) -> Dataset:
        """Insere a definição e devolve o Dataset já com ``table_name`` sorteado."""
        safe_slug = validate_identifier(slug, what="slug do cadastro")
        if await self.get(safe_slug, user_id=user_id) is not None:
            raise SchemaError(
                f"Já existe um cadastro '{safe_slug}'. Use outro nome ou altere o existente."
            )
        code = new_code()
        table_name = build_table_name(safe_slug, code)
        now = datetime.utcnow()
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO neo_data_dataset (id, user_id, slug, code, table_name, "
                    "title, description, schema_json, skill_name, enabled, schema_version, "
                    "created_at, updated_at) VALUES (:id, :user_id, :slug, :code, :table_name, "
                    ":title, :description, CAST(:schema_json AS JSONB), :skill_name, true, "
                    ":schema_version, :created_at, :updated_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "slug": safe_slug,
                    "code": code,
                    "table_name": table_name,
                    "title": (title or safe_slug).strip()[:80],
                    "description": (description or "").strip(),
                    "schema_json": schema.model_dump_json(),
                    "skill_name": skill_name,
                    "schema_version": schema.version,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await session.commit()
        self.invalidate()
        created = await self.get(safe_slug, user_id=user_id)
        if created is None:  # pragma: no cover — insert sem leitura de volta
            raise SchemaError(f"Falha ao criar o cadastro '{safe_slug}'.")
        return created

    async def update_schema(self, dataset: Dataset, schema: DatasetSchema) -> Dataset:
        """Grava um schema novo para um cadastro existente (o DDL é do chamador)."""
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    "UPDATE neo_data_dataset SET schema_json = CAST(:schema_json AS JSONB), "
                    "schema_version = :schema_version, updated_at = :updated_at WHERE id = :id"
                ),
                {
                    "schema_json": schema.model_dump_json(),
                    "schema_version": schema.version,
                    "updated_at": datetime.utcnow(),
                    "id": dataset.id,
                },
            )
            await session.commit()
        self.invalidate()
        refreshed = await self.get(dataset.slug, user_id=dataset.user_id)
        return refreshed or dataset

    async def update_meta(
        self, dataset: Dataset, *, title: str | None = None,
        description: str | None = None, enabled: bool | None = None,
    ) -> Dataset:
        sets, params = [], {"id": dataset.id, "updated_at": datetime.utcnow()}
        if title is not None:
            sets.append("title = :title")
            params["title"] = title.strip()[:80]
        if description is not None:
            sets.append("description = :description")
            params["description"] = description.strip()
        if enabled is not None:
            sets.append("enabled = :enabled")
            params["enabled"] = bool(enabled)
        if not sets:
            return dataset
        async with self._db.get_session() as session:
            await session.execute(
                text(f"UPDATE neo_data_dataset SET {', '.join(sets)}, updated_at = :updated_at WHERE id = :id"),
                params,
            )
            await session.commit()
        self.invalidate()
        return await self.get(dataset.slug, user_id=dataset.user_id) or dataset

    async def delete_definition(self, dataset: Dataset) -> None:
        """Remove só a DEFINIÇÃO. A tabela física é decisão separada (§3.5)."""
        async with self._db.get_session() as session:
            await session.execute(
                text("DELETE FROM neo_data_dataset WHERE id = :id"), {"id": dataset.id}
            )
            await session.commit()
        self.invalidate()

    # ------------------------------------------------------------------

    async def audit(
        self,
        *,
        dataset_slug: str,
        action: str,
        actor: str,
        user_id: uuid.UUID | None,
        row_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Trilha do irreversível. Falha aqui NUNCA derruba a operação principal."""
        import json

        try:
            async with self._db.get_session() as session:
                await session.execute(
                    text(
                        "INSERT INTO neo_data_audit (id, user_id, dataset_slug, action, actor, "
                        "row_id, payload, created_at) VALUES (:id, :user_id, :slug, :action, "
                        ":actor, :row_id, CAST(:payload AS JSONB), :created_at)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "user_id": user_id,
                        "slug": dataset_slug,
                        "action": action,
                        "actor": actor,
                        "row_id": row_id,
                        "payload": json.dumps(payload or {}, default=str),
                        "created_at": datetime.utcnow(),
                    },
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DatasetRegistry] auditoria falhou (%s/%s): %s", dataset_slug, action, exc)
