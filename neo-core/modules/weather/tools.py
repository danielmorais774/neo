"""Weather tool definitions — server-side."""

from shared import ToolParameter, ToolSchema

WEATHER_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="get_weather",
        description="Obtém informações do clima/tempo para uma cidade usando a API Open-Meteo (gratuita, sem chave)",
        module="weather",
        execution="server-side",
        timeout=15,
        parameters={
            "city": ToolParameter(
                type="string",
                description="Nome da cidade (ex: 'Brasília', 'São Paulo', 'Rio de Janeiro')",
                required=True,
            ),
        },
    ),
]
