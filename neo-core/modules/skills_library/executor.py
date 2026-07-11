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

    def __init__(self, library: SkillLibrary) -> None:
        self.library = library

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "skill_read":
            return self._skill_read(arguments)
        if tool_name == "skill_list":
            self.library.maybe_reload()
            return self.library.catalog_listing()
        raise ValueError(f"Tool desconhecida no módulo skills_library: {tool_name}")

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
