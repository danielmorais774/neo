"""HomeAssistantPipeline — orchestration for the home_assistant skill.

Single, immediate-execution flow (no mission/approval): build the skill system
prompt, run one agent loop with the HA tools, and return the result. The agent
resolves entity aliases via ``ha_list_entities`` and then queries
(``ha_get_state``) or acts (``ha_call_service``).
"""

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from core.agent.agent_executor import AgentExecutor
from core.agent.persona import neo_persona
from core.memory import ConversationMemory
from core.notifications.broker import NotificationBroker
from core.skills import SkillPipeline, SkillResult
from core.skills.home_assistant.adapter import HomeAssistantAdapter
from core.skills.home_assistant.executor import HomeAssistantExecutor
from core.skills.home_assistant.tools import HOME_ASSISTANT_TOOLS
from db.async_client import AsyncPostgresClient
from shared import Message, MessageRole

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompt.md"
_FALLBACK_PROMPT = (
    "Você é o Neo controlando a casa inteligente via Home Assistant. "
    "Use ha_list_entities para resolver o entity_id, depois ha_get_state ou "
    "ha_call_service. Nunca invente entity_id. Responda curto, em português."
)


class HomeAssistantPipeline(SkillPipeline):
    """Immediate-execution pipeline for Home Assistant control/query."""

    def __init__(
        self,
        agent_executor: AgentExecutor,
        db: AsyncPostgresClient,
        broker: NotificationBroker,
    ) -> None:
        self.agent_executor = agent_executor
        self.db = db
        self.broker = broker
        self.adapter = HomeAssistantAdapter()
        self.executor = HomeAssistantExecutor(self.adapter)

    async def run(
        self,
        session_id: UUID,
        user_message: str,
        history: list[dict[str, str]],
        mission_id: UUID | None = None,
        extra: dict[str, Any] | None = None,
        user_id: UUID | None = None,
    ) -> SkillResult:
        if not self.adapter.is_configured:
            return SkillResult(
                ok=False,
                message=(
                    "O Home Assistant ainda não está configurado. Defina "
                    "HOME_ASSISTANT_URL e HOME_ASSISTANT_TOKEN no neo-core."
                ),
            )

        self.executor.set_context(user_id)
        system_prompt = self._build_system_prompt()
        memory = self._build_memory(session_id, history, user_message)

        try:
            result = await self.agent_executor.execute(
                task=user_message,
                session_id=session_id,
                memory=memory,
                system_prompt_override=system_prompt,
                tool_subset=HOME_ASSISTANT_TOOLS,
                tool_executor=self.executor,
                available_tools=HOME_ASSISTANT_TOOLS,
            )
        except Exception as e:
            logger.exception("Home Assistant pipeline failed")
            return SkillResult(ok=False, message=f"Erro ao falar com o Home Assistant: {e}")

        status = result.get("status")
        final = result.get("final_response") or "Pronto."
        ok = status in ("completed", "success") or status is None
        return SkillResult(ok=ok, message=final)

    def _build_system_prompt(self) -> str:
        try:
            base = _PROMPT_PATH.read_text(encoding="utf-8")
        except Exception:
            base = _FALLBACK_PROMPT
        # Voz do Neo (slim) vem da fonte única; sem o gatilho do nome — o roteador
        # já desvia "alexa, ..." pro núcleo cognitivo, onde o gatilho vive.
        return neo_persona("slim") + "\n\n" + base

    def _build_memory(
        self,
        session_id: UUID,
        history: list[dict[str, str]] | None,
        current_user_message: str,
    ) -> ConversationMemory:
        """Carry recent turns for context (e.g. follow-ups like 'agora apaga')."""
        memory = ConversationMemory(session_id=session_id)
        rows = history or []
        for index, row in enumerate(rows):
            role = row.get("role")
            content = (row.get("content") or "").strip()
            if not content or role not in {"user", "assistant"}:
                continue
            # Skip the trailing duplicate of the current message.
            if role == "user" and content == current_user_message and index == len(rows) - 1:
                continue
            memory.add_message(Message(role=MessageRole(role), content=content))
        return memory
