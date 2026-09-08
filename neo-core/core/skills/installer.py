"""Instalador de skills (docs/datasets-crud-plan.md §3).

**Instalar é um evento; usar é o estado.** Toda análise cara — o que a skill
precisa, qual DDL rodar, qual tela publicar — acontece aqui, uma vez. Depois a
skill é parte do Neo: o turno não paga descoberta nenhuma.

Três etapas, e a terceira é a que importa:

``plan``   lê o SKILL.md, compara com o que já existe e descreve em português o
           que falta. Não toca em nada.
``apply``  executa o plano. Idempotente por construção (todo DDL é
           ``IF NOT EXISTS``): rodar duas vezes dá o mesmo estado.
``verify`` relê do BANCO — a tabela existe? as colunas batem? as tools resolvem
           no agregador? Sem esta etapa, "instalei" é afirmação sem prova, e é
           exatamente assim que a skill HA dizia "desliguei" sem ter desligado.

Drift (§3.4): ``install_hash`` é o sha256 do frontmatter normalizado. Mudança
cosmética (corpo, keywords) não muda o hash relevante; aditiva (campo novo) só
auto-aplica com ``NEO_SKILL_AUTO_INSTALL``; destrutiva (remover campo) nunca é
automática. É o que torna seguro o Neo editar o próprio SKILL.md no futuro.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.datasets.registry import DatasetRegistry
from core.datasets.schema import DatasetSchema, SchemaError, parse_fields, validate_identifier
from core.datasets.service import DatasetService
from core.tools.aggregator import ToolAggregator, ToolNameConflict, get_aggregator

logger = logging.getLogger(__name__)

# Estados possíveis em `skill_install.state` (db/models/dataset.py).
PLANNED, INSTALLED, STALE, FAILED, UNINSTALLED = (
    "planejada", "instalada", "desatualizada", "falhou", "desinstalada",
)


@dataclass
class InstallAction:
    """Um passo do plano. ``destructive`` nunca roda sem humano confirmando."""

    kind: str  # create_dataset | add_fields | bind_tools | publish_screen | noop
    target: str
    detail: str
    payload: dict[str, Any] = field(default_factory=dict)
    destructive: bool = False


@dataclass
class InstallPlan:
    skill: str
    actions: list[InstallAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    install_hash: str = ""
    version: int = 1

    @property
    def is_noop(self) -> bool:
        return not [a for a in self.actions if a.kind != "noop"]

    @property
    def has_destructive(self) -> bool:
        return any(a.destructive for a in self.actions)

    def render(self) -> str:
        """Plano legível — é o que o painel mostra antes do apply."""
        if self.is_noop:
            return f"'{self.skill}' já está instalada e em dia. Nada a fazer."
        lines = [f"Instalar '{self.skill}':"]
        for action in self.actions:
            if action.kind == "noop":
                continue
            mark = "!" if action.destructive else "+"
            lines.append(f"  {mark} {action.detail}")
        for warning in self.warnings:
            lines.append(f"  ~ {warning}")
        if not self.has_destructive:
            lines.append("  ~ nenhuma migração destrutiva")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "noop": self.is_noop,
            "destrutivo": self.has_destructive,
            "install_hash": self.install_hash,
            "texto": self.render(),
            "acoes": [
                {
                    "tipo": a.kind,
                    "alvo": a.target,
                    "detalhe": a.detail,
                    "destrutivo": a.destructive,
                }
                for a in self.actions
                if a.kind != "noop"
            ],
            "avisos": self.warnings,
        }


@dataclass
class InstallState:
    skill: str
    state: str
    install_hash: str = ""
    datasets: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    screens: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""
    installed_at: datetime | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "estado": self.state,
            "install_hash": self.install_hash,
            "cadastros": self.datasets,
            "tools": self.tools,
            "telas": self.screens,
            "erro": self.last_error,
            "instalada_em": self.installed_at.isoformat() if self.installed_at else None,
            "problemas": self.problems,
        }


def compute_install_hash(entry: Any) -> str:
    """sha256 do que MUDA a instalação — corpo e description ficam de fora.

    Editar o texto da skill (o que o Neo vai fazer para "melhorar a própria
    habilidade") não deve marcar a skill como desatualizada; mudar os campos de
    um cadastro deve.
    """
    payload = {
        "kind": getattr(entry, "kind", ""),
        "tools": sorted(getattr(entry, "tools", []) or []),
        "datasets": getattr(entry, "datasets", []) or [],
        "screens": getattr(entry, "screens", []) or [],
        "requires": sorted(getattr(entry, "requires", []) or []),
        "version": getattr(entry, "version", 1),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SkillInstaller:
    """Plan/apply/verify/uninstall de uma skill declarativa."""

    def __init__(
        self,
        *,
        db: Any,
        library: Any,
        dataset_registry: DatasetRegistry,
        aggregator: ToolAggregator | None = None,
        settings: Any = None,
    ) -> None:
        self._db = db
        self._library = library
        self._datasets = dataset_registry
        self._service = DatasetService(db=db, registry=dataset_registry)
        self._aggregator = aggregator or get_aggregator()
        self._settings = settings

    # ------------------------------------------------------------------
    # Estado persistido
    # ------------------------------------------------------------------

    async def get_state(self, name: str) -> InstallState:
        async with self._db.get_session() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT name, state, install_hash, datasets, tools, screens, "
                        "last_error, installed_at FROM skill_install WHERE name = :name"
                    ),
                    {"name": name},
                )
            ).mappings().first()
        if row is None:
            return InstallState(skill=name, state=PLANNED)
        return InstallState(
            skill=row["name"],
            state=row["state"],
            install_hash=row["install_hash"] or "",
            datasets=list(row["datasets"] or []),
            tools=list(row["tools"] or []),
            screens=list(row["screens"] or []),
            last_error=row["last_error"] or "",
            installed_at=row["installed_at"],
        )

    async def all_states(self) -> dict[str, InstallState]:
        async with self._db.get_session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT name, state, install_hash, datasets, tools, screens, "
                        "last_error, installed_at FROM skill_install"
                    )
                )
            ).mappings().fetchall()
        return {
            r["name"]: InstallState(
                skill=r["name"],
                state=r["state"],
                install_hash=r["install_hash"] or "",
                datasets=list(r["datasets"] or []),
                tools=list(r["tools"] or []),
                screens=list(r["screens"] or []),
                last_error=r["last_error"] or "",
                installed_at=r["installed_at"],
            )
            for r in rows
        }

    async def _save_state(self, state: InstallState, *, version: int = 0) -> None:
        now = datetime.utcnow()
        async with self._db.get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO skill_install (name, state, install_hash, installed_version, "
                    "datasets, tools, screens, last_error, installed_at, updated_at) VALUES "
                    "(:name, :state, :hash, :version, CAST(:datasets AS JSONB), "
                    "CAST(:tools AS JSONB), CAST(:screens AS JSONB), :err, :installed_at, :now) "
                    "ON CONFLICT (name) DO UPDATE SET state = EXCLUDED.state, "
                    "install_hash = EXCLUDED.install_hash, "
                    "installed_version = EXCLUDED.installed_version, "
                    "datasets = EXCLUDED.datasets, tools = EXCLUDED.tools, "
                    "screens = EXCLUDED.screens, last_error = EXCLUDED.last_error, "
                    "installed_at = COALESCE(EXCLUDED.installed_at, skill_install.installed_at), "
                    "updated_at = EXCLUDED.updated_at"
                ),
                {
                    "name": state.skill,
                    "state": state.state,
                    "hash": state.install_hash,
                    "version": version,
                    "datasets": json.dumps(state.datasets),
                    "tools": json.dumps(state.tools),
                    "screens": json.dumps(state.screens, default=str),
                    "err": state.last_error[:2000],
                    "installed_at": state.installed_at,
                    "now": now,
                },
            )
            await session.commit()

    # ------------------------------------------------------------------
    # PLAN
    # ------------------------------------------------------------------

    async def plan(self, name: str, *, user_id: uuid.UUID | None = None) -> InstallPlan:
        entry = self._library.get(name)
        if entry is None:
            raise LookupError(
                f"Skill '{name}' não está no catálogo. "
                f"Disponíveis: {', '.join(sorted(e.name for e in self._library.all(enabled_only=False)))}."
            )
        plan = InstallPlan(
            skill=name,
            install_hash=compute_install_hash(entry),
            version=int(getattr(entry, "version", 1) or 1),
        )

        for spec in self._dataset_specs(entry):
            slug = spec["slug"]
            existing = await self._datasets.get(slug, user_id=user_id)
            fields = spec.get("fields")

            if existing is None:
                if not fields:
                    plan.warnings.append(
                        f"cadastro '{slug}' não existe e a skill não descreve os campos "
                        "— crie pelo painel ou peça ao Neo (skill 'criar-cadastro')."
                    )
                    continue
                schema = DatasetSchema(fields=parse_fields(fields))
                plan.actions.append(
                    InstallAction(
                        kind="create_dataset",
                        target=slug,
                        detail=(
                            f"criar cadastro `{slug}` → tabela neo_data.{slug}_<código>\n"
                            + "      " + " · ".join(f.describe() for f in schema.fields)
                        ),
                        payload={"spec": spec},
                    )
                )
                continue

            if fields:
                declared = parse_fields(fields)
                known = {f.name for f in existing.schema.fields}
                novos = [f for f in declared if f.name not in known]
                sumidos = [n for n in known if n not in {f.name for f in declared}]
                if novos:
                    plan.actions.append(
                        InstallAction(
                            kind="add_fields",
                            target=slug,
                            detail=f"acrescentar em `{slug}`: " + " · ".join(f.describe() for f in novos),
                            payload={"spec": spec},
                        )
                    )
                if sumidos:
                    plan.warnings.append(
                        f"o SKILL.md não cita mais {', '.join(sumidos)} em '{slug}' — "
                        "as colunas foram MANTIDAS (remover é destrutivo e só pelo painel)."
                    )
            if not fields:
                plan.actions.append(
                    InstallAction(kind="noop", target=slug, detail=f"cadastro `{slug}` já existe")
                )

        tools = self._tool_names(entry)
        if tools:
            faltando = [t for t in tools if not self._aggregator.exists(t)]
            if faltando:
                plan.warnings.append(
                    f"tools não registradas no neo-core: {', '.join(faltando)} — "
                    "a skill instala, mas o cérebro não vai recebê-las."
                )
            usaveis = [t for t in tools if t not in faltando]
            if usaveis:
                plan.actions.append(
                    InstallAction(
                        kind="bind_tools",
                        target=name,
                        detail=f"ligar tools: {', '.join(usaveis)}",
                        payload={"tools": usaveis},
                    )
                )

        for screen in self._screens(entry):
            plan.actions.append(
                InstallAction(
                    kind="publish_screen",
                    target=str(screen.get("dataset") or ""),
                    detail=(
                        f"publicar tela \"{screen.get('titulo') or screen.get('dataset')}\" "
                        f"no painel (/dados/{screen.get('dataset')})"
                    ),
                    payload={"screen": screen},
                )
            )
        return plan

    # ------------------------------------------------------------------
    # APPLY
    # ------------------------------------------------------------------

    async def apply(
        self, name: str, *, user_id: uuid.UUID | None = None, plan: InstallPlan | None = None
    ) -> InstallState:
        plan = plan or await self.plan(name, user_id=user_id)
        state = await self.get_state(name)
        state.install_hash = plan.install_hash
        created_datasets: list[str] = list(state.datasets)
        bound_tools: list[str] = []
        screens: list[dict[str, Any]] = []

        try:
            for action in plan.actions:
                if action.kind == "create_dataset":
                    spec = action.payload["spec"]
                    schema = DatasetSchema(fields=parse_fields(spec["fields"]))
                    dataset = await self._datasets.create(
                        slug=spec["slug"],
                        title=spec.get("title") or spec["slug"].replace("_", " ").capitalize(),
                        description=spec.get("description") or "",
                        schema=schema,
                        user_id=user_id,
                        skill_name=name,
                    )
                    await self._service.ensure_table(dataset)
                    await self._datasets.audit(
                        dataset_slug=dataset.slug, action="create", actor="instalador",
                        user_id=user_id, payload={"skill": name},
                    )
                    if dataset.slug not in created_datasets:
                        created_datasets.append(dataset.slug)

                elif action.kind == "add_fields":
                    spec = action.payload["spec"]
                    existing = await self._datasets.resolve(spec["slug"], user_id=user_id)
                    declared = parse_fields(spec["fields"])
                    known = {f.name for f in existing.schema.fields}
                    merged = list(existing.schema.fields) + [f for f in declared if f.name not in known]
                    new_schema = DatasetSchema(
                        fields=merged,
                        list_columns=existing.schema.list_columns,
                        search_fields=existing.schema.search_fields,
                        order_by=existing.schema.order_by,
                        label_field=existing.schema.label_field,
                    )
                    diff = await self._service.apply_schema_diff(existing, new_schema)
                    await self._datasets.update_schema(existing, new_schema)
                    await self._datasets.audit(
                        dataset_slug=existing.slug, action="add_field", actor="instalador",
                        user_id=user_id, payload=diff,
                    )
                    if existing.slug not in created_datasets:
                        created_datasets.append(existing.slug)

                elif action.kind == "bind_tools":
                    bound_tools = list(action.payload.get("tools") or [])

                elif action.kind == "publish_screen":
                    screens.append(action.payload["screen"])

                elif action.kind == "noop":
                    slug = action.target
                    if slug and slug not in created_datasets:
                        created_datasets.append(slug)

            # Telas: uma skill sem `screens:` mas com cadastro ganha a tela padrão
            # — "prepara a tela CRUD se necessário" sem exigir declaração.
            if not screens:
                for slug in created_datasets:
                    dataset = await self._datasets.get(slug, user_id=user_id)
                    if dataset is not None:
                        screens.append({"dataset": slug, "titulo": dataset.title})

            state.datasets = created_datasets
            state.tools = bound_tools
            state.screens = screens
            state.state = INSTALLED
            state.last_error = ""
            state.installed_at = state.installed_at or datetime.utcnow()
            await self._save_state(state, version=plan.version)
            logger.info(
                "[installer] '%s' instalada — cadastros=%s tools=%s telas=%d",
                name, created_datasets, bound_tools, len(screens),
            )
        except Exception as exc:  # noqa: BLE001 — falha vira ESTADO, não exceção solta
            state.state = FAILED
            state.last_error = str(exc)
            await self._save_state(state, version=plan.version)
            logger.exception("[installer] '%s' falhou: %s", name, exc)
            raise

        self._library.maybe_reload()
        return await self.verify(name, user_id=user_id)

    # ------------------------------------------------------------------
    # VERIFY
    # ------------------------------------------------------------------

    async def verify(self, name: str, *, user_id: uuid.UUID | None = None) -> InstallState:
        """Relê do banco e do agregador. Estado ``instalada`` só se TUDO bater."""
        state = await self.get_state(name)
        entry = self._library.get(name)
        problems: list[str] = []

        if entry is None:
            state.problems = [f"skill '{name}' não está mais no disco"]
            return state

        for spec in self._dataset_specs(entry):
            slug = spec["slug"]
            dataset = await self._datasets.get(slug, user_id=user_id)
            if dataset is None:
                problems.append(f"cadastro '{slug}' não existe")
                continue
            columns = set(await self._service.table_columns(dataset))
            if not columns:
                problems.append(f"tabela de '{slug}' não existe no banco")
                continue
            faltando = [f.name for f in dataset.schema.fields if f.name not in columns]
            if faltando:
                problems.append(f"'{slug}': colunas ausentes no banco — {', '.join(faltando)}")

        for tool in state.tools:
            if not self._aggregator.exists(tool):
                problems.append(f"tool '{tool}' não está no agregador")

        current_hash = compute_install_hash(entry)
        if state.state == INSTALLED and current_hash != state.install_hash:
            state.state = STALE
            problems.append("o SKILL.md mudou depois da instalação — rode o plano de novo")

        if problems and state.state == INSTALLED:
            state.state = FAILED
            state.last_error = "; ".join(problems)
        elif not problems and state.state in (FAILED, PLANNED):
            # Autocura: o que falhou antes pode ter sido resolvido por fora.
            if state.installed_at is not None:
                state.state = INSTALLED
                state.last_error = ""

        state.problems = problems
        await self._save_state(state)
        return state

    # ------------------------------------------------------------------
    # UNINSTALL
    # ------------------------------------------------------------------

    async def uninstall(self, name: str) -> InstallState:
        """Solta tools e telas. **Nunca** dropa tabela — o dado é do usuário (§3.5)."""
        removed = self._aggregator.unbind_skill(name)
        state = await self.get_state(name)
        state.state = UNINSTALLED
        state.tools = []
        state.screens = []
        state.last_error = ""
        await self._save_state(state)
        logger.info(
            "[installer] '%s' desinstalada — tools soltas=%s; cadastros MANTIDOS=%s",
            name, removed, state.datasets,
        )
        state.problems = (
            [f"cadastros mantidos (dados preservados): {', '.join(state.datasets)}"]
            if state.datasets else []
        )
        return state

    # ------------------------------------------------------------------
    # Leitura do frontmatter
    # ------------------------------------------------------------------

    def _dataset_specs(self, entry: Any) -> list[dict[str, Any]]:
        """Normaliza ``datasets:`` para ``[{slug, title, description, fields}]``.

        Aceita as três formas do plano: o slug puro (referência a um cadastro que
        já existe), o objeto inline com ``campos``, e o arquivo
        ``datasets/<slug>.yaml`` dentro da pasta da skill.
        """
        specs: list[dict[str, Any]] = []
        for raw in getattr(entry, "datasets", []) or []:
            if isinstance(raw, str):
                spec: dict[str, Any] = {"slug": validate_identifier(raw, what="slug do cadastro")}
            elif isinstance(raw, dict):
                slug = validate_identifier(
                    str(raw.get("slug") or raw.get("nome") or ""), what="slug do cadastro"
                )
                spec = {
                    "slug": slug,
                    "title": raw.get("titulo") or raw.get("title") or "",
                    "description": raw.get("descricao") or raw.get("description") or "",
                    "fields": raw.get("campos") or raw.get("fields"),
                }
            else:
                raise SchemaError(f"entrada inválida em datasets:: {raw!r}")
            if not spec.get("fields"):
                from_file = self._dataset_from_file(entry, spec["slug"])
                if from_file:
                    spec = {**from_file, **{k: v for k, v in spec.items() if v}}
                    spec["fields"] = from_file.get("fields")
            specs.append(spec)
        return specs

    def _dataset_from_file(self, entry: Any, slug: str) -> dict[str, Any] | None:
        """``skills/<name>/datasets/<slug>.yaml`` — a forma longa da definição."""
        path = getattr(entry, "path", None)
        if path is None:
            return None
        for suffix in (".yaml", ".yml"):
            candidate = path / "datasets" / f"{slug}{suffix}"
            if not candidate.is_file():
                continue
            try:
                import yaml

                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except Exception as exc:  # noqa: BLE001
                raise SchemaError(f"datasets/{slug}{suffix} inválido: {exc}") from exc
            if not isinstance(data, dict):
                raise SchemaError(f"datasets/{slug}{suffix} precisa ser um objeto YAML.")
            return {
                "slug": slug,
                "title": data.get("titulo") or data.get("title") or "",
                "description": data.get("descricao") or data.get("description") or "",
                "fields": data.get("campos") or data.get("fields"),
            }
        return None

    def _tool_names(self, entry: Any) -> list[str]:
        """Declaradas + as implícitas: ``data_*`` de quem tem cadastro e
        ``event_*`` de quem declara ``eventos:``.

        Precisa bater com ``SkillLibrary.tool_names_for``, senão o `verify` diz
        que a skill tem menos capacidade do que ela realmente destrava no turno.
        """
        names = list(getattr(entry, "tools", []) or [])
        if getattr(entry, "datasets", None):
            from modules.datasets.tools import DATA_TOOLS

            names += [t.name for t in DATA_TOOLS]
        if getattr(entry, "eventos", None) and getattr(
            self._settings, "neo_events_enabled", False
        ):
            from modules.events.tools import EVENT_TOOLS

            names += [t.name for t in EVENT_TOOLS]
        return list(dict.fromkeys(names))

    def _screens(self, entry: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in getattr(entry, "screens", []) or []:
            if isinstance(raw, str):
                out.append({"dataset": raw, "titulo": raw.capitalize()})
            elif isinstance(raw, dict) and raw.get("dataset"):
                out.append(
                    {
                        "dataset": str(raw["dataset"]),
                        "titulo": raw.get("titulo") or raw.get("title") or str(raw["dataset"]).capitalize(),
                        "icone": raw.get("icone") or raw.get("icon") or "",
                    }
                )
        return out
