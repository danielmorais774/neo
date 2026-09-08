"""Executor das tools skill_read/skill_list — leitura pura da SkillLibrary.

O retorno é uma STRING formatada (não dict): o tool loop do cognitivo interpola
o resultado em texto ("Result: ..."), e um corpo markdown com quebras de linha
reais é muito mais legível para o modelo do que um dict com ``\\n`` escapado.
"""

from __future__ import annotations

import logging
from typing import Any

from core.skills.library import SkillLibrary

logger = logging.getLogger(__name__)


class SkillsLibraryExecutor:
    """Executa skill_read/skill_list contra a :class:`SkillLibrary` singleton."""

    def __init__(self, library: SkillLibrary, dataset_registry: Any = None) -> None:
        self.library = library
        # Opcional: com ele, ler uma skill que declara `datasets:` já devolve o
        # SCHEMA do cadastro junto do corpo — um round de conversa a menos
        # (docs/datasets-crud-plan.md §7.2). Sem ele, nada quebra: o modelo cai
        # no `data_schema`.
        self.dataset_registry = dataset_registry

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "skill_read":
            return await self._skill_read_async(arguments)
        if tool_name == "skill_list":
            self.library.maybe_reload()
            return self.library.catalog_listing()
        raise ValueError(f"Tool desconhecida no módulo skills_library: {tool_name}")

    async def _skill_read_async(self, arguments: dict[str, Any]) -> str:
        """skill_read + os blocos gerados (cadastros e assinaturas de tools)."""
        base = self._skill_read(arguments)
        if arguments.get("file"):
            return base
        name = str(arguments.get("name") or "").strip()
        entry = self.library.get(name)
        if entry is None:
            return base

        blocks: list[str] = []
        slugs = [
            d if isinstance(d, str) else str((d or {}).get("slug") or (d or {}).get("nome") or "")
            for d in (entry.datasets or [])
        ]
        # Teto de 3 cadastros inline (§7.2): do 4º em diante o modelo chama
        # data_schema — melhor pagar um round do que inflar toda leitura.
        for slug in [s for s in slugs if s][:3]:
            block = await self._dataset_block(slug)
            if block:
                blocks.append(block)
        restantes = [s for s in slugs if s][3:]
        if restantes:
            blocks.append(
                "Outros cadastros desta skill (use data_schema para ver os campos): "
                + ", ".join(restantes)
            )

        signatures = self._signature_block(name)
        if signatures:
            blocks.append(signatures)
        return base + ("\n\n" + "\n\n".join(blocks) if blocks else "")

    async def _dataset_block(self, slug: str) -> str:
        """'### Cadastro `produtos` … Campos: …' — a partir do registry."""
        if self.dataset_registry is None:
            return ""
        try:
            dataset = await self.dataset_registry.get(slug)
        except Exception as exc:  # noqa: BLE001 — enriquecimento nunca derruba a leitura
            logger.warning("[SkillLibrary] cadastro '%s' não descrito: %s", slug, exc)
            return ""
        if dataset is None:
            return (
                f"ATENÇÃO — o cadastro `{slug}` declarado por esta skill ainda NÃO "
                "existe no banco. Instale a skill pelo painel (aba Skills → Instalar) "
                "antes de tentar gravar nele."
            )
        return dataset.describe_for_model()

    def _signature_block(self, name: str) -> str:
        """Assinaturas das tools destravadas — o que falta ao modelo antes de chamar."""
        try:
            from core.tools.aggregator import get_aggregator

            aggregator = get_aggregator()
            bindings = [
                b for b in (aggregator.get(t) for t in self.library.tool_names_for(name))
                if b is not None
            ]
            return aggregator.render_signatures(bindings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SkillLibrary] assinaturas indisponíveis para '%s': %s", name, exc)
            return ""

    def _skill_read(self, arguments: dict[str, Any]) -> str:
        name = str(arguments.get("name") or "").strip()
        file = arguments.get("file") or None
        self.library.maybe_reload()
        data = self.library.read(name, file=str(file) if file else None)
        logger.info("[SkillLibrary] read name=%s file=%s", name, file or "-")

        if file:
            return f"# {data['skill']} — {data['file']}\n\n{data['content']}"

        parts = [f"# SKILL: {data['skill']} ({data['kind']})", "", data["content"]]
        if data.get("references"):
            parts += [
                "",
                "Arquivos de referência (leia sob demanda com "
                f"skill_read(name=\"{data['skill']}\", file=...)):",
                *(f"- {r}" for r in data["references"]),
            ]
        if data.get("scripts"):
            parts += [
                "",
                "Scripts prontos no SEU sandbox (use estes caminhos no neo_bash, "
                "não reescreva o script):",
                *(f"- {s}" for s in data["scripts"]),
            ]
        if data.get("tools_unlocked"):
            parts += [
                "",
                "Tools destravadas neste turno (já disponíveis para você chamar): "
                + ", ".join(data["tools_unlocked"]),
            ]
        if data.get("tools_unavailable"):
            parts += [
                "",
                "ATENÇÃO — tools desta skill INDISPONÍVEIS agora (módulo não "
                "registrado): " + ", ".join(data["tools_unavailable"]) + ". Não "
                "tente chamá-las; explique a limitação se precisar delas.",
            ]
        return "\n".join(parts)
