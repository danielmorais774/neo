"""Skills Library module — tools de leitura da biblioteca de skills (SKILL.md).

Ver docs/skills-library-plan.md. A biblioteca em si vive em core/skills/library.py;
este módulo expõe as tools server-side (skill_read/skill_list) no padrão dos
demais modules/*.
"""

from modules.skills_library.executor import SkillsLibraryExecutor
from modules.skills_library.tools import SKILLS_LIBRARY_TOOLS

__all__ = ["SKILLS_LIBRARY_TOOLS", "SkillsLibraryExecutor"]
