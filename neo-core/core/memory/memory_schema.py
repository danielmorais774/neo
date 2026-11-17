"""Schema dataclasses for the persistent memory.md system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Discovery:
    fact: str
    timestamp: str  # ISO format


@dataclass
class HistoryEntry:
    timestamp: str  # ISO format
    task: str
    status: str  # "success" | "failed"


@dataclass
class ActivePlanMemory:
    goal: str
    steps: list[str]  # list of "[ ] description" or "[✓] description"
    created_at: str  # ISO format


@dataclass
class MemoryState:
    project_context: list[str] = field(default_factory=list)
    user_preferences: list[str] = field(default_factory=list)
    active_plan: ActivePlanMemory | None = None
    discoveries: list[Discovery] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    architectural_decisions: list[str] = field(default_factory=list)
    raw_sections: dict[str, str] = field(default_factory=dict)  # preserve unknown sections

    # Limits
    MAX_DISCOVERIES: int = 30
    MAX_HISTORY: int = 20

    def context_summary(self) -> str:
        """Return a compact summary for injection into LLM prompts."""
        parts: list[str] = []
        if self.project_context:
            parts.append("## Contexto do Projeto\n" + "\n".join(self.project_context))
        if self.user_preferences:
            parts.append("## Preferências do Usuário\n" + "\n".join(self.user_preferences))
        if self.discoveries:
            recent = self.discoveries[-10:]
            parts.append(
                "## Descobertas Recentes\n"
                + "\n".join(f"- {d.fact}" for d in recent)
            )
        return "\n\n".join(parts)
