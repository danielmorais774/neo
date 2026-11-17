"""Executor das tools ``data_*`` (docs/datasets-crud-plan.md §6).

User-scoped como o de finanças: o executor GLOBAL fica sem usuário e recusa
educadamente; o cognitivo cria um fresco por request no ``CognitiveReadExecutor``
(sem estado compartilhado, sem corrida entre sessões).

Contrato ``execute(nome, args) -> dict`` — o primeiro dos dois que o
``ToolExecutor._dispatch`` conhece.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.datasets.registry import DatasetNotFound, DatasetRegistry
from core.datasets.schema import DatasetSchema, SchemaError, parse_fields
from core.datasets.service import DatasetService

logger = logging.getLogger(__name__)


class DatasetsExecutor:
    """Despacha ``data_list``/``data_get``/``data_create``/… para o DatasetService."""

    def __init__(self, *, db: Any, registry: DatasetRegistry) -> None:
        self._registry = registry
        self._service = DatasetService(db=db, registry=registry)
        self._user_id: uuid.UUID | None = None

    def set_context(self, user_id: uuid.UUID | None) -> None:
        self._user_id = user_id

    # ------------------------------------------------------------------

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if self._user_id is None:
            raise ValueError(
                f"{tool_name} precisa de um usuário identificado (os cadastros são dele)."
            )
        try:
            return await self._dispatch(tool_name, arguments or {})
        except (SchemaError, DatasetNotFound) as exc:
            # Erro de uso vira MENSAGEM, não stacktrace: é ela que ensina o
            # modelo a corrigir o argumento no round seguinte.
            raise ValueError(str(exc)) from exc

    async def _dispatch(self, tool_name: str, args: dict[str, Any]) -> Any:
        if tool_name == "data_define":
            return await self._define(args)

        slug = str(args.get("dataset") or "").strip()
        dataset = await self._registry.resolve(slug, user_id=self._user_id)

        if tool_name == "data_schema":
            return {
                "cadastro": dataset.slug,
                "titulo": dataset.title,
                "descricao": dataset.description,
                "campos": [f.describe() for f in dataset.schema.fields],
                "total_linhas": await self._service.count_rows(dataset, user_id=self._user_id),
            }

        if tool_name == "data_list":
            return await self._service.list_rows(
                dataset,
                user_id=self._user_id,
                busca=args.get("busca"),
                filtro=args.get("filtro") or None,
                ordem=args.get("ordem"),
                limite=int(args.get("limite") or 25),
                pagina=int(args.get("pagina") or 1),
            )

        if tool_name == "data_get":
            row = await self._service.get_row(
                dataset, user_id=self._user_id, row_id=_as_uuid(args.get("id"))
            )
            if row is None:
                raise SchemaError(
                    f"Não achei essa linha em '{dataset.slug}'. Use data_list para "
                    "localizar o id certo."
                )
            return {"cadastro": dataset.slug, "linha": row}

        if tool_name == "data_create":
            result = await self._service.create_row(
                dataset, user_id=self._user_id, campos=args.get("campos") or {}, actor="tool",
            )
            return {"cadastro": dataset.slug, **result}

        if tool_name == "data_update":
            result = await self._service.update_row(
                dataset,
                user_id=self._user_id,
                row_id=_as_uuid(args.get("id")),
                campos=args.get("campos") or {},
                actor="tool",
            )
            return {"cadastro": dataset.slug, **result}

        if tool_name == "data_delete":
            result = await self._service.delete_row(
                dataset,
                user_id=self._user_id,
                row_id=_as_uuid(args.get("id")),
                confirmar=bool(args.get("confirmar")),
                actor="tool",
            )
            return {"cadastro": dataset.slug, **result}

        raise ValueError(f"Tool desconhecida no módulo datasets: {tool_name}")

    # ------------------------------------------------------------------

    async def _define(self, args: dict[str, Any]) -> Any:
        """Cria um cadastro ou acrescenta campos a um existente.

        Só ADITIVO: remover campo ou trocar tipo é destrutivo e fica no painel,
        com humano na frente (§3.4 do plano).
        """
        slug = str(args.get("slug") or "").strip().lower()
        modo = str(args.get("modo") or "criar").strip().lower()
        campos = parse_fields(args.get("campos"))

        existing = await self._registry.get(slug, user_id=self._user_id)
        if existing is not None and modo == "criar":
            raise SchemaError(
                f"Já existe um cadastro '{slug}' (campos: "
                f"{', '.join(existing.schema.field_names)}). Use modo='acrescentar' "
                "para adicionar campos, ou escolha outro nome."
            )
        if existing is None and modo == "acrescentar":
            raise SchemaError(f"Não existe cadastro '{slug}' para acrescentar campos.")

        if existing is None:
            schema = DatasetSchema(fields=campos)
            dataset = await self._registry.create(
                slug=slug,
                title=str(args.get("titulo") or slug.replace("_", " ").capitalize()),
                description=str(args.get("descricao") or ""),
                schema=schema,
                user_id=self._user_id,
            )
            await self._service.ensure_table(dataset)
            await self._registry.audit(
                dataset_slug=dataset.slug, action="create", actor="tool",
                user_id=self._user_id, payload={"campos": [f.name for f in campos]},
            )
            # Conferido: a tabela existe MESMO? Sem esta releitura, "criei o
            # cadastro" seria afirmação sem prova.
            columns = await self._service.table_columns(dataset)
            return {
                "ok": bool(columns),
                "cadastro": dataset.slug,
                "tabela": f"neo_data.{dataset.table_name}",
                "conferido": {"colunas_no_banco": columns},
                "painel": f"/dados/{dataset.slug}",
                "nota": (
                    "Cadastro criado e tela disponível no painel. Para o Neo usá-lo "
                    "na conversa, ele precisa de uma skill que declare "
                    f"datasets: [{dataset.slug}]."
                ),
            }

        merged = list(existing.schema.fields)
        known = {f.name for f in merged}
        novos = [f for f in campos if f.name not in known]
        if not novos:
            raise SchemaError(
                f"Nenhum campo novo — '{slug}' já tem: {', '.join(sorted(known))}."
            )
        merged += novos
        new_schema = DatasetSchema(
            fields=merged,
            list_columns=existing.schema.list_columns,
            search_fields=existing.schema.search_fields,
            order_by=existing.schema.order_by,
            label_field=existing.schema.label_field,
        )
        diff = await self._service.apply_schema_diff(existing, new_schema)
        updated = await self._registry.update_schema(existing, new_schema)
        await self._registry.audit(
            dataset_slug=updated.slug, action="add_field", actor="tool",
            user_id=self._user_id, payload=diff,
        )
        columns = await self._service.table_columns(updated)
        return {
            "ok": all(f.name in columns for f in novos),
            "cadastro": updated.slug,
            "acrescentados": [f.name for f in novos],
            "conferido": {"colunas_no_banco": columns},
        }


def _as_uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value).strip())
    except (ValueError, AttributeError, TypeError) as exc:
        raise SchemaError(
            f"'{value}' não é um id válido. Use o `id` que veio de data_list/data_create."
        ) from exc
