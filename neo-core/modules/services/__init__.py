"""Módulo services — tools service_* do Service Gateway (HTTP arbitrário sem
o LLM ver credencial). Ver docs/service-gateway-dsl-plan.md."""

from modules.services.executor import ServicesExecutor
from modules.services.tools import SERVICE_TOOL_NAMES, SERVICE_TOOLS

__all__ = ["SERVICE_TOOLS", "SERVICE_TOOL_NAMES", "ServicesExecutor"]
