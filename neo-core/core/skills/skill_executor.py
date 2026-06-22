"""SkillExecutor — central dispatcher that routes messages to skill pipelines."""

import asyncio
import logging
from typing import Any
from uuid import UUID
from uuid import UUID as _UUID

from core.agent.agent_executor import AgentExecutor
from core.skills import SkillResult
from core.skills.manifest import SkillManifest
from core.notifications.broker import NotificationBroker
from db.async_client import AsyncPostgresClient

logger = logging.getLogger(__name__)

# Global semaphore to limit concurrent LLM calls across all skills
_GLOBAL_SEMAPHORE = asyncio.Semaphore(8)


class SkillExecutor:
    """Central dispatcher for skill pipelines.

    Receives routing decisions from IntentRouter and executes the appropriate
    skill pipeline.
    """

    def __init__(
        self,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        notification_broker: NotificationBroker,
    ) -> None:
        self.agent_executor = agent_executor
        self.db = db
        self.broker = notification_broker

    async def execute(
        self,
        skill_name: str,
        session_id: UUID,
        user_message: str,
        conversation_history: list[dict[str, str]],
        user_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SkillResult:
        """Execute a skill pipeline by name.

        Args:
            skill_name: name of the skill to execute
            session_id: conversation session ID
            user_message: the user's message
            conversation_history: recent messages as [{role, content}]
            user_id: the user ID
            extra: additional context (e.g. contact_label, goal)

        Returns:
            SkillResult with outcome details
        """
        manifest = SkillManifest.get(skill_name)
        pipeline = self._create_pipeline(skill_name)

        logger.info(
            "SkillExecutor.execute: skill=%s session_id=%s user_id=%s",
            skill_name,
            session_id,
            user_id,
        )

        async with _GLOBAL_SEMAPHORE:
            # Resolve user_id from extra if not provided
            resolved_user_id = user_id
            if not resolved_user_id and (extra or {}).get("user_id"):
                resolved_user_id = extra["user_id"]
            resolved_mission_id = None
            raw_mission_id = (extra or {}).get("mission_id")
            if isinstance(raw_mission_id, UUID):
                resolved_mission_id = raw_mission_id
            elif isinstance(raw_mission_id, str) and raw_mission_id.strip():
                try:
                    resolved_mission_id = _UUID(raw_mission_id.strip())
                except ValueError:
                    logger.warning("SkillExecutor.execute: invalid mission_id in extra: %s", raw_mission_id)
            return await pipeline.run(
                session_id=session_id,
                user_message=user_message,
                history=conversation_history,
                mission_id=resolved_mission_id,
                extra=extra or {},
                user_id=resolved_user_id,
            )

    def _create_pipeline(self, skill_name: str) -> Any:
        """Factory: create pipeline instance for a skill.

        Import pipeline class dynamically to avoid circular imports.
        """
        if skill_name in ("whatsapp_messaging", "whatsapp_order"):
            from core.skills.whatsapp_messaging.pipeline import WhatsAppMessagingPipeline
            return WhatsAppMessagingPipeline(
                agent_executor=self.agent_executor,
                db=self.db,
                broker=self.broker,
            )
        if skill_name == "home_assistant":
            from core.skills.home_assistant.pipeline import HomeAssistantPipeline
            return HomeAssistantPipeline(
                agent_executor=self.agent_executor,
                db=self.db,
                broker=self.broker,
            )
        if skill_name == "music":
            from core.skills.music.pipeline import MusicPipeline
            return MusicPipeline(
                agent_executor=self.agent_executor,
                db=self.db,
                broker=self.broker,
            )
        raise KeyError(f"No pipeline found for skill '{skill_name}'")


import asyncio  # noqa: E402
