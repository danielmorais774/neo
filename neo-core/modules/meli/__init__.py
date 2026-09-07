"""Integração com a API oficial do Mercado Livre (docs/product-search-plan.md)."""

from modules.meli.client import MeliClient, MeliError
from modules.meli.executor import MeliExecutor
from modules.meli.tools import MELI_TOOL_NAMES, MELI_TOOLS

__all__ = [
    "MeliClient",
    "MeliError",
    "MeliExecutor",
    "MELI_TOOLS",
    "MELI_TOOL_NAMES",
]
