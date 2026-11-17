"""API dos cadastros generativos (docs/datasets-crud-plan.md §F2/§F6).

Duas famílias de rota, servindo o MESMO ``DatasetService`` que as tools do
cérebro usam — é isso que garante que painel e Neo obedeçam às mesmas regras
(obrigatoriedade, enum, soft delete, auditoria):

- ``/api/v1/datasets`` — definições e linhas (a tela genérica do neo-panel);
- ``/api/v1/skills/{name}/install`` — plan/apply/verify/uninstall do instalador.

``GET /api/v1/datasets/tipos`` publica o mapa FECHADO de tipos: é dele que o
painel deriva os widgets, então backend e frente nunca divergem sobre o que
existe.

Auth: painel JWT, como as demais rotas de gestão (files.py §14.1).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from api.security import require_panel_jwt
from core.datasets.registry import Dataset, DatasetNotFound, DatasetRegistry
from core.datasets.schema import (
    FIELD_TYPES,
    DatasetSchema,
    SchemaError,
    parse_fields,
    parse_schema,
)
from core.datasets.service import DatasetService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/datasets", tags=["datasets"])
install_router = APIRouter(prefix="/api/v1/skills", tags=["skills-install"])

_registry: DatasetRegistry | None = None
_service: DatasetService | None = None
_installer: Any = None
_db: Any = None


def set_dependencies(
    registry: DatasetRegistry | None, db: Any = None, installer: Any = None
) -> None:
    global _registry, _service, _installer, _db
    _registry = registry
    _db = db
    _installer = installer
    _service = DatasetService(db=db, registry=registry) if registry is not None and db else None


def _require_registry() -> DatasetRegistry:
    if _registry is None or _service is None:
        # Flag off → a feature não existe para o cliente (rollback = flag off).
        raise HTTPException(
            status_code=503, detail="Cadastros desabilitados (NEO_DATASETS_ENABLED)"
        )
    return _registry


async def _resolve_user(user_id: str | None) -> UUID:
    """Mesma política do devices.py: explícito ganha; senão, o único usuário."""
    if user_id:
        try:
            return UUID(user_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id inválido")
    if _db is None:
        raise HTTPException(status_code=503, detail="Banco indisponível")
    user_ids = await _db.list_user_ids()
    if len(user_ids) != 1:
        raise HTTPException(
            status_code=400,
            detail=f"{len(user_ids)} usuários cadastrados — informe user_id explicitamente.",
        )
    return user_ids[0]


async def _get(slug: str, user: UUID) -> Dataset:
    registry = _require_registry()
    try:
        return await registry.resolve(slug, user_id=user)
    except DatasetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Metadados
# ---------------------------------------------------------------------------


# A docstring do módulo já dizia "Auth: painel JWT" — esta rota tinha ficado sem.
# Achado pela varredura de tests/auth/test_route_coverage.py.
@router.get("/tipos", dependencies=[Depends(require_panel_jwt)])
async def field_types() -> dict[str, Any]:
    """Mapa fechado de tipos — a fonte que o painel usa para escolher widget."""
    return {
        "tipos": [
            {
                "type": name,
                "widget": spec.widget,
                "hint": spec.hint,
                "options": spec.takes_options,
                "pg_type": spec.pg_type,
            }
            for name, spec in FIELD_TYPES.items()
        ]
    }


@router.get("", dependencies=[Depends(require_panel_jwt)])
async def list_datasets(user_id: str | None = Query(default=None)) -> dict[str, Any]:
    registry = _require_registry()
    user = await _resolve_user(user_id)
    items = await registry.all(user_id=user)
    out = []
    for dataset in items:
        out.append(
            {
                **dataset.to_dict(),
                "total_linhas": await _service.count_rows(dataset, user_id=user),
            }
        )
    return {"cadastros": out}


@router.get("/{slug}", dependencies=[Depends(require_panel_jwt)])
async def get_dataset(slug: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    return {
        **dataset.to_dict(),
        "total_linhas": await _service.count_rows(dataset, user_id=user),
        "colunas_no_banco": await _service.table_columns(dataset),
    }


@router.post("", status_code=201, dependencies=[Depends(require_panel_jwt)])
async def create_dataset(
    payload: dict[str, Any] = Body(...), user_id: str | None = Query(default=None)
) -> dict[str, Any]:
    """Cria a definição E materializa a tabela — as duas coisas ou nenhuma."""
    registry = _require_registry()
    user = await _resolve_user(user_id)
    try:
        fields = parse_fields(payload.get("campos") or payload.get("fields"))
        schema = DatasetSchema(
            fields=fields,
            list_columns=payload.get("list_columns") or [],
            search_fields=payload.get("search_fields") or [],
            order_by=payload.get("order_by") or "",
            label_field=payload.get("label_field") or "",
        )
        dataset = await registry.create(
            slug=str(payload.get("slug") or ""),
            title=str(payload.get("titulo") or payload.get("title") or ""),
            description=str(payload.get("descricao") or payload.get("description") or ""),
            schema=schema,
            user_id=user,
            skill_name=payload.get("skill_name"),
        )
        await _service.ensure_table(dataset)
    except SchemaError as exc:
        raise _bad_request(exc) from exc
    await registry.audit(
        dataset_slug=dataset.slug, action="create", actor="painel", user_id=user,
        payload={"campos": [f.name for f in schema.fields]},
    )
    return {
        **dataset.to_dict(),
        "colunas_no_banco": await _service.table_columns(dataset),
    }


@router.put("/{slug}/schema", dependencies=[Depends(require_panel_jwt)])
async def update_schema(
    slug: str,
    payload: dict[str, Any] = Body(...),
    permitir_destrutivo: bool = Query(default=False),
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Altera a estrutura. Remover campo exige ``permitir_destrutivo`` explícito."""
    registry = _require_registry()
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    try:
        new_schema = parse_schema(payload.get("schema") or payload)
        diff = await _service.apply_schema_diff(
            dataset, new_schema, allow_destructive=permitir_destrutivo
        )
        updated = await registry.update_schema(dataset, new_schema)
    except SchemaError as exc:
        raise _bad_request(exc) from exc
    await registry.audit(
        dataset_slug=slug,
        action="drop_field" if diff.get("removidos") else "add_field",
        actor="painel", user_id=user, payload=diff,
    )
    return {
        **updated.to_dict(),
        "alteracoes": diff,
        "colunas_no_banco": await _service.table_columns(updated),
    }


@router.patch("/{slug}", dependencies=[Depends(require_panel_jwt)])
async def patch_dataset(
    slug: str, payload: dict[str, Any] = Body(...), user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    registry = _require_registry()
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    updated = await registry.update_meta(
        dataset,
        title=payload.get("titulo") or payload.get("title"),
        description=payload.get("descricao") or payload.get("description"),
        enabled=payload.get("enabled"),
    )
    return updated.to_dict()


@router.delete("/{slug}", dependencies=[Depends(require_panel_jwt)])
async def delete_dataset(
    slug: str,
    apagar_dados: bool = Query(default=False),
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Remove a definição. A TABELA só cai com ``apagar_dados=true`` explícito."""
    registry = _require_registry()
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    linhas = await _service.count_rows(dataset, user_id=user)
    await registry.audit(
        dataset_slug=slug, action="drop_table" if apagar_dados else "delete", actor="painel",
        user_id=user, payload={"linhas": linhas, "tabela": dataset.table_name},
    )
    if apagar_dados:
        from sqlalchemy import text as _sql

        from core.datasets.ddl import drop_table_sql

        async with _db.get_session() as session:
            await session.execute(_sql(drop_table_sql(dataset.table_name)))
            await session.commit()
    await registry.delete_definition(dataset)
    return {
        "ok": True,
        "cadastro": slug,
        "dados_apagados": apagar_dados,
        "linhas_afetadas": linhas,
        "nota": (
            "Tabela removida do banco."
            if apagar_dados
            else f"Definição removida; a tabela neo_data.{dataset.table_name} "
            f"continua no banco com {linhas} linha(s)."
        ),
    }


# ---------------------------------------------------------------------------
# Linhas
# ---------------------------------------------------------------------------


@router.get("/{slug}/rows", dependencies=[Depends(require_panel_jwt)])
async def list_rows(
    slug: str,
    busca: str | None = Query(default=None),
    ordem: str | None = Query(default=None),
    limite: int = Query(default=25, ge=1, le=200),
    pagina: int = Query(default=1, ge=1),
    incluir_excluidos: bool = Query(default=False),
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    try:
        return await _service.list_rows(
            dataset, user_id=user, busca=busca, ordem=ordem, limite=limite,
            pagina=pagina, include_deleted=incluir_excluidos,
        )
    except SchemaError as exc:
        raise _bad_request(exc) from exc


@router.post("/{slug}/rows", status_code=201, dependencies=[Depends(require_panel_jwt)])
async def create_row(
    slug: str, payload: dict[str, Any] = Body(...), user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    try:
        return await _service.create_row(
            dataset, user_id=user, campos=payload.get("campos") or payload, actor="painel"
        )
    except SchemaError as exc:
        raise _bad_request(exc) from exc


@router.get("/{slug}/rows/{row_id}", dependencies=[Depends(require_panel_jwt)])
async def get_row(
    slug: str, row_id: UUID, user_id: str | None = Query(default=None)
) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    row = await _service.get_row(dataset, user_id=user, row_id=row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Linha não encontrada")
    return {"linha": row}


@router.put("/{slug}/rows/{row_id}", dependencies=[Depends(require_panel_jwt)])
async def update_row(
    slug: str, row_id: UUID, payload: dict[str, Any] = Body(...),
    user_id: str | None = Query(default=None),
) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    try:
        return await _service.update_row(
            dataset, user_id=user, row_id=row_id,
            campos=payload.get("campos") or payload, actor="painel",
        )
    except SchemaError as exc:
        raise _bad_request(exc) from exc


@router.delete("/{slug}/rows/{row_id}", dependencies=[Depends(require_panel_jwt)])
async def delete_row(
    slug: str, row_id: UUID, user_id: str | None = Query(default=None)
) -> dict[str, Any]:
    """O painel tem o humano na frente, então já vem confirmado — mas é SOFT."""
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    try:
        return await _service.delete_row(
            dataset, user_id=user, row_id=row_id, confirmar=True, actor="painel"
        )
    except SchemaError as exc:
        raise _bad_request(exc) from exc


@router.post("/{slug}/rows/{row_id}/restaurar", dependencies=[Depends(require_panel_jwt)])
async def restore_row(
    slug: str, row_id: UUID, user_id: str | None = Query(default=None)
) -> dict[str, Any]:
    user = await _resolve_user(user_id)
    dataset = await _get(slug, user)
    return await _service.restore_row(dataset, user_id=user, row_id=row_id, actor="painel")


# ---------------------------------------------------------------------------
# Instalador
# ---------------------------------------------------------------------------


def _require_installer() -> Any:
    if _installer is None:
        raise HTTPException(
            status_code=503, detail="Instalador indisponível (NEO_DATASETS_ENABLED)"
        )
    return _installer


@install_router.get("/{name}/install", dependencies=[Depends(require_panel_jwt)])
async def plan_install(name: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
    """Dry-run: o que a instalação FARIA. Não toca em nada."""
    installer = _require_installer()
    user = await _resolve_user(user_id)
    try:
        plan = await installer.plan(name, user_id=user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SchemaError as exc:
        raise _bad_request(exc) from exc
    state = await installer.get_state(name)
    return {"plano": plan.to_dict(), "estado": state.to_dict()}


@install_router.post("/{name}/install", dependencies=[Depends(require_panel_jwt)])
async def apply_install(name: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
    installer = _require_installer()
    user = await _resolve_user(user_id)
    try:
        state = await installer.apply(name, user_id=user)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SchemaError as exc:
        raise _bad_request(exc) from exc
    except Exception as exc:  # noqa: BLE001 — falha de DDL vira 400 com a razão
        raise HTTPException(status_code=400, detail=f"Instalação falhou: {exc}") from exc
    # O gate de `datasets:` da SkillLibrary trabalha com um snapshot sync —
    # renová-lo aqui é o que faz a skill aparecer no catálogo já na próxima
    # mensagem, sem esperar restart.
    if _registry is not None:
        library = getattr(installer, "_library", None)
        if library is not None:
            library.set_known_datasets([d.slug for d in await _registry.all()])
    return {"estado": state.to_dict()}


@install_router.get("/{name}/install/verify", dependencies=[Depends(require_panel_jwt)])
async def verify_install(name: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
    installer = _require_installer()
    user = await _resolve_user(user_id)
    state = await installer.verify(name, user_id=user)
    return {"estado": state.to_dict()}


@install_router.delete("/{name}/install", dependencies=[Depends(require_panel_jwt)])
async def uninstall(name: str) -> dict[str, Any]:
    installer = _require_installer()
    state = await installer.uninstall(name)
    return {"estado": state.to_dict()}


@install_router.get("/install/estados", dependencies=[Depends(require_panel_jwt)])
async def all_install_states() -> dict[str, Any]:
    installer = _require_installer()
    states = await installer.all_states()
    return {"estados": {k: v.to_dict() for k, v in states.items()}}
