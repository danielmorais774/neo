"""Weather tool executor — uses Open-Meteo free API (no API key needed).

No hardcoded cities and no guessing: a city is geocoded on demand (works
worldwide), or precise ``lat``/``lon`` (e.g. from device GPS) are used directly.
When neither is given, the tool signals ``needs_city`` so the caller resolves the
user's location (PKS → GPS → ask) instead of defaulting to some random city.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherExecutor:
    """Executes weather tool calls using Open-Meteo API."""

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a weather tool.

        Args:
            tool_name: Tool name (currently only 'get_weather').
            arguments: ``city`` and/or ``lat``/``lon``.

        Returns:
            Weather data dict, or ``{"error": ..., "needs_city": True}`` when the
            location is unknown.
        """
        if tool_name != "get_weather":
            raise ValueError(f"Unknown weather tool: {tool_name}")

        city = str(arguments.get("city") or "").strip()
        lat = arguments.get("lat", arguments.get("latitude"))
        lon = arguments.get("lon", arguments.get("longitude"))

        # 1. Precise coordinates (e.g. device GPS) win — no geocoding needed.
        coords: tuple[float, float] | None = None
        label = city
        if lat is not None and lon is not None:
            try:
                coords = (float(lat), float(lon))
                label = city or "sua localização"
            except (TypeError, ValueError):
                coords = None

        # 2. Geocode the city name (any city worldwide).
        if coords is None:
            if not city:
                return {
                    "error": "needs_city",
                    "needs_city": True,
                    "message": (
                        "Nenhuma cidade informada. Resolva a localização do usuário "
                        "(get_user_location) ou pergunte de qual cidade ele quer o clima."
                    ),
                }
            geocoded = await self._geocode(city)
            if not geocoded:
                return {
                    "error": "city_not_found",
                    "needs_city": True,
                    "message": f"Não encontrei a cidade '{city}'. Confirme o nome com o usuário.",
                }
            coords, label = geocoded

        lat_f, lon_f = coords
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    WEATHER_URL,
                    params={
                        "latitude": lat_f,
                        "longitude": lon_f,
                        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
                        "timezone": "auto",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:  # network/timeout/HTTP error → report, don't crash the agent
            logger.warning(f"Weather fetch failed for '{label}' ({lat_f},{lon_f}): {e}")
            return {"error": f"Falha ao consultar o clima: {e}"}

        current = data.get("current", {})
        units = data.get("current_units", {})

        weather_code = current.get("weather_code", 0)
        description = self._weather_code_to_description(weather_code)

        return {
            "city": label,
            "temperature": f"{current.get('temperature_2m', 'N/A')}{units.get('temperature_2m', '°C')}",
            "feels_like": f"{current.get('apparent_temperature', 'N/A')}{units.get('apparent_temperature', '°C')}",
            "humidity": f"{current.get('relative_humidity_2m', 'N/A')}{units.get('relative_humidity_2m', '%')}",
            "wind_speed": f"{current.get('wind_speed_10m', 'N/A')}{units.get('wind_speed_10m', 'km/h')}",
            "condition": description,
        }

    async def _geocode(self, city: str) -> tuple[tuple[float, float], str] | None:
        """Geocode a city name using Open-Meteo. Returns (coords, label) or None."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    GEOCODING_URL,
                    params={"name": city, "count": 1, "language": "pt"},
                )
                resp.raise_for_status()
                data = resp.json()

            results = data.get("results", [])
            if results:
                top = results[0]
                label_parts = [top.get("name", city)]
                if top.get("admin1"):
                    label_parts.append(str(top["admin1"]))
                if top.get("country"):
                    label_parts.append(str(top["country"]))
                return (top["latitude"], top["longitude"]), ", ".join(label_parts)
            return None
        except Exception as e:
            logger.warning(f"Geocoding failed for '{city}': {e}")
            return None

    @staticmethod
    def _weather_code_to_description(code: int) -> str:
        """Convert WMO weather code to Portuguese description."""
        descriptions = {
            0: "Céu limpo",
            1: "Predominantemente limpo",
            2: "Parcialmente nublado",
            3: "Nublado",
            45: "Neblina",
            48: "Neblina com geada",
            51: "Garoa leve",
            53: "Garoa moderada",
            55: "Garoa intensa",
            61: "Chuva leve",
            63: "Chuva moderada",
            65: "Chuva forte",
            71: "Neve leve",
            73: "Neve moderada",
            75: "Neve forte",
            80: "Pancadas de chuva leves",
            81: "Pancadas de chuva moderadas",
            82: "Pancadas de chuva fortes",
            95: "Tempestade",
            96: "Tempestade com granizo leve",
            99: "Tempestade com granizo forte",
        }
        return descriptions.get(code, f"Código meteorológico {code}")
