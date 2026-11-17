"""Tool sql_query_ro — SQL somente-leitura no Postgres do Neo.

Backing da skill tool_backed ``postgres-neo`` (docs/skills-library-plan.md).
NÃO entra no read set default do cognitivo: só aparece via tool gating, depois
que o modelo leu a skill (que ensina o schema e as regras de uso).
"""

from shared import ToolParameter, ToolSchema

SQLRO_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="sql_query_ro",
        description=(
            "Executa um SELECT somente-leitura no banco Postgres do Neo (neodb: "
            "PKS, sessões de conversa, missões, preferências, jobs). Rejeita "
            "qualquer escrita. Sempre use LIMIT; prefira agregações a despejar "
            "linhas. O schema das tabelas está na skill 'postgres-neo'."
        ),
        module="sqlro",
        execution="server-side",
        timeout=20,
        parameters={
            "query": ToolParameter(
                type="string",
                description="Um único statement SELECT/WITH (sem ponto-e-vírgula extra).",
                required=True,
            ),
            "limit": ToolParameter(
                type="integer",
                description="Máximo de linhas retornadas (padrão 50, teto 200).",
                required=False,
            ),
        },
    ),
]
