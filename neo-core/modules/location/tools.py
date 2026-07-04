"""Location tool definition — server-side."""

from shared import ToolParameter, ToolSchema

LOCATION_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="get_user_location",
        description=(
            "Resolve a cidade provável do usuário SEM chutar: primeiro consulta o "
            "PKS (onde o usuário mora), depois o GPS do dispositivo (se disponível). "
            "Use ANTES de get_weather quando o usuário NÃO disser a cidade. Se "
            "retornar needs_user_input=true, PERGUNTE a cidade ao usuário — não "
            "invente. Quando o usuário informar a cidade, chame de novo com "
            "save_city para o Neo aprender e nunca mais perguntar."
        ),
        module="location",
        execution="server-side",
        timeout=10,
        parameters={
            "save_city": ToolParameter(
                type="string",
                description=(
                    "Cidade informada pelo usuário, para gravar no PKS (aprender). "
                    "Deixe vazio para apenas consultar a localização conhecida."
                ),
                required=False,
            ),
        },
    ),
]
