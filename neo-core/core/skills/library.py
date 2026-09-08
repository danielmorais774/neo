"""SkillLibrary — biblioteca de skills declarativas (SKILL.md).

Fonte única das skills "leves" do Neo (docs/skills-library-plan.md):

- **instructional** — só um SKILL.md; o cérebro lê e segue com as tools que já tem.
- **scripted** — SKILL.md + scripts/ prontos, executados no sandbox do workbench
  (montados read-only em /skills dentro do container neo-workbench).
- **tool_backed** — SKILL.md + tools server-side; ler a skill DESTRAVA as tools
  no tool loop do cognitivo (tool gating — as tools não pagam prompt até lá).

Progressive disclosure: o prompt cognitivo só carrega o catálogo (1 linha por
skill, via :meth:`SkillLibrary.catalog_note`); o corpo é lido sob demanda pela
tool ``skill_read`` (:mod:`modules.skills_library`).

Fail-safe por construção: skill inválida é logada e PULADA (nunca derruba o
boot); nome/descrição são validados porque entram em prompt.

Escrita (neo-panel, api/routes/skills.py): :meth:`SkillLibrary.write_skill`,
:meth:`write_file` e :meth:`delete_skill` validam ANTES de tocar o disco e
recarregam o catálogo em seguida — o painel nunca consegue gravar uma skill que
o loader recusaria depois.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MAX_DESCRIPTION_CHARS = 200
_MAX_BODY_BYTES = 64 * 1024

# --- Escrita pelo painel ------------------------------------------------------
# Teto de um arquivo de references/ ou scripts/ (o corpo do SKILL.md usa
# _MAX_BODY_BYTES). Generoso: um schema.md real passa fácil de 16 KB.
_MAX_FILE_BYTES = 256 * 1024
# Só estas subpastas são graváveis — SKILL.md passa por write_skill (validado).
_EDITABLE_DIRS = ("references", "scripts")
_EDITABLE_SUFFIXES = frozenset({".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".csv", ".sql"})
# Ordem estável do frontmatter gerado (o resto do YAML original é preservado).
_FRONTMATTER_KEYS = (
    "name", "description", "keywords", "kind", "register", "group", "discovery",
    "tools", "datasets", "screens", "env", "requires", "enabled", "version",
)
# Modos de descoberta (docs/datasets-crud-plan.md §8.2): quanto do catálogo
# esta skill paga em TODA mensagem.
_DISCOVERY_MODES = ("always", "keyword", "oculta")
_WRITABLE_KINDS = ("instructional", "scripted", "tool_backed")

# Kinds das skills declarativas; "pipeline" é reservado ao merge com os
# SkillManifest Python (nível 4) na fase de unificação.
SkillKind = Literal["instructional", "scripted", "tool_backed", "pipeline"]


def _norm(text: str) -> str:
    """Lowercase + remove acentos (matching de keywords robusto, como no router)."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _keyword_hit(keyword: str, message: str) -> bool:
    """Keyword casa como palavra/frase inteira — 'gasto' não casa em 'gastronomia'."""
    if not keyword:
        return False
    return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", message) is not None


class SkillEntry(BaseModel):
    """Uma skill carregada — metadados do frontmatter + caminho no disco."""

    name: str
    description: str
    keywords: list[str] = []
    kind: SkillKind = "instructional"
    path: Path | None = None  # pasta da skill (None para kind=pipeline)
    tools: list[str] = []     # tool_backed: tools destravadas pela leitura
    env: list[str] = []       # scripted: envs que os scripts esperam no sandbox
    requires: list[str] = []  # gates de runtime (ex.: "workbench")
    enabled: bool = True
    version: int = 1
    # Registro da situação que esta skill impõe enquanto ativa (ex.: "oficina").
    # None = não impõe. Aplicado via register_note no resultado do skill_read
    # (o prompt do turno já foi montado quando a skill é lida). No frontmatter a
    # chave é ``register:``; o campo se chama ``tone_register`` para não
    # sombrear ``BaseModel.register``. Ver docs/context-register-plan.md.
    tone_register: str | None = None

    # --- Cadastros generativos (docs/datasets-crud-plan.md §7.1) -------------
    # Aceita três formas: o slug puro ("produtos" — referência a um cadastro que
    # já existe), o objeto inline com `campos:`, e o slug + `datasets/<slug>.yaml`
    # na pasta da skill. Quem normaliza é o SkillInstaller._dataset_specs.
    datasets: list[Any] = []
    # Telas publicadas no neo-panel. Vazio + datasets → o instalador publica a
    # tela padrão de cada cadastro ("prepara a tela CRUD se necessário").
    screens: list[Any] = []
    # Orçamento de tokens (§8): como esta skill aparece no catálogo do prompt.
    #   keyword — só quando a mensagem casa (PADRÃO das skills novas)
    #   always  — 1 linha em toda mensagem (o comportamento antigo de todas)
    #   oculta  — nunca; só por nome exato ou tool_find
    discovery: str = "always"
    # Skills do mesmo grupo colapsam numa ÚNICA linha de catálogo.
    group: str = ""

    # pipeline-only (nível 4) — preenchidos no merge com SkillManifest (F4).
    intent_type: Any | None = None
    handoff: bool = False


class SkillLibrary:
    """Carrega e serve as skills declarativas de ``root`` (``<name>/SKILL.md``)."""

    def __init__(
        self,
        root: str | Path,
        settings: Any,
        tool_registry: Any | None = None,
    ) -> None:
        self.root = Path(root)
        self.settings = settings
        self.tool_registry = tool_registry
        self._entries: dict[str, SkillEntry] = {}
        # Assinatura (path, mtime, size) dos SKILL.md — hot-reload barato por stat.
        self._signature: tuple = ()
        # Skills puladas no load (pasta → motivo). Só existiam no log; o painel
        # precisa delas para mostrar "1 skill inválida" e deixar corrigir.
        self._errors: list[dict[str, str]] = []
        # Slugs de cadastro que EXISTEM no banco. `None` = ainda não sabemos
        # (boot antes do registry carregar) e aí o gate de `datasets:` fica
        # aberto — fail-open: melhor uma skill a mais no catálogo do que o
        # catálogo inteiro sumindo porque o Postgres demorou a responder.
        self._known_datasets: set[str] | None = None

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    def load(self) -> None:
        """(Re)escaneia ``root``. Skill inválida é logada e pulada — nunca levanta."""
        entries: dict[str, SkillEntry] = {}
        signature: list[tuple] = []
        errors: list[dict[str, str]] = []
        if not self.root.is_dir():
            logger.info("[SkillLibrary] root inexistente (%s) — biblioteca vazia", self.root)
            self._entries, self._signature, self._errors = {}, (), []
            return

        def skip(skill_dir: Path, reason: str) -> None:
            logger.warning("[SkillLibrary] '%s' pulada: %s", skill_dir.name, reason)
            errors.append({"skill": skill_dir.name, "error": reason})

        for skill_dir in sorted(self.root.iterdir()):
            # Pastas de suporte (_TEMPLATE, .git, …) não são skills.
            if not skill_dir.is_dir() or skill_dir.name.startswith(("_", ".")):
                continue
            md = skill_dir / "SKILL.md"
            if not md.is_file():
                skip(skill_dir, "sem SKILL.md")
                continue
            try:
                stat = md.stat()
                signature.append((str(md), stat.st_mtime_ns, stat.st_size))
                entry = self._parse_skill(skill_dir, md)
            except Exception as e:  # noqa: BLE001 — fail-safe: nunca derruba o boot
                skip(skill_dir, str(e))
                continue
            if entry is None:
                continue
            if entry.name in entries:
                skip(skill_dir, f"nome duplicado '{entry.name}'")
                continue
            entries[entry.name] = entry

        self._entries = entries
        self._signature = tuple(sorted(signature))
        self._errors = errors
        logger.info(
            "[SkillLibrary] %d skill(s) carregadas de %s: %s",
            len(entries), self.root, ", ".join(entries) or "(nenhuma)",
        )

    def maybe_reload(self) -> None:
        """Recarrega se algum SKILL.md mudou (stat-only; barato por turno)."""
        try:
            signature: list[tuple] = []
            if self.root.is_dir():
                for skill_dir in self.root.iterdir():
                    if not skill_dir.is_dir() or skill_dir.name.startswith(("_", ".")):
                        continue
                    md = skill_dir / "SKILL.md"
                    if md.is_file():
                        stat = md.stat()
                        signature.append((str(md), stat.st_mtime_ns, stat.st_size))
            if tuple(sorted(signature)) != self._signature:
                logger.info("[SkillLibrary] mudança detectada — recarregando")
                self.load()
        except Exception as e:  # noqa: BLE001
            logger.warning("[SkillLibrary] maybe_reload falhou (mantendo cache): %s", e)

    def _parse_skill(self, skill_dir: Path, md: Path) -> SkillEntry | None:
        """Parseia um SKILL.md. Levanta ``ValueError`` (→ pulada) quando inválida."""
        raw = md.read_text(encoding="utf-8", errors="replace")
        meta, _body = split_frontmatter(raw)
        if meta is None:
            raise ValueError("SKILL.md sem frontmatter YAML")

        name = str(meta.get("name") or skill_dir.name).strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"nome inválido: {name!r}")
        if name != skill_dir.name:
            # Pasta e frontmatter divergindo confunde skill_read(name) — a pasta manda.
            logger.warning(
                "[SkillLibrary] '%s': frontmatter name=%r difere da pasta — usando o da pasta",
                skill_dir.name, name,
            )
            name = skill_dir.name

        description = " ".join(str(meta.get("description") or "").split())
        if not description:
            raise ValueError("sem 'description' no frontmatter")
        if len(description) > _MAX_DESCRIPTION_CHARS:
            logger.warning(
                "[SkillLibrary] '%s': description > %d chars — truncada",
                name, _MAX_DESCRIPTION_CHARS,
            )
            description = description[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"

        kind = str(meta.get("kind") or "instructional").strip()
        if kind not in _WRITABLE_KINDS:
            raise ValueError(f"kind inválido: {kind!r} (use {', '.join(_WRITABLE_KINDS)})")

        requires = [str(r) for r in (meta.get("requires") or [])]
        if kind == "scripted" and "workbench" not in requires:
            # Scripts rodam no sandbox — o require é implícito; injeta e avisa.
            requires.append("workbench")

        # register: inválido não derruba a skill — só avisa e ignora (fail-open).
        register = str(meta.get("register") or "").strip().lower() or None
        if register is not None:
            from core.agent.register import Register

            if register not in {r.value for r in Register}:
                logger.warning(
                    "[SkillLibrary] '%s': register inválido %r — ignorado (use um de: %s)",
                    name, register, ", ".join(r.value for r in Register),
                )
                register = None

        datasets = meta.get("datasets") or []
        if not isinstance(datasets, list):
            raise ValueError("'datasets' precisa ser uma lista")
        screens = meta.get("screens") or []
        if not isinstance(screens, list):
            raise ValueError("'screens' precisa ser uma lista")

        # discovery inválido não derruba a skill — cai no padrão e avisa.
        discovery = str(meta.get("discovery") or "").strip().lower()
        if discovery not in _DISCOVERY_MODES:
            if discovery:
                logger.warning(
                    "[SkillLibrary] '%s': discovery inválido %r — usando 'always' "
                    "(use um de: %s)",
                    name, discovery, ", ".join(_DISCOVERY_MODES),
                )
            # Sem declaração explícita vale o DEFAULT DA INSTÂNCIA, que nasce
            # "always" — exatamente o comportamento de antes deste plano. Trocar
            # o default para "keyword" (economia da §8) é decisão de operação, e
            # não pode acontecer como efeito colateral de um deploy: uma skill
            # que some do catálogo é uma skill que o cérebro não descobre mais.
            # As skills geradas pelo instalador declaram `discovery: keyword`
            # explicitamente, então já nascem baratas.
            discovery = str(
                getattr(self.settings, "skills_discovery_default", "always") or "always"
            ).strip().lower()
            if discovery not in _DISCOVERY_MODES:
                discovery = "always"

        entry = SkillEntry(
            name=name,
            description=description,
            keywords=[str(k) for k in (meta.get("keywords") or [])],
            kind=kind,  # type: ignore[arg-type]
            path=skill_dir,
            tools=[str(t) for t in (meta.get("tools") or [])],
            env=[str(e) for e in (meta.get("env") or [])],
            requires=requires,
            enabled=bool(meta.get("enabled", True)),
            version=int(meta.get("version", 1)),
            tone_register=register,
            datasets=datasets,
            screens=screens,
            discovery=discovery,
            group=str(meta.get("group") or "").strip().lower(),
        )

        # tool_backed com tool não registrada: entra no catálogo, mas avisa — a
        # tool só falta se o módulo não foi registrado no boot (api/main.py).
        if entry.kind == "tool_backed" and self.tool_registry is not None:
            missing = [t for t in entry.tools if not self.tool_registry.exists(t)]
            if missing:
                logger.warning(
                    "[SkillLibrary] '%s': tools não registradas: %s (skill_read vai "
                    "avisar; registre o módulo em api/main.py)",
                    name, ", ".join(missing),
                )
        return entry

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------

    def get(self, name: str) -> SkillEntry | None:
        return self._entries.get((name or "").strip())

    def all(self, *, enabled_only: bool = True) -> list[SkillEntry]:
        entries = list(self._entries.values())
        if enabled_only:
            entries = [e for e in entries if e.enabled and self._requires_ok(e)]
        return entries

    def errors(self) -> list[dict[str, str]]:
        """Skills puladas no último load (pasta + motivo) — para o painel."""
        return list(self._errors)

    def is_available(self, entry: SkillEntry) -> bool:
        """A skill está no catálogo do cérebro agora? (enabled + requires satisfeitos)"""
        return entry.enabled and self._requires_ok(entry)

    def exists_on_disk(self, name: str) -> bool:
        """Existe pasta com SKILL.md para ``name`` — inclusive se ela não carregou."""
        return (self._skill_dir(name) / "SKILL.md").is_file()

    def set_known_datasets(self, slugs: Any) -> None:
        """Informa quais cadastros existem (chamado no boot e após instalar).

        Sem isso o cérebro leria uma skill que aponta para tabela que não existe
        e só descobriria no erro da tool — o mesmo tipo de falha silenciosa que
        as tools fantasma criavam.
        """
        self._known_datasets = {str(s).strip().lower() for s in (slugs or [])}

    def _dataset_slugs(self, entry: SkillEntry) -> list[str]:
        out = []
        for raw in entry.datasets or []:
            slug = raw if isinstance(raw, str) else (raw or {}).get("slug") or (raw or {}).get("nome")
            if slug:
                out.append(str(slug).strip().lower())
        return out

    def _requires_ok(self, entry: SkillEntry) -> bool:
        """Gates de runtime: um require insatisfeito tira a skill do catálogo."""
        if entry.datasets and self._known_datasets is not None:
            faltando = [s for s in self._dataset_slugs(entry) if s not in self._known_datasets]
            if faltando:
                logger.info(
                    "[SkillLibrary] '%s' fora do catálogo: cadastro(s) ausente(s) %s "
                    "— instale a skill pelo painel",
                    entry.name, ", ".join(faltando),
                )
                return False
        for req in entry.requires:
            if req == "workbench":
                if not getattr(self.settings, "workbench_enabled", False):
                    return False
                if self.tool_registry is not None and not self.tool_registry.exists("neo_bash"):
                    return False
            else:
                logger.warning(
                    "[SkillLibrary] '%s': require desconhecido %r — ignorado",
                    entry.name, req,
                )
        return True

    def tool_names_for(self, name: str) -> list[str]:
        """Nomes das tools desta skill: as declaradas + as implícitas do cadastro.

        Declarar ``datasets:`` é o que faz uma skill ganhar as ``data_*`` sem
        listá-las uma a uma — e é o que permite ``write_skill`` aceitar uma skill
        nova sem código Python novo, sem abrir a porta para tool arbitrária.
        """
        entry = self.get(name)
        if entry is None:
            return []
        names = list(entry.tools)
        if entry.datasets:
            try:
                from modules.datasets.tools import DATA_TOOLS

                names += [t.name for t in DATA_TOOLS]
            except Exception as exc:  # noqa: BLE001 — módulo ausente não derruba o gating
                logger.warning("[SkillLibrary] data tools indisponíveis: %s", exc)
        return list(dict.fromkeys(names))

    def tools_for(self, name: str) -> list[Any]:
        """ToolSchemas destravadas pela leitura de uma skill.

        Resolve os nomes no AGREGADOR (core/tools/aggregator.py), que espelha o
        ToolRegistry do boot e ainda conhece as tools de origem `dataset`/`skill`.
        Nome que não resolve é PULADO: o gating nunca oferece ao modelo uma tool
        que o executor não sabe despachar — foi assim que nasceram as client
        tools fantasma que travavam 300s.
        """
        entry = self.get(name)
        if entry is None:
            return []
        # `datasets:` habilita o gating mesmo numa skill instructional — o que
        # define se há tool a destravar é o que a skill declara, não o `kind`.
        if entry.kind != "tool_backed" and not entry.datasets:
            return []
        names = self.tool_names_for(name)
        if not names:
            return []
        try:
            from core.tools.aggregator import get_aggregator

            schemas = get_aggregator().schemas_for(names)
            if schemas:
                return schemas
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillLibrary] agregador indisponível (%s) — caindo no registry", exc)
        # Fallback: agregador vazio (boot antigo, teste isolado) → registry.
        if self.tool_registry is None:
            return []
        return [self.tool_registry.get(n) for n in names if self.tool_registry.exists(n)]

    # ------------------------------------------------------------------
    # Leitura (nível 1 de disclosure — tool skill_read)
    # ------------------------------------------------------------------

    def read(self, name: str, file: str | None = None) -> dict[str, Any]:
        """Corpo do SKILL.md (ou um arquivo relativo da skill, ex. references/x.md).

        Levanta ``ValueError`` com mensagem amigável (vira o ``error`` do
        ToolResult) para nome inexistente ou path fora da pasta da skill.
        """
        entry = self.get(name)
        if entry is None or not self._requires_ok(entry) or not entry.enabled:
            available = ", ".join(sorted(e.name for e in self.all())) or "(nenhuma)"
            raise ValueError(
                f"Skill '{name}' não existe no catálogo. Disponíveis: {available}."
            )
        if entry.path is None:
            raise ValueError(f"Skill '{name}' é um pipeline — não tem SKILL.md para ler.")

        max_bytes = int(getattr(self.settings, "skill_read_max_bytes", 16_384))
        if file:
            target = self._resolve_inside(entry.path, file)
            content = self._read_capped(target, max_bytes)
            return {"skill": entry.name, "file": file, "kind": entry.kind, "content": content}

        raw = (entry.path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        _meta, body = split_frontmatter(raw)
        body = (body if body is not None else raw).strip()
        if len(body.encode("utf-8")) > min(max_bytes, _MAX_BODY_BYTES):
            body = _truncate_utf8(body, min(max_bytes, _MAX_BODY_BYTES)) + (
                "\n\n... [truncado — peça um arquivo de references/ específico]"
            )

        result: dict[str, Any] = {"skill": entry.name, "kind": entry.kind, "content": body}
        # Registro declarado pela skill: como o prompt do turno já foi montado
        # quando o modelo chama skill_read, a diretiva pega carona no resultado
        # da tool (mesma mecânica do tool gating). Só quando a feature está ativa.
        if entry.tone_register and getattr(self.settings, "neo_register_enabled", False) is True:
            from core.agent.register import Register, render_register_directive

            try:
                result["register_note"] = (
                    f"Desta leitura em diante, o registro da situação é "
                    f"'{entry.tone_register}'.\n"
                    + render_register_directive(Register(entry.tone_register))
                )
            except ValueError:  # pragma: no cover — validado no load
                pass
        extras = self._list_extras(entry)
        result.update(extras)
        # Cadastros declarados: quem enriquece com o schema é o executor (a
        # consulta ao registry é async e este método é sync). Aqui só vai a
        # lista — é ela que o executor usa para montar o bloco.
        if entry.datasets:
            result["datasets"] = [
                d if isinstance(d, str) else str((d or {}).get("slug") or (d or {}).get("nome") or "")
                for d in entry.datasets
            ]
        declared = self.tool_names_for(entry.name)
        if declared:
            registered = [t.name for t in self.tools_for(entry.name)]
            missing = [t for t in declared if t not in registered]
            result["tools_unlocked"] = registered
            if missing:
                result["tools_unavailable"] = missing
        return result

    def _list_extras(self, entry: SkillEntry) -> dict[str, Any]:
        """references/ (lidos via skill_read) e scripts/ (caminhos DO SANDBOX)."""
        extras: dict[str, Any] = {}
        assert entry.path is not None
        refs_dir = entry.path / "references"
        if refs_dir.is_dir():
            refs = sorted(
                f"references/{f.name}" for f in refs_dir.iterdir() if f.is_file()
            )
            if refs:
                extras["references"] = refs
        scripts_dir = entry.path / "scripts"
        if scripts_dir.is_dir():
            # Caminho que o modelo deve usar no neo_bash: o mount read-only do
            # sandbox (/skills), não o path do host/neo-core.
            scripts = sorted(
                f"/skills/{entry.name}/scripts/{f.name}"
                for f in scripts_dir.iterdir()
                if f.is_file()
            )
            if scripts:
                extras["scripts"] = scripts
        return extras

    @staticmethod
    def _resolve_inside(skill_dir: Path, relative: str) -> Path:
        """Resolve ``relative`` DENTRO da pasta da skill (anti path-traversal)."""
        rel = (relative or "").strip()
        if not rel or rel.startswith(("/", "~")) or "\\" in rel:
            raise ValueError(f"Caminho inválido: {relative!r} (use relativo, ex.: references/x.md)")
        target = (skill_dir / rel).resolve()
        root = skill_dir.resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise ValueError(f"Caminho fora da skill: {relative!r}")
        if not target.is_file():
            raise ValueError(f"Arquivo não encontrado na skill: {relative!r}")
        return target

    @staticmethod
    def _read_capped(path: Path, max_bytes: int) -> str:
        data = path.read_bytes()[: max_bytes + 1]
        text = data.decode("utf-8", errors="replace")
        if len(data) > max_bytes:
            text = _truncate_utf8(text, max_bytes) + "\n... [truncado]"
        return text

    # ------------------------------------------------------------------
    # Escrita (neo-panel — api/routes/skills.py)
    # ------------------------------------------------------------------

    def _skill_dir(self, name: str) -> Path:
        """Pasta da skill. O nome vira caminho — valida SEMPRE antes de tocar o disco."""
        clean = (name or "").strip()
        if not _NAME_RE.match(clean):
            raise ValueError(
                f"Nome inválido: {name!r}. Use minúsculas, números e hífen, "
                "começando por letra/número (ex.: 'analise-csv')."
            )
        return self.root / clean

    def read_source(self, name: str) -> dict[str, Any]:
        """Frontmatter + corpo crus, para edição no painel.

        Funciona mesmo para skill que o loader recusou — senão um SKILL.md
        quebrado só seria consertável por SSH.
        """
        md = self._skill_dir(name) / "SKILL.md"
        if not md.is_file():
            raise ValueError(f"Skill '{name}' não existe em {self.root}.")
        raw = md.read_text(encoding="utf-8", errors="replace")
        yaml_block, body = _split_raw(raw)
        meta: dict[str, Any] = {}
        parse_error: str | None = None
        if yaml_block is None:
            parse_error = "SKILL.md sem frontmatter YAML"
        else:
            try:
                parsed = yaml.safe_load(yaml_block)
                if isinstance(parsed, dict):
                    meta = parsed
                else:
                    parse_error = "frontmatter não é um mapa YAML"
            except Exception as e:  # noqa: BLE001 — YAML quebrado ainda é editável
                parse_error = f"YAML inválido: {e}"
        return {
            "name": name,
            "meta": meta,
            "body": body.strip(),
            "loaded": self.get(name) is not None,
            "parse_error": parse_error,
        }

    def write_skill(
        self,
        name: str,
        *,
        description: str,
        body: str,
        kind: str = "instructional",
        keywords: list[str] | None = None,
        tools: list[str] | None = None,
        env: list[str] | None = None,
        enabled: bool = True,
        version: int = 1,
        register: str | None = None,
        datasets: list[Any] | None = None,
        screens: list[Any] | None = None,
        discovery: str | None = None,
        group: str | None = None,
    ) -> SkillEntry:
        """Cria/sobrescreve o SKILL.md de ``name``. Valida antes; recarrega depois.

        As mesmas regras do loader, aplicadas *na entrada*: o painel não
        consegue gravar uma skill que o boot recusaria. ``requires`` é derivado
        do ``kind`` (nunca vem do cliente).
        """
        skill_dir = self._skill_dir(name)

        description = " ".join((description or "").split())
        if not description:
            raise ValueError("A descrição é obrigatória — é a única linha que o cérebro vê sempre.")
        if len(description) > _MAX_DESCRIPTION_CHARS:
            raise ValueError(
                f"Descrição com {len(description)} chars; o máximo é {_MAX_DESCRIPTION_CHARS}."
            )

        if kind not in _WRITABLE_KINDS:
            raise ValueError(f"kind inválido: {kind!r}. Use um de: {', '.join(_WRITABLE_KINDS)}.")

        body = (body or "").strip()
        if not body:
            raise ValueError("O corpo do SKILL.md é obrigatório (as instruções da skill).")
        if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
            raise ValueError(f"Corpo acima de {_MAX_BODY_BYTES // 1024} KB — mova detalhe para references/.")

        tools = [t.strip() for t in (tools or []) if t and t.strip()]
        datasets = list(datasets or [])
        screens = list(screens or [])

        # Declarar `datasets:` JÁ é declarar tools: o gating soma as data_* em
        # tool_names_for. É isto que faz uma skill nova ganhar capacidade sem
        # código Python novo — sem afrouxar o resto: `tools:` continua tendo de
        # existir no registry/agregador.
        if kind == "tool_backed":
            if not tools and not datasets:
                raise ValueError(
                    "Uma skill tool_backed precisa declarar ao menos uma tool "
                    "(ou um cadastro em 'datasets', que traz as tools data_*)."
                )
            missing = [t for t in tools if not self._tool_exists(t)]
            if missing:
                raise ValueError(
                    f"Tools não registradas no neo-core: {', '.join(missing)}. "
                    "Uma tool nova exige código (modules/<x>/ + api/main.py) — ou "
                    "declare 'datasets' para ganhar as tools de cadastro."
                )
        elif tools:
            raise ValueError(f"'tools' só vale para kind=tool_backed (esta é {kind}).")

        for raw in datasets:
            slug = raw if isinstance(raw, str) else (raw or {}).get("slug") or (raw or {}).get("nome")
            if not slug:
                raise ValueError(f"entrada inválida em 'datasets': {raw!r} (falta o slug).")
            from core.datasets.schema import validate_identifier

            validate_identifier(str(slug), what="slug do cadastro")

        discovery = (discovery or "").strip().lower() or None
        if discovery is not None and discovery not in _DISCOVERY_MODES:
            raise ValueError(
                f"discovery inválido: {discovery!r}. Use um de: {', '.join(_DISCOVERY_MODES)}."
            )
        group = (group or "").strip().lower() or None

        register = (register or "").strip().lower() or None
        if register is not None:
            from core.agent.register import Register

            if register not in {r.value for r in Register}:
                raise ValueError(
                    f"register inválido: {register!r}. "
                    f"Use um de: {', '.join(r.value for r in Register)}."
                )

        # requires é derivado, não declarado: scripts só rodam no sandbox.
        requires = ["workbench"] if kind == "scripted" else []

        # Preserva chaves extras de um SKILL.md escrito à mão (o painel não é o
        # único autor — o repo também é).
        extra: dict[str, Any] = {}
        md = skill_dir / "SKILL.md"
        if md.is_file():
            old_meta, _ = split_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            extra = {k: v for k, v in (old_meta or {}).items() if k not in _FRONTMATTER_KEYS}

        meta: dict[str, Any] = {
            "name": name,
            "description": description,
            "keywords": [k.strip() for k in (keywords or []) if k and k.strip()],
            "kind": kind,
            "enabled": bool(enabled),
            "version": int(version),
        }
        if register:
            meta["register"] = register
        if tools:
            meta["tools"] = tools
        if datasets:
            meta["datasets"] = datasets
        if screens:
            meta["screens"] = screens
        if discovery:
            meta["discovery"] = discovery
        if group:
            meta["group"] = group
        if env:
            meta["env"] = [e.strip() for e in env if e and e.strip()]
        if requires:
            meta["requires"] = requires
        meta.update(extra)

        skill_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(md, f"---\n{_dump_frontmatter(meta)}---\n\n{body}\n")
        self.load()

        entry = self.get(name)
        if entry is None:  # defensivo: validação e loader não podem divergir
            reason = next((e["error"] for e in self._errors if e["skill"] == name), "motivo desconhecido")
            raise ValueError(f"Skill gravada mas recusada pelo loader: {reason}")
        logger.info("[SkillLibrary] write_skill name=%s kind=%s enabled=%s", name, kind, enabled)
        return entry

    def _tool_exists(self, tool_name: str) -> bool:
        """A tool existe em ALGUMA origem? (agregador primeiro, registry depois)"""
        try:
            from core.tools.aggregator import get_aggregator

            if get_aggregator().exists(tool_name):
                return True
        except Exception:  # noqa: BLE001 — agregador é opcional
            pass
        return self.tool_registry is not None and self.tool_registry.exists(tool_name)

    def set_enabled(self, name: str, enabled: bool) -> SkillEntry:
        """Liga/desliga a skill reescrevendo só o ``enabled:`` do frontmatter."""
        src = self.read_source(name)
        if src["parse_error"]:
            # Reescrever aqui apagaria o frontmatter que o operador precisa ver.
            raise ValueError(f"SKILL.md de '{name}' está quebrado ({src['parse_error']}) — edite-o antes.")
        meta = dict(src["meta"])
        meta["enabled"] = bool(enabled)
        md = self._skill_dir(name) / "SKILL.md"
        _atomic_write(md, f"---\n{_dump_frontmatter(meta)}---\n\n{src['body']}\n")
        self.load()
        entry = self.get(name)
        if entry is None:
            raise ValueError(f"Skill '{name}' é inválida — corrija o SKILL.md antes de ligar/desligar.")
        return entry

    def delete_skill(self, name: str) -> None:
        """Apaga a pasta da skill. Só apaga o que É uma skill (tem SKILL.md)."""
        skill_dir = self._skill_dir(name)
        if not (skill_dir / "SKILL.md").is_file():
            raise ValueError(f"Skill '{name}' não existe em {self.root}.")
        resolved = skill_dir.resolve()
        if resolved.parent != self.root.resolve():
            raise ValueError(f"Recusando apagar fora da raiz de skills: {resolved}")
        shutil.rmtree(resolved)
        self.load()
        logger.info("[SkillLibrary] delete_skill name=%s", name)

    def list_files(self, name: str) -> list[dict[str, Any]]:
        """Arquivos de references/ e scripts/ da skill (path relativo + tamanho)."""
        skill_dir = self._skill_dir(name)
        if not (skill_dir / "SKILL.md").is_file():
            raise ValueError(f"Skill '{name}' não existe em {self.root}.")
        files: list[dict[str, Any]] = []
        for sub in _EDITABLE_DIRS:
            d = skill_dir / sub
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if not f.is_file():
                    continue
                rel = f"{sub}/{f.name}"
                files.append({
                    "path": rel,
                    "size": f.stat().st_size,
                    "editable": f.suffix.lower() in _EDITABLE_SUFFIXES,
                    # scripts rodam no sandbox por ESTE caminho, não pelo do host
                    "sandbox_path": f"/skills/{name}/{rel}" if sub == "scripts" else None,
                })
        return files

    def read_file(self, name: str, file: str) -> str:
        """Conteúdo integral de um arquivo da skill (edição no painel, cap 256 KB)."""
        skill_dir = self._skill_dir(name)
        return self._read_capped(self._resolve_inside(skill_dir, file), _MAX_FILE_BYTES)

    def write_file(self, name: str, file: str, content: str) -> dict[str, Any]:
        """Grava um arquivo em references/ ou scripts/ da skill."""
        skill_dir = self._skill_dir(name)
        if not (skill_dir / "SKILL.md").is_file():
            raise ValueError(f"Skill '{name}' não existe — crie o SKILL.md primeiro.")
        target = self._resolve_writable(skill_dir, file)
        data = (content or "").encode("utf-8")
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(f"Arquivo acima de {_MAX_FILE_BYTES // 1024} KB.")
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content or "")
        logger.info("[SkillLibrary] write_file skill=%s file=%s bytes=%d", name, file, len(data))
        return {"path": file, "size": len(data)}

    def delete_file(self, name: str, file: str) -> None:
        """Apaga um arquivo de references/ ou scripts/."""
        target = self._resolve_writable(self._skill_dir(name), file)
        if not target.is_file():
            raise ValueError(f"Arquivo não encontrado na skill: {file!r}")
        target.unlink()
        logger.info("[SkillLibrary] delete_file skill=%s file=%s", name, file)

    @staticmethod
    def _resolve_writable(skill_dir: Path, relative: str) -> Path:
        """Resolve um caminho GRAVÁVEL: references/<f> ou scripts/<f>, extensão na allowlist.

        Mais estrito que :meth:`_resolve_inside` (que só lê): sem subpastas
        aninhadas, sem SKILL.md (esse passa por write_skill, que valida), sem
        binário/extensão fora da lista. E o arquivo pode não existir ainda.
        """
        rel = (relative or "").strip()
        if not rel or rel.startswith(("/", "~", ".")) or "\\" in rel:
            raise ValueError(f"Caminho inválido: {relative!r} (use 'references/x.md' ou 'scripts/y.py')")
        parts = PurePosixPath(rel).parts
        if len(parts) != 2 or parts[0] not in _EDITABLE_DIRS:
            raise ValueError(
                f"Caminho inválido: {relative!r}. Só {'/ ou '.join(_EDITABLE_DIRS)}/ "
                "com um nível (ex.: 'references/schema.md')."
            )
        if any(p in ("..", ".") for p in parts):
            raise ValueError(f"Caminho inválido: {relative!r}")
        suffix = PurePosixPath(parts[1]).suffix.lower()
        if suffix not in _EDITABLE_SUFFIXES:
            raise ValueError(
                f"Extensão não permitida: {suffix or '(nenhuma)'}. "
                f"Permitidas: {', '.join(sorted(_EDITABLE_SUFFIXES))}."
            )
        target = (skill_dir / rel).resolve()
        root = skill_dir.resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise ValueError(f"Caminho fora da skill: {relative!r}")
        return target

    # ------------------------------------------------------------------
    # Catálogo (nível 0 de disclosure — system note do cognitivo)
    # ------------------------------------------------------------------

    def catalog_note(self, user_message: str | None = None) -> str | None:
        """Nota de sistema com 1 linha por skill (~20 tokens cada). None se vazio.

        Acima de ``skills_catalog_max_entries``, prefiltra por keywords da
        mensagem e anuncia que o catálogo está parcial (skill_list cobre o resto).
        """
        self.maybe_reload()
        entries = self.all()
        if not entries:
            return None

        # Orçamento (docs/datasets-crud-plan.md §8): antes de qualquer teto, tira
        # do catálogo quem não precisa estar nele NESTA mensagem. Skill `always`
        # (o padrão histórico) continua sempre; `keyword` só entra quando a
        # mensagem casa; `oculta` nunca — só por nome exato ou tool_find.
        entries = self._discovery_filter(entries, user_message or "")
        if not entries:
            return None

        max_entries = int(getattr(self.settings, "skills_catalog_max_entries", 30))
        truncated = False
        if len(entries) > max_entries:
            entries = self._prefilter(entries, user_message or "", max_entries)
            truncated = True

        lines = self._catalog_lines(entries)
        note = (
            "## Suas skills (procedimentos prontos)\n"
            "Você tem uma biblioteca de skills — instruções e ferramentas prontas "
            "para tarefas específicas. Catálogo (nome — quando usar):\n"
            + "\n".join(lines)
            + "\n\nQuando o pedido casa com uma skill: chame `skill_read(name)` UMA "
            "vez, leia e SIGA as instruções (elas dizem quais tools/scripts usar). "
            "Não chame skill_read para assuntos sem skill correspondente e não "
            "invente nomes fora do catálogo."
        )
        if truncated:
            note += (
                "\nCatálogo PARCIAL (filtrado para esta mensagem) — use `skill_list()` "
                "para ver todas as skills."
            )
        match = self._match_note(self.all(), user_message or "")
        if match:
            note += "\n\n" + match
        return note

    def keyword_match(self, user_message: str | None) -> str | None:
        """Nome da skill tool_backed cujas keywords casam com a mensagem (ou None).

        Separado do ``autoload_for`` porque o CHAMADOR precisa distinguir "o usuário
        falou do assunto agora" (renova a skill ativa na sessão) de "veio grudado do
        turno anterior" (consome o prazo).
        """
        self.maybe_reload()
        msg = _norm(user_message or "")
        if not msg:
            return None
        best: tuple[int, str] | None = None
        for e in self.all():
            if e.kind != "tool_backed":
                continue
            hits = sum(1 for k in e.keywords if _keyword_hit(_norm(k), msg))
            if hits and (best is None or hits > best[0]):
                best = (hits, e.name)
        return best[1] if best else None

    def autoload_for(
        self, user_message: str | None, sticky: str | None = None
    ) -> tuple[SkillEntry, str, list[Any]] | None:
        """Skill tool_backed com match DURO de keyword: corpo + tools já no round 1.

        A progressive disclosure economiza prompt, mas deixa a ATIVAÇÃO na sorte do
        modelo: com o mesmo pedido ele ora chama ``skill_read``, ora responde de
        memória e inventa o resultado (foi o que quebrou a 'financas' em prod —
        "registrei" sem gravar nada). Quando a keyword casa, a economia não vale o
        risco: antecipamos o nível 1 (corpo + tools destravadas), e o modelo só
        precisa escolher a tool. Sem match, nada muda — catálogo + skill_read.

        ``sticky`` é a skill que já está ATIVA na sessão. Sem ela, a ativação seria
        por mensagem — e conversa é multi-turno: "recebi 50 reais" casa, mas o "so
        salva"/"variados"/"sim" que vêm depois NÃO casam, as tools sumiam no meio do
        fluxo e o modelo voltava a alucinar que tinha registrado (sessão d4e2ba0f).
        Keyword nova sempre vence a sticky (troca de assunto entre skills).

        Devolve ``None`` quando nenhuma skill casa (ou as tools dela não estão
        registradas). O corpo vem do mesmo ``read()`` que o skill_read serve.
        """
        self.maybe_reload()
        msg = _norm(user_message or "")
        best: tuple[int, SkillEntry] | None = None
        if msg:
            for e in self.all():
                if e.kind != "tool_backed":
                    continue
                hits = sum(1 for k in e.keywords if _keyword_hit(_norm(k), msg))
                if hits and (best is None or hits > best[0]):
                    best = (hits, e)
        if best is not None:
            entry = best[1]
        elif sticky:
            candidate = self.get(sticky)
            if candidate is None or candidate.kind != "tool_backed" or not candidate.enabled:
                return None
            entry = candidate
        else:
            return None
        tools = self.tools_for(entry.name)
        if not tools:
            return None
        try:
            body = str(self.read(entry.name).get("content") or "")
        except Exception as e:  # noqa: BLE001 — autoload nunca derruba o turno
            logger.warning("[SkillLibrary] autoload de '%s' falhou: %s", entry.name, e)
            return None
        if not body:
            return None
        return entry, body, tools

    def autoload_note(self, entry: SkillEntry, body: str) -> str:
        """System note com o corpo da skill autocarregada + ordem de usar as tools."""
        tool_names = ", ".join(f"`{t}`" for t in entry.tools)
        return (
            f"## Skill ATIVA nesta mensagem: `{entry.name}`\n"
            "Esta mensagem casou com a skill abaixo, então ela já está carregada e as "
            f"tools dela ({tool_names}) JÁ estão disponíveis para você — não precisa "
            "chamar `skill_read`. Siga as instruções e chame a tool AGORA, nesta mesma "
            "resposta.\n"
            "O dado desta skill vive nas tools dela: não responda de memória, não use "
            "`search_pks` e não peça confirmação para registrar/consultar. Dizer que "
            "registrou/consultou sem ter chamado a tool é mentira — ou chama, ou não "
            "afirma que fez.\n"
            "MAS isso vale para a AÇÃO PEDIDA AGORA. Se você já executou a ação numa "
            "mensagem ANTERIOR desta conversa, ela está feita: NÃO repita a escrita "
            "para 'confirmar' — releia com uma tool de consulta. Escrever duas vezes "
            "cobra o usuário duas vezes.\n\n"
            + body
        )

    def _match_note(self, entries: list[SkillEntry], user_message: str) -> str | None:
        """Aviso quando as keywords da mensagem casam com uma skill. None se nenhuma.

        O catálogo sozinho deixa a decisão de chamar ``skill_read`` na sorte do
        modelo — e para assunto que ele acha que "lembra" (finanças) ele responde
        de memória, cai no ``search_pks`` (onde o dado NÃO está) ou promete
        registrar depois. Este aviso transforma o match em ordem, mantendo a
        guarda ("se o pedido for sobre isso") para o falso positivo sair barato.
        """
        msg = _norm(user_message)
        if not msg:
            return None
        matched: list[tuple[int, SkillEntry, list[str]]] = []
        for e in entries:
            hits = [k for k in e.keywords if _keyword_hit(_norm(k), msg)]
            if hits:
                matched.append((len(hits), e, hits))
        if not matched:
            return None
        matched.sort(key=lambda m: (-m[0], m[1].name))
        top = matched[: self._MATCH_NOTE_MAX_SKILLS]
        names = ", ".join(
            f"`{e.name}` (casou: {', '.join(hits[:3])})" for _, e, hits in top
        )
        return (
            f"⚠️ Esta mensagem casa com {names}. Se o pedido for sobre isso, chame "
            "`skill_read` dessa skill ANTES de responder e execute o que ela mandar "
            "AGORA, nesta mesma resposta — as tools dela são liberadas na hora. "
            "Não responda de memória, não procure no `search_pks` (o dado dessas "
            "skills NÃO mora no PKS), não peça confirmação para registrar/consultar "
            "e não prometa fazer depois.\n"
            "Registrar/gravar/lançar aqui é CHAMAR A TOOL da skill — não existe "
            "handoff nem confirmação para isso. Dizer \"registrei\"/\"já lancei\" sem "
            "ter chamado a tool nesta resposta é MENTIRA: ou você chama a tool, ou "
            "não afirma que fez."
        )

    _MATCH_NOTE_MAX_SKILLS = 2

    def _discovery_filter(
        self, entries: list[SkillEntry], user_message: str
    ) -> list[SkillEntry]:
        """Aplica o modo de descoberta de cada skill (§8.2 do plano).

        É aqui que 20 cadastros instalados deixam de custar ~400 tokens em toda
        mensagem: eles nascem `discovery: keyword` e só aparecem quando o assunto
        aparece. Skill sem `keywords` declarada em modo `keyword` seria
        indescobrível — essa cai para `always`, que é o comportamento seguro.
        """
        msg = _norm(user_message)
        out: list[SkillEntry] = []
        for entry in entries:
            mode = (entry.discovery or "always").lower()
            if mode == "oculta":
                continue
            if mode == "keyword" and entry.keywords:
                name_token = _norm(entry.name.replace("-", " "))
                hit = bool(msg) and (
                    name_token in msg
                    or any(_keyword_hit(_norm(k), msg) for k in entry.keywords if k)
                )
                if not hit:
                    continue
            out.append(entry)
        return out

    def _catalog_lines(self, entries: list[SkillEntry]) -> list[str]:
        """Uma linha por skill — MENOS as agrupadas, que viram uma linha só.

        `group: cadastros` com 20 skills passa de ~400 tokens para ~25. O modelo
        continua sabendo que elas existem e como abrir cada uma (`skill_read`).
        """
        lines: list[str] = []
        groups: dict[str, list[SkillEntry]] = {}
        for entry in entries:
            if entry.group:
                groups.setdefault(entry.group, []).append(entry)
            else:
                lines.append(f"- {entry.name} — {entry.description}")
        for group, members in sorted(groups.items()):
            if len(members) == 1:
                only = members[0]
                lines.append(f"- {only.name} — {only.description}")
                continue
            names = ", ".join(sorted(m.name for m in members))
            lines.append(
                f"- {group} ({len(members)}) — {names}. "
                f'Chame skill_read("<nome>") no que casar com o pedido.'
            )
        return lines

    def _prefilter(
        self, entries: list[SkillEntry], user_message: str, max_entries: int
    ) -> list[SkillEntry]:
        """Ranqueia por hits de keyword/nome na mensagem; empata → ordem alfabética."""
        msg = _norm(user_message)

        def score(e: SkillEntry) -> int:
            hits = sum(1 for k in e.keywords if k and _norm(k) in msg)
            if _norm(e.name.replace("-", " ")) in msg:
                hits += 2
            return hits

        ranked = sorted(entries, key=lambda e: (-score(e), e.name))
        return ranked[:max_entries]

    def catalog_listing(self) -> str:
        """Listagem completa (tool skill_list) — sem budget, sob demanda."""
        self.maybe_reload()
        entries = self.all()
        if not entries:
            return "Nenhuma skill instalada."
        return "\n".join(f"- {e.name} [{e.kind}] — {e.description}" for e in entries)

    # ------------------------------------------------------------------
    # Nota para o intent router (F4)
    # ------------------------------------------------------------------

    _ROUTER_NOTE_MAX_SKILLS = 12
    _ROUTER_NOTE_MAX_KEYWORDS = 4

    def router_note(self) -> str | None:
        """Seção compacta para o prompt do classificador de intenção.

        Diz ao router que assuntos cobertos por skills .md são resolvidos pelo
        CÉREBRO (DIRECT_RESPONSE) — para "consulta o banco X" nunca cair em
        run_command/CLIENT_TOOL. Só nomes+keywords (barato); nunca o corpo.
        """
        self.maybe_reload()
        # O SEGUNDO vazamento de tokens (§8.1): esta nota é paga em TODA mensagem
        # do classificador, não só nas do cognitivo. Skill `oculta` não entra, e
        # cada grupo entra uma vez só — o router não precisa saber que existem 20
        # cadastros, só que o assunto é do cérebro.
        entries = [e for e in self.all() if (e.discovery or "always") != "oculta"]
        if not entries:
            return None
        parts, seen_groups = [], set()
        for e in entries[: self._ROUTER_NOTE_MAX_SKILLS]:
            if e.group:
                if e.group in seen_groups:
                    continue
                seen_groups.add(e.group)
                group_keywords = sorted(
                    {k for m in entries if m.group == e.group for k in m.keywords}
                )[: self._ROUTER_NOTE_MAX_KEYWORDS]
                parts.append(
                    f"{e.group} ({', '.join(group_keywords)})" if group_keywords else e.group
                )
                continue
            kws = ", ".join(e.keywords[: self._ROUTER_NOTE_MAX_KEYWORDS])
            parts.append(f"{e.name} ({kws})" if kws else e.name)
        return (
            "## Assuntos com skill interna do cérebro (→ DIRECT_RESPONSE)\n"
            "O núcleo cognitivo tem skills próprias para: "
            + " · ".join(parts)
            + ".\nPedidos sobre esses assuntos NÃO são CLIENT_TOOL nem SERVER_TOOL → "
            "DIRECT_RESPONSE (o cérebro lê a skill e resolve sozinho, no próprio "
            "sandbox/banco quando preciso)."
        )


def _split_raw(raw: str) -> tuple[str | None, str]:
    """Corta o texto em (bloco YAML, corpo) SEM parsear — (None, texto) se não há fecho.

    Separado de :func:`split_frontmatter` porque o painel precisa recuperar o
    corpo mesmo quando o YAML está quebrado (para o operador consertar).
    """
    text = raw.lstrip("﻿")
    if not text.startswith("---"):
        return None, text
    parts = text.split("\n---", 2)
    # parts[0] = "---\n<yaml...>" (sem o fecho); precisa existir um fecho.
    if len(parts) < 2:
        return None, text
    yaml_block = parts[0].lstrip("-").strip("\n")
    # Reanexa o que sobrou caso o corpo contenha "\n---" (hr markdown).
    body = parts[1] if len(parts) == 2 else parts[1] + "\n---" + parts[2]
    return yaml_block, body


def split_frontmatter(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """Separa frontmatter YAML do corpo. (None, corpo) quando não há frontmatter."""
    yaml_block, body = _split_raw(raw)
    if yaml_block is None:
        return None, body
    meta = yaml.safe_load(yaml_block)
    if not isinstance(meta, dict):
        return None, body
    return meta, body


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Corta ``text`` para caber em ``max_bytes`` sem quebrar um caractere."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class _SkillDumper(yaml.SafeDumper):
    """Mapas em bloco, listas inline — ``keywords: [csv, planilha]``, como nas skills à mão."""


_SkillDumper.add_representer(
    list,
    lambda dumper, data: dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True),
)


def _dump_frontmatter(meta: dict[str, Any]) -> str:
    """Serializa o frontmatter chave a chave, em ordem estável e sem quebrar linha.

    Chave a chave (em vez de um dump do dict inteiro) para manter a ordem de
    :data:`_FRONTMATTER_KEYS`, com os extras do autor humano no fim.
    """
    keys = [k for k in _FRONTMATTER_KEYS if k in meta]
    keys += [k for k in meta if k not in _FRONTMATTER_KEYS]  # extras preservados
    lines = [
        yaml.dump(
            {key: meta[key]},
            Dumper=_SkillDumper,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=10**6,  # sem wrap: description longa vira 1 linha
        ).strip()
        for key in keys
    ]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    """Escreve via tmp + ``os.replace`` — o hot-reload por mtime nunca vê meio arquivo."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
