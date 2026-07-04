"""Weather tool definitions — server-side."""

from shared import ToolParameter, ToolSchema

WEATHER_TOOLS: list[ToolSchema] = [
    ToolSchema(
        name="get_weather",
        description=(
            "Obtém o clima/tempo usando a API Open-Meteo (gratuita, sem chave). "
            "Informe a 'city' (qualquer cidade do mundo) OU 'lat'/'lon' (ex: GPS). "
            "NÃO invente cidade: se o usuário não disser e você não souber, use "
            "get_user_location antes. Sem cidade nem coordenadas, retorna needs_city."
        ),
        module="weather",
        execution="server-side",
        timeout=15,
        parameters={
            "city": ToolParameter(
                type="string",
                description="Nome da cidade (ex: 'Brasília', 'Campinas', 'Rio de Janeiro'). Opcional se passar lat/lon.",
                required=False,
            ),
            "lat": ToolParameter(
                type="number",
                description="Latitude (ex: do GPS do dispositivo). Use junto com lon, dispensa a cidade.",
                required=False,
            ),
            "lon": ToolParameter(
                type="number",
                description="Longitude (ex: do GPS do dispositivo). Use junto com lat.",
                required=False,
            ),
        },
    ),
]
