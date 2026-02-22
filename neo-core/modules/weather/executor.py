"""Weather tool executor — uses Open-Meteo free API (no API key needed)."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# City → (lat, lon) lookup for common Brazilian cities
CITY_COORDS: dict[str, tuple[float, float]] = {
    "brasilia": (-15.7801, -47.9292),
    "sao paulo": (-23.5505, -46.6333),
    "rio de janeiro": (-22.9068, -43.1729),
    "belo horizonte": (-19.9167, -43.9345),
    "salvador": (-12.9714, -38.5014),
    "curitiba": (-25.4284, -49.2733),
    "fortaleza": (-3.7172, -38.5433),
    "recife": (-8.0476, -34.8770),
    "porto alegre": (-30.0346, -51.2177),
    "manaus": (-3.1190, -60.0217),
    "goiania": (-16.6869, -49.2648),
    "campinas": (-22.9099, -47.0626),
    "florianopolis": (-27.5954, -48.5480),
    "vitoria": (-20.3155, -40.3128),
    "natal": (-5.7945, -35.2110),
    "londrina": (-23.3103, -51.1628),
    "new york": (40.7128, -74.0060),
    "london": (51.5074, -0.1278),
    "tokyo": (35.6762, 139.6503),
    "paris": (48.8566, 2.3522),
    "berlin": (52.5200, 13.4050),
    "lisbon": (38.7223, -9.1393),
    "lisboa": (38.7223, -9.1393),
}

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


class WeatherExecutor:
    """Executes weather tool calls using Open-Meteo API."""

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a weather tool.

        Args:
            tool_name: Tool name (currently only 'get_weather').
            arguments: Tool arguments (must contain 'city').

        Returns:
            Weather data dict.
        """
        if tool_name != "get_weather":
            raise ValueError(f"Unknown weather tool: {tool_name}")

        city = arguments.get("city", "")
        return await self._get_weather(city)

    async def _get_weather(self, city: str) -> dict[str, Any]:
        """Get weather for a city."""
        city_lower = city.lower().strip()

        # Try local lookup first
        coords = CITY_COORDS.get(city_lower)

        if not coords:
            # Use geocoding API
            coords = await self._geocode(city)

        if not coords:
            return {"error": f"Cidade '{city}' não encontrada"}

        lat, lon = coords

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                WEATHER_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
                    "timezone": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        units = data.get("current_units", {})

        weather_code = current.get("weather_code", 0)
        description = self._weather_code_to_description(weather_code)

        return {
            "city": city,
            "temperature": f"{current.get('temperature_2m', 'N/A')}{units.get('temperature_2m', '°C')}",
            "feels_like": f"{current.get('apparent_temperature', 'N/A')}{units.get('apparent_temperature', '°C')}",
            "humidity": f"{current.get('relative_humidity_2m', 'N/A')}{units.get('relative_humidity_2m', '%')}",
            "wind_speed": f"{current.get('wind_speed_10m', 'N/A')}{units.get('wind_speed_10m', 'km/h')}",
            "condition": description,
        }

    async def _geocode(self, city: str) -> tuple[float, float] | None:
        """Geocode a city name using Open-Meteo."""
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
                return (results[0]["latitude"], results[0]["longitude"])
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
