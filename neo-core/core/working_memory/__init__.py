"""Memória de trabalho do Neo — o caderno da tarefa atual.

Ver docs/working-memory-plan.md. O domínio (store + render) fica aqui; as tools
``wm_*`` e o executor de módulo ficam em ``modules/working_memory`` — mesma
divisão de ``core/skills/library.py`` × ``modules/skills_library``.
"""

from core.working_memory.artifacts import (
    ARTIFACTS_SLUG,
    ARTIFACTS_TITLE,
    parse_artifacts,
    path_map,
    render_context_block,
    render_entry,
    render_summary,
)
from core.working_memory.render import (
    format_when,
    render_auto_entry,
    render_index,
    render_job_entry,
    render_job_summary,
    render_tail,
    stamp,
)
from core.working_memory.store import (
    WorkingMemoryError,
    WorkingMemoryStore,
    derive_summary,
    extract_links,
    slugify,
)

__all__ = [
    "ARTIFACTS_SLUG",
    "ARTIFACTS_TITLE",
    "WorkingMemoryError",
    "WorkingMemoryStore",
    "derive_summary",
    "extract_links",
    "format_when",
    "parse_artifacts",
    "path_map",
    "render_context_block",
    "render_entry",
    "render_summary",
    "render_auto_entry",
    "render_index",
    "render_job_entry",
    "render_job_summary",
    "render_tail",
    "slugify",
    "stamp",
]
