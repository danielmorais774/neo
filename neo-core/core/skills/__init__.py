"""Skill system — base abstractions."""

from abc import ABC, abstractmethod
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class SkillResult(BaseModel):
    """Result returned by a skill pipeline after running."""

    ok: bool
    message: str
    mission_id: UUID | None = None
    draft_text: str | None = None
    notification_kind: str | None = None
    extra: dict[str, Any] | None = None
    mission_contact_label: str | None = None
    mission_contact_jid: str | None = None
    mission_contact_phone: str | None = None


class SkillPipeline(ABC):
    """Abstract base class for a skill pipeline.

    Subclass this to implement the run() method.
    """

    @abstractmethod
    async def run(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        mission_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
        user_id: UUID | None = None,
    ) -> SkillResult:
        """Run the skill pipeline. Override in subclasses."""
        ...


# NOTE: SkillExecutor must be imported lazily (inside functions) to avoid
# circular imports. Use: from core.skills.skill_executor import SkillExecutor
