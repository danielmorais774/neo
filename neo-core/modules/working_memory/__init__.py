"""Módulo working_memory — tools wm_write/wm_read (caderno de trabalho do Neo).

Ver docs/working-memory-plan.md. O store e a renderização vivem em
``core/working_memory``; aqui ficam só as tools e o executor server-side, no
padrão dos demais ``modules/*``.
"""

from modules.working_memory.executor import WorkingMemoryExecutor
from modules.working_memory.tools import WORKING_MEMORY_TOOL_NAMES, WORKING_MEMORY_TOOLS

__all__ = [
    "WORKING_MEMORY_TOOLS",
    "WORKING_MEMORY_TOOL_NAMES",
    "WorkingMemoryExecutor",
]
