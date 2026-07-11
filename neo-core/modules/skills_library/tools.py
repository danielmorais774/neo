"""Tools da biblioteca de skills — server-side, READ-ONLY.

``skill_read`` é o nível 1 da progressive disclosure (docs/skills-library-plan.md):
o catálogo no prompt tem só nome+descrição; o corpo do SKILL.md é pago aqui, sob
demanda. ``skill_list`` cobre o caso de catálogo prefiltrado (parcial) no prompt.
Ambas entram no read set do cognitivo (core/cognitive/read_tools.py) — leitura
pura, zero side effects.
"""

from shared import ToolParameter, ToolSchema

SKILLS_LIBRARY_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="skill_read",
        description=(
            "Lê as instruções de uma skill do seu catálogo (ou um arquivo de "
            "references/ dela). Chame UMA vez por skill e SIGA o que está escrito — "
            "as instruções dizem quais tools/scripts usar. Só para skills que "
            "aparecem no catálogo."
        ),
        module="skills_library",
        execution="server-side",
        timeout=10,
        parameters={
            "name": ToolParameter(
                type="string",
                description="Nome exato da skill, como aparece no catálogo (ex: 'diagnostico-voz').",
                required=True,
            ),
            "file": ToolParameter(
                type="string",
                description=(
                    "Arquivo relativo dentro da skill (ex: 'references/schema.md'). "
                    "Omita para ler as instruções principais (SKILL.md)."
                ),
                required=False,
            ),
        },
    ),
    ToolSchema(
        name="skill_list",
        description=(
            "Lista TODAS as skills instaladas (nome, tipo e descrição). Use apenas "
            "quando o catálogo do prompt estiver marcado como parcial ou quando "
            "precisar conferir se uma skill existe."
        ),
        module="skills_library",
        execution="server-side",
        timeout=10,
        parameters={},
    ),
]
