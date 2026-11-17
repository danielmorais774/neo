"""Skills API — CRUD das skills declarativas para a aba Skills do neo-panel.

Três dos quatro níveis da taxonomia (docs/skills-library-plan.md) nascem de um
`SKILL.md` e portanto podem ser criados pela web:

- ``instructional`` — só o corpo markdown;
- ``scripted``      — corpo + ``scripts/`` (rodam no sandbox do workbench);
- ``tool_backed``   — corpo + tools JÁ registradas no neo-core (tool nova é código).

O nível 4 (``pipeline``) é pacote Python: aparece na listagem, read-only.

Toda a validação e a segurança de caminho ficam no :class:`SkillLibrary` — as
rotas só traduzem ``ValueError`` em HTTP 400. Protegido pelo panel JWT
(``api/main.py``). Escrita bem-sucedida recarrega o catálogo na hora: a próxima
mensagem do usuário já vê a skill nova, sem restart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from core.skills.library import SkillEntry, SkillLibrary
from core.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])

_library: SkillLibrary | None = None
_tool_registry = None


def set_dependencies(library: SkillLibrary | None, tool_registry=None) -> None:
    """Injeta a library singleton (None quando skills_library_enabled=false)."""
    global _library, _tool_registry
    _library = library
    _tool_registry = tool_registry


def _lib() -> SkillLibrary:
    if _library is None:
        raise HTTPException(
            status_code=503,
            detail="Biblioteca de skills desligada (SKILLS_LIBRARY_ENABLED=false).",
        )
    return _library


class SkillWrite(BaseModel):
    """Payload de criação/edição de um SKILL.md."""

    name: str | None = Field(None, description="Só no POST; no PUT vem da URL.")
    description: str = Field(..., description="≤ 200 chars — a linha que o cérebro vê SEMPRE.")
    body: str = Field(..., description="Corpo markdown do SKILL.md (as instruções).")
    kind: str = Field("instructional", description="instructional | scripted | tool_backed")
    keywords: list[str] = []
    tools: list[str] = Field([], description="Só kind=tool_backed; tools já registradas.")
    # Cadastros generativos (docs/datasets-crud-plan.md §7.1): slug puro, objeto
    # com `campos`, ou slug + datasets/<slug>.yaml na pasta da skill.
    datasets: list = []
    screens: list = []
    discovery: str | None = Field(
        None, description="always | keyword | oculta — quanto de catálogo esta skill paga."
    )
    group: str | None = Field(None, description="Colapsa skills do mesmo grupo numa linha.")
    env: list[str] = []
    enabled: bool = True
    version: int = 1
    # Chave "register" no JSON; nome interno diferente para não sombrear
    # BaseModel.register (mesma razão do SkillEntry.tone_register).
    tone_register: str | None = Field(
        None,
        alias="register",
        description="Registro da situação imposto pela skill (silencioso|parceiro|oficina|combate|protocolo|confidente); None = não impõe.",
    )

    model_config = ConfigDict(populate_by_name=True)


class FileWrite(BaseModel):
    content: str = Field(..., description="Conteúdo integral do arquivo.")


class EnabledWrite(BaseModel):
    enabled: bool


def _entry_payload(lib: SkillLibrary, entry: SkillEntry) -> dict:
    """Um item da listagem: metadados + por que a skill está (ou não) no catálogo."""
    available = lib.is_available(entry)
    reason = None
    if not entry.enabled:
        reason = "desligada"
    elif not available:
        faltando = [
            slug for slug in lib._dataset_slugs(entry)
            if lib._known_datasets is not None and slug not in lib._known_datasets
        ]
        if faltando:
            reason = f"cadastro não instalado: {', '.join(faltando)}"
        else:
            reason = f"requisito não satisfeito: {', '.join(entry.requires)}"
    return {
        "name": entry.name,
        "description": entry.description,
        "keywords": entry.keywords,
        "kind": entry.kind,
        "register": entry.tone_register,
        "tools": entry.tools,
        "datasets": entry.datasets,
        "screens": entry.screens,
        "discovery": entry.discovery,
        "group": entry.group,
        "env": entry.env,
        "requires": entry.requires,
        "enabled": entry.enabled,
        "version": entry.version,
        "available": available,
        "unavailable_reason": reason,
        "editable": True,
    }


def _pipeline_payload(manifest: SkillManifest) -> dict:
    """Skill nível 4 — só leitura: o comportamento dela mora em código Python."""
    return {
        "name": manifest.name,
        "description": manifest.description,
        "keywords": manifest.keywords,
        "kind": "pipeline",
        "register": manifest.tone_register or None,
        "tools": manifest.tool_names,
        "enabled": True,
        "available": True,
        "editable": False,
        "intent_type": getattr(manifest.intent_type, "value", str(manifest.intent_type)),
        "handoff": manifest.handoff,
        "version": manifest.version,
    }


@router.get("")
async def list_skills():
    """Catálogo completo: skills .md (editáveis), pipelines (read-only) e erros de load."""
    lib = _lib()
    lib.maybe_reload()
    entries = sorted(lib.all(enabled_only=False), key=lambda e: e.name)
    return {
        "root": str(lib.root),
        "skills": [_entry_payload(lib, e) for e in entries],
        "pipelines": [_pipeline_payload(m) for m in SkillManifest.all().values()],
        "errors": lib.errors(),
    }


@router.get("/tools")
async def list_tools():
    """Tools registradas — o dropdown de uma skill tool_backed.

    Vem antes de ``/{name}`` de propósito: senão o path param captura 'tools'.
    """
    _lib()
    if _tool_registry is None:
        return {"tools": []}
    # Índice do agregador (docs/datasets-crud-plan.md §2): além dos nomes, de
    # ONDE cada tool veio. É o que deixa a aba Skills mostrar "esta tool é do
    # cadastro", "esta veio de modules/finance" — sem isso o dropdown é uma
    # lista chapada de 80 nomes sem procedência.
    catalogo = []
    try:
        from core.tools.aggregator import get_aggregator

        catalogo = [b.to_dict() | {"assinatura": b.signature()} for b in get_aggregator().all()]
    except Exception as exc:  # noqa: BLE001 — agregador é opcional
        logger.warning("[skills] agregador indisponível: %s", exc)
    return {"tools": sorted(_tool_registry.list_names()), "catalogo": catalogo}


@router.get("/catalog")
async def catalog_preview():
    """Exatamente o que o cérebro lê por turno — para conferir o custo em tokens."""
    lib = _lib()
    note = lib.catalog_note(None)
    router_note = lib.router_note()
    return {
        "catalog_note": note,
        "router_note": router_note,
        # ~4 chars/token: estimativa suficiente para dimensionar o custo fixo.
        "estimated_tokens": (len(note or "") + len(router_note or "")) // 4,
    }


@router.post("/reload")
async def reload_library():
    """Força um re-scan (o hot-reload por mtime já cobre o caso normal)."""
    lib = _lib()
    lib.load()
    return {"ok": True, "count": len(lib.all()), "errors": lib.errors()}


@router.get("/{name}")
async def get_skill(name: str):
    """Fonte da skill para edição — funciona mesmo com o SKILL.md quebrado."""
    lib = _lib()
    try:
        source = lib.read_source(name)
        files = lib.list_files(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    entry = lib.get(name)
    return {
        **source,
        "files": files,
        "entry": _entry_payload(lib, entry) if entry else None,
    }


def _exists(lib: SkillLibrary, name: str) -> bool:
    """exists_on_disk validando o nome — nome inválido é 400, não 500."""
    try:
        return lib.exists_on_disk(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("", status_code=201)
async def create_skill(payload: SkillWrite):
    lib = _lib()
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="O nome da skill é obrigatório.")
    if _exists(lib, name):
        raise HTTPException(status_code=409, detail=f"Skill '{name}' já existe.")
    return _write(lib, name, payload)


@router.put("/{name}")
async def update_skill(name: str, payload: SkillWrite):
    lib = _lib()
    if not _exists(lib, name):
        raise HTTPException(status_code=404, detail=f"Skill '{name}' não existe.")
    return _write(lib, name, payload)


def _write(lib: SkillLibrary, name: str, payload: SkillWrite) -> dict:
    try:
        entry = lib.write_skill(
            name,
            description=payload.description,
            body=payload.body,
            kind=payload.kind,
            keywords=payload.keywords,
            tools=payload.tools,
            env=payload.env,
            enabled=payload.enabled,
            version=payload.version,
            register=payload.tone_register,
            datasets=payload.datasets,
            screens=payload.screens,
            discovery=payload.discovery,
            group=payload.group,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _entry_payload(lib, entry)


@router.patch("/{name}/enabled")
async def set_enabled(name: str, payload: EnabledWrite):
    lib = _lib()
    try:
        entry = lib.set_enabled(name, payload.enabled)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _entry_payload(lib, entry)


@router.delete("/{name}")
async def delete_skill(name: str):
    lib = _lib()
    try:
        lib.delete_skill(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True, "deleted": name}


@router.get("/{name}/files/{file:path}")
async def read_file(name: str, file: str):
    lib = _lib()
    try:
        return {"skill": name, "path": file, "content": lib.read_file(name, file)}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/{name}/files/{file:path}")
async def write_file(name: str, file: str, payload: FileWrite):
    """Grava references/*.md ou scripts/*.py — extensão e nível de pasta na allowlist."""
    lib = _lib()
    try:
        return lib.write_file(name, file, payload.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/{name}/files/{file:path}")
async def delete_file(name: str, file: str):
    lib = _lib()
    try:
        lib.delete_file(name, file)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "deleted": file}
