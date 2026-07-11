"""SQL read-only module — backing da skill tool_backed ``postgres-neo``.

A tool NÃO entra no read set default do cognitivo: só aparece via tool gating
(depois de skill_read). Ver docs/skills-library-plan.md.
"""

from modules.sqlro.executor import SqlRoExecutor, validate_ro_query
from modules.sqlro.tools import SQLRO_TOOLS

__all__ = ["SQLRO_TOOLS", "SqlRoExecutor", "validate_ro_query"]
