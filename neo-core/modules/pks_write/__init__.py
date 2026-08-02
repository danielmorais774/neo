"""Módulo pks_write — tool pks_remember (escrita sob demanda no PKS).

Ver docs/pks-write-on-demand-plan.md. A tool delega ao PKSMemoryPlanner por
baixo (modo confirmado); nada é escrito cru.
"""

from modules.pks_write.executor import PKSWriteExecutor
from modules.pks_write.tools import PKS_WRITE_TOOLS

PKS_WRITE_TOOL_NAMES = frozenset(t.name for t in PKS_WRITE_TOOLS)

__all__ = ["PKS_WRITE_TOOLS", "PKS_WRITE_TOOL_NAMES", "PKSWriteExecutor"]
