"""Desktop module."""

from modules.desktop.ai_service import AIService, AIServiceResponse
from modules.desktop.service import DesktopService
from modules.desktop.tools import DESKTOP_TOOLS

__all__ = [
    "AIService",
    "AIServiceResponse",
    "DesktopService",
    "DESKTOP_TOOLS",
]
