"""PKS search tool definition — server-side."""

from shared import ToolParameter, ToolSchema

PKS_SEARCH_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="search_pks",
        description=(
            "Busca ATIVAMENTE no PKS (memória persistente do usuário) por entidades/"
            "fatos salvos — treinos, rotinas, dispositivos, preferências, regras. "
            "Use SEMPRE que precisar verificar se algo foi realmente salvo (ex.: o "
            "usuário pergunta 'salvou mesmo?', 'tá no PKS?') ou recuperar um fato "
            "específico. Retorna as entidades reais encontradas com score — nunca "
            "afirme que algo está salvo sem confirmar aqui."
        ),
        # Classificar não é usar: a política "nunca afirme sem confirmar"
        # é do cérebro e fica na description.
        router_summary=(
            "Busca fatos salvos na memória persistente do usuário (PKS)."
        ),
        module="pks_search",
        execution="server-side",
        timeout=15,
        parameters={
            "query": ToolParameter(
                type="string",
                description="Termos de busca (ex: 'treino quinta', 'ar-condicionado quarto').",
                required=True,
            ),
        },
    ),
]
