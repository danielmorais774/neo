"""Location executor — resolves the user's probable city without guessing.

Resolution order (product decision): PKS ``located_in`` → device GPS (from the
request metadata) → ask the user. ``save_city`` persists the user's answer so Neo
learns it once and never asks again. Standard module-executor contract
(``execute(tool_name, arguments)``) with a per-request ``set_context``, mirroring
``modules/pks_search``.
"""

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class LocationExecutor:
    """Executes the ``get_user_location`` tool against the user's PKS + device GPS."""

    def __init__(self, pks_service: Any) -> None:
        self.pks = pks_service
        self._user_id: UUID | None = None
        self._geo: dict[str, Any] | None = None

    def set_context(self, user_id: UUID | None, geo: dict[str, Any] | None = None) -> None:
        """Scope subsequent resolutions to a single user (and optional device GPS)."""
        self._user_id = user_id
        self._geo = geo

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name != "get_user_location":
            raise ValueError(f"Unknown location tool: {tool_name}")
        if not self._user_id:
            raise ValueError(
                "get_user_location requer um usuário identificado (user_id ausente)."
            )

        # Learning path: the user just told us their city → persist it.
        save_city = str(arguments.get("save_city") or "").strip()
        if save_city:
            stored = await self.pks.remember_user_city(user_id=self._user_id, city=save_city)
            if stored:
                return {"found": True, "needs_user_input": False, "saved": True, **stored}
            # No primary user to link the relation to — still echo the city back.
            return {
                "found": True,
                "needs_user_input": False,
                "saved": False,
                "source": "user",
                "city": save_city,
                "full": save_city,
            }

        # 1. PKS (authoritative home).
        pks_loc = await self.pks.get_user_location(user_id=self._user_id)
        if pks_loc:
            return {"found": True, "needs_user_input": False, **pks_loc}

        # 2. Device GPS — the only auto-detect source (no IP/timezone).
        geo = self._extract_gps(self._geo)
        if geo:
            return {"found": True, "needs_user_input": False, "source": "gps", **geo}

        # 3. Unknown → the agent must ask the user.
        return {
            "found": False,
            "needs_user_input": True,
            "message": (
                "Localização do usuário desconhecida. Pergunte de qual cidade ele "
                "quer o clima e, ao receber a resposta, grave com save_city."
            ),
        }

    @staticmethod
    def _extract_gps(geo: dict[str, Any] | None) -> dict[str, Any] | None:
        """Normalize a device GPS payload into ``{lat, lon[, city]}`` or ``None``."""
        if not isinstance(geo, dict):
            return None
        lat = geo.get("lat", geo.get("latitude"))
        lon = geo.get("lon", geo.get("longitude"))
        try:
            out: dict[str, Any] = {"lat": float(lat), "lon": float(lon)}
        except (TypeError, ValueError):
            return None
        if geo.get("city"):
            out["city"] = str(geo["city"])
        return out
