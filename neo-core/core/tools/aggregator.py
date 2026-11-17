"""Agregador de tools (docs/datasets-crud-plan.md §2).

Um índice de **bindings** por cima do :class:`~core.tools.registry.ToolRegistry`.
Não substitui o registry — o :class:`~core.tools.executor.ToolExecutor` continua
consultando ele para despachar. O que o agregador acrescenta:

1. **Endereço único** para tools que nascem em lugares diferentes do código
   (``modules/*/tools.py``, o gerador de cadastros, o ``tools.py`` de uma skill
   instalada). Achar uma tool deixa de depender de saber onde ela mora.
2. **Descoberta barata** — ``tool_find`` devolve ``nome — resumo`` sem pagar o
   schema; o schema só entra no prompt quando a tool é desbloqueada.
3. **Origem e dono**, que é o que deixa ``SkillLibrary.tools_for`` perguntar
   "quais tools são desta skill?" sem a lista literal do frontmatter.

Duas formas de o modelo chegar numa tool (§2.3 do plano):

- **Nível A (default)**: ``skill_read`` desbloqueia os *schemas reais* no turno.
  O provider valida os argumentos — é o caminho correto e é o que já roda hoje.
- **Nível B (fallback)**: ``tool_run(nome, args)``, para a cauda longa e para
  tools carregadas depois do boot. Custa precisão (sem schema tipado o modelo
  inventa argumento — histórico: ``entity_id`` da skill HA, ``file_path`` do
  classificador), então o executor valida contra o schema do binding e devolve
  a assinatura inteira no erro.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from shared import ToolSchema

logger = logging.getLogger(__name__)

ToolOrigin = Literal["core", "dataset", "skill"]

#: Nome único global. Os providers aceitam só ``[A-Za-z0-9_-]`` em nome de
#: função, então nada de namespace com ponto — a unicidade é imposta aqui.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,48}$")
_MAX_SUMMARY = 90


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


class ToolNameConflict(ValueError):
    """Nome já tomado por outra origem.

    A instalação RECUSA em vez de renomear: o corpo do SKILL.md cita a tool pelo
    nome, e um rename silencioso quebraria a instrução que o modelo vai ler.
    """


@dataclass
class ToolBinding:
    """Uma tool endereçável pelo nome único, com sua procedência."""

    name: str
    summary: str
    schema: ToolSchema
    origin: ToolOrigin
    source: str
    module: str
    owner_skill: str | None = None
    tags: list[str] = field(default_factory=list)

    def to_dict(self, *, with_schema: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "summary": self.summary,
            "origin": self.origin,
            "source": self.source,
            "module": self.module,
            "owner_skill": self.owner_skill,
            "tags": list(self.tags),
        }
        if with_schema:
            data["parameters"] = {
                pname: {
                    "type": p.type,
                    "description": p.description,
                    "required": p.required,
                    "enum": p.enum,
                }
                for pname, p in self.schema.parameters.items()
            }
            data["description"] = self.schema.description
        return data

    def signature(self) -> str:
        """``data_create(dataset, campos)`` — uma linha, para o bloco lido pelo modelo."""
        required = [n for n, p in self.schema.parameters.items() if p.required]
        optional = [f"{n}?" for n, p in self.schema.parameters.items() if not p.required]
        return f"{self.name}({', '.join(required + optional)})"


def _derive_summary(schema: ToolSchema) -> str:
    """Primeira frase da description, capada. É o que o índice paga."""
    text = (schema.router_summary or schema.description or "").strip()
    sentence = re.split(r"(?<=[.!?])\s", text)[0] if text else schema.name
    sentence = " ".join(sentence.split())
    if len(sentence) > _MAX_SUMMARY:
        sentence = sentence[: _MAX_SUMMARY - 1].rstrip() + "…"
    return sentence


class ToolAggregator:
    """Índice global de tools, alimentado por três origens."""

    def __init__(self, registry: Any | None = None) -> None:
        self._bindings: dict[str, ToolBinding] = {}
        self._registry = registry

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def bind(
        self,
        schema: ToolSchema,
        *,
        origin: ToolOrigin,
        source: str,
        owner_skill: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        replace: bool = False,
    ) -> ToolBinding:
        """Registra (ou substitui) um binding. Levanta em colisão de nome."""
        name = (schema.name or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"nome de tool inválido: {name!r}. Use minúsculas, números e "
                "underscore (3–49 chars), começando por letra."
            )
        existing = self._bindings.get(name)
        if existing is not None and not replace:
            if existing.source == source and existing.origin == origin:
                return existing  # idempotente: mesma origem, mesmo arquivo
            raise ToolNameConflict(
                f"a tool '{name}' já está registrada por {existing.source} "
                f"(origem {existing.origin}). Escolha outro nome."
            )
        binding = ToolBinding(
            name=name,
            summary=summary or _derive_summary(schema),
            schema=schema,
            origin=origin,
            source=source,
            module=schema.module,
            owner_skill=owner_skill,
            tags=list(tags or []),
        )
        self._bindings[name] = binding
        return binding

    def bind_many(self, schemas: list[ToolSchema], **kwargs: Any) -> list[ToolBinding]:
        return [self.bind(s, **kwargs) for s in schemas]

    def unbind(self, name: str) -> bool:
        return self._bindings.pop(name, None) is not None

    def unbind_skill(self, skill_name: str) -> list[str]:
        """Solta tudo que uma skill trouxe — usado no uninstall."""
        removed = [n for n, b in self._bindings.items() if b.owner_skill == skill_name]
        for name in removed:
            del self._bindings[name]
        return removed

    def mirror_registry(self, registry: Any) -> int:
        """Espelha o ``ToolRegistry`` do boot — as tools que já existiam.

        Idempotente: rodar de novo depois de registrar tools novas só acrescenta.
        Nunca sobrescreve um binding de outra origem (uma tool de skill com o
        mesmo nome de uma tool core seria um conflito, não um espelho).
        """
        self._registry = registry
        count = 0
        for schema in registry.get_all():
            if schema.name in self._bindings:
                continue
            try:
                self.bind(
                    schema,
                    origin="core",
                    source=f"modules/{schema.module}",
                    tags=[schema.module],
                )
                count += 1
            except (ValueError, ToolNameConflict) as exc:
                # Tool do core com nome fora do padrão do agregador não pode
                # derrubar o boot — ela continua funcionando pelo registry.
                logger.warning("[ToolAggregator] '%s' não indexada: %s", schema.name, exc)
        return count

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def get(self, name: str) -> ToolBinding | None:
        return self._bindings.get((name or "").strip())

    def exists(self, name: str) -> bool:
        return (name or "").strip() in self._bindings

    def all(self) -> list[ToolBinding]:
        return sorted(self._bindings.values(), key=lambda b: b.name)

    def by_origin(self, origin: ToolOrigin) -> list[ToolBinding]:
        return [b for b in self.all() if b.origin == origin]

    def by_skill(self, skill_name: str) -> list[ToolBinding]:
        return [b for b in self.all() if b.owner_skill == skill_name]

    def schemas_for(self, names: list[str]) -> list[ToolSchema]:
        """Resolve nomes → schemas. Nome desconhecido é PULADO, nunca inventado.

        Mesma política do ``SkillLibrary.tools_for`` original: o gating nunca
        oferece ao modelo uma tool que o executor não sabe despachar (foi assim
        que nasceram as client tools fantasma que travavam 300s).
        """
        out: list[ToolSchema] = []
        for name in names:
            binding = self.get(name)
            if binding is not None:
                out.append(binding.schema)
        return out

    def search(self, query: str, *, limit: int = 12) -> list[ToolBinding]:
        """Busca no índice por nome, tag e resumo. Sem query → tudo (capado)."""
        term = _fold(query or "").strip()
        if not term:
            return self.all()[:limit]
        tokens = [t for t in re.split(r"\W+", term) if t]

        def score(b: ToolBinding) -> int:
            haystack_name = _fold(b.name)
            haystack_rest = _fold(f"{b.summary} {' '.join(b.tags)} {b.owner_skill or ''}")
            points = 0
            for token in tokens:
                if token == haystack_name:
                    points += 10
                elif token in haystack_name:
                    points += 4
                if token in haystack_rest:
                    points += 1
            return points

        ranked = [(score(b), b) for b in self.all()]
        hits = [b for s, b in sorted(ranked, key=lambda p: (-p[0], p[1].name)) if s > 0]
        return hits[:limit]

    def render_signatures(self, bindings: list[ToolBinding]) -> str:
        """Bloco de assinaturas anexado ao corpo da skill lida (§7.2 do plano)."""
        if not bindings:
            return ""
        lines = [f"- {b.signature()} — {b.summary}" for b in bindings]
        return "### Tools desta skill\n" + "\n".join(lines)

    def stats(self) -> dict[str, int]:
        counts = {"core": 0, "dataset": 0, "skill": 0}
        for binding in self._bindings.values():
            counts[binding.origin] = counts.get(binding.origin, 0) + 1
        counts["total"] = len(self._bindings)
        return counts


# ---------------------------------------------------------------------------
# Singleton de processo
# ---------------------------------------------------------------------------
# Único como o ToolRegistry: quem precisa dele (SkillLibrary, executores,
# rotas do painel) não recebe injeção em todo lugar. `reset` existe para os
# testes — o boot chama `mirror_registry` e é idempotente.

_aggregator: ToolAggregator | None = None


def get_aggregator() -> ToolAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = ToolAggregator()
    return _aggregator


def reset_aggregator() -> None:
    global _aggregator
    _aggregator = None
